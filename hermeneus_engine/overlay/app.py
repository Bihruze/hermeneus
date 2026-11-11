"""The overlay's controller, between the event stream and a window. No AppKit here.

``Panel`` is what a platform window must do. macOS has ``panel_mac.MacPanel``;
a Windows build would supply a layered, topmost, no-activate window with the
same methods (not built; see README). Tests drive ``OverlayApp`` with a fake.
"""

from __future__ import annotations

from typing import Optional, Protocol

from ..overlay_settings import OverlaySettings
from . import pace
from .model import PENDING, TRANSLATED, OverlayModel, Run, Strings, View, fit_head
from .placement import Display, Positions, Rect, Spot, display_at, frame_for, spot_for

HISTORY_ROWS = 6  # rows the strip grows to while the user looks back
HISTORY_IDLE_S = 8.0  # back to live after this long without a scroll


class Panel(Protocol):
    def displays(self) -> list[Display]: ...
    def main_display(self) -> Display: ...
    def measure(self, text: str, run: Run, settings: OverlaySettings) -> float: ...
    def measure_original(self, text: str, settings: OverlaySettings) -> float: ...
    def strip_height(self, settings: OverlaySettings, rows: Optional[int] = None,
                     original: Optional[bool] = None) -> float: ...
    def text_inset(self) -> float: ...
    def set_frame(self, frame: Rect) -> None: ...
    def render(self, view: View, settings: OverlaySettings) -> None: ...
    def set_visible(self, visible: bool) -> None: ...


