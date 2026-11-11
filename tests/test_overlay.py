"""The subtitle overlay without AppKit: settings, reducer, layout, placement, stream follower, process control."""

import asyncio
import io
import json
import os
import signal

import pytest

from pipeline_fakes import remove_tmpdir, short_tmpdir

from hermeneus_engine import overlay_settings as ovs
from hermeneus_engine.ipc import SessionUnavailable
from hermeneus_engine.overlay import control
from hermeneus_engine.overlay.app import OverlayApp
from hermeneus_engine.overlay.follower import follow
from hermeneus_engine.overlay.model import PENDING, OverlayModel, Run, fit_head
from hermeneus_engine.overlay.placement import Display, Positions, Rect, Spot, frame_for, spot_for
from hermeneus_engine.overlay.service import OverlayService


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def line(seq, lid, state, original, translation="", speaker="s1:speaker_0"):
    return {"type": "line", "seq": seq, "line": {"line_id": lid, "state": state, "original": original,
                                                 "translation": translation, "speaker_id": speaker}}


def chars(text, run):  # one unit per character; labels are as wide as their text
    return float(len(text))


# -- settings ----------------------------------------------------------------------

def test_settings_defaults_and_clamping():
    s = ovs.from_settings({})
    # Overlay on by default (W29); target language only and a solid background by default (W28: the owner G6 finding).
    assert (s.auto, s.text_size, s.max_lines, s.show_original, s.opacity, s.show_speaker) == (True, 26, 2, False, 90, True)
    assert (s.pace, s.reading_cps) == ("read", 0)
    s = ovs.from_settings({"overlay_pace": "LIVE", "overlay_reading_cps": 3})
    assert (s.pace, s.reading_cps) == ("live", 5)
    s = ovs.from_settings({"overlay_pace": "fast", "overlay_reading_cps": 99})
    assert (s.pace, s.reading_cps) == ("read", 30)
    s = ovs.from_settings({"overlay_text_size": 200, "overlay_max_lines": 0, "overlay_opacity": "-5",
                           "overlay_show_original": "off", "overlay": "yes", "overlay_show_speaker": 0})
    assert (s.text_size, s.max_lines, s.opacity, s.show_original, s.auto, s.show_speaker) == (64, 1, 0, False, True, False)
    assert ovs.from_settings({"overlay_text_size": True}).text_size == 26  # a bool is not a size


def test_settings_json_round_trip_and_garbage():
    s = ovs.from_settings({"overlay_max_lines": 3, "overlay_opacity": 55})
    assert ovs.OverlaySettings.from_json(s.to_json()) == s
    assert ovs.OverlaySettings.from_json("not json") == ovs.OverlaySettings()
    assert ovs.OverlaySettings.from_json("[1]") == ovs.OverlaySettings()


def test_plugin_yaml_declares_every_overlay_key():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    for key in ovs.DEFAULTS:
        assert f"\n  {key}:\n" in text, key


# -- reducer and compose -----------------------------------------------------------


def test_keeps_a_bounded_number_of_lines():
    m = OverlayModel(keep=3)
    for i in range(10):
        m.apply(line(i + 1, f"l{i}", "translated", "o", f"t{i}"), 0)
    assert list(m.lines) == ["l7", "l8", "l9"]


# -- wrap and roll -------------------------------------------------------------------


def test_the_original_is_cut_at_its_end_never_its_front():
    assert fit_head("short", 20, chars, Run("", PENDING)) == "short"
    out = fit_head("the quick brown fox jumps over", 12, chars, Run("", PENDING))
    assert out == "the quick\u2026" and len(out) <= 12


# -- placement -----------------------------------------------------------------------

D1 = Display("1:Built-in", Rect(0, 0, 1440, 900))
D2 = Display("2:External", Rect(1440, 0, 1920, 1080))


def test_frame_is_kept_on_its_display_and_round_trips():
    f = frame_for(Spot(), D1, 100)
    assert (f.w, f.x, f.y) == (864, 288, 72)
    edge = frame_for(Spot(cx=1.0, bottom=1.0, width=0.5), D1, 100)
    assert edge.x + edge.w == 1440 and edge.y + edge.h == 900
    back = spot_for(f, D1)
    assert frame_for(back, D1, 100) == f


