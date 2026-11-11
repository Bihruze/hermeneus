"""The macOS overlay window: non-activating NSPanels drawn with the Hermes token table.

The strip is a borderless ``NSPanel`` with ``NSWindowStyleMaskNonactivatingPanel``
that ignores the mouse, so every click goes to the app underneath. Two small
child panels take the mouse: the grip at the left edge moves the strip (right
click opens a menu: width presets, reset position, hide) and the handle at the
right edge resizes its width. All float at ``NSFloatingWindowLevel`` on every
Space and next to full-screen windows (``CanJoinAllSpaces | FullScreenAuxiliary``),
the same choices Hermes Desktop makes for its own pet overlay window
(apps/desktop/electron/main.ts, the pet overlay ``BrowserWindow``:
``type: 'panel'``, ``focusable: false``, ``setAlwaysOnTop(true, 'floating')``,
``visibleOnFullScreen: true``).

Neither panel can become the key window, and the process runs with the
accessory activation policy, so the overlay never takes keyboard focus and
never shows a Dock icon. It needs no macOS permission: it only draws its own
windows and reads the pointer position from its own mouse events.

Layout, bottom up: the translation rows, then the original in small text
above them (as on the 127.0.0.1 page). The strip grows and shrinks with its
rows, keeping its bottom edge. Text carries a soft shadow in the surface
colour so it stays readable at any background opacity. A change cross-fades
in 150 ms unless the user asked macOS to reduce motion.
"""

from __future__ import annotations

from typing import Callable, Optional

import objc
from AppKit import (
    NSAnimationContext,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSEvent,
    NSFloatingWindowLevel,
    NSFont,
    NSFontAttributeName,
    NSFontManager,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageView,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSMutableAttributedString,
    NSPanel,
    NSScreen,
    NSShadow,
    NSShadowAttributeName,
    NSStrokeColorAttributeName,
    NSStrokeWidthAttributeName,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSWorkspace,
)
from Foundation import NSObject

from ..overlay_settings import OverlaySettings
from . import theme
from .model import FAILED, LABEL, PENDING, Run, View
from .placement import MIN_WIDTH_PT, WIDTH_PRESETS, Display, Rect, display_key

PAD_X = 20.0  # text inset from the strip's sides; the grip and the resize handle sit inside them
PAD_Y = 10.0
HANDLE_W = 10.0
HANDLE_INSET = 5.0
LINE_SPACING = 1.22
ORIGINAL_SCALE = 0.58
LABEL_SCALE = 0.62
ORIGINAL_GAP = 2.0
FADE_S = 0.15
OUTLINE_PERCENT = 12.0  # stroke width as a percentage of the font size (NSStrokeWidthAttributeName, positive = outline only)
LEVEL = NSFloatingWindowLevel
BEHAVIOUR = (NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
             | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle)
SYSTEM_FAMILIES = {"-apple-system", "blinkmacsystemfont", "system-ui", "sf pro text", "sf pro", "sans-serif",
                   "ui-sans-serif"}


def _color(c: theme.Rgba):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(c.r, c.g, c.b, c.a)


def resolve_font(stack: str, size: float, css_weight: int):
    """The first family of a CSS stack this Mac has; the system font for the system names."""
    for part in (stack or "").split(","):
        name = part.strip().strip("'\"")
        if not name:
            continue
        if name.lower() in SYSTEM_FAMILIES:
            return NSFont.systemFontOfSize_weight_(size, theme.ns_weight(css_weight))
        manager_weight = {400: 5, 500: 6, 600: 8, 700: 9}.get(css_weight, 5)
        font = NSFontManager.sharedFontManager().fontWithFamily_traits_weight_size_(name, 0, manager_weight, size)
        if font is not None:
            return font
    return NSFont.systemFontOfSize_weight_(size, theme.ns_weight(css_weight))


def start_application():
    """The accessory NSApplication for this process: no Dock icon, no menu bar, no activation."""
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    return app


def system_is_dark(view) -> bool:
    match = view.effectiveAppearance().bestMatchFromAppearancesWithNames_([NSAppearanceNameAqua,
                                                                          NSAppearanceNameDarkAqua])
    return match == NSAppearanceNameDarkAqua


def reduce_motion() -> bool:
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except AttributeError:
        return False


