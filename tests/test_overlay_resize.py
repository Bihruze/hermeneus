"""Free resize of the native strip (W31): frame math, row snapping, clamping, per-display memory and the
round trip of the dragged rows through the overlay_max_lines setting. No AppKit except the last tests."""

import json

import pytest

from pipeline_fakes import remove_tmpdir, short_tmpdir

from hermeneus_engine import overlay_settings as ovs
from hermeneus_engine.overlay import control, pace
from hermeneus_engine.overlay.app import HISTORY_ROWS, OverlayApp
from hermeneus_engine.overlay.placement import (MIN_WIDTH_PT, Display, Positions, Rect, Spot,
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


def make_in(data_dir):
    """An app using the real positions file under the overlay directory, as the overlay process does."""
    control.overlay_dir(data_dir)
    panel = FakePanel()
    app = OverlayApp(panel, Positions(control.positions_file(data_dir)), ovs.OverlaySettings())
    app.place()
    return panel, app


# -- reset, history, pace -----------------------------------------------------------

def test_double_click_reset_gives_default_width_and_rows_in_place(data_dir):
    panel, app = make(data_dir)
    app.resized(Rect(100, 200, 1200, height(5)), True)
    app.reset_size()
    spot = Positions(data_dir / "p.json").spot(D1.key)
    assert (spot.width, spot.rows) == (0.6, 2) and abs(panel.frames[-1].y - 200) < 1e-6
    assert app.settings.max_lines == 2 and "wanted" not in stored(data_dir)["lines"]


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