def test_positions_are_stored_per_display(data_dir):
    path = data_dir / "positions.json"
    p = Positions(path)
    p.remember(D2.key, Spot(0.3, 0.2, 0.5))
    p.remember(D1.key, Spot(0.6, 0.1, 0.7))
    again = Positions(path)
    assert again.spot(D2.key) == Spot(0.3, 0.2, 0.5) and again.spot(D1.key) == Spot(0.6, 0.1, 0.7)
    assert again.choose([D1, D2], D2) == D1  # last used
    assert again.choose([D2], D2) == D2  # last display gone
    path.write_text("{broken", encoding="utf-8")
    assert Positions(path).spot("x") == Spot()


# -- the controller with a fake window -------------------------------------------------

class FakePanel:
    def __init__(self, displays=(D1, D2)):
        self._displays = list(displays)
        self.frames, self.renders, self.visible = [], [], []

    def displays(self):
        return self._displays

    def main_display(self):
        return self._displays[0]

    def measure(self, text, run, settings):
        return float(len(text) * 10)

    def measure_original(self, text, settings):
        return float(len(text) * 6)

    def strip_height(self, settings, rows=None, original=None):
        rows = settings.max_lines if rows is None else rows
        original = settings.show_original if original is None else original
        return 40.0 * rows + (20 if original else 0)

    def text_inset(self):
        return 20.0

    def set_frame(self, frame):
        self.frames.append(frame)

    def render(self, view, settings):
        self.renders.append(view)

    def set_visible(self, visible):
        self.visible.append(visible)


def test_app_draws_only_on_change_and_hides_when_empty(data_dir):
    panel = FakePanel()
    app = OverlayApp(panel, Positions(data_dir / "p.json"), ovs.OverlaySettings())
    app.on_event({"type": "hello", "seq": 1, "session": {"state": "live"}}, 0)
    assert panel.visible == [True] and panel.renders[-1].notice == "Waiting for speech"
    app.on_event(line(2, "a", "translated", "Hi", "Merhaba"), 1)
    n = len(panel.renders)
    app.refresh(2)
    app.refresh(3)
    assert len(panel.renders) == n  # nothing changed, nothing drawn
    app.refresh(1 + 7.5)
    assert panel.visible[-1] is False


def test_app_remembers_a_move_on_the_display_it_lands_on(data_dir):
    panel = FakePanel()
    app = OverlayApp(panel, Positions(data_dir / "p.json"), ovs.OverlaySettings())
    app.place()
    app.moved(Rect(1600, 300, 900, 100))
    stored = Positions(data_dir / "p.json")
    assert stored.last == D2.key and abs(stored.spot(D2.key).width - 900 / 1920) < 1e-3
    app.set_width(0.8)
    assert abs(panel.frames[-1].w - 0.8 * 1920) < 1e-6
    app.apply_settings(ovs.OverlaySettings(max_lines=3, show_original=False))
    assert panel.frames[-1].h == 120


# -- the stream follower ------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def test_follow_ends_when_the_session_stops():
    seen = []

    async def subscribe(after):
        yield {"type": "hello", "seq": 4}
        yield line(5, "a", "partial", "x")

    why = run(follow(subscribe, seen.append, lambda: False))
    assert why == "ended" and [e["type"] for e in seen] == ["hello", "line"]


def test_follow_reconnects_from_the_last_seq_while_the_session_lives():
    calls, seen = [], []

    async def subscribe(after):
        calls.append(after)
        if len(calls) == 1:
            yield line(7, "a", "partial", "x")
            raise ConnectionResetError
        if len(calls) == 2:
            raise SessionUnavailable("socket gone")
        yield line(8, "a", "partial", "y")

    alive = iter([True, True, False])

    async def no_sleep(_s):
        return None

    why = run(follow(subscribe, seen.append, lambda: next(alive), sleep=no_sleep))
    assert why == "ended" and calls == [0, 7, 7] and len(seen) == 2


def test_follow_gives_up_without_a_connection():
    clock = iter([0.0, 10.0, 40.0])

    async def subscribe(after):
        raise SessionUnavailable("no")
        yield  # pragma: no cover

    async def no_sleep(_s):
        return None

    why = run(follow(subscribe, lambda e: None, lambda: True, clock=lambda: next(clock), sleep=no_sleep))
    assert why == "lost"


# -- process control ------------------------------------------------------------------------