class OverlayApp:
    def __init__(self, panel: Panel, positions: Positions, settings: OverlaySettings) -> None:
        self.panel = panel
        self.positions = positions
        self.settings = settings
        self.model = OverlayModel()
        self.frame: Optional[Rect] = None
        self.display: Optional[Display] = None
        self.view = View()
        self.visible = False
        self.strings = Strings()
        self.queue = pace.PaceQueue(pace=settings.pace)
        self.rows_cache = pace.RowCache()
        self.offset: Optional[int] = None  # rows scrolled back from the newest; None while live
        self.history_at = 0.0
        self.history_rows = 0  # rows in the history the last time it was drawn
        self.live_from = 0.0  # the live strip shows units from this time on (after it cleared)

    # -- placement ----------------------------------------------------------------

    def place(self) -> Rect:
        displays = self.panel.displays()
        display = self.positions.choose(displays, self.panel.main_display())
        self.display = display
        self.frame = frame_for(self.positions.spot(display.key), display, self.panel.strip_height(self.settings))
        self.panel.set_frame(self.frame)
        return self.frame

    def moved(self, frame: Rect) -> None:
        """The user dropped the strip at ``frame``: remember it for the display it is on."""
        displays = self.panel.displays()
        display = display_at(frame, displays) or self.display or self.panel.main_display()
        spot = spot_for(frame, display)
        self.frame = frame_for(spot, display, frame.h)
        self.display = display
        self.panel.set_frame(self.frame)
        self.positions.remember(display.key, spot)

    def set_width(self, fraction: float) -> None:
        if self.display is None or self.frame is None:
            return
        spot = spot_for(self.frame, self.display)
        spot = Spot(spot.cx, spot.bottom, fraction)
        self.positions.remember(self.display.key, spot)
        self.frame = frame_for(spot, self.display, self.frame.h)
        self.panel.set_frame(self.frame)
        self.refresh(self._now)

    def reset_position(self) -> None:
        if self.display is None:
            return
        self.positions.remember(self.display.key, Spot())
        self.place()
        self.refresh(self._now)

    def screens_changed(self) -> None:
        """Displays were added, removed or rearranged: put the strip back on an attached one."""
        self.place()
        self.refresh(self._now)

    # -- content ------------------------------------------------------------------

    _now = 0.0

    def _fit_height(self, view: View) -> None:
        """The strip follows its content up to max lines, keeping its bottom edge."""
        if view.empty or self.frame is None or self.display is None:
            return
        rows = len(view.lines) or 1
        height = self.panel.strip_height(self.settings, rows, bool(view.original))
        if abs(height - self.frame.h) < 0.5:
            return
        self.frame = frame_for(spot_for(self.frame, self.display), self.display, height)
        self.panel.set_frame(self.frame)

    def repaint(self) -> None:
        """Fonts or colours changed: measure and draw again."""
        if self.frame is None:
            return
        self.view = View()
        self.rows_cache = pace.RowCache()
        self.place()
        self.refresh(self._now)

    def apply_settings(self, settings: OverlaySettings) -> None:
        if settings == self.settings:
            return
        self.settings = settings
        self.queue.pace = settings.pace
        self.rows_cache = pace.RowCache()
        self.place()
        self.view = View()  # force a redraw with the new look
        self.refresh(self._now)

    def on_event(self, event: dict, now: float) -> None:
        self._now = now
        before = len(self.model.lines)
        if self.model.apply(event, now):
            if before and not self.model.lines:  # reset, or a new owner process: start again
                self.queue.clear()
                self.offset = None
            self.refresh(now)

    # -- history ------------------------------------------------------------------

    def scroll(self, rows: int, now: float) -> None:
        """Look back ``rows`` rows (negative: forward). The first step back opens the history (the strip
        grows to ``HISTORY_ROWS``); scrolling forward past the newest row is live again."""
        self._now = now
        if self.offset is None:
            if rows <= 0:
                return
            self.offset = rows - 1
        else:
            self.offset += rows
        if self.offset < 0:
            self.back_to_live(now)
            return
        self.history_at = now
        self.refresh(now)

    def back_to_live(self, now: float) -> None:
        self._now = now
        if self.offset is not None:
            self.offset = None
            self.refresh(now)

    # -- content ------------------------------------------------------------------

    _sample = "abcdefghijklmnopqrstuvwxyz çğıöşü"

    def _unit_chars(self, width: float) -> int:
        """How many characters a unit may hold so it fits the strip's rows whole (85%, for the label and
        uneven breaks), at most a two-line subtitle."""
        key = (width, self.settings)
        if getattr(self, "_unit_key", None) != key:
            per_char = self.panel.measure(self._sample, Run(self._sample, TRANSLATED), self.settings) / len(self._sample)
            rows_chars = width / per_char if per_char > 0 else pace.CHUNK_CHARS
            self._unit_key = key
            self._unit_value = int(max(16, min(pace.UNIT_CHARS, 0.85 * self.settings.max_lines * rows_chars)))
        return self._unit_value

    def _cps(self, unit: pace.Unit) -> float:
        return pace.reading_cps(unit.language, self.settings.reading_cps)

    def build(self, now: float, width: float) -> View:
        s = self.settings
        q = self.queue
        q.limit = self._unit_chars(width)
        q.sync(self.model, now, s.show_original)
        q.advance(now, self._cps)
        measure = lambda text, run: self.panel.measure(text, run, s)  # noqa: E731
        shown = q.shown()
        pending = any(line.state == "committed" or (line.state == "partial" and line.original.strip())
                      for line in self.model.lines.values())
        if self.offset is not None and now - self.history_at > HISTORY_IDLE_S:
            self.offset = None
        if self.offset is not None:
            rows = pace.all_rows(shown, self.model, width=width, measure=measure, show_speaker=s.show_speaker,
                                 strings=self.strings, cache=self.rows_cache)
            count = max(s.max_lines, HISTORY_ROWS)
            # New rows arrived below while the user reads back: keep the same rows in view.
            if self.offset > 0 and self.history_rows and len(rows) > self.history_rows:
                self.offset += len(rows) - self.history_rows
            self.history_rows = len(rows)
            if rows:
                self.offset = max(0, min(self.offset, len(rows) - count))
                lines = pace.window(rows, count, self.offset)
                return View(lines, "", "", history=True, newer=self.offset, older=len(rows) - self.offset > count)
            self.offset = None
        self.history_rows = 0
        last = shown[-1] if shown else None
        if last is not None and q.quiet_since() is not None and \
                now > max(last.shown_at + pace.HOLD_S, q.free_at + pace.HOLD_S - last.dwell):
            self.live_from = now  # quiet long enough: the strip clears, and starts afresh
        live = [u for u in shown if u.shown_at >= self.live_from]
        rows = pace.all_rows(live, self.model, width=width, measure=measure, show_speaker=s.show_speaker,
                             strings=self.strings, cache=self.rows_cache) if live else []
        lines = pace.window(rows, s.max_lines)
        original = ""
        if s.show_original and live and live[-1].kind == TRANSLATED and live[-1].original:
            original = fit_head(live[-1].original, width, lambda text, run: self.panel.measure_original(text, s),
                                Run(live[-1].original, PENDING))
        notice = ""
        if not lines:
            if self.model.state == "reconnecting":
                notice = self.model.message or self.strings.reconnecting
            elif self.model.running and not self.model.seen_line:
                notice = self.strings.waiting
        if not self.model.running and self.model.state:
            pending = False
        older = len(rows) > len(lines) or len(shown) > len(live)
        return View(lines, original, notice, pending=pending and bool(lines or not notice), older=older)

    def refresh(self, now: float) -> None:
        """Rebuild the view; draw only when it changed, so an unchanged line never repaints."""
        self._now = now
        if self.frame is None:
            self.place()
        s = self.settings
        width = max(0.0, self.frame.w - 2 * self.panel.text_inset())
        view = self.build(now, width)
        if view != self.view:
            self.view = view
            self._fit_height(view)
            self.panel.render(view, s)
        visible = not view.empty
        if visible != self.visible:
            self.visible = visible
            self.panel.set_visible(visible)
