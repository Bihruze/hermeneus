"""Free resize of the native strip (W31): frame math, row snapping, clamping, per-display memory and the
round trip of the dragged rows through the overlay_max_lines setting. No AppKit except the last tests."""

import json

import pytest

from pipeline_fakes import remove_tmpdir, short_tmpdir

from hermeneus_engine import overlay_settings as ovs
from hermeneus_engine.overlay import control, pace
from hermeneus_engine.overlay.app import HISTORY_ROWS, OverlayApp
from hermeneus_engine.overlay.placement import (MIN_WIDTH_PT, Display, Positions, Rect, Spot, frame_for,
                                                resize_frame, snap_rows, wanted_rows)
from hermeneus_engine.overlay.service import OverlayService

D1 = Display("1:Built-in", Rect(0, 0, 1440, 900))
D2 = Display("2:External", Rect(1440, 0, 1920, 1080))
ROW = 40.0


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def height(rows, original=False):
    return ROW * rows + 20 + (20 if original else 0)


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
        return height(rows, original)

    def text_inset(self):
        return 20.0

    def set_frame(self, frame):
        self.frames.append(frame)

    def render(self, view, settings):
        self.renders.append((view, settings))

    def set_visible(self, visible):
        self.visible.append(visible)


def make(data_dir, **settings):
    panel = FakePanel()
    app = OverlayApp(panel, Positions(data_dir / "p.json"), ovs.OverlaySettings(**settings))
    app.place()
    return panel, app


def stored(data_dir):
    return json.loads((data_dir / "p.json").read_text())


# -- settings range -----------------------------------------------------------------

def test_max_lines_range_is_one_to_six_default_two():
    assert ovs.from_settings({}).max_lines == 2
    assert ovs.from_settings({"overlay_max_lines": 6}).max_lines == 6
    assert ovs.from_settings({"overlay_max_lines": 9}).max_lines == 6
    assert ovs.from_settings({"overlay_max_lines": 0}).max_lines == 1


# -- frame math ---------------------------------------------------------------------

B = Rect(0, 0, 1440, 900)
START = Rect(400, 100, 600, height(2))


def test_right_edge_changes_width_only_and_keeps_the_left_edge():
    f = resize_frame(START, "right", 150, 80, B, height(1), height(6))
    assert (f.x, f.y, f.w, f.h) == (400, 100, 750, START.h)


def test_top_edge_changes_height_only_and_keeps_the_bottom():
    f = resize_frame(START, "top", 150, 55, B, height(1), height(6))
    assert (f.x, f.y, f.w, f.h) == (400, 100, 600, START.h + 55)  # follows the mouse, unsnapped


def test_corner_changes_both():
    f = resize_frame(START, "corner", -100, 40, B, height(1), height(6))
    assert (f.x, f.y, f.w, f.h) == (400, 100, 500, START.h + 40)


def test_width_clamps_to_320_and_to_the_visible_width_growing_leftward_at_the_edge():
    assert resize_frame(START, "right", -1000, 0, B, height(1), height(6)).w == MIN_WIDTH_PT
    wide = resize_frame(START, "right", 5000, 0, B, height(1), height(6))
    assert (wide.x, wide.w) == (0, 1440)  # the whole visible width; the strip moved left to stay on screen
    near = resize_frame(Rect(1000, 100, 400, 100), "right", 200, 0, B, height(1), height(6))
    assert near.x + near.w == 1440 and near.w == 600


def test_height_clamps_to_one_and_six_rows_and_to_the_screen_top():
    assert resize_frame(START, "top", 0, -500, B, height(1), height(6)).h == height(1)
    assert resize_frame(START, "top", 0, 5000, B, height(1), height(6)).h == height(6)
    high = resize_frame(Rect(400, 800, 600, height(2)), "top", 0, 200, B, height(1), height(6))
    assert high.y + high.h == 900 and high.h == height(6)  # grows downward once the top reaches the menu bar