def test_check_available_refuses_cleanly():
    with pytest.raises(control.OverlayUnavailable, match="macOS"):
        control.check_available("linux")
    with pytest.raises(control.OverlayUnavailable, match="PyObjC"):
        control.check_available("darwin", find_spec=lambda name: None, gui_session=lambda: True)
    with pytest.raises(control.OverlayUnavailable, match="desktop session"):
        control.check_available("darwin", find_spec=lambda name: object(), gui_session=lambda: False)
    control.check_available("darwin", find_spec=lambda name: object(), gui_session=lambda: True)


def test_chat_platforms_cannot_open_a_window_on_the_mac():
    for local in ("cli", "tui", "desktop"):
        control.check_platform(local)
    for remote in ("telegram", None, ""):  # a caller that names no surface is not local
        with pytest.raises(control.OverlayUnavailable):
            control.check_platform(remote)


class FakeChild:
    def __init__(self, data_dir, write, code=None):
        self.r, w = os.pipe()
        if write:
            os.write(w, write)
        os.close(w)
        self.stdout = io.FileIO(self.r, "rb")
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code


OK = dict(platform="darwin", gui_session=lambda: True, find_spec=lambda n: object())


def test_show_needs_a_session_and_waits_for_ready(data_dir):
    with pytest.raises(control.OverlayUnavailable, match="No translation session is running"):
        control.show(data_dir, ovs.OverlaySettings(), session_running=False, **OK)
    launched = []

    def popen(cmd, **kw):
        launched.append((cmd, kw))
        return FakeChild(data_dir, b"ready\n")

    out = control.show(data_dir, ovs.OverlaySettings(max_lines=3), session_running=True, popen=popen, **OK)
    cmd, kw = launched[0]
    assert cmd[1:4] == ["-m", "hermeneus_engine.overlay", "--data-dir"] and kw["start_new_session"] is True
    assert kw["stdin"] is not None and "PYTHONPATH" in kw["env"]
    assert json.loads(control.settings_file(data_dir).read_text())["max_lines"] == 3
    assert out["message"] == "the overlay is open"


def test_show_reports_a_child_that_does_not_open(data_dir):
    with pytest.raises(control.OverlayUnavailable, match="exit 3"):
        control.show(data_dir, ovs.OverlaySettings(), session_running=True,
                     popen=lambda cmd, **kw: FakeChild(data_dir, b"", code=3), **OK)
    child = FakeChild(data_dir, b"")
    with pytest.raises(control.OverlayUnavailable, match="did not open"):
        control.show(data_dir, ovs.OverlaySettings(), session_running=True, popen=lambda cmd, **kw: child,
                     timeout_s=0.2, **OK)
    assert child.terminated


def test_running_pid_and_hide_follow_the_lock(data_dir):
    import fcntl
    assert control.running_pid(data_dir) is None
    control.overlay_dir(data_dir)
    fd = os.open(control.lock_file(data_dir), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, json.dumps({"pid": 4242}).encode())
    try:
        assert control.running_pid(data_dir) == 4242
        sent = []
        control.hide(data_dir, wait_s=0.1, kill=lambda pid, sig: sent.append((pid, sig)))
        assert sent == [(4242, signal.SIGTERM)]
    finally:
        os.close(fd)
    assert control.running_pid(data_dir) is None
    assert control.hide(data_dir)["message"] == "the overlay is not open"


def test_service_auto_opens_only_when_the_setting_is_on(data_dir):
    shown = []

    async def status():
        return {"state": "live"}

    def show_fn(d, settings, *, session_running):
        shown.append(session_running)
        return {"visible": True, "pid": 1, "message": "the overlay is open"}

    off = OverlayService(lambda: data_dir, lambda: {"overlay": False}, status, show_fn=show_fn)
    assert run(off.auto("cli")) == "" and shown == []
    # Never chosen: on, so the first Start opens the overlay (W29, one-click requirement).
    default = OverlayService(lambda: data_dir, lambda: {}, status, show_fn=show_fn)
    assert run(default.auto("cli")) == "The subtitle overlay is open." and shown == [True]
    shown.clear()
    on = OverlayService(lambda: data_dir, lambda: {"overlay": True}, status, show_fn=show_fn)
    assert run(on.auto("cli")) == "The subtitle overlay is open." and shown == [True]
    assert "did not open" in run(on.auto("discord"))


