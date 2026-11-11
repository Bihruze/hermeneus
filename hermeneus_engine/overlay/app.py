"""The overlay's controller, between the event stream and a window. No AppKit here.

``Panel`` is what a platform window must do. macOS has ``panel_mac.MacPanel``;
a Windows build would supply a layered, topmost, no-activate window with the
same methods (not built; see README). Tests drive ``OverlayApp`` with a fake.
"""

from __future__ import annotations

from typing import Optional, Protocol

from ..overlay_settings import OverlaySettings
from .model import OverlayModel, Run, Strings, View, build_view
from .placement import Display, Positions, Rect, Spot, display_at, frame_for, spot_for


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
        self.place()
        self.refresh(self._now)

    def apply_settings(self, settings: OverlaySettings) -> None:
        if settings == self.settings:
            return
        self.settings = settings
        self.place()
        self.view = View()  # force a redraw with the new look
        self.refresh(self._now)

    def on_event(self, event: dict, now: float) -> None:
        self._now = now
        if self.model.apply(event, now):
            self.refresh(now)

    def refresh(self, now: float) -> None:
        """Rebuild the view; draw only when it changed, so an unchanged line never repaints."""
        self._now = now
        if self.frame is None:
            self.place()
        s = self.settings
        width = max(0.0, self.frame.w - 2 * self.panel.text_inset())
        view = build_view(self.model, now, width=width, max_lines=s.max_lines, show_speaker=s.show_speaker,
                          show_original=s.show_original, strings=self.strings,
                          measure=lambda text, run: self.panel.measure(text, run, s),
                          original_measure=lambda text, run: self.panel.measure_original(text, s))
        if view != self.view:
            self.view = view
            self._fit_height(view)
            self.panel.render(view, s)
        visible = not view.empty
        if visible != self.visible:
            self.visible = visible
            self.panel.set_visible(visible)