def test_tiny_display_never_overflows():
    small = Rect(0, 0, 300, 150)
    f = resize_frame(Rect(0, 0, 300, 100), "corner", 500, 500, small, height(1), height(6))
    assert (f.x, f.y, f.w, f.h) == (0, 0, 300, 150)


# -- snapping -----------------------------------------------------------------------

def test_release_snaps_to_the_nearest_whole_row():
    assert snap_rows(height(3), height) == 3
    assert snap_rows(height(3) + ROW * 0.49, height) == 3
    assert snap_rows(height(3) + ROW * 0.51, height) == 4
    assert snap_rows(5, height) == 1
    assert snap_rows(5000, height) == 6
    assert snap_rows(height(2) + ROW / 2, height) == 2  # a tie keeps the smaller strip


# -- persistence --------------------------------------------------------------------

def test_spot_rows_persist_and_old_files_still_load(data_dir):
    path = data_dir / "p.json"
    path.write_text(json.dumps({"last": D1.key, "displays": {D1.key: {"cx": 0.5, "bottom": 0.1, "width": 0.5}}}))
    p = Positions(path)
    assert p.spot(D1.key).rows is None and p.seen is None and p.wanted is None
    p.set_rows(D1.key, 9)
    p.set_lines(seen=2, wanted=5)
    again = Positions(path)
    assert again.spot(D1.key) == Spot(0.5, 0.1, 0.5, 6) and (again.seen, again.wanted) == (2, 5)
    path.write_text(json.dumps({"displays": {D1.key: {"rows": "many"}}, "lines": {"seen": True, "wanted": "x"}}))
    garbage = Positions(path)
    assert D1.key not in garbage.spots and garbage.seen is None and garbage.wanted is None


def test_drag_height_on_release_snaps_remembers_rows_and_hands_them_back(data_dir):
    panel, app = make(data_dir)
    f = app.frame
    app.resized(Rect(f.x, f.y, f.w + 200, height(5) - 12), True)
    assert app.settings.max_lines == 5 and app.config.max_lines == 2
    assert panel.frames[-1].h == height(5)  # snapped, bottom unchanged
    assert panel.frames[-1].y == f.y and abs(panel.frames[-1].w - (f.w + 200)) < 1e-6
    body = stored(data_dir)
    assert body["displays"][D1.key]["rows"] == 5 and body["lines"] == {"seen": 2, "wanted": 5}
    assert abs(body["displays"][D1.key]["width"] - (f.w + 200) / 1440) < 1e-3


def test_width_drag_keeps_the_chosen_rows(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)
    app.resized(Rect(app.frame.x, app.frame.y, 800, height(1)), False)  # the content-fitted height is not rows
    assert app.settings.max_lines == 4 and stored(data_dir)["displays"][D1.key]["rows"] == 4


def test_rows_are_per_display_and_travel_with_a_move(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.moved(Rect(1600, 300, 900, app.frame.h))  # dropped on the external display
    assert app.display.key == D2.key and app.settings.max_lines == 5
    p = Positions(data_dir / "p.json")
    p.set_rows(D2.key, 1)
    app.positions = p
    app.screens_changed()
    assert app.settings.max_lines == 1 and panel.frames[-1].h == height(1)


def test_restart_keeps_dragged_rows_while_the_pane_has_not_saved_them(data_dir):
    _, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)
    panel2, app2 = make(data_dir)  # the overlay opens again; the setting is still 2
    assert app2.settings.max_lines == 4 and panel2.frames[-1].h == height(4)


# -- the setting round trip ---------------------------------------------------------

def test_the_handed_back_rows_arrive_as_the_setting_and_the_handback_ends(data_dir):
    _, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.apply_settings(ovs.OverlaySettings(max_lines=5))  # the pane saved it
    body = stored(data_dir)
    assert body["lines"] == {"seen": 5} and app.settings.max_lines == 5


def test_another_look_change_before_the_pane_saved_keeps_the_rows(data_dir):
    _, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.apply_settings(ovs.OverlaySettings(text_size=34))  # max_lines still 2: not a rows change
    assert app.settings.max_lines == 5 and app.settings.text_size == 34
    assert stored(data_dir)["lines"]["wanted"] == 5


