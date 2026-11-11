"""The macOS overlay window: two non-activating NSPanels drawn with Hermes theme values.

The strip is a borderless ``NSPanel`` with ``NSWindowStyleMaskNonactivatingPanel``
that ignores the mouse, so every click goes to the app underneath. Its child,
a small grip panel at the strip's left edge, takes the mouse: dragging it moves
the strip, and a right click opens a menu (width, reset position, hide). Both
float at ``NSFloatingWindowLevel`` on every Space and next to full-screen
windows (``CanJoinAllSpaces | FullScreenAuxiliary``), the same choices Hermes
Desktop makes for its own pet overlay window (apps/desktop/electron/main.ts,
the pet overlay ``BrowserWindow``: ``type: 'panel'``, ``focusable: false``,
``setAlwaysOnTop(true, 'floating')``, ``visibleOnFullScreen: true``).

Neither panel can become the key window, and the process runs with the
accessory activation policy, so the overlay never takes keyboard focus and
never shows a Dock icon. It needs no macOS permission: it only draws its own
windows and reads the pointer position from its own mouse events.
"""

from __future__ import annotations

from typing import Callable, Optional

import objc
from AppKit import (
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSPanel,
    NSScreen,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSFloatingWindowLevel,
)
from Foundation import NSObject

from ..overlay_settings import OverlaySettings
from . import theme
from .model import LABEL, PENDING, FAILED, Run, View
from .placement import WIDTH_PRESETS, Display, Rect, display_key

PAD_X = 20.0  # text inset from the strip's sides; the grip sits inside the left one
PAD_Y = 10.0
GRIP_W = 10.0
GRIP_INSET = 5.0
LINE_SPACING = 1.22
ORIGINAL_SCALE = 0.58
LABEL_SCALE = 0.62
LEVEL = NSFloatingWindowLevel
BEHAVIOUR = (NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
             | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle)


def _color(c: theme.Rgba):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(c.r, c.g, c.b, c.a)


def _font(size: float, weight: float):
    # -apple-system, the first face of --dt-font-sans that macOS has, is the system font.
    return NSFont.systemFontOfSize_weight_(size, weight)


def start_application():
    """The accessory NSApplication for this process: no Dock icon, no menu bar, no activation."""
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    return app


def is_dark(view) -> bool:
    match = view.effectiveAppearance().bestMatchFromAppearancesWithNames_([NSAppearanceNameAqua,
                                                                          NSAppearanceNameDarkAqua])
    return match == NSAppearanceNameDarkAqua


