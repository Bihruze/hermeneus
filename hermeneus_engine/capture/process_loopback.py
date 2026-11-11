"""Capture backend built on WASAPI process loopback (Windows, build 20348 and later).

The Windows counterpart of ``process_tap``: no driver, and the same three kinds
of source.

* ``system``: every process except the Hermeneus process tree
  (``PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE`` on this process), so
  spoken translation that this process plays never reaches its own capture.
* ``app:<exe>``: one app's process tree, for example ``app:chrome.exe``
  (``PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`` on the oldest
  ``chrome.exe`` whose parent is not ``chrome.exe``). Apps whose tree contains
  Hermeneus are not offered.
* ``pid:<pid>``: one process tree. Not listed.

The stream runs in a worker process (``_loopback_worker``), started with a
minimal environment and stopped through its stdin, then TerminateProcess.
This module never imports comtypes, pycaw or psutil, so the Hermes process
keeps its own COM apartment untouched.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import logging
import os
import sys
import threading
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from .. import subproc
from ..contracts import CaptureError, CaptureSource, FrameSink
from ._loopback_engine import STATUS_NO_AUDIO, STATUS_RECOVERED, STATUS_STALLED
from ._loopback_worker import KIND_LIST, KIND_STATUS
from ._tap_worker import KIND_AUDIO, KIND_ERROR, KIND_READY, KIND_STATS, read_frame

log = logging.getLogger(__name__)

BACKEND_NAME = "process-loopback"
SYSTEM_SOURCE_ID = "system"
SYSTEM_SOURCE_LABEL = "All system audio except Hermeneus"
# AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS, "Minimum supported client: Windows 10 Build 20348".
MIN_BUILD = 20348
REQUIRED_MODULES = ("comtypes", "pycaw", "psutil")
START_TIMEOUT_S = 30.0
LIST_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 5.0

StatusCallback = Callable[[str, str, dict], None]

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# The Windows allowlist and its reasons live in ``subproc.WINDOWS_ENV_ALLOWLIST``.
WORKER_ENV_ALLOWLIST = subproc.WINDOWS_ENV_ALLOWLIST

# CREATE_NO_WINDOW keeps a console window from flashing up under Hermes Desktop;
# CREATE_NEW_PROCESS_GROUP keeps a Ctrl+C in Hermes's console out of the worker,
# which the parent stops itself.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_live: "weakref.WeakSet[ProcessLoopbackBackend]" = weakref.WeakSet()
_atexit_lock = threading.Lock()
_atexit_registered = False


def _stop_all_at_exit() -> None:
    for backend in list(_live):
        try:
            backend.stop()
        except Exception:
            log.exception("process loopback teardown at exit failed")


def _register_atexit() -> None:
    global _atexit_registered
    with _atexit_lock:
        if not _atexit_registered:
            atexit.register(_stop_all_at_exit)
            _atexit_registered = True


def windows_build() -> Optional[int]:
    if sys.platform != "win32":
        return None
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return None


def worker_env(parent: Optional[dict] = None, path: Optional[list] = None) -> dict:
    """Minimal environment for the worker; never carries API keys, tokens or secrets.

    PYTHONPATH carries this package and the parent's ``sys.path`` directories, where Hermes puts
    plugin dependencies at runtime (see ``subproc.python_env``)."""
    return subproc.python_env(dict(os.environ) if parent is None else parent, path, windows=True)


def spawn_worker() -> subproc.Popen:
    flags = (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) if sys.platform == "win32" else 0
    return subproc.popen([sys.executable, "-m", "hermeneus_engine.capture._loopback_worker"],
                         stdin=subproc.PIPE, stdout=subproc.PIPE, stderr=subproc.PIPE,
                         env=worker_env(), creationflags=flags)


class _Run:
    def __init__(self, source: CaptureSource, sink: FrameSink, proc) -> None:
        self.source = source
        self.sink = sink
        self.proc = proc
        self.ready = threading.Event()
        self.error: str = ""
        self.stopping = False
        self.final_stats: Optional[dict] = None
        self.ready_info: dict = {}
        self.sink_errors = 0
        self.stderr_tail: deque = deque(maxlen=20)
        self.reader: Optional[threading.Thread] = None
        self.stderr_reader: Optional[threading.Thread] = None


class ProcessLoopbackBackend:
    name = BACKEND_NAME

    def __init__(self, *, spawn: Callable[[], Any] = spawn_worker, own_pid: Optional[int] = None,
                 build: Optional[Callable[[], Optional[int]]] = None,
                 has_module: Optional[Callable[[str], bool]] = None,
                 start_timeout: float = START_TIMEOUT_S, list_timeout: float = LIST_TIMEOUT_S,
                 on_status: Optional[StatusCallback] = None) -> None:
        self._spawn = spawn
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._build = build or windows_build
        self._has_module = has_module or (lambda name: importlib.util.find_spec(name) is not None)
        self._start_timeout = start_timeout
        self._list_timeout = list_timeout
        self._lock = threading.RLock()
        self._run: Optional[_Run] = None
        self.on_status = on_status
        self.last_stats: Optional[dict] = None
        self.last_status: Optional[tuple[str, str]] = None
        self.status_message: str = ""   # latest non-fatal watchdog note; the session shows it
        self.failure: str = ""          # set when the worker stops on its own during capture

    # ------------------------------------------------------------------ protocol

    def available(self) -> tuple[bool, str]:
        build = self._build()
        if build is None:
            return False, "Process loopback needs Windows; this is not Windows."
        if build < MIN_BUILD:
            return False, (f"Process loopback needs Windows build {MIN_BUILD} or later; this is build {build}. "
                           "Windows 11 has it.")
        missing = [m for m in REQUIRED_MODULES if not self._has_module(m)]
        if missing:
            return False, f"Missing Python packages: {', '.join(missing)}. Reinstall the plugin."
        return True, "WASAPI process loopback is available."

    def list_sources(self) -> list[CaptureSource]:
        sources = [CaptureSource(backend=self.name, source_id=SYSTEM_SOURCE_ID, label=SYSTEM_SOURCE_LABEL)]
        try:
            apps = self._list_apps()
        except CaptureError as exc:
            # The whole-system source does not need the list; offer it alone.
            log.warning("process loopback: listing apps failed: %s", exc)
            apps = []
        for app in sorted(apps, key=lambda a: (str(a.get("label", "")).lower(), str(a.get("exe", "")))):
            exe = str(app.get("exe", ""))
            if not exe:
                continue
            sources.append(CaptureSource(backend=self.name, source_id=f"app:{exe}", label=str(app.get("label") or exe),
                                         detail="playing now" if app.get("playing") else ""))
        return sources

    def start(self, source: CaptureSource, sink: FrameSink) -> None:
        with self._lock:
            if self._run is not None:
                raise CaptureError("Capture is already running. Stop it before starting another source.")
            spec = self._spec_for(source)
            self.failure = ""
            self.status_message = ""
            self.last_status = None
            try:
                proc = self._spawn()
            except OSError as exc:
                raise CaptureError(f"Could not start the capture worker: {exc}") from exc
            run = _Run(source, sink, proc)
            run.reader = threading.Thread(target=self._read, args=(run,), name="hermeneus-loopback-reader",
                                          daemon=True)
            run.stderr_reader = threading.Thread(target=self._read_stderr, args=(run,),
                                                 name="hermeneus-loopback-stderr", daemon=True)
            run.reader.start()
            run.stderr_reader.start()
            try:
                proc.stdin.write((json.dumps(spec) + "\n").encode())
                proc.stdin.flush()
            except OSError:
                pass  # the reader reports why the worker went away
            if not run.ready.wait(self._start_timeout):
                self._finish(run)
                raise CaptureError(f"Capture from {source.label} did not start within {self._start_timeout:.0f} s.")
            if run.error:
                self._finish(run)
                raise CaptureError(run.error)
            self._run = run
            _live.add(self)
            _register_atexit()

    def stop(self) -> None:
        with self._lock:
            run = self._run
            self._run = None
            if run is None:
                return
            self._finish(run)
            _live.discard(self)

    # ------------------------------------------------------------------ sources

    def _spec_for(self, source: CaptureSource) -> dict:
        if source.backend != self.name:
            raise CaptureError(f"{source.label} belongs to the {source.backend} backend, not {self.name}.")
        sid = source.source_id
        if sid == SYSTEM_SOURCE_ID:
            return {"mode": "system", "exclude_pid": self._own_pid}
        if sid.startswith("app:") and len(sid) > 4:
            return {"mode": "app", "exe": sid[4:].lower(), "own_pid": self._own_pid}
        if sid.startswith("pid:"):
            try:
                pid = int(sid[4:])
            except ValueError:
                raise CaptureError(f"{sid} is not a valid process id.") from None
            if pid <= 0:
                raise CaptureError(f"{sid} is not a valid process id.")
            return {"mode": "pid", "pid": pid}
        raise CaptureError(f"Unknown process loopback source {sid}.")

    def _list_apps(self) -> list[dict]:
        """Ask a short-lived worker for the apps with audio sessions. Never prompts."""
        try:
            proc = self._spawn()
        except OSError as exc:
            raise CaptureError(f"Could not start the capture worker: {exc}") from exc
        result: dict = {}

        def read() -> None:
            try:
                frame = read_frame(proc.stdout)
            except (OSError, ValueError):
                frame = None
            if frame is not None:
                result["frame"] = frame

        reader = threading.Thread(target=read, name="hermeneus-loopback-list", daemon=True)
        reader.start()
        try:
            proc.stdin.write((json.dumps({"mode": "list", "own_pid": self._own_pid}) + "\n").encode())
            proc.stdin.flush()
        except OSError:
            pass
        reader.join(self._list_timeout)
        _close_quietly(proc)
        frame = result.get("frame")
        if frame is None:
            raise CaptureError("The capture worker did not list the apps playing audio.")
        kind, _, payload = frame
        if kind == KIND_ERROR:
            raise CaptureError(_message(payload) or "The capture worker could not list apps.")
        if kind != KIND_LIST:
            raise CaptureError("The capture worker sent an unexpected reply.")
        try:
            apps = json.loads(payload).get("apps") or []
        except (ValueError, AttributeError):
            raise CaptureError("The capture worker sent an unreadable app list.") from None
        return [a for a in apps if isinstance(a, dict)]

    # ------------------------------------------------------------------ worker IO

    def _read(self, run: _Run) -> None:
        out = run.proc.stdout
        while True:
            try:
                frame = read_frame(out)
            except (OSError, ValueError):
                frame = None
            if frame is None:
                break
            kind, timestamp, payload = frame
            if kind == KIND_AUDIO:
                try:
                    run.sink(payload, timestamp)
                except Exception:
                    run.sink_errors += 1
                    if run.sink_errors == 1:
                        log.exception("capture sink raised; later errors are counted only")
            elif kind == KIND_READY:
                try:
                    run.ready_info = json.loads(payload)
                except ValueError:
                    run.ready_info = {}
                run.ready.set()
            elif kind == KIND_ERROR:
                run.error = _message(payload) or "The capture worker reported an error."
                run.ready.set()
            elif kind == KIND_STATUS:
                self._handle_status(payload)
            elif kind == KIND_STATS:
                try:
                    run.final_stats = json.loads(payload)
                except ValueError:
                    run.final_stats = None
        if not run.ready.is_set():
            tail = " ".join(run.stderr_tail)
            run.error = "The capture worker exited before capture started." + (f" {tail}" if tail else "")
            run.ready.set()
        elif not run.stopping and not run.error:
            self.failure = f"Capture from {run.source.label} stopped unexpectedly."
            log.error("%s %s", self.failure, " ".join(run.stderr_tail))

    def _handle_status(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
            code, text, details = str(msg["code"]), str(msg.get("message", "")), dict(msg.get("details") or {})
        except (ValueError, KeyError, TypeError):
            return
        self.last_status = (code, text)
        if code in (STATUS_NO_AUDIO, STATUS_STALLED):
            self.status_message = text
        elif code == STATUS_RECOVERED:
            self.status_message = ""
        log.warning("process loopback: %s: %s", code, text)
        if self.on_status is not None:
            try:
                self.on_status(code, text, details)
            except Exception:
                log.exception("status callback failed")

    def _read_stderr(self, run: _Run) -> None:
        err = run.proc.stderr
        if err is None:
            return
        for raw in iter(err.readline, b""):
            text = raw.decode(errors="replace").rstrip()
            if text:
                run.stderr_tail.append(text)
                log.warning("%s", text)

    def _finish(self, run: _Run) -> None:
        """Ask the worker to tear down, wait, then TerminateProcess. Never raises.

        On Windows ``Popen.terminate()`` and ``kill()`` both call TerminateProcess
        (subprocess docs), so the escalation is stdin end of file, then one
        terminate; a second call is harmless.
        """
        run.stopping = True
        proc = run.proc
        try:
            proc.stdin.close()
        except Exception:
            pass
        for escalate in (None, proc.terminate, proc.kill):
            if escalate is not None:
                try:
                    escalate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
                break
            except subproc.TimeoutExpired:
                log.warning("capture worker did not exit; escalating")
        for t in (run.reader, run.stderr_reader):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        stats = run.final_stats or {}
        stats["sink_errors"] = run.sink_errors
        stats["worker_exit_code"] = proc.returncode
        self.last_stats = stats
        if stats.get("teardown_failures"):
            log.error("process loopback teardown reported failures: %s", stats["teardown_failures"])


def _close_quietly(proc) -> None:
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=STOP_TIMEOUT_S)
    except subproc.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_S)
        except Exception:
            pass
    except Exception:
        pass


def _message(payload: bytes) -> str:
    try:
        return str(json.loads(payload).get("message", ""))
    except (ValueError, AttributeError):
        return payload.decode(errors="replace")