def test_a_rows_change_from_the_pane_wins_on_this_display(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.apply_settings(ovs.OverlaySettings(max_lines=5))
    app.apply_settings(ovs.OverlaySettings(max_lines=3))  # the user picked 3 in the pane
    assert app.settings.max_lines == 3 and panel.frames[-1].h == height(3)
    body = stored(data_dir)
    assert "rows" not in body["displays"][D1.key] and body["lines"] == {"seen": 3}


def test_a_setting_changed_while_the_overlay_was_closed_wins(data_dir):
    _, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.apply_settings(ovs.OverlaySettings(max_lines=5))
    _, app2 = make(data_dir, max_lines=1)  # edited in the config, then the overlay opened
    assert app2.settings.max_lines == 1


def test_dragging_back_to_the_setting_cancels_the_handback(data_dir):
    _, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(2)), True)
    assert "wanted" not in stored(data_dir)["lines"]


def test_service_reports_resized_until_the_setting_holds_it(data_dir):
    _, app = make_in(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(6)), True)
    settings = {"overlay_max_lines": 2}

    async def status():
        return {"state": "live"}

    svc = OverlayService(lambda: data_dir, lambda: settings, status)
    assert svc.status()["resized"] == {"max_lines": 6}
    assert wanted_rows(control.positions_file(data_dir)) == 6
    settings["overlay_max_lines"] = 6
    assert "resized" not in svc.status()
    assert "resized" not in OverlayService(lambda: data_dir / "none", lambda: {}, status).status()


def make_in(data_dir, **settings):
    """An app using the real positions file under the overlay directory, as the overlay process does."""
    control.overlay_dir(data_dir)
    panel = FakePanel()
    app = OverlayApp(panel, Positions(control.positions_file(data_dir)), ovs.OverlaySettings(**settings))
    app.place()
    return panel, app


def service(data_dir, settings, visible=False):
    async def status():
        return {"state": "idle"}

    return OverlayService(lambda: data_dir, lambda: settings, status,
                          status_fn=lambda _d: {"visible": visible, "pid": 1 if visible else None})


# -- a later rows choice always wins over an older drag (A8-1, A8-2, A8-5) -----------

def test_a_stale_drag_never_overwrites_a_later_pick_while_the_overlay_is_closed(data_dir):
    """A8-1 (auditor probe P1): the pane saved a drag of 4 with the overlay closed, then the user picked 3."""
    control.overlay_dir(data_dir)
    path = control.positions_file(data_dir)
    path.write_text(json.dumps({"last": "", "displays": {}, "lines": {"seen": 2, "wanted": 4}}))
    settings = {"overlay_max_lines": 4}  # the pane saved the drag
    svc = service(data_dir, settings)
    assert svc.status().get("resized") is None
    assert json.loads(path.read_text())["lines"] == {"seen": 4}  # the hand-back ended on the plugin side
    settings["overlay_max_lines"] = 3  # later the user picks 3, the overlay still closed
    assert svc.status().get("resized") is None
    assert svc.apply().get("resized") is None
    settings["overlay_max_lines"] = 2  # and back to the value the overlay saw before the drag
    assert svc.status().get("resized") is None
    _, app = make_in(data_dir, max_lines=2)  # the overlay opens: the pick wins
    assert app.settings.max_lines == 2


def test_a_pick_back_to_the_old_value_never_revives_an_unseen_drag(data_dir):
    """A8-10 (re-audit probe R1): a drag to 4 the pane never saw, then the user sets 3, then 2 again."""
    control.overlay_dir(data_dir)
    path = control.positions_file(data_dir)
    path.write_text(json.dumps({"last": D1.key, "displays": {D1.key: {"cx": 0.5, "bottom": 0.1, "width": 0.5,
                                                                      "rows": 4}},
                                "lines": {"seen": 2, "wanted": 4}}))
    settings = {"overlay_max_lines": 3}
    svc = service(data_dir, settings)
    assert svc.status().get("resized") is None
    settings["overlay_max_lines"] = 2
    assert svc.status().get("resized") is None
    body = json.loads(path.read_text())
    assert body["lines"] == {"seen": 3} and "rows" not in body["displays"][D1.key]
    _, app = make_in(data_dir, max_lines=2)  # the overlay opens with the latest pick
    assert app.settings.max_lines == 2