class HermeneusOverlayPanel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class HermeneusOverlayStripView(NSView):
    """Draws the strip: a rounded surface, then the lines bottom-aligned, then the original."""

    def initWithFrame_(self, frame):
        self = objc.super(HermeneusOverlayStripView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.owner = None
        return self

    def isOpaque(self):
        return False

    def isFlipped(self):
        return False

    def viewDidChangeEffectiveAppearance(self):
        if self.owner is not None:
            self.owner._appearance_changed()

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw(self)


class HermeneusOverlayGripView(NSView):
    """The drag handle. Takes the mouse without activating the app."""

    def initWithFrame_(self, frame):
        self = objc.super(HermeneusOverlayGripView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.owner = None
        self._start = None
        return self

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, _event):
        if self.owner is not None:
            self._start = (NSEvent.mouseLocation(), self.owner.strip.frame().origin)

    def mouseDragged_(self, _event):
        if self.owner is None or self._start is None:
            return
        (sx, sy), origin = (self._start[0].x, self._start[0].y), self._start[1]
        now = NSEvent.mouseLocation()
        self.owner.strip.setFrameOrigin_((origin.x + now.x - sx, origin.y + now.y - sy))

    def mouseUp_(self, _event):
        if self.owner is not None and self._start is not None:
            self._start = None
            self.owner._drag_ended()

    def rightMouseDown_(self, event):
        if self.owner is not None:
            NSMenu.popUpContextMenu_withEvent_forView_(self.owner._menu(), event, self)

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw_grip(self)


class HermeneusOverlayMenuTarget(NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(HermeneusOverlayMenuTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def setWidth_(self, item):
        self.owner.on_width(WIDTH_PRESETS[item.representedObject()])

    def resetPosition_(self, _item):
        self.owner.on_reset()

    def hideOverlay_(self, _item):
        self.owner.on_hide()


def _make_panel(rect, *, mouse: bool) -> NSPanel:
    panel = HermeneusOverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False)
    panel.setFloatingPanel_(True)
    panel.setBecomesKeyOnlyIfNeeded_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setLevel_(LEVEL)
    panel.setCollectionBehavior_(BEHAVIOUR)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setIgnoresMouseEvents_(not mouse)
    panel.setReleasedWhenClosed_(False)
    panel.setAnimationBehavior_(2)  # NSWindowAnimationBehaviorNone: no zoom when it appears
    return panel


class MacPanel:
    """``app.Panel`` for macOS."""

    def __init__(self, *, on_moved: Callable[[Rect], None] = lambda r: None,
                 on_width: Callable[[float], None] = lambda w: None,
                 on_reset: Callable[[], None] = lambda: None,
                 on_hide: Callable[[], None] = lambda: None,
                 appearance: Optional[str] = None) -> None:
        self.on_moved, self.on_width, self.on_reset, self.on_hide = on_moved, on_width, on_reset, on_hide
        self.view = View()
        self.settings = OverlaySettings()
        self.strip = _make_panel(NSMakeRect(0, 0, 600, 80), mouse=False)
        self.strip.setHasShadow_(True)
        self.strip_view = HermeneusOverlayStripView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 80))
        self.strip_view.owner = self
        self.strip.setContentView_(self.strip_view)
        self.grip = _make_panel(NSMakeRect(0, 0, GRIP_W, 30), mouse=True)
        self.grip.setHasShadow_(False)
        self.grip_view = HermeneusOverlayGripView.alloc().initWithFrame_(NSMakeRect(0, 0, GRIP_W, 30))
        self.grip_view.owner = self
        self.grip.setContentView_(self.grip_view)
        self._menu_target = HermeneusOverlayMenuTarget.alloc().initWithOwner_(self)
        self._fonts: dict = {}
        if appearance is not None:  # screenshots render a chosen appearance; normal runs follow the system
            from AppKit import NSAppearance
            look = NSAppearance.appearanceNamed_(appearance)
            self.strip.setAppearance_(look)
            self.grip.setAppearance_(look)

    # -- Panel protocol -------------------------------------------------------------

    def displays(self) -> list[Display]:
        out = []
        for screen in NSScreen.screens() or []:
            number = int(screen.deviceDescription().get("NSScreenNumber", 0))
            name = str(screen.localizedName()) if hasattr(screen, "localizedName") else ""
            f = screen.visibleFrame()
            out.append(Display(display_key(number, name), Rect(f.origin.x, f.origin.y, f.size.width, f.size.height)))
        return out

    def main_display(self) -> Display:
        screen = NSScreen.mainScreen() or (NSScreen.screens() or [None])[0]
        number = int(screen.deviceDescription().get("NSScreenNumber", 0))
        for d in self.displays():
            if d.key.startswith(f"{number}:"):
                return d
        return self.displays()[0]

    def _font_for(self, run_kind: str, settings: OverlaySettings):
        key = (run_kind, settings.text_size)
        font = self._fonts.get(key)
        if font is None:
            size = settings.text_size
            if run_kind == LABEL:
                font = _font(round(size * LABEL_SCALE), theme.WEIGHT_LABEL)
            elif run_kind == "original":
                font = _font(max(12, round(size * ORIGINAL_SCALE)), theme.WEIGHT_TEXT)
            else:
                font = _font(size, theme.WEIGHT_TEXT)
            self._fonts[key] = font
        return font

    def measure(self, text: str, run: Run, settings: OverlaySettings) -> float:
        attrs = {NSFontAttributeName: self._font_for(run.kind, settings)}
        width = NSAttributedString.alloc().initWithString_attributes_(text, attrs).size().width
        return width + (6.0 if run.kind == LABEL and text.strip() else 0.0)

    def measure_original(self, text: str, settings: OverlaySettings) -> float:
        attrs = {NSFontAttributeName: self._font_for("original", settings)}
        return NSAttributedString.alloc().initWithString_attributes_(text, attrs).size().width

    def _line_h(self, font) -> float:
        return round((font.ascender() - font.descender()) * LINE_SPACING)

    def strip_height(self, settings: OverlaySettings) -> float:
        h = PAD_Y * 2 + settings.max_lines * self._line_h(self._font_for(PENDING, settings))
        if settings.show_original:
            h += self._line_h(self._font_for("original", settings)) + 2
        return float(h)

    def text_inset(self) -> float:
        return PAD_X

    def set_frame(self, frame: Rect) -> None:
        self.strip.setFrame_display_(NSMakeRect(frame.x, frame.y, frame.w, frame.h), True)
        self._place_grip()

    def _place_grip(self) -> None:
        f = self.strip.frame()
        h = max(20.0, min(40.0, f.size.height - 2 * PAD_Y))
        self.grip.setFrame_display_(NSMakeRect(f.origin.x + GRIP_INSET, f.origin.y + (f.size.height - h) / 2,
                                               GRIP_W, h), True)

    def render(self, view: View, settings: OverlaySettings) -> None:
        self.view, self.settings = view, settings
        self.strip_view.setNeedsDisplay_(True)
        self.grip_view.setNeedsDisplay_(True)
        self.strip.invalidateShadow()

    def set_visible(self, visible: bool) -> None:
        if visible:
            self.strip.orderFrontRegardless()  # never makeKeyAndOrderFront: no focus change
            if self.grip.parentWindow() is None:
                self.strip.addChildWindow_ordered_(self.grip, 1)  # NSWindowAbove
            self.grip.orderFrontRegardless()
        else:
            if self.grip.parentWindow() is not None:
                self.strip.removeChildWindow_(self.grip)
            self.grip.orderOut_(None)
            self.strip.orderOut_(None)

    def close(self) -> None:
        self.set_visible(False)

    @property
    def window_ids(self) -> tuple[int, int]:
        return int(self.strip.windowNumber()), int(self.grip.windowNumber())

    # -- drawing ----------------------------------------------------------------------

    def _palette(self, view) -> theme.Palette:
        return theme.palette(is_dark(view))

    def _appearance_changed(self) -> None:
        for view in (getattr(self, "strip_view", None), getattr(self, "grip_view", None)):
            if view is not None:
                view.setNeedsDisplay_(True)

    def _draw(self, view) -> None:
        p = self._palette(view)
        s = self.settings
        bounds = view.bounds()
        surface = p.surface.with_alpha(s.background_alpha)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1), theme.RADIUS_PT, theme.RADIUS_PT)
        _color(surface).setFill()
        path.fill()
        if s.opacity > 0:
            _color(p.stroke).setStroke()
            path.setLineWidth_(1.0)
            path.stroke()
        y = PAD_Y
        if self.view.original:
            font = self._font_for("original", s)
            text = NSAttributedString.alloc().initWithString_attributes_(
                self.view.original, {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(p.text_secondary)})
            text.drawAtPoint_((PAD_X, y - font.descender()))
            y += self._line_h(font) + 2
        elif s.show_original:
            y += self._line_h(self._font_for("original", s)) + 2
        main = self._font_for(PENDING, s)
        line_h = self._line_h(main)
        lines = self.view.lines
        if not lines and self.view.notice:
            lines = ((Run(self.view.notice, PENDING),),)
        for line in reversed(lines):
            text = NSMutableAttributedString.alloc().init()
            for run in line:
                text.appendAttributedString_(self._run_text(run, p, s))
            text.drawAtPoint_((PAD_X, y + (line_h - (main.ascender() - main.descender())) / 2 - main.descender()))
            y += line_h

    def _run_text(self, run: Run, p: theme.Palette, s: OverlaySettings):
        font = self._font_for(run.kind, s)
        if run.kind == LABEL:
            color = p.speaker(run.speaker or 1)
            return NSAttributedString.alloc().initWithString_attributes_(
                run.text + "  ", {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(color)})
        color = {PENDING: p.text_tertiary, FAILED: p.text_secondary}.get(run.kind, p.text)
        return NSAttributedString.alloc().initWithString_attributes_(
            run.text, {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(color)})

    def _draw_grip(self, view) -> None:
        p = self._palette(view)
        bounds = view.bounds()
        _color(p.text_tertiary).setFill()
        cx = bounds.size.width / 2
        for row in range(3):
            for col in (-1, 1):
                y = bounds.size.height / 2 + (row - 1) * 6
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx + col * 2.5 - 1.25, y - 1.25, 2.5, 2.5)).fill()

    # -- interaction --------------------------------------------------------------------

    def _drag_ended(self) -> None:
        f = self.strip.frame()
        self.on_moved(Rect(f.origin.x, f.origin.y, f.size.width, f.size.height))

    def _menu(self):
        menu = NSMenu.alloc().initWithTitle_("Hermeneus overlay")
        menu.setAutoenablesItems_(False)
        width = self.strip.frame().size.width
        screen = self.strip.screen() or NSScreen.mainScreen()
        current = width / screen.visibleFrame().size.width if screen else 0
        for name, label in (("narrow", "Narrow"), ("medium", "Medium"), ("wide", "Wide")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, "setWidth:", "")
            item.setTarget_(self._menu_target)
            item.setRepresentedObject_(name)
            item.setState_(1 if abs(WIDTH_PRESETS[name] - current) < 0.02 else 0)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        for label, action in (("Reset position", "resetPosition:"), ("Hide overlay", "hideOverlay:")):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, action, "")
            item.setTarget_(self._menu_target)
            menu.addItem_(item)
        return menu