class HermeneusOverlayPanel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class HermeneusOverlayOwnedView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(HermeneusOverlayOwnedView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.owner = None
        return self

    def isOpaque(self):
        return False


class HermeneusOverlayStripView(HermeneusOverlayOwnedView):
    """The surface: a rounded fill and the token hairline."""

    def viewDidChangeEffectiveAppearance(self):
        if self.owner is not None:
            self.owner._appearance_changed()

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw_surface(self)


class HermeneusOverlayTextView(HermeneusOverlayOwnedView):
    """The rows and the original, drawn bottom up."""

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw_text(self)


class HermeneusOverlayHandleView(HermeneusOverlayOwnedView):
    """A grip (``role`` "move") or a width handle (``role`` "resize"). Takes the mouse without activating."""

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, _event):
        if self.owner is not None:
            self.start = (NSEvent.mouseLocation(), self.owner.strip.frame())

    def mouseDragged_(self, _event):
        start = getattr(self, "start", None)
        if self.owner is None or start is None:
            return
        now = NSEvent.mouseLocation()
        self.owner._dragged(self.role, start[1], now.x - start[0].x, now.y - start[0].y)

    def mouseUp_(self, _event):
        if self.owner is not None and getattr(self, "start", None) is not None:
            self.start = None
            self.owner._drag_ended()

    def rightMouseDown_(self, event):
        if self.owner is not None:
            NSMenu.popUpContextMenu_withEvent_forView_(self.owner._menu(), event, self)

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw_handle(self)


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


def _view(cls, frame, owner, **attrs):
    view = cls.alloc().initWithFrame_(frame)
    view.owner = owner
    for key, value in attrs.items():
        setattr(view, key, value)
    return view


