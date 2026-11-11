"""The overlay process: ``python -m hermeneus_engine.overlay --data-dir <plugin data dir>``.

Started by ``overlay.control.show``. It takes the overlay lock, opens the
window, prints ``ready`` on stdout, then follows the session's Unix socket on
a background thread and draws on the main thread. It exits when the session
stops, on SIGTERM (``hermes hermeneus overlay hide``) or from the grip's
"Hide overlay" menu item.

Exit codes: 0 the session ended or the overlay was hidden, 3 no display,
4 another overlay already runs.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import objc
from Foundation import NSObject

from ..ipc import SessionClient
from ..overlay_settings import OverlaySettings
from ..runtime import lock_holder, socket_path
from . import control, theme
from .app import OverlayApp
from .follower import follow
from .model import Strings
from .placement import Positions

TICK_S = 0.1  # the display queue puts a line up within this of its time


def _take_lock(data_dir: Path):
    control.overlay_dir(data_dir)
    path = control.lock_file(data_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps({"pid": os.getpid()}).encode("utf-8"))
    return fd


def _read_settings(data_dir: Path) -> tuple[OverlaySettings, float]:
    path = control.settings_file(data_dir)
    try:
        return OverlaySettings.from_json(path.read_text(encoding="utf-8")), path.stat().st_mtime
    except OSError:
        return OverlaySettings(), 0.0


def _apply_theme(data_dir: Path, panel, overlay, state: dict) -> None:
    """Repaint with the token values the Desktop pane sent, when ``theme.json`` changed."""
    path = control.theme_file(data_dir)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime == state["theme_mtime"]:
        return
    state["theme_mtime"] = mtime
    sent = theme.load(path) if mtime else None
    panel.set_palette(sent)
    overlay.strings = Strings(**dict(sent.strings)) if sent is not None else Strings()
    panel.set_strings(overlay.strings)
    overlay.repaint()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermeneus_engine.overlay")
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    lock_fd = _take_lock(data_dir)
    if lock_fd is None:
        print("hermeneus overlay: another overlay is open", file=sys.stderr)
        return 4
    if not lock_holder(data_dir):
        print("hermeneus overlay: no session is running", file=sys.stderr)
        return 0

    from AppKit import NSScreen, NSTimer
    from Foundation import NSNotificationCenter
    from PyObjCTools import AppHelper

    from . import panel_mac

    nsapp = panel_mac.start_application()
    if not NSScreen.screens():
        print("hermeneus overlay: no display", file=sys.stderr)
        return 3

    settings, settings_mtime = _read_settings(data_dir)
    state = {"mtime": settings_mtime, "quit": False, "theme_mtime": None}
    stop_event_holder: dict = {}

    def quit_soon(*_args) -> None:
        state["quit"] = True

    panel = panel_mac.MacPanel(on_moved=lambda frame: overlay.moved(frame),
                               on_width=lambda w: overlay.set_width(w),
                               on_reset=lambda: overlay.reset_position(),
                               on_hide=quit_soon,
                               on_scroll=lambda rows: overlay.scroll(rows, time.monotonic()),
                               on_live=lambda: overlay.back_to_live(time.monotonic()),
                               on_resized=lambda frame, rows: overlay.resized(frame, rows),
                               on_reset_size=lambda: overlay.reset_size())
    overlay = OverlayApp(panel, Positions(control.positions_file(data_dir)), settings)
    _apply_theme(data_dir, panel, overlay, state)
    overlay.place()
    overlay.refresh(time.monotonic())

    signal.signal(signal.SIGTERM, quit_soon)
    signal.signal(signal.SIGINT, quit_soon)

    def on_event(event: dict) -> None:
        AppHelper.callAfter(overlay.on_event, event, time.monotonic())

    def run_follower() -> None:
        async def go() -> str:
            stop = asyncio.Event()
            stop_event_holder["loop"] = asyncio.get_running_loop()
            stop_event_holder["stop"] = stop
            client = SessionClient(socket_path(data_dir))
            return await follow(client.subscribe, on_event, lambda: lock_holder(data_dir) is not None, stop=stop)
        try:
            why = asyncio.run(go())
        except Exception as exc:  # the window must close even if the stream code fails
            print(f"hermeneus overlay: event stream failed: {exc!r}", file=sys.stderr)
            why = "failed"
        print(f"hermeneus overlay: closing ({why})", file=sys.stderr)
        state["quit"] = True

    follower = threading.Thread(target=run_follower, name="hermeneus-overlay-events", daemon=True)
    follower.start()

    def tick() -> None:
        if state["quit"]:
            panel.close()
            loop, stop = stop_event_holder.get("loop"), stop_event_holder.get("stop")
            if loop is not None and stop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(stop.set)
                except RuntimeError:  # the follower's loop closed in between
                    pass
            _stop_app(nsapp)
            return
        _apply_theme(data_dir, panel, overlay, state)
        new, mtime = _read_settings(data_dir)
        if mtime != state["mtime"]:
            state["mtime"] = mtime
            overlay.apply_settings(new)
        overlay.refresh(time.monotonic())  # the next queued line goes up, or the strip clears after its hold

    timer_target = HermeneusOverlayTicker.alloc().initWithCallback_(tick)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(TICK_S, timer_target, "tick:", None, True)
    screens = HermeneusOverlayTicker.alloc().initWithCallback_(overlay.screens_changed)
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        screens, "tick:", "NSApplicationDidChangeScreenParametersNotification", None)

    sys.stdout.write("ready\n")
    sys.stdout.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)

    # NSApp.run directly: AppHelper.runEventLoop answers an exception with an alert
    # panel, which would take focus. An exception in a callback is logged instead.
    nsapp.run()
    return 0


def _stop_app(nsapp) -> None:
    """Leave NSApp.run. ``stop:`` takes effect after the next event, so post one."""
    from AppKit import NSEvent

    nsapp.stop_(None)
    wake = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        15, (0, 0), 0, 0, 0, None, 0, 0, 0)  # NSEventTypeApplicationDefined
    nsapp.postEvent_atStart_(wake, True)


class HermeneusOverlayTicker(NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(HermeneusOverlayTicker, self).init()
        if self is None:
            return None
        self.callback = callback
        return self

    def tick_(self, _arg):
        self.callback()


if __name__ == "__main__":
    sys.exit(main())