def test_strip_height_follows_its_content_and_keeps_its_bottom(data_dir):
    # Before: the strip kept max-lines height, so one short line sat in an empty box (A3 F9).
    panel = FakePanel()
    app = OverlayApp(panel, Positions(data_dir / "p.json"), ovs.OverlaySettings(max_lines=3, show_original=True))
    app.place()
    bottom = panel.frames[-1].y
    assert panel.frames[-1].h == 140
    app.on_event(line(1, "a", "translated", "Hi", "Merhaba"), 0)
    assert panel.frames[-1].h == 60 and panel.frames[-1].y == bottom  # one row and the original
    app.on_event(line(2, "b", "translated", "and more", "ve daha fazlası", "s1:speaker_1"), 0.5)
    app.refresh(1.0)  # the second line goes up once the first had its minimum time
    assert panel.frames[-1].h == 100 and panel.frames[-1].y == bottom  # two rows (a new speaker) and the original


# -- shared token table (A3 F3, F10, F15) -------------------------------------------------

from hermeneus_engine import overlay_tokens as tok  # noqa: E402


def test_css_colours_as_getcomputedstyle_writes_them():
    assert tok.parse_color("rgb(255, 0, 0)") == tok.Rgba(1, 0, 0, 1)
    assert tok.parse_color("rgba(0, 0, 0, 0.5)").a == 0.5
    assert tok.parse_color("rgb(0 0 0 / 50%)").a == 0.5
    assert tok.parse_color("color(srgb 0.5 0.25 1 / 0.94)") == tok.Rgba(0.5, 0.25, 1, 0.94)
    assert tok.parse_color("#ffffff80").a == 128 / 255
    for bad in ("red", "url(x)", "rgb(1,2)", 7, "x" * 100):
        assert tok.parse_color(bad) is None


def test_payload_round_trips_and_rejects_what_it_cannot_read():
    payload = {"mode": "light", "name": "github",
               "colors": {"surface": "#ffffff", "text": "rgba(31, 35, 40, 0.94)", "text_secondary": "#1f2328bd",
                          "text_tertiary": "#1f23288a", "stroke": "rgba(0, 83, 253, 0.22)",
                          "speakers": ["#0053fd", "#1f8a65"]},
               "radius_px": 2.4, "font": "-apple-system"}
    p = tok.palette_from_payload(payload)
    assert not p.dark and p.speaker(3) == p.speaker(1) and p.source == "desktop:github"
    assert tok.palette_from_json(tok.palette_to_json(p)) == p
    assert tok.palette_from_json("{") is None
    for broken in ({**payload, "colors": {**payload["colors"], "stroke": "blue"}}, {**payload, "radius_px": True},
                   {**payload, "colors": {**payload["colors"], "speakers": []}}):
        with pytest.raises(tok.ThemeError):
            tok.palette_from_payload(broken)


def test_fallback_hairline_is_the_popover_token_not_a_multiple():
    # --ui-stroke-secondary for nous light: color-mix(#0053fd 16%, #1f2328 at 7%) (styles.css:380-384).
    stroke = tok.FALLBACK[False].stroke
    assert abs(stroke.a - (0.16 + 0.84 * 0.07)) < 1e-9
    assert tok.FALLBACK[False].radius_px == 2.4 and tok.WEIGHT_LABEL == 600


def test_service_applies_settings_live_and_reports_armed(data_dir):
    import fcntl
    settings = {"overlay": True, "overlay_text_size": 40}

    async def status():
        return {"state": "idle"}

    svc = OverlayService(lambda: data_dir, lambda: settings, status)
    assert svc.status()["armed"] is True
    svc.apply()
    assert not control.settings_file(data_dir).exists()  # nothing open, nothing written
    control.overlay_dir(data_dir)
    fd = os.open(control.lock_file(data_dir), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, b'{"pid": 99}')
    try:
        svc.apply()
        assert json.loads(control.settings_file(data_dir).read_text())["text_size"] == 40
    finally:
        os.close(fd)


def test_overlay_process_gets_no_secret_of_the_hermes_process(tmp_path):
    # Before: child_env copied os.environ, so ELEVENLABS_API_KEY and every provider key reached the child (A2 S1).
    parent = {"PATH": "/usr/bin", "HOME": "/Users/x", "LANG": "tr_TR.UTF-8", "ELEVENLABS_API_KEY": "sk-fake",
              "OPENROUTER_API_KEY": "or-fake", "GITHUB_TOKEN": "ghp-fake", "HERMES_CLIENT_SECRET": "s",
              "PYTHONHOME": "/x", "HERMES_HOME": "/h"}
    env = control.child_env(parent, [str(tmp_path), "", "/does/not/exist"])
    assert set(env) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "PYTHONUNBUFFERED"}
    assert not any(k.endswith(("_KEY", "_TOKEN", "_SECRET")) for k in env)
    assert "sk-fake" not in repr(env) and "ghp-fake" not in repr(env)
    paths = env["PYTHONPATH"].split(os.pathsep)
    assert paths[0] == str(control.plugin_root()) and str(tmp_path) in paths and "/does/not/exist" not in paths


