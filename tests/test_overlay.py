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
from hermeneus_engine.overlay.model import (FAILED, LABEL, PENDING, TRANSLATED, OverlayModel, Run, build_view,
                                            compose, fit_head, layout, wrap)
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
    assert (s.auto, s.text_size, s.max_lines, s.show_original, s.opacity, s.show_speaker) == (False, 26, 2, True, 80, True)
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

def test_partial_shows_at_once_then_its_translation_takes_its_place():
    m = OverlayModel()
    m.apply({"type": "hello", "seq": 1, "session": {"state": "live"}}, 0)
    assert compose(m, 0, show_speaker=False, show_original=True)[2] == "Waiting for speech"
    assert m.apply(line(2, "a", "partial", "Good evening"), 1)
    runs, original, _ = compose(m, 1, show_speaker=False, show_original=True)
    assert runs == [Run("Good evening", PENDING, 1)] and original == ""
    m.apply(line(3, "a", "translated", "Good evening", "İyi akşamlar"), 2)
    runs, original, _ = compose(m, 2, show_speaker=False, show_original=True)
    assert runs == [Run("İyi akşamlar", TRANSLATED, 1)] and original == "Good evening"


def test_older_seq_and_unchanged_lines_do_not_redraw():
    m = OverlayModel()
    assert m.apply(line(5, "a", "partial", "x"), 0)
    assert not m.apply(line(4, "a", "partial", "older"), 0)
    assert not m.apply(line(6, "a", "partial", "x"), 0)  # same content
    assert compose(m, 0, show_speaker=False, show_original=False)[0][0].text == "x"


def test_failed_line_shows_its_original_and_speaker_labels_follow_relabel():
    m = OverlayModel()
    m.apply(line(1, "a", "translated", "Hello", "Merhaba", "s1:speaker_0"), 0)
    m.apply(line(2, "b", "failed", "Fast talk", "", "s1:speaker_1"), 0)
    runs, _, _ = compose(m, 0, show_speaker=True, show_original=True)
    assert [(r.text, r.kind) for r in runs] == [("Speaker 1", LABEL), ("Merhaba", TRANSLATED),
                                                ("Speaker 2", LABEL), ("Fast talk", FAILED)]
    m.apply({"type": "relabel", "seq": 3, "from": "s1:speaker_1", "to": "s1:speaker_0"}, 0)
    runs, _, _ = compose(m, 0, show_speaker=True, show_original=True)
    assert [r.text for r in runs] == ["Speaker 1", "Merhaba", "Fast talk"]


def test_reset_and_new_owner_hello_drop_lines_and_quiet_lines_clear():
    m = OverlayModel()
    m.apply(line(9, "a", "translated", "a", "b"), 0)
    m.apply({"type": "reset"}, 0)
    assert not m.lines and m.last_seq == 0
    m.apply(line(9, "a", "translated", "a", "b"), 0)
    m.apply({"type": "hello", "seq": 2, "session": {"state": "live"}}, 0)
    assert not m.lines
    m.apply(line(3, "c", "translated", "c", "d"), 10)
    assert compose(m, 10 + 6.9, show_speaker=False, show_original=False)[0]
    assert compose(m, 10 + 7.1, show_speaker=False, show_original=False) == ([], "", "")


def test_keeps_a_bounded_number_of_lines():
    m = OverlayModel(keep=3)
    for i in range(10):
        m.apply(line(i + 1, f"l{i}", "translated", "o", f"t{i}"), 0)
    assert list(m.lines) == ["l7", "l8", "l9"]


# -- wrap and roll -------------------------------------------------------------------

def test_wrap_rolls_to_the_newest_lines_and_keeps_styles():
    runs = [Run("one two three", TRANSLATED), Run("four five", PENDING)]
    lines = wrap(runs, 9, chars, 2)
    assert [[(r.text, r.kind) for r in ln] for ln in lines] == [[("three", TRANSLATED)],
                                                                [("four five", PENDING)]]
    assert len(wrap(runs, 9, chars, 3)) == 3
    assert wrap(runs, 100, chars, 1) == ((Run("one two three ", TRANSLATED), Run("four five", PENDING)),)


