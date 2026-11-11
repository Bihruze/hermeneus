"""Session ownership: the machine-wide lock, the Unix socket and the owner thread.

One translation session runs per Hermes home at a time. The process that
starts it holds an exclusive ``flock`` on ``<run dir>/lock`` for the
session's lifetime and serves the session's bus on ``<run dir>/sock``. The
kernel releases a ``flock`` when its holder dies, so a crashed owner never
leaves a lock that blocks the next start; the PID written into the file is
only for messages.

``SessionRuntime`` is the async core (used directly by ``hermes hermeneus
run`` on its own ``asyncio.run`` loop). ``SessionHost`` runs one runtime on a
dedicated thread with its own loop, for owners inside Hermes processes whose
command handlers return before the session ends.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import fcntl
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .bus import SubtitleBus
from .ipc import BusServer
from .session import SessionComponents, SessionOptions, TranslationSession

logger = logging.getLogger(__name__)

RUN_DIR_NAME = "run"
LOCK_NAME = "lock"
SOCKET_NAME = "sock"

# sockaddr_un.sun_path is 104 bytes on macOS (sys/un.h), including the NUL.
MAX_SOCKET_PATH_BYTES = 103

START_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 2.0


class SessionBusy(RuntimeError):
    """Another session already runs on this machine."""

    def __init__(self, info: dict) -> None:
        self.info = info
        pid = info.get("pid")
        where = f" (process {pid})" if pid else ""
        super().__init__(f"a translation session is already running{where}. "
                         "Stop it with /translate stop or hermes hermeneus run, then start again.")


def run_dir(data_dir: Path) -> Path:
    """``<data dir>/run``, created with mode 0700."""
    path = Path(data_dir) / RUN_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def socket_path(data_dir: Path) -> Path:
    return Path(data_dir) / RUN_DIR_NAME / SOCKET_NAME


def lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / RUN_DIR_NAME / LOCK_NAME


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionLock:
    """Exclusive, crash-safe session lock."""

    def __init__(self, data_dir: Path) -> None:
        self.path = lock_path(data_dir)
        self._fd: Optional[int] = None

    def acquire(self, info: dict) -> None:
        run_dir(self.path.parent.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise SessionBusy(read_lock_info(self.path)) from None
            raise
        # A previous owner that died left its content and maybe its socket.
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(info).encode("utf-8"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.ftruncate(fd, 0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None


def read_lock_info(path: Path) -> dict:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        info = json.loads(raw) if raw.strip() else {}
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_holder(data_dir: Path) -> Optional[dict]:
    """Lock info when a live process holds the session lock, else None. Never takes the lock for long."""
    path = lock_path(data_dir)
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                info = read_lock_info(path)
                info["pid_alive"] = pid_alive(int(info.get("pid") or 0))
                return info
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def check_socket_path(path: Path) -> None:
    if len(os.fsencode(str(path))) > MAX_SOCKET_PATH_BYTES:
        raise RuntimeError(
            f"the session socket path {path} is longer than the {MAX_SOCKET_PATH_BYTES} bytes macOS allows. "
            "Use a shorter HERMES_HOME.")


def remove_stale_socket(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(mode):
        os.unlink(path)
    else:
        raise RuntimeError(f"{path} exists and is not a socket; remove it by hand")


# Builds the components. The second argument is where a capture backend
# reports, from its own thread, that capture stopped by itself.
ComponentBuilder = Callable[[SessionOptions, Callable[[BaseException], None]], SessionComponents]


class SessionRuntime:
    """Lock, socket server and session for one run, on the current loop."""

    def __init__(self, data_dir: Path, options: SessionOptions, build: ComponentBuilder, *,
                 owner: str, bus: Optional[SubtitleBus] = None, overlay_page=None) -> None:
        self.data_dir = Path(data_dir)
        self.options = options
        self._build = build
        self.owner = owner
        # An overlay_page.OverlayPage, or None. It serves while the session runs.
        self.overlay_page = overlay_page
        self.bus = bus or SubtitleBus(max_lines=options.max_lines)
        self.session: Optional[TranslationSession] = None
        self._lock = SessionLock(self.data_dir)
        self._server: Optional[BusServer] = None
        self._stop_requested: Optional[asyncio.Event] = None

    async def start(self) -> None:
        """Take the lock, serve the socket, start the session. Raises SessionBusy or the start error."""
        sock = socket_path(self.data_dir)
        check_socket_path(sock)
        self._stop_requested = asyncio.Event()
        self._lock.acquire({"pid": os.getpid(), "owner": self.owner, "started_at": time.time(),
                            "socket": str(sock)})
        try:
            remove_stale_socket(sock)
            self._server = BusServer(sock, self.bus, on_stop=self.request_stop, on_overlay=self._overlay_request)
            await self._server.start()
            loop = asyncio.get_running_loop()

            def on_capture_error(error: BaseException) -> None:
                session = self.session
                if session is not None:
                    try:
                        loop.call_soon_threadsafe(session.capture_failed, error)
                    except RuntimeError:
                        pass  # the loop already closed

            components = self._build(self.options, on_capture_error)
            self.session = TranslationSession(self.options, components, self.bus)
            await self.session.start()
        except BaseException:
            await self._close_server()
            self._lock.release()
            raise
        if self.overlay_page is not None:
            # After the session is live, on a worker thread: the page never delays the audio path.
            await asyncio.to_thread(self.overlay_page.auto_start)

    async def _overlay_request(self, on: Optional[bool], port: Optional[int]) -> dict:
        if self.overlay_page is None:
            return {"type": "overlay", "running": False, "url": None, "port": None,
                    "error": "this session was started without the overlay page"}
        return await asyncio.to_thread(self.overlay_page.switch, on, port)

    def request_stop(self) -> None:
        """Ask the run to stop. Safe from the runtime's loop thread."""
        if self._stop_requested is not None:
            self._stop_requested.set()

    async def wait_stop_requested(self) -> None:
        """Return when a stop was asked for, for example by ``{"op": "stop"}`` on the socket."""
        assert self._stop_requested is not None, "start() was not called"
        await self._stop_requested.wait()

    async def wait(self) -> None:
        """Return when the session ended by itself or a stop was requested, then clean up."""
        assert self.session is not None and self._stop_requested is not None
        stop_wait = asyncio.create_task(self._stop_requested.wait())
        closed_wait = asyncio.create_task(self.session.wait_closed())
        try:
            await asyncio.wait({stop_wait, closed_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
            closed_wait.cancel()
        await self.close()

    async def close(self, message: str = "stopped") -> None:
        if self.session is not None and not self.session.closed:
            await self.session.stop(message)
        if self.overlay_page is not None:
            await asyncio.to_thread(self.overlay_page.stop)
        await self._close_server()
        self.bus.close()
        self._lock.release()

    async def _close_server(self) -> None:
        if self._server is not None:
            server, self._server = self._server, None
            await server.close()


class SessionHost:
    """Runs one SessionRuntime on its own thread and event loop."""

    def __init__(self, data_dir: Path, *, owner: str) -> None:
        self.data_dir = Path(data_dir)
        self.owner = owner
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.runtime: Optional[SessionRuntime] = None
        self._mutex = threading.Lock()

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def bus(self) -> Optional[SubtitleBus]:
        return self.runtime.bus if self.runtime is not None else None

    def start(self, options: SessionOptions, build: ComponentBuilder, *,
              timeout: float = START_TIMEOUT_S, overlay_page=None) -> dict:
        """Start a session on a new thread. Returns its status. Raises SessionBusy or the start error.

        The thread runs in a copy of the caller's context, so profile and
        secret scopes that Hermes binds in context variables stay visible to
        ``get_secret`` calls the session makes later.
        """
        with self._mutex:
            if self.running:
                raise SessionBusy({"pid": os.getpid(), "owner": self.owner})
            started = threading.Event()
            outcome: dict = {}
            runtime = SessionRuntime(self.data_dir, options, build, owner=self.owner, overlay_page=overlay_page)

            async def main() -> None:
                self._loop = asyncio.get_running_loop()
                try:
                    await runtime.start()
                except BaseException as exc:
                    outcome["error"] = exc
                    started.set()
                    return
                started.set()
                await runtime.wait()

            def thread_main() -> None:
                try:
                    asyncio.run(main())
                except BaseException as exc:  # never let the thread die silently
                    logger.exception("hermeneus: session thread ended with an error")
                    outcome.setdefault("error", exc)
                    started.set()
                finally:
                    self._loop = None

            ctx = contextvars.copy_context()
            thread = threading.Thread(target=ctx.run, args=(thread_main,), name="hermeneus-session", daemon=True)
            self.runtime = runtime
            self._thread = thread
            thread.start()
        if not started.wait(timeout):
            self.stop()
            raise TimeoutError(f"the session did not start within {timeout:.0f} seconds")
        if "error" in outcome:
            thread.join(STOP_TIMEOUT_S)
            raise outcome["error"]
        return runtime.bus.status()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Stop the session and wait for the thread. True when it ended within ``timeout``."""
        thread, loop, runtime = self._thread, self._loop, self.runtime
        if thread is None:
            return True
        if loop is not None and runtime is not None:
            try:
                loop.call_soon_threadsafe(runtime.request_stop)
            except RuntimeError:
                pass
        thread.join(timeout)
        return not thread.is_alive()