class MacPanel:
    """``app.Panel`` for macOS."""

    def __init__(self, *, on_moved: Callable[[Rect], None] = lambda r: None,
                 on_width: Callable[[float], None] = lambda w: None,
                 on_reset: Callable[[], None] = lambda: None,
                 on_hide: Callable[[], None] = lambda: None,
                 appearance: Optional[str] = None, motion: Optional[bool] = None, halo: bool = True) -> None:
        self.on_moved, self.on_width, self.on_reset, self.on_hide = on_moved, on_width, on_reset, on_hide
        self.view = View()
        self.settings = OverlaySettings()
        self.sent: Optional[theme.Palette] = None
        self._motion = motion
        self._halo = halo  # False only in contrast measurements, to compare with and without the halo
        self._fonts: dict = {}
        self.strip = _make_panel(NSMakeRect(0, 0, 600, 80), mouse=False)
        self.strip.setHasShadow_(True)
        self.strip_view = _view(HermeneusOverlayStripView, NSMakeRect(0, 0, 600, 80), self)
        self.strip_view.setWantsLayer_(True)
        self.text_view = _view(HermeneusOverlayTextView, NSMakeRect(0, 0, 600, 80), self)
        self.text_view.setAutoresizingMask_(18)  # width and height follow the strip
        self.strip_view.addSubview_(self.text_view)
        self.strip.setContentView_(self.strip_view)
        self.grip, self.grip_view = self._handle("move")
        self.sizer, self.sizer_view = self._handle("resize")
        self._menu_target = HermeneusOverlayMenuTarget.alloc().initWithOwner_(self)
        self._fade: Optional[NSImageView] = None
        if appearance is not None:  # renders for review pick an appearance; normal runs follow the system
            from AppKit import NSAppearance
            look = NSAppearance.appearanceNamed_(appearance)
            for window in (self.strip, self.grip, self.sizer):
                window.setAppearance_(look)

    def _handle(self, role: str):
        panel = _make_panel(NSMakeRect(0, 0, HANDLE_W, 30), mouse=True)
        panel.setHasShadow_(False)
        view = _view(HermeneusOverlayHandleView, NSMakeRect(0, 0, HANDLE_W, 30), self, role=role, start=None)
        panel.setContentView_(view)
        return panel, view

    # -- palette and motion -------------------------------------------------------------

    def set_palette(self, sent: Optional[theme.Palette]) -> None:
        """The token values the Desktop pane sent, or None for the nous fallback."""
        if sent != self.sent:
            self.sent = sent
            self._fonts.clear()
            self._appearance_changed()

    def palette(self) -> theme.Palette:
        return theme.palette(system_is_dark(self.strip_view), self.sent)

    def motion(self) -> bool:
        return (not reduce_motion()) if self._motion is None else self._motion

    # -- Panel protocol -------------------------------------------------------------------

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

    def _font_for(self, kind: str, settings: OverlaySettings):
        key = (kind, settings.text_size)
        font = self._fonts.get(key)
        if font is None:
            size = settings.text_size
            stack = self.palette().font
            if kind == LABEL:
                font = resolve_font(stack, round(size * LABEL_SCALE), theme.WEIGHT_LABEL)
            elif kind == "original":
                font = resolve_font(stack, max(12, round(size * ORIGINAL_SCALE)), theme.WEIGHT_TEXT)
            else:
                font = resolve_font(stack, size, theme.WEIGHT_TEXT)
            self._fonts[key] = font
        return font

    def measure(self, text: str, run: Run, settings: OverlaySettings) -> float:
        attrs = {NSFontAttributeName: self._font_for(run.kind, settings)}
        width = NSAttributedString.alloc().initWithString_attributes_(text, attrs).size().width
        return width + (6.0 if run.kind == LABEL and text.strip() else 0.0)

    def measure_original(self, text: str, settings: OverlaySettings) -> float:
        attrs = {NSFontAttributeName: self._font_for("original", settings)}
        return NSAttributedString.alloc().initWithString_attributes_(text, attrs).size().width

    @staticmethod
    def _line_h(font) -> float:
        return round((font.ascender() - font.descender()) * LINE_SPACING)

    def strip_height(self, settings: OverlaySettings, rows: Optional[int] = None,
                     original: Optional[bool] = None) -> float:
        """Height for ``rows`` rows (default: max lines) and the original line when shown."""
        rows = settings.max_lines if rows is None else max(1, rows)
        original = settings.show_original if original is None else original
        h = PAD_Y * 2 + rows * self._line_h(self._font_for(PENDING, settings))
        if original:
            h += self._line_h(self._font_for("original", settings)) + ORIGINAL_GAP
        return float(h)

    def text_inset(self) -> float:
        return PAD_X

    def set_frame(self, frame: Rect) -> None:
        target = NSMakeRect(frame.x, frame.y, frame.w, frame.h)
        current = self.strip.frame()
        moving_only = abs(current.size.height - frame.h) < 0.5 and abs(current.size.width - frame.w) < 0.5
        if self.strip.isVisible() and self.motion() and not moving_only:
            def grow(context):
                context.setDuration_(FADE_S)
                self.strip.animator().setFrame_display_(target, True)
            NSAnimationContext.runAnimationGroup_completionHandler_(grow, self._place_handles)
        else:
            self.strip.setFrame_display_(target, True)
        self._place_handles(frame)

    def _place_handles(self, frame: Optional[Rect] = None) -> None:
        if frame is None:
            f = self.strip.frame()
            frame = Rect(f.origin.x, f.origin.y, f.size.width, f.size.height)
        h = max(20.0, min(40.0, frame.h - 2 * PAD_Y))
        y = frame.y + (frame.h - h) / 2
        self.grip.setFrame_display_(NSMakeRect(frame.x + HANDLE_INSET, y, HANDLE_W, h), True)
        self.sizer.setFrame_display_(NSMakeRect(frame.x + frame.w - HANDLE_INSET - HANDLE_W, y, HANDLE_W, h), True)

    def render(self, view: View, settings: OverlaySettings) -> None:
        if self.strip.isVisible() and self.motion() and not self.view.empty and view != self.view:
            self._cross_fade()
        self.view, self.settings = view, settings
        self.strip_view.setNeedsDisplay_(True)
        self.text_view.setNeedsDisplay_(True)
        for handle in (self.grip_view, self.sizer_view):
            handle.setNeedsDisplay_(True)
        self.strip.invalidateShadow()

    def _cross_fade(self) -> None:
        """Lay a picture of the old text over the new and fade it out: a smooth swap and roll."""
        bounds = self.text_view.bounds()
        rep = self.text_view.bitmapImageRepForCachingDisplayInRect_(bounds)
        if rep is None:
            return
        self.text_view.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
        image = NSImage.alloc().initWithSize_(bounds.size)
        image.addRepresentation_(rep)
        if self._fade is not None:
            self._fade.removeFromSuperview()
        fade = NSImageView.alloc().initWithFrame_(bounds)
        fade.setImage_(image)
        fade.setAutoresizingMask_(18)
        self.strip_view.addSubview_(fade)
        self._fade = fade

        def run(context):
            context.setDuration_(FADE_S)
            fade.animator().setAlphaValue_(0.0)

        def done():
            fade.removeFromSuperview()
            if self._fade is fade:
                self._fade = None

        NSAnimationContext.runAnimationGroup_completionHandler_(run, done)

    def set_visible(self, visible: bool) -> None:
        handles = (self.grip, self.sizer)
        if visible:
            self.strip.orderFrontRegardless()  # never makeKeyAndOrderFront: no focus change
            for handle in handles:
                if handle.parentWindow() is None:
                    self.strip.addChildWindow_ordered_(handle, 1)  # NSWindowAbove
                handle.orderFrontRegardless()
        else:
            for handle in handles:
                if handle.parentWindow() is not None:
                    self.strip.removeChildWindow_(handle)
                handle.orderOut_(None)
            self.strip.orderOut_(None)

    def close(self) -> None:
        self.set_visible(False)

    @property
    def window_ids(self) -> tuple[int, int, int]:
        return int(self.strip.windowNumber()), int(self.grip.windowNumber()), int(self.sizer.windowNumber())

    # -- drawing ----------------------------------------------------------------------

    def _appearance_changed(self) -> None:
        for name in ("strip_view", "text_view", "grip_view", "sizer_view"):
            view = getattr(self, name, None)
            if view is not None:
                view.setNeedsDisplay_(True)

    def _draw_surface(self, view) -> None:
        p = self.palette()
        bounds = view.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1), p.radius_px, p.radius_px)
        _color(p.surface.with_alpha(p.surface.a * self.settings.background_alpha)).setFill()
        path.fill()
        if self.settings.opacity > 0:
            _color(p.stroke).setStroke()
            path.setLineWidth_(1.0)
            path.stroke()

    def _shadow(self, p: theme.Palette, size: float):
        """A soft halo in the surface colour, so text stays readable over any video at any opacity."""
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(_color(p.surface.with_alpha(0.9 if self._halo else 0.0)))
        shadow.setShadowBlurRadius_(max(2.0, size * 0.14))
        shadow.setShadowOffset_((0, 0))
        return shadow

    def _draw_text(self, view) -> None:
        p = self.palette()
        s = self.settings
        main = self._font_for(PENDING, s)
        line_h = self._line_h(main)
        shadow = self._shadow(p, s.text_size)
        lines = self.view.lines
        if not lines and self.view.notice:
            lines = ((Run(self.view.notice, "notice"),),)
        y = PAD_Y
        for line in reversed(lines):
            text = NSMutableAttributedString.alloc().init()
            for run in line:
                text.appendAttributedString_(self._run_text(run, p, s, shadow))
            self._draw_legible(text, p, (PAD_X, y + (line_h - (main.ascender() - main.descender())) / 2 - main.descender()))
            y += line_h
        if self.view.original:
            font = self._font_for("original", s)
            attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(p.text_secondary),
                     NSShadowAttributeName: self._shadow(p, font.pointSize())}
            text = NSAttributedString.alloc().initWithString_attributes_(self.view.original, attrs)
            self._draw_legible(text, p, (PAD_X, y + ORIGINAL_GAP - font.descender()))

    def _draw_legible(self, text, p: theme.Palette, point) -> None:
        """Text over video. Under 50% background an outline in the surface colour goes under the glyphs,
        so they keep their edge on bright or dark video; the soft halo is drawn with the glyphs."""
        if self._halo and self.settings.opacity < 50:
            outline = NSMutableAttributedString.alloc().initWithAttributedString_(text)
            whole = (0, outline.length())
            outline.addAttribute_value_range_(NSStrokeColorAttributeName, _color(p.surface.with_alpha(0.95)), whole)
            outline.addAttribute_value_range_(NSStrokeWidthAttributeName, OUTLINE_PERCENT, whole)
            outline.drawAtPoint_(point)
        text.drawAtPoint_(point)

    def _run_text(self, run: Run, p: theme.Palette, s: OverlaySettings, shadow):
        font = self._font_for(run.kind, s)
        if run.kind == LABEL:
            color = p.speaker(run.speaker or 1)
            text = run.text + "  "
        else:
            # A line still being spoken and an untranslated line use secondary text (74%), as on the page.
            color = {PENDING: p.text_secondary, FAILED: p.text_secondary, "notice": p.text_tertiary}.get(run.kind,
                                                                                                         p.text)
            text = run.text
        return NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(color),
                   NSShadowAttributeName: shadow})

    def _draw_handle(self, view) -> None:
        p = self.palette()
        bounds = view.bounds()
        _color(p.text_tertiary).setFill()
        cx = bounds.size.width / 2
        columns = (-1, 1) if view.role == "move" else (0,)
        for row in range(3):
            for col in columns:
                y = bounds.size.height / 2 + (row - 1) * 6
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx + col * 2.5 - 1.25, y - 1.25, 2.5, 2.5)).fill()

    # -- interaction --------------------------------------------------------------------

    def _dragged(self, role: str, start, dx: float, dy: float) -> None:
        if role == "move":
            self.strip.setFrameOrigin_((start.origin.x + dx, start.origin.y + dy))
            return
        width = max(MIN_WIDTH_PT, start.size.width + dx)
        self.strip.setFrame_display_(NSMakeRect(start.origin.x, start.origin.y, width, start.size.height), True)
        self._place_handles()

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