def test_wrap_breaks_a_word_wider_than_the_line():
    lines = wrap([Run("abcdefghij", TRANSLATED)], 4, chars, 3)
    assert ["".join(r.text for r in ln) for ln in lines] == ["abcd", "efgh", "ij"]


def test_build_view_names_the_speaker_again_when_the_label_rolls_off():
    m = OverlayModel()
    m.apply(line(1, "a", "translated", "orig", "aa bb cc dd ee ff gg"), 0)
    view = build_view(m, 0, width=9, measure=chars, max_lines=2, show_speaker=True, show_original=False)
    assert view.lines[0][0].kind == LABEL and view.lines[0][0].text == "Speaker 1"
    assert all(sum(chars(r.text, r) for r in ln) <= 9 + 1 for ln in view.lines)


def test_the_original_is_cut_at_its_end_never_its_front():
    assert fit_head("short", 20, chars, Run("", PENDING)) == "short"
    out = fit_head("the quick brown fox jumps over", 12, chars, Run("", PENDING))
    assert out == "the quick\u2026" and len(out) <= 12


def test_each_speaker_turn_starts_its_own_row():
    # Before: "Speaker 1 Merhaba. Speaker 2 Nasılsın?" shared one row (A3 F4).
    m = OverlayModel()
    m.apply(line(1, "a", "translated", "Hi.", "Merhaba.", "s1:speaker_0"), 0)
    m.apply(line(2, "b", "translated", "How are you?", "Nasılsın?", "s1:speaker_1"), 0)
    m.apply(line(3, "c", "translated", "Fine.", "İyiyim.", "s1:speaker_0"), 0)
    view = build_view(m, 0, width=200, measure=chars, max_lines=3, show_speaker=True, show_original=False)
    assert [[r.text.strip() for r in ln] for ln in view.lines] == [
        ["Speaker 1", "Merhaba."], ["Speaker 2", "Nasılsın?"], ["Speaker 1", "İyiyim."]]
    view = build_view(m, 0, width=200, measure=chars, max_lines=2, show_speaker=False, show_original=False)
    assert ["".join(r.text for r in ln) for ln in view.lines] == ["Nasılsın?", "İyiyim."]


def test_same_speaker_lines_share_a_row_and_older_turns_leave_whole():
    runs = [Run("Speaker 1", LABEL, 1), Run("aa bb", TRANSLATED, 1), Run("cc", TRANSLATED, 1),
            Run("Speaker 2", LABEL, 2), Run("dd ee ff gg", TRANSLATED, 2)]
    rows = layout(runs, 14, chars, 2, True)
    assert ["".join(r.text for r in row) for row in rows] == ["Speaker 2 dd", "ee ff gg"]
    rows = layout(runs, 40, chars, 2, True)
    assert ["".join(r.text for r in row) for row in rows] == ["Speaker 1 aa bb cc", "Speaker 2 dd ee ff gg"]


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

    def strip_height(self, settings):
        return 40.0 * settings.max_lines + (20 if settings.show_original else 0)

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
    for local in (None, "cli", "tui", "desktop"):
        control.check_platform(local)
    with pytest.raises(control.OverlayUnavailable, match="telegram"):
        control.check_platform("telegram")


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
    with pytest.raises(control.OverlayUnavailable, match="no translation session"):
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

    off = OverlayService(lambda: data_dir, lambda: {}, status, show_fn=show_fn)
    assert run(off.auto("cli")) == "" and shown == []
    on = OverlayService(lambda: data_dir, lambda: {"overlay": True}, status, show_fn=show_fn)
    assert run(on.auto("cli")) == "The subtitle overlay is open." and shown == [True]
    assert "did not open" in run(on.auto("discord"))


def test_older_lines_leave_whole_before_the_newest_is_cut():
    m = OverlayModel()
    m.apply(line(1, "a", "translated", "o", "first line here"), 0)
    m.apply(line(2, "b", "translated", "o", "second", "s1:speaker_0"), 0)
    view = build_view(m, 0, width=20, measure=chars, max_lines=1, show_speaker=True, show_original=False)
    assert [[r.text for r in ln] for ln in view.lines] == [["Speaker 1 ", "second"]]
    view = build_view(m, 0, width=40, measure=chars, max_lines=1, show_speaker=False, show_original=False)
    assert ["".join(r.text for r in ln) for ln in view.lines] == ["first line here second"]