def test_show_spawns_the_child_with_the_allowlisted_env(data_dir, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-fake")
    monkeypatch.setenv("SOME_PROVIDER_TOKEN", "tok-fake")
    seen = {}

    def popen(cmd, **kw):
        seen.update(kw["env"])
        return FakeChild(data_dir, b"ready\n")

    control.show(data_dir, ovs.OverlaySettings(), session_running=True, popen=popen, **OK)
    assert "ELEVENLABS_API_KEY" not in seen and "SOME_PROVIDER_TOKEN" not in seen and "PYTHONPATH" in seen


def test_theme_payload_strings_are_checked():
    base = {"mode": "light", "colors": {"surface": "#ffffff", "text": "#000000", "text_secondary": "#000000bd",
                                        "text_tertiary": "#0000008a", "stroke": "#00000020", "speakers": ["#0053fd"]}}
    p = tok.palette_from_payload({**base, "strings": {"speaker": "Konuşmacı {n}", "not_translated": "Çevrilmedi‮",
                                                      "other": "ignored"}})
    assert dict(p.strings) == {"speaker": "Konuşmacı {n}", "not_translated": "Çevrilmedi"}
    assert tok.palette_from_json(tok.palette_to_json(p)).strings == p.strings
    for bad in ({"speaker": "Konuşmacı"}, {"waiting": ""}, {"reconnecting": "x" * 61}, "text"):
        with pytest.raises(tok.ThemeError):
            tok.palette_from_payload({**base, "strings": bad})
    assert tok.palette_from_payload(base).strings == ()


def test_a_hand_edited_theme_file_with_an_unknown_word_key_still_loads():
    # Before: an unknown key reached Strings(**...) and raised on every tick (A3 reaudit3 N10).
    from hermeneus_engine.overlay.model import Strings
    raw = json.loads(tok.palette_to_json(tok.FALLBACK[False]))
    raw["strings"] = [["speaker", "Konuşmacı {n}"], ["shout", "x"], ["waiting", 7]]
    p = tok.palette_from_json(json.dumps(raw))
    assert dict(p.strings) == {"speaker": "Konuşmacı {n}"}
    assert Strings(**dict(p.strings)).label(2) == "Konuşmacı 2"


# -- legibility under a see-through background -------------------------------------

def test_text_edge_keeps_the_halo_alone_from_50_percent_and_adds_a_thin_outline_below():
    from hermeneus_engine.overlay import theme
    for opacity in (50, 80, 100):
        edge = theme.text_edge(opacity)
        assert (edge.outline_percent, edge.halo_alpha, edge.halo_blur, edge.solid) == (0.0, 0.9, 0.14, False)
    for opacity in (0, 30, 49):
        edge = theme.text_edge(opacity)
        assert edge.solid and 0 < edge.outline_percent <= 10  # thin: at most 5% of the size outside the glyph


def test_ink_is_opaque_below_50_percent_so_the_outline_never_shows_through_the_letters():
    from hermeneus_engine.overlay import theme
    from hermeneus_engine.overlay_tokens import FALLBACK
    for dark in (False, True):
        p = FALLBACK[dark]
        assert theme.ink(p.text_secondary, p.surface, theme.text_edge(80)) == p.text_secondary
        for color in (p.text, p.text_secondary, p.failed_color, *p.speakers):
            solid = theme.ink(color, p.surface, theme.text_edge(0))
            assert solid.a == 1.0
            assert solid == color.over(p.surface.with_alpha(1.0))


# -- banner style ----------------------------------------------------------------------

def test_overlay_style_defaults_to_banner_and_accepts_theme():
    # banner is the default (owner decision, W28 and W29); theme follows the Hermes Desktop theme.
    assert ovs.from_settings({}).style == "banner"
    assert ovs.from_settings({"overlay_style": " Banner "}).style == "banner"
    assert ovs.from_settings({"overlay_style": " Theme "}).style == "theme"
    assert ovs.from_settings({"overlay_style": "neon"}).style == "banner"
    assert ovs.from_settings({"overlay_style": 3}).style == "banner"
    s = ovs.from_settings({"overlay_style": "banner"})
    assert ovs.OverlaySettings.from_json(s.to_json()) == s


def test_banner_style_overrides_the_sent_theme_and_the_default_does_not():
    from hermeneus_engine.overlay import theme
    from hermeneus_engine.overlay_tokens import BANNER, FALLBACK
    sent = FALLBACK[False]
    assert theme.palette(True, None) is FALLBACK[True]
    assert theme.palette(True, sent) is sent
    assert theme.palette(False, sent, "banner") is BANNER
    assert theme.palette(False, None, "theme") is FALLBACK[False]


def _delta_e(a, b):
    """CIE76 distance in Lab (D65)."""
    def lab(c):
        def lin(v):
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b_ = lin(c.r), lin(c.g), lin(c.b)
        x = (0.4124 * r + 0.3576 * g + 0.1805 * b_) / 0.95047
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b_
        z = (0.0193 * r + 0.1192 * g + 0.9505 * b_) / 1.08883
        f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in (x, y, z)]
        return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])
    la, lb = lab(a), lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5