def test_the_plugin_side_changes_only_lines_and_rows_of_a_fresh_file(data_dir, monkeypatch):
    """A8-9 (re-audit probe R3): an overlay move saved after the plugin process first read the file is kept."""
    import contextlib
    from hermeneus_engine.overlay import placement
    _, app = make_in(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)
    real, done = placement.locked, []

    @contextlib.contextmanager
    def overlay_saves_first(path):
        if not done:  # the overlay saves a new place just before the plugin process takes the lock
            done.append(1)
            app.moved(frame_for(Spot(0.2, 0.4, 0.5), D1, app.frame.h))
        with real(path):
            yield

    monkeypatch.setattr(placement, "locked", overlay_saves_first)
    assert service(data_dir, {"overlay_max_lines": 4}).status().get("resized") is None  # the pane saved 4
    assert done
    moved = app.positions.spot(D1.key)
    body = json.loads(control.positions_file(data_dir).read_text())
    spot = body["displays"][D1.key]
    assert (spot["cx"], spot["bottom"], spot["rows"]) == (round(moved.cx, 4), round(moved.bottom, 4), 4)
    assert moved.bottom == pytest.approx(0.4) and body["lines"] == {"seen": 4}


def test_two_writers_never_collide_on_the_temp_file(data_dir):
    """A8-8 (re-audit probe R4): the overlay and the plugin process save at the same time."""
    import threading
    path = control.positions_file(data_dir)
    a, b = Positions(path), Positions(path)
    a.remember(D1.key, Spot(0.3, 0.2, 0.5, 4))
    errors, failed = [], []

    def overlay(n=1500):
        for _ in range(n):
            try:
                a.set_lines(seen=2, wanted=4)
                failed.extend([] if a.save() else [1])
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    def plugin(n=1500):
        for _ in range(n):
            try:
                wanted_rows(path, 4)
                b.set_lines(seen=3)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    threads = [threading.Thread(target=overlay), threading.Thread(target=plugin)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and failed == []
    assert isinstance(json.loads(path.read_text()), dict)
    assert not list(path.parent.glob("*.tmp"))  # no temp file left behind


def test_a_failed_save_never_stops_new_settings_from_applying(data_dir, monkeypatch):
    from hermeneus_engine.overlay import placement
    panel, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(placement, "_write", broken)
    app.apply_settings(ovs.OverlaySettings(max_lines=3, text_size=34))
    assert app.settings.max_lines == 3 and app.settings.text_size == 34


def test_a_pick_made_before_the_open_overlay_read_it_wins(data_dir):
    """A8-2 (auditor probe P1b): wanted 4 is pending and the user picks 3 before the overlay's next tick."""
    _, app = make_in(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)
    assert service(data_dir, {"overlay_max_lines": 2}, visible=True).status()["resized"] == {"max_lines": 4}
    assert service(data_dir, {"overlay_max_lines": 3}, visible=True).status().get("resized") is None
    app.apply_settings(ovs.OverlaySettings(max_lines=3))  # the overlay reads settings.json
    assert app.settings.max_lines == 3 and app.positions.wanted is None


def test_a_rows_change_from_the_pane_applies_to_every_display(data_dir):
    """A8-5 (auditor probe P3): rows dragged on another display do not come back after a pane change."""
    a, b = Display("1:A", Rect(0, 0, 1440, 878)), Display("2:B", Rect(-1920, 120, 1920, 1055))
    panel = FakePanel([a, b])
    app = OverlayApp(panel, Positions(data_dir / "p.json"), ovs.OverlaySettings(max_lines=2))
    app.place()
    app.moved(frame_for(Spot(0.5, 0.1, 0.5), b, height(2)))
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.apply_settings(ovs.OverlaySettings(max_lines=5))  # the pane saved the drag
    app.moved(frame_for(Spot(0.5, 0.1, 0.5), a, app.frame.h))
    app.apply_settings(ovs.OverlaySettings(max_lines=2))  # then the user picked 2 in the pane, on display A
    assert app.settings.max_lines == 2
    panel._displays = [b]  # A unplugged
    app.screens_changed()
    assert app.display.key == b.key and app.settings.max_lines == 2
    assert all("rows" not in d for d in stored(data_dir)["displays"].values())


# -- a drag is held (A8-4) and never undone by text arriving mid-drag (A8-3) ---------

def test_the_dragged_height_is_held_with_empty_rows_until_text_fills_them(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    feed(app, 1)
    assert len(panel.renders[-1][0].lines) == 1 and panel.frames[-1].h == height(5)
    app.apply_settings(ovs.OverlaySettings(max_lines=4))  # a pane pick: a maximum, the strip fits its text
    feed(app, 1, start=50.0)
    assert app.settings.max_lines == 4 and panel.frames[-1].h == height(1)


def test_a_move_refits_the_text_that_arrived_while_the_grip_was_held(data_dir):
    panel, app = make(data_dir)
    feed(app, 1)
    app.moved(Rect(300, 300, app.frame.w, height(4)))  # the drag started from a taller strip
    assert panel.frames[-1].h == height(1)


def test_text_arriving_mid_drag_does_not_move_the_held_strip():
    """A8-3 (auditor probe a8_middrag_refresh): offscreen, no window is ordered on screen, no events."""
    pytest.importorskip("AppKit")
    from hermeneus_engine.overlay import panel_mac
    path = short_tmpdir()
    panel_mac.start_application()
    app = None
    panel = panel_mac.MacPanel(motion=False, on_resized=lambda f, r: app.resized(f, r))
    panel.set_visible = lambda v: None
    panel._sync_tabs = lambda v: None
    try:
        app = OverlayApp(panel, Positions(path / "positions.json"), ovs.OverlaySettings())
        app.place()
        t = feed(app, 1)
        start = panel.strip.frame()
        target = panel.strip_height(app.settings, 5, False)
        panel._dragged("top", start, 0, target - start.size.height)  # the user holds the top handle at 5 rows
        app.on_event({"type": "line", "seq": 99, "line": {"line_id": "late", "state": "translated",
                                                          "original": "x", "translation": "Bir cümle daha geldi.",
                                                          "speaker_id": "s1:speaker_0"}}, t)
        for i in range(30):
            app.refresh(t + i * 0.1)  # the 0.1 s tick
        assert panel.strip.frame().size.height == target
        panel._drag_ended("top")  # let go without moving again
        assert app.settings.max_lines == 5
        assert Positions(path / "positions.json").spot(app.display.key).rows == 5
        assert panel.strip.frame().size.height == target  # held after release too
    finally:
        panel.close()
        remove_tmpdir(path)


def test_history_opening_mid_drag_releases_the_hold():
    """A8-12: the height handles hide while history opens; set_frame must not stay blocked, and the drag is void."""
    pytest.importorskip("AppKit")
    from hermeneus_engine.overlay import panel_mac
    path = short_tmpdir()
    panel_mac.start_application()
    app = None
    resized = []
    panel = panel_mac.MacPanel(motion=False, on_resized=lambda f, r: resized.append(f))
    panel.set_visible = lambda v: None
    try:
        app = OverlayApp(panel, Positions(path / "positions.json"), ovs.OverlaySettings())
        app.place()
        t = feed(app, 12)
        start = panel.strip.frame()
        panel._drag_began("top")
        panel._dragged("top", start, 0, 30)
        app.scroll(1, t)  # history opens while the top handle is held
        assert panel._held is None
        assert panel.strip.frame().size.height == panel.strip_height(app.settings, HISTORY_ROWS, False)
        panel._drag_ended("top")  # a late mouse up from the hidden handle
        assert resized == []
        app.set_width(0.7)
        assert abs(panel.strip.frame().size.width - app.frame.w) < 0.5  # set_frame works again
    finally:
        panel.close()
        remove_tmpdir(path)


# -- reset, history, pace -----------------------------------------------------------

def test_double_click_reset_gives_default_width_and_the_setting_rows_in_place(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(100, 200, 1200, height(5)), True)
    app.reset_size()
    spot = Positions(data_dir / "p.json").spot(D1.key)
    assert (spot.width, spot.rows) == (0.6, None) and abs(panel.frames[-1].y - 200) < 1e-6
    assert app.settings.max_lines == 2 and "wanted" not in stored(data_dir)["lines"]


def test_double_click_reset_never_rewrites_the_setting(data_dir):
    """A8-6: with overlay_max_lines 3 the reset draws 3 rows and hands nothing back to the pane."""
    _, app = make_in(data_dir, max_lines=3)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    app.reset_size()
    assert app.settings.max_lines == 3 and app.positions.wanted is None
    assert "resized" not in service(data_dir, {"overlay_max_lines": 3}).status()


def feed(app, n, start=0.0):
    app.on_event({"type": "hello", "seq": 1, "session": {"state": "live"}}, start)
    for i in range(n):
        app.on_event({"type": "line", "seq": 10 + i, "line": {"line_id": f"l{i}", "state": "translated",
                                                             "original": "x",
                                                             "translation": f"Cümle {i} tamamlandı ve bitti.",
                                                             "speaker_id": "s1:speaker_0"}}, start)
    t = start
    while app.queue.waiting() or t < start + 1:
        app.refresh(t)
        t += 0.1
    return t + 0.5


def test_history_growth_never_overwrites_the_chosen_rows(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(0, 700, 360, height(3)), True)  # 360 pt: one sentence per row, high on the screen
    now = feed(app, 12)
    assert len(panel.renders[-1][0].lines) == 3
    app.scroll(1, now)
    view = panel.renders[-1][0]
    assert view.history and len(view.lines) == HISTORY_ROWS
    f = panel.frames[-1]
    assert f.h == height(HISTORY_ROWS) and f.y + f.h <= 900  # kept inside the visible frame
    assert app.settings.max_lines == 3 and stored(data_dir)["displays"][D1.key]["rows"] == 3
    app.back_to_live(now + 1)
    assert len(panel.renders[-1][0].lines) == 3 and panel.frames[-1].h == height(3)


def test_six_rows_roll_up_like_two(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(0, 100, 360, height(6)), True)
    feed(app, 9)
    rows = [" ".join(r.text for r in row if r.kind != "label") for row in panel.renders[-1][0].lines]
    assert rows == [f"Cümle {i} tamamlandı ve bitti." for i in range(3, 9)]
    assert app._unit_chars(1360) == pace.UNIT_CHARS  # still at most a two-line subtitle per unit


# -- the AppKit side: where the handles sit, and their cursors -------------------------

def test_handles_sit_inside_the_strip_and_do_not_overlap():
    pytest.importorskip("AppKit")
    from hermeneus_engine.overlay import panel_mac
    for frame in (Rect(100, 50, 320, 58), Rect(0, 0, 1440, 290)):
        rects = panel_mac.handle_frames(frame)
        for x, y, w, h in rects.values():
            assert frame.x <= x and x + w <= frame.x + frame.w + 1e-6
            assert frame.y <= y and y + h <= frame.y + frame.h + 1e-6
            assert w <= 40 and h <= 40  # small: the rest of the strip lets clicks through
        sx, sy, sw, sh = rects["sizer"]
        cx, cy, cw, ch = rects["corner"]
        assert sy + sh <= cy + 1e-6  # the width handle stays under the corner square


def test_each_handle_has_a_cursor():
    pytest.importorskip("AppKit")
    from hermeneus_engine.overlay import panel_mac
    from AppKit import NSCursor
    assert panel_mac.handle_cursor("right") == NSCursor.resizeLeftRightCursor()
    assert panel_mac.handle_cursor("top") == NSCursor.resizeUpDownCursor()
    assert panel_mac.handle_cursor("corner") is not None
    assert panel_mac.handle_cursor("move") == NSCursor.openHandCursor()


# -- two processes on positions.json (A8-13, the lock wait) ----------------------------

PLUGIN_POLL = r'''
import asyncio, json, sys
from pathlib import Path
from hermeneus_engine.overlay.service import OverlayService
async def st(): return {"state": "live"}
svc = OverlayService(lambda: Path(sys.argv[1]), lambda: {"overlay_max_lines": int(sys.argv[2])}, st,
                     status_fn=lambda _d: {"visible": True, "pid": 1})
print(json.dumps(svc.status().get("resized")))
'''

LOCK_HOLDER = r'''
import sys, time
from hermeneus_engine.overlay.placement import locked
with locked(sys.argv[1]):
    print("held", flush=True)
    time.sleep(30)
'''


def _python(code, *args, **kw):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": root}
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], env=env, cwd=root, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def plugin_poll(data_dir, setting):
    proc = _python(PLUGIN_POLL, data_dir, setting)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    return json.loads(out)


def test_an_open_overlay_with_an_older_copy_never_revives_a_settled_drag(data_dir):
    """A8-13 (re-audit probe a8_twoproc T2): the plugin runs in its own process. Drag 4; the user picks 3 (the
    plugin settles); the overlay, not yet having read settings.json, saves a move; the user picks back 2."""
    _, app = make_in(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)
    assert plugin_poll(data_dir, 3) is None
    path = control.positions_file(data_dir)
    assert json.loads(path.read_text())["lines"] == {"seen": 3}
    app.moved(frame_for(Spot(0.3, 0.2, 0.5), D1, app.frame.h))  # whole file from the overlay's older copy
    body = json.loads(path.read_text())
    assert body["lines"] == {"seen": 3} and "rows" not in body["displays"][D1.key]
    assert body["displays"][D1.key]["bottom"] == pytest.approx(0.2)  # the move itself is kept
    assert plugin_poll(data_dir, 2) is None
    app.apply_settings(ovs.OverlaySettings(max_lines=2, text_size=34))  # the overlay's tick reads the setting
    assert app.settings.max_lines == 2 and plugin_poll(data_dir, 2) is None


def test_a_new_drag_after_a_settle_is_the_overlays_own(data_dir):
    """The merge keeps what the overlay changed itself: a drag made after the plugin settled is not undone."""
    _, app = make_in(data_dir)
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(4)), True)
    assert plugin_poll(data_dir, 3) is None  # the pick of 3 won on disk
    app.resized(Rect(app.frame.x, app.frame.y, app.frame.w, height(5)), True)
    body = json.loads(control.positions_file(data_dir).read_text())
    assert body["displays"][D1.key]["rows"] == 5 and body["lines"]["wanted"] == 5


def test_a_held_lock_never_blocks_the_overlay(data_dir):
    """Another process holds the lock and never lets go: the overlay's save gives up within about 50 ms and
    returns False, and the next save after the holder is gone writes everything."""
    import time
    from hermeneus_engine.overlay import placement
    path = control.positions_file(data_dir)
    pos = Positions(path)
    holder = _python(LOCK_HOLDER, path)
    try:
        assert holder.stdout.readline().strip() == "held"
        pos.spots[D1.key] = Spot(0.3, 0.2, 0.5, 4)
        start = time.monotonic()
        assert pos.save() is False
        assert time.monotonic() - start < placement.LOCK_WAIT_S + 0.1
        assert wanted_rows(path, 2) is None  # the plugin side gives up too
    finally:
        holder.kill()
        holder.communicate(timeout=10)
    assert pos.save() is True and Positions(path).spot(D1.key) == Spot(0.3, 0.2, 0.5, 4)
