"""The macOS overlay window: non-activating NSPanels drawn with the Hermes token table.

The strip is a borderless ``NSPanel`` with ``NSWindowStyleMaskNonactivatingPanel``
that ignores the mouse, so every click goes to the app underneath. Small
child panels take the mouse: the grip at the left edge moves the strip (right
click opens a menu: width presets, reset position, hide; a double click
gives back the default width and lets the rows follow the setting), the handle at the right edge resizes its width,
the handle at the top edge its height and the one in the top right corner
both (the bottom edge stays put; on release the height snaps to whole rows,
``OverlayApp.resized``), and the history tab above the strip's right end
opens the history (click, or scroll over it). Each handle shows a resize
cursor while the pointer is over it. While the history is open the
strip itself takes the mouse: scrolling over it moves through earlier rows, a
click on it or on the "Back to live" tab above it returns to live, and so does
eight seconds without a scroll (``app.HISTORY_IDLE_S``). Then it ignores the
mouse again. All float at ``NSFloatingWindowLevel`` on every
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
colour so it stays readable at any background opacity; under 50% it also
gets a thin outline behind an opaque fill (``theme.text_edge``). A change cross-fades
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
    NSCursor,
    NSCursorFrameResizeDirectionsAll,
    NSCursorFrameResizePositionTopRight,
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
    NSTrackingActiveAlways,
    NSTrackingArea,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
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
from ..overlay_tokens import STRIP_RADIUS_PX
from . import theme
from .model import FAILED, LABEL, MARK, PENDING, Run, Strings, View
from ..overlay_settings import MAX_LINES_MAX, MAX_LINES_MIN
from .placement import WIDTH_PRESETS, Display, Rect, display_key, resize_frame

PAD_X = 20.0  # text inset from the strip's sides; the grip and the resize handle sit inside them
PAD_Y = 10.0
HANDLE_W = 10.0
HANDLE_INSET = 5.0
TOP_HANDLE_W = 36.0  # the top edge handle: a short bar in the strip's top padding
CORNER = 14.0  # the top right corner handle, a square
RESIZE_ROLES = ("right", "top", "corner")
LINE_SPACING = 1.22
ORIGINAL_SCALE = 0.58
LABEL_SCALE = 0.62
ORIGINAL_GAP = 2.0
FADE_S = 0.15
TAB_H = 22.0  # the history and "back to live" tabs above the strip's top edge
TAB_GAP = 3.0
LABEL_PAD = 4.0  # the speaker number's pill reaches this far beyond its digits
DOT_R = 2.2  # the "translation coming" dots
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
    """The surface: a rounded fill and the token hairline. Takes the mouse only while the history is open."""

    def acceptsFirstMouse_(self, _event):
        return True

    def scrollWheel_(self, event):
        if self.owner is not None:
            self.owner._scrolled(event)

    def mouseDown_(self, _event):
        if self.owner is not None:
            self.owner.on_live()

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


def handle_cursor(role: str):
    """The pointer over a handle: an open hand on the grip, resize arrows on the edges and the corner."""
    if role == "right":
        return NSCursor.resizeLeftRightCursor()
    if role == "top":
        return NSCursor.resizeUpDownCursor()
    if role == "corner":
        if NSCursor.respondsToSelector_("frameResizeCursorFromPosition:inDirections:"):  # macOS 15 and later
            return NSCursor.frameResizeCursorFromPosition_inDirections_(NSCursorFrameResizePositionTopRight,
                                                                        NSCursorFrameResizeDirectionsAll)
        return NSCursor.crosshairCursor()
    return NSCursor.openHandCursor()


class HermeneusOverlayHandleView(HermeneusOverlayOwnedView):
    """The grip (``role`` "move") or a resize handle (``role`` "right", "top" or "corner"). Takes the mouse
    without activating; nothing around it does, so clicks next to it still reach the app underneath."""

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseEntered_(self, _event):
        handle_cursor(self.role).set()

    def mouseExited_(self, _event):
        if getattr(self, "start", None) is None:  # while dragging the pointer may run ahead of the handle
            NSCursor.arrowCursor().set()

    def mouseDown_(self, event):
        if self.owner is None:
            return
        if self.role == "move" and event.clickCount() == 2:
            self.start = None
            self.owner.on_reset_size()
            return
        self.owner._drag_began(self.role)
        self.start = (NSEvent.mouseLocation(), self.owner.strip.frame())

    def mouseDragged_(self, _event):
        start = getattr(self, "start", None)
        if self.owner is None or start is None:
            return
        now = NSEvent.mouseLocation()
        handle_cursor(self.role).set()
        self.owner._dragged(self.role, start[1], now.x - start[0].x, now.y - start[0].y)

    def mouseUp_(self, _event):
        if self.owner is not None and getattr(self, "start", None) is not None:
            self.start = None
            self.owner._drag_ended(self.role)
            NSCursor.arrowCursor().set()

    def rightMouseDown_(self, event):
        if self.owner is not None:
            NSMenu.popUpContextMenu_withEvent_forView_(self.owner._menu(), event, self)

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw_handle(self)


class HermeneusOverlayTabView(HermeneusOverlayOwnedView):
    """The history tab (``role`` "history": click or scroll to look back) and the "back to live" tab."""

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, _event):
        if self.owner is None:
            return
        if self.role == "live":
            self.owner.on_live()
        else:
            self.owner.on_scroll(1 if not self.owner.view.history else max(1, len(self.owner.view.lines) - 1))

    def scrollWheel_(self, event):
        if self.owner is not None:
            self.owner._scrolled(event)

    def drawRect_(self, _rect):
        if self.owner is not None:
            self.owner._draw_tab(self)


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


def handle_frames(frame: Rect) -> dict:
    """Where the grip and the resize handles sit for a strip at ``frame``: ``{panel attribute: (x, y, w, h)}``.
    All lie inside the strip: the grip and the width handle in the side padding, the height handle in the top
    padding, the corner square in the top right corner, above the width handle."""
    top = frame.y + frame.h
    right = frame.x + frame.w
    below_corner = frame.h - CORNER  # the side handles stay under the corner square
    h = max(16.0, min(40.0, below_corner - PAD_Y))
    y = frame.y + max(0.0, (below_corner - h) / 2)
    return {
        "grip": (frame.x + HANDLE_INSET, frame.y + (frame.h - h) / 2, HANDLE_W, h),
        "sizer": (right - HANDLE_INSET - HANDLE_W, y, HANDLE_W, h),
        "top_handle": (frame.x + (frame.w - TOP_HANDLE_W) / 2, top - HANDLE_W, TOP_HANDLE_W, HANDLE_W),
        "corner": (right - CORNER, top - CORNER, CORNER, CORNER),
    }


class MacPanel:
    """``app.Panel`` for macOS."""

    def __init__(self, *, on_moved: Callable[[Rect], None] = lambda r: None,
                 on_width: Callable[[float], None] = lambda w: None,
                 on_reset: Callable[[], None] = lambda: None,
                 on_hide: Callable[[], None] = lambda: None,
                 on_scroll: Callable[[int], None] = lambda rows: None,
                 on_live: Callable[[], None] = lambda: None,
                 on_resized: Callable[[Rect, bool], None] = lambda r, rows: None,
                 on_reset_size: Callable[[], None] = lambda: None,
                 appearance: Optional[str] = None, motion: Optional[bool] = None, halo: bool = True) -> None:
        self.on_moved, self.on_width, self.on_reset, self.on_hide = on_moved, on_width, on_reset, on_hide
        self.on_scroll, self.on_live = on_scroll, on_live
        self.on_resized, self.on_reset_size = on_resized, on_reset_size
        self._held: Optional[str] = None  # the handle the mouse holds, if any: no refit moves the strip meanwhile
        self._drag_frame: Optional[Rect] = None  # the strip as last dragged, read on release
        self.strings = Strings()
        self._wheel = 0.0  # scroll distance not yet a whole row
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
        self.sizer, self.sizer_view = self._handle("right")
        self.top_handle, self.top_view = self._handle("top")
        self.corner, self.corner_view = self._handle("corner")
        self.history_tab, self.history_view = self._tab("history")
        self.live_tab, self.live_view = self._tab("live")
        self._menu_target = HermeneusOverlayMenuTarget.alloc().initWithOwner_(self)
        self._fade: Optional[NSImageView] = None
        if appearance is not None:  # renders for review pick an appearance; normal runs follow the system
            from AppKit import NSAppearance
            look = NSAppearance.appearanceNamed_(appearance)
            for window in (self.strip, self.grip, self.sizer, self.top_handle, self.corner, self.history_tab,
                           self.live_tab):
                window.setAppearance_(look)

    def _handle(self, role: str):
        panel = _make_panel(NSMakeRect(0, 0, HANDLE_W, 30), mouse=True)
        panel.setHasShadow_(False)
        view = _view(HermeneusOverlayHandleView, NSMakeRect(0, 0, HANDLE_W, 30), self, role=role, start=None)
        # Enter and exit events even while another app is active, for the cursor. The view only reads its own
        # pointer events, so this needs no permission.
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            NSMakeRect(0, 0, 0, 0), NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways | NSTrackingInVisibleRect,
            view, None)
        view.addTrackingArea_(area)
        panel.setContentView_(view)
        return panel, view

    def _tab(self, role: str):
        panel = _make_panel(NSMakeRect(0, 0, TAB_H, TAB_H), mouse=True)
        panel.setHasShadow_(False)
        view = _view(HermeneusOverlayTabView, NSMakeRect(0, 0, TAB_H, TAB_H), self, role=role)
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
        p = theme.palette(system_is_dark(self.strip_view), self.sent, self.settings.style)
        return theme.see_through(p, self.settings.opacity)

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
        stack = self.palette().font
        key = (kind, settings.text_size, stack)
        font = self._fonts.get(key)
        if font is None:
            size = settings.text_size
            if kind in (LABEL, MARK):
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
        if run.kind == LABEL and text.strip():
            # Drawn as thin space, number, thin space and two spaces (``_run_text``).
            return NSAttributedString.alloc().initWithString_attributes_(
                "\u2009" + text.strip() + "\u2009  ", attrs).size().width
        return width + (6.0 if run.kind == MARK and text.strip() else 0.0)

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
        if self._held is not None:  # the user holds a handle: the drag decides the frame until release
            return
        target = NSMakeRect(frame.x, frame.y, frame.w, frame.h)
        current = self.strip.frame()
        moving_only = abs(current.size.height - frame.h) < 0.5 and abs(current.size.width - frame.w) < 0.5
        # A height change animates (AppKit's resize animation, about 0.2 s) unless motion is reduced.
        animate = bool(self.strip.isVisible() and self.motion() and not moving_only)
        self.strip.setFrame_display_animate_(target, True, animate)
        self._place_handles(frame)

    def _place_handles(self, frame: Optional[Rect] = None) -> None:
        if frame is None:
            f = self.strip.frame()
            frame = Rect(f.origin.x, f.origin.y, f.size.width, f.size.height)
        for handle, rect in handle_frames(frame).items():
            getattr(self, handle).setFrame_display_(NSMakeRect(*rect), True)
        top = frame.y + frame.h + TAB_GAP
        self.history_tab.setFrame_display_(NSMakeRect(frame.x + frame.w - 8 - 30, top, 30, TAB_H), True)
        live_w = self._tab_text_width(self.strings.back_to_live) + 24
        self.live_tab.setFrame_display_(NSMakeRect(frame.x + (frame.w - live_w) / 2, top, live_w, TAB_H), True)

    def _tab_text_width(self, text: str) -> float:
        attrs = {NSFontAttributeName: self._font_for(LABEL, self.settings)}
        return NSAttributedString.alloc().initWithString_attributes_(text, attrs).size().width

    def set_strings(self, strings: Strings) -> None:
        self.strings = strings
        self._place_handles()
        self.live_view.setNeedsDisplay_(True)

    def render(self, view: View, settings: OverlaySettings) -> None:
        # A roll or a new line cross-fades; text added at the end of the bottom row does too, briefly.
        if self.strip.isVisible() and self.motion() and not self.view.empty and view.lines != self.view.lines:
            self._cross_fade()
        self.view, self.settings = view, settings
        self.strip_view.setNeedsDisplay_(True)
        self.text_view.setNeedsDisplay_(True)
        for handle in (self.grip_view, self.sizer_view, self.top_view, self.corner_view, self.history_view,
                       self.live_view):
            handle.setNeedsDisplay_(True)
        self.strip.invalidateShadow()
        # The strip takes the mouse only while the history is open (scroll through it, click for live).
        self.strip.setIgnoresMouseEvents_(not view.history)
        self._sync_tabs(self.strip.isVisible())

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
        if not visible:
            self._held, self._drag_frame = None, None  # a hidden handle gets no mouse up
        handles = (self.grip, self.sizer, self.top_handle, self.corner)
        if visible:
            self.strip.orderFrontRegardless()  # never makeKeyAndOrderFront: no focus change
            for handle in handles:
                self._show_child(handle, True)
        else:
            for handle in handles:
                self._show_child(handle, False)
            self.strip.orderOut_(None)
        self._sync_tabs(visible)

    def _show_child(self, child, show: bool) -> None:
        if show:
            if child.parentWindow() is None:
                self.strip.addChildWindow_ordered_(child, 1)  # NSWindowAbove
            child.orderFrontRegardless()
        else:
            if child.parentWindow() is not None:
                self.strip.removeChildWindow_(child)
            child.orderOut_(None)

    def _sync_tabs(self, visible: bool) -> None:
        """The history tab while there is something to look back at; "back to live" while looking back."""
        self._show_child(self.history_tab, visible and (self.view.older or self.view.history))
        self._show_child(self.live_tab, visible and self.view.history)
        # The history makes the strip taller for a while; its height is not the user's to drag then.
        for handle in (self.top_handle, self.corner):
            self._show_child(handle, visible and not self.view.history)

    def _scrolled(self, event) -> None:
        """Wheel or trackpad: whole rows back (up) or forward (down), as a text view scrolls."""
        delta = float(event.scrollingDeltaY())
        if not event.hasPreciseScrollingDeltas():
            delta *= self._line_h(self._font_for(PENDING, self.settings)) / 3.0  # a wheel notch is about a third of a row
        self._wheel += delta
        row = self._line_h(self._font_for(PENDING, self.settings))
        rows = int(self._wheel / row)
        if rows:
            self._wheel -= rows * row
            self.on_scroll(rows)

    def close(self) -> None:
        self.set_visible(False)

    # -- drawing ----------------------------------------------------------------------

    def _appearance_changed(self) -> None:
        for name in ("strip_view", "text_view", "grip_view", "sizer_view", "top_view", "corner_view", "history_view",
                     "live_view"):
            view = getattr(self, name, None)
            if view is not None:
                view.setNeedsDisplay_(True)

    def _draw_surface(self, view) -> None:
        p = self.palette()
        bounds = view.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1), STRIP_RADIUS_PX, STRIP_RADIUS_PX)
        _color(p.surface.with_alpha(p.surface.a * self.settings.background_alpha)).setFill()
        path.fill()
        if self.settings.opacity > 0 and p.glow is not None:
            # The banner's soft blue edge: a wider translucent stroke just inside the hairline. No blur.
            inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(2.0, 2.0, bounds.size.width - 4, bounds.size.height - 4), STRIP_RADIUS_PX - 1.5,
                STRIP_RADIUS_PX - 1.5)
            _color(p.glow.with_alpha(p.glow.a * self.settings.background_alpha)).setStroke()
            inner.setLineWidth_(3.0)
            inner.stroke()
        if self.settings.opacity > 0:
            _color(p.stroke).setStroke()
            path.setLineWidth_(1.0)
            path.stroke()

    def _shadow(self, p: theme.Palette, size: float):
        """A soft halo in the surface colour, so text stays readable over any video at any opacity."""
        edge = theme.text_edge(self.settings.opacity)
        shadow = NSShadow.alloc().init()
        shadow.setShadowColor_(_color(p.surface.with_alpha(edge.halo_alpha if self._halo else 0.0)))
        shadow.setShadowBlurRadius_(max(2.0, size * edge.halo_blur))
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
        bottom_w = 0.0
        for index, line in enumerate(reversed(lines)):
            text = NSMutableAttributedString.alloc().init()
            for run in line:
                text.appendAttributedString_(self._run_text(run, p, s, shadow))
            point = (PAD_X, y + (line_h - (main.ascender() - main.descender())) / 2 - main.descender())
            if line and line[0].kind == LABEL:
                self._draw_label_pill(line[0], p, s, point)
            self._draw_legible(text, p, point)
            if index == 0:
                bottom_w = text.size().width if line else 0.0
            y += line_h
        if self.view.pending and not self.view.history:
            self._draw_pending(view, p, main, bottom_w, bool(lines))
        if self.view.original:
            font = self._font_for("original", s)
            ink = theme.ink(p.text_secondary, p.surface, theme.text_edge(s.opacity))
            attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(ink),
                     NSShadowAttributeName: self._shadow(p, font.pointSize())}
            text = NSAttributedString.alloc().initWithString_attributes_(self.view.original, attrs)
            self._draw_legible(text, p, (PAD_X, y + ORIGINAL_GAP - font.descender()))

    def _draw_label_pill(self, run: Run, p: theme.Palette, s: OverlaySettings, point) -> None:
        """The compact speaker label: the speaker's number in a thin pill of the speaker's colour."""
        font = self._font_for(LABEL, s)
        width = NSAttributedString.alloc().initWithString_attributes_("\u2009" + run.text + "\u2009",
                                                                     {NSFontAttributeName: font}).size().width
        baseline = point[1] + (-self._font_for(PENDING, s).descender())
        h = font.capHeight() + 2 * LABEL_PAD
        rect = NSMakeRect(point[0] - LABEL_PAD / 2, baseline - LABEL_PAD - 0.5, width + LABEL_PAD, h)
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, h / 2, h / 2)
        color = p.speaker(run.speaker or 1)
        _color(color.with_alpha(0.18)).setFill()
        pill.fill()
        _color(color).setStroke()
        pill.setLineWidth_(1.2)
        pill.stroke()

    def _draw_pending(self, view, p: theme.Palette, main, bottom_w: float, has_rows: bool) -> None:
        """Three small dots after the bottom row: speech was heard and its translation is on its way."""
        line_h = self._line_h(main)
        x = PAD_X + bottom_w + DOT_R * 4 if has_rows else PAD_X
        x = min(x, view.bounds().size.width - PAD_X - DOT_R * 10)
        cy = PAD_Y + line_h / 2 - main.descender() * 0.0 - line_h * 0.12
        _color(theme.ink(p.text_tertiary, p.surface, theme.text_edge(self.settings.opacity))).setFill()
        for i in range(3):
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(x + i * DOT_R * 3.2, cy - DOT_R, DOT_R * 2, DOT_R * 2)).fill()

    def _draw_tab(self, view) -> None:
        """A small rounded tab in the strip's colours: an upward chevron (history) or "back to live"."""
        p = self.palette()
        bounds = view.bounds()
        rect = NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, rect.size.height / 2, rect.size.height / 2)
        _color(p.surface.with_alpha(max(0.9, self.settings.background_alpha))).setFill()
        path.fill()
        _color(p.stroke).setStroke()
        path.setLineWidth_(1.0)
        path.stroke()
        w, h = bounds.size.width, bounds.size.height
        if view.role == "history":
            chevron = NSBezierPath.bezierPath()
            chevron.moveToPoint_((w / 2 - 5, h / 2 - 2.5))
            chevron.lineToPoint_((w / 2, h / 2 + 2.5))
            chevron.lineToPoint_((w / 2 + 5, h / 2 - 2.5))
            chevron.setLineWidth_(1.6)
            chevron.setLineCapStyle_(1)
            chevron.setLineJoinStyle_(1)
            _color(p.text_secondary).setStroke()
            chevron.stroke()
            return
        font = self._font_for(LABEL, self.settings)
        text = NSAttributedString.alloc().initWithString_attributes_(
            self.strings.back_to_live, {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(p.text)})
        size = text.size()
        text.drawAtPoint_(((w - size.width) / 2, (h - size.height) / 2))

    def _draw_legible(self, text, p: theme.Palette, point) -> None:
        """Text over video. Under 50% background a thin outline in the surface colour goes strictly behind
        the glyphs: the fill is opaque (``theme.ink``) and drawn on top, so only the outer half of the stroke
        shows and letters keep their shape and spacing. The outline pass casts no shadow; the soft halo is
        drawn with the fill only."""
        edge = theme.text_edge(self.settings.opacity)
        if self._halo and edge.outline_percent > 0:
            outline = NSMutableAttributedString.alloc().initWithAttributedString_(text)
            whole = (0, outline.length())
            outline.addAttribute_value_range_(NSStrokeColorAttributeName, _color(p.surface.with_alpha(1.0)), whole)
            outline.addAttribute_value_range_(NSStrokeWidthAttributeName, edge.outline_percent, whole)
            outline.removeAttribute_range_(NSShadowAttributeName, whole)
            outline.drawAtPoint_(point)
        text.drawAtPoint_(point)

    def _run_text(self, run: Run, p: theme.Palette, s: OverlaySettings, shadow):
        font = self._font_for(run.kind, s)
        if run.kind == LABEL:
            color = p.speaker(run.speaker or 1)
            text = "\u2009" + run.text + "\u2009  "  # thin spaces inside the pill, then the gap to the text
        elif run.kind == MARK:
            color = p.failed_color  # --ui-red, the colour the pane gives a failed line's caption
            text = run.text + "  "
        else:
            # A line still being spoken and an untranslated line use secondary text (74%), as on the page.
            color = {PENDING: p.text_secondary, FAILED: p.text_secondary, "notice": p.text_tertiary}.get(run.kind,
                                                                                                         p.text)
            text = run.text
        color = theme.ink(color, p.surface, theme.text_edge(s.opacity))
        return NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(color),
                   NSShadowAttributeName: shadow})

    def _draw_handle(self, view) -> None:
        p = self.palette()
        bounds = view.bounds()
        w, h = bounds.size.width, bounds.size.height
        if view.role == "corner":
            # Two short diagonal strokes in the top right corner, the usual sign of a resizable corner.
            _color(p.text_tertiary).setStroke()
            for reach in (7.0, 11.0):
                line = NSBezierPath.bezierPath()
                line.moveToPoint_((w - reach, h - 3.0))
                line.lineToPoint_((w - 3.0, h - reach))
                line.setLineWidth_(1.3)
                line.setLineCapStyle_(1)
                line.stroke()
            return
        _color(p.text_tertiary).setFill()
        cx, cy = w / 2, h / 2
        if view.role == "top":
            dots = [(cx + i * 6, cy) for i in (-1, 0, 1)]
        else:
            columns = (-1, 1) if view.role == "move" else (0,)
            dots = [(cx + col * 2.5, cy + (row - 1) * 6) for row in range(3) for col in columns]
        for x, y in dots:
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - 1.25, y - 1.25, 2.5, 2.5)).fill()

    # -- interaction --------------------------------------------------------------------

    def _drag_began(self, role: str) -> None:
        self._held, self._drag_frame = role, None

    def _dragged(self, role: str, start, dx: float, dy: float) -> None:
        self._held = role
        if role == "move":
            self.strip.setFrameOrigin_((start.origin.x + dx, start.origin.y + dy))
            self._drag_frame = Rect(start.origin.x + dx, start.origin.y + dy, start.size.width, start.size.height)
            return
        # The height follows the mouse while dragging; it snaps to whole rows on release (``OverlayApp.resized``).
        screen = self.strip.screen() or NSScreen.mainScreen()
        v = screen.visibleFrame()
        original = bool(self.view.original)
        frame = resize_frame(Rect(start.origin.x, start.origin.y, start.size.width, start.size.height), role, dx, dy,
                             Rect(v.origin.x, v.origin.y, v.size.width, v.size.height),
                             self.strip_height(self.settings, MAX_LINES_MIN, original),
                             self.strip_height(self.settings, MAX_LINES_MAX, original))
        self.strip.setFrame_display_(NSMakeRect(frame.x, frame.y, frame.w, frame.h), True)
        self._place_handles(frame)
        self._drag_frame = frame

    def _drag_ended(self, role: str = "move") -> None:
        """The mouse let go: snap from the frame the user last dragged to, whatever arrived meanwhile."""
        rect = self._drag_frame
        if rect is None:
            f = self.strip.frame()
            rect = Rect(f.origin.x, f.origin.y, f.size.width, f.size.height)
        self._held, self._drag_frame = None, None
        if role == "move":
            self.on_moved(rect)
        else:
            self.on_resized(rect, role != "right")

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
