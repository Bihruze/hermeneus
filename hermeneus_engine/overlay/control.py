"""Open, close and find the overlay process. This side runs in the plugin's process.

The overlay is a separate Python process (``python -m hermeneus_engine.overlay``)
with its own NSApplication on its own main thread, because AppKit windows
belong to a process's main thread and the Hermes process's main thread is not
the plugin's. The process holds ``<data dir>/overlay/lock`` while it runs, so
there is at most one, and it ends by itself when the session stops.

Nothing here waits for a person. ``show`` waits at most ``READY_TIMEOUT_S``
for the window to report that it is on screen and otherwise stops the child
and raises (catalog rule 12).
"""

from __future__ import annotations

import ctypes
import errno
import importlib.util
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..overlay_settings import OverlaySettings

OVERLAY_DIR = "overlay"
READY_TIMEOUT_S = 6.0
HIDE_WAIT_S = 2.0
READY_LINE = b"ready\n"

# Where a slash command can open the overlay: surfaces whose user sits at this
# Mac. A message from a chat platform (Telegram, Discord, ...) may come from
# anywhere, so it is refused instead of opening a window nobody watches.
LOCAL_PLATFORMS = frozenset({"", "cli", "tui", "desktop", "local"})


class OverlayUnavailable(RuntimeError):
    """The overlay cannot open here; the message says why and what to do."""


def overlay_dir(data_dir: Path) -> Path:
    path = Path(data_dir) / OVERLAY_DIR
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def lock_file(data_dir: Path) -> Path:
    return Path(data_dir) / OVERLAY_DIR / "lock"


def settings_file(data_dir: Path) -> Path:
    return Path(data_dir) / OVERLAY_DIR / "settings.json"


def log_file(data_dir: Path) -> Path:
    return Path(data_dir) / OVERLAY_DIR / "overlay.log"


def positions_file(data_dir: Path) -> Path:
    return Path(data_dir) / OVERLAY_DIR / "positions.json"


def theme_file(data_dir: Path) -> Path:
    """The token values the Desktop pane last sent (``overlay_tokens``); read by both overlays."""
    return Path(data_dir) / OVERLAY_DIR / "theme.json"


def write_theme(data_dir: Path, palette) -> None:
    from ..overlay_tokens import palette_to_json
    overlay_dir(data_dir)
    path = theme_file(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(palette_to_json(palette), encoding="utf-8")
    os.replace(tmp, path)


# -- environment checks -------------------------------------------------------------

def _gui_session() -> bool:
    """True when this user has a window server session (not an SSH-only or launchd daemon context).

    ``CGSessionCopyCurrentDictionary`` answers NULL without a GUI session and
    needs no permission. It is called through ctypes on the system framework,
    so no binary ships with the plugin.
    """
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError:
        return False
    cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ref = cg.CGSessionCopyCurrentDictionary()
    if not ref:
        return False
    cf.CFRelease(ref)
    return True


def check_available(platform: str = sys.platform, *, gui_session=_gui_session,
                    find_spec=importlib.util.find_spec) -> None:
    """Raise OverlayUnavailable when this machine cannot show the native overlay."""
    if platform != "darwin":
        raise OverlayUnavailable("the floating subtitle overlay is built for macOS; on this system use the "
                                 "Desktop pane")
    if find_spec("AppKit") is None:
        raise OverlayUnavailable("PyObjC Cocoa is not installed in Hermes's Python. Reinstall the plugin with "
                                 "its dependencies: hermes plugins install hermeneus")
    if not gui_session():
        raise OverlayUnavailable("there is no desktop session for this user here (SSH or a background "
                                 "service), so no window can open")


def check_platform(platform: Optional[str]) -> None:
    if (platform or "") not in LOCAL_PLATFORMS:
        raise OverlayUnavailable("the overlay opens on this Mac's screen, so it is not opened from a chat "
                                 f"platform ({platform}). Use /translate overlay show in Hermes on the Mac.")


# -- the running overlay ------------------------------------------------------------

def running_pid(data_dir: Path) -> Optional[int]:
    """The overlay's pid while one holds the lock, else None."""
    if sys.platform != "darwin":
        return None  # the overlay runs only on macOS; fcntl does not exist on Windows
    import fcntl
    path = lock_file(data_dir)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                try:
                    return int(json.loads(path.read_text(encoding="utf-8") or "{}").get("pid") or 0) or None
                except (OSError, ValueError):
                    return None
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def write_settings(data_dir: Path, settings: OverlaySettings) -> None:
    """Hand the settings to the overlay. A running overlay applies them within a second."""
    path = settings_file(data_dir)
    overlay_dir(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(settings.to_json(), encoding="utf-8")
    os.replace(tmp, path)


def plugin_root() -> Path:
    """The directory that holds ``hermeneus_engine``."""
    return Path(__file__).resolve().parents[2]


def child_command(data_dir: Path) -> list[str]:
    return [sys.executable, "-m", "hermeneus_engine.overlay", "--data-dir", str(data_dir)]


def child_env(parent: Optional[dict] = None, path: Optional[list] = None) -> dict:
    """The overlay process's environment: the capture worker's allowlist (PATH, HOME, TMPDIR and the
    locale), so no API key, token or secret of the Hermes process reaches it, plus PYTHONPATH with
    the parent's ``sys.path`` so PyObjC imports where Hermes put it."""
    from ..capture.process_tap import worker_env
    return worker_env(parent, path)


def status(data_dir: Path) -> dict:
    pid = running_pid(data_dir)
    return {"visible": pid is not None, "pid": pid}


def show(data_dir: Path, settings: OverlaySettings, *, session_running: bool,
         popen=subprocess.Popen, timeout_s: float = READY_TIMEOUT_S, **checks) -> dict:
    """Open the overlay for the running session (or update the open one). Returns ``status``."""
    if not session_running:
        raise OverlayUnavailable("no translation session is running. Start one first; with the overlay "
                                 "setting on, the overlay opens with every session")
    check_available(**checks)
    write_settings(data_dir, settings)
    if running_pid(data_dir) is not None:
        return {**status(data_dir), "message": "the overlay is open"}
    log = open(log_file(data_dir), "wb")
    try:
        child = popen(child_command(data_dir), cwd=str(plugin_root()), env=child_env(), stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=log, start_new_session=True, close_fds=True)
    finally:
        log.close()
    try:
        _wait_ready(child, timeout_s)
    finally:
        if child.stdout is not None:
            child.stdout.close()
    return {**status(data_dir), "message": "the overlay is open"}


def _wait_ready(child, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    got = b""
    fd = child.stdout.fileno()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(fd, 64)
        if not chunk:
            break
        got += chunk
        if READY_LINE in got:
            return
    code = child.poll()
    if code is None:
        child.terminate()
        try:
            child.wait(1.0)
        except subprocess.TimeoutExpired:
            child.kill()
        raise OverlayUnavailable(f"the overlay did not open within {timeout_s:.0f} s; see the overlay log")
    if code == 0:
        raise OverlayUnavailable("the overlay closed at once: no session was answering")
    raise OverlayUnavailable(f"the overlay could not open (exit {code}); see the overlay log")


def hide(data_dir: Path, *, wait_s: float = HIDE_WAIT_S, kill=os.kill) -> dict:
    pid = running_pid(data_dir)
    if pid is None:
        return {**status(data_dir), "message": "the overlay is not open"}
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and running_pid(data_dir) is not None:
        time.sleep(0.05)
    return {**status(data_dir), "message": "the overlay is closed"}