def test_banner_text_and_speakers_meet_wcag_aa_on_the_navy_and_stay_apart():
    from hermeneus_engine.overlay_tokens import BANNER, contrast
    navy = BANNER.surface
    assert BANNER.dark and navy.a == 1.0
    for color in (BANNER.text, BANNER.text_secondary, BANNER.text_tertiary, BANNER.failed_color, *BANNER.speakers):
        assert contrast(color.over(navy), navy) >= 4.5
    colors = (*BANNER.speakers, BANNER.failed_color)
    for i, a in enumerate(colors):
        for b in colors[i + 1:]:
            assert _delta_e(a, b) > 20


def test_pane_style_picker_offers_the_settings_choices_with_the_default_first():
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text()
    m = re.search(r"export const OVERLAY_STYLES = Object\.freeze\(\[([^\]]*)\]\)", source)
    offered = [s.strip().strip("'") for s in m.group(1).split(",")]
    assert set(offered) == set(ovs.STYLES)
    assert offered[0] == ovs.DEFAULTS[ovs.KEY_STYLE] == "banner"
    # The pane writes it through the same settings route as the other look controls.
    assert "setOverlayLook({ overlay_style: id })" in source


def test_every_overlay_speaker_colour_meets_aa_in_both_styles_and_both_themes():
    # Owner decision (W28): the theme style's speaker 1 (#0053fd on the dark surface) was 3.01:1.
    from hermeneus_engine.overlay_tokens import AA, BANNER, FALLBACK, contrast, legible
    from hermeneus_engine.overlay import theme
    for p in (FALLBACK[False], FALLBACK[True], BANNER):
        ground = p.surface.with_alpha(1.0)
        for n in range(1, len(p.speakers) + 1):
            assert contrast(p.speaker(n), ground) >= AA, (p.source, p.dark, n)
        assert contrast(p.failed_color, ground) >= AA
        # Under 50% background the light theme turns light text on a dark edge; its speakers stay AA there too.
        see = theme.see_through(p, 30)
        for n in range(1, len(see.speakers) + 1):
            assert contrast(see.speaker(n), see.surface.with_alpha(1.0)) >= AA
    dark_blue = legible(tok.Rgba.hex("#0053fd"), FALLBACK[True].surface)
    assert dark_blue != tok.Rgba.hex("#0053fd") and dark_blue.b > dark_blue.r  # lighter, still blue
    assert legible(BANNER.speakers[0], BANNER.surface) == BANNER.speakers[0]  # already AA: unchanged
    # The page gets the same AA colours.
    from hermeneus_engine import overlay_page
    tokens = overlay_page.theme_tokens(None)
    assert tokens["dark"]["--speaker-1"] == FALLBACK[True].speaker(1).css()


def test_the_strip_has_6px_corners_in_both_styles():
    from hermeneus_engine import overlay_page
    from hermeneus_engine.overlay_tokens import STRIP_RADIUS_PX
    assert STRIP_RADIUS_PX == 6.0
    tokens = overlay_page.theme_tokens(None)
    assert {tokens[m]["--radius"] for m in ("light", "dark", "banner")} == {"6px"}
