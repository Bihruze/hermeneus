"""Capture backend built on Core Audio process taps (macOS 14.2 and later).

A process tap reads the audio that processes send to the output device, before
the output volume is applied. The tap itself runs in a worker process
(``_tap_worker``) so the realtime IOProc never waits for this interpreter's
GIL. This module lists sources, starts and stops that worker and forwards its
PCM to the engine's sink.

Sources:

* ``system``: every process except Hermeneus (this process and the worker), so
  speech that Hermeneus plays from this process never reaches its own capture.
* ``app:<bundle id>``: one app and its helper processes, for example
  ``com.google.Chrome`` together with ``com.google.Chrome.helper``. Helpers that
  open audio during capture join the tap within about a second.
* ``pid:<pid>``: one process. Not listed; it covers processes without a bundle
  id such as ``afplay``.

The first capture triggers the macOS System Audio Recording prompt. It names
the app responsible for this Python process: the terminal app when Hermes runs
in a terminal, or Hermes Desktop when Desktop started it.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import weakref
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ..contracts import CaptureError, CaptureSource, FrameSink
from ._tap_engine import STATUS_NO_AUDIO, STATUS_RECOVERED, STATUS_STALLED
from ._tap_worker import KIND_AUDIO, KIND_ERROR, KIND_READY, KIND_STATS, KIND_STATUS, read_frame

log = logging.getLogger(__name__)

BACKEND_NAME = "process-tap"
SYSTEM_SOURCE_ID = "system"
SYSTEM_SOURCE_LABEL = "All system audio except Hermeneus"
# AudioHardwareCreateProcessTap is API_AVAILABLE(macos(14.2)) in AudioHardwareTapping.h.
MIN_MACOS = (14, 2)
START_TIMEOUT_S = 30.0
PERMISSION_PANE = "System Settings > Privacy & Security > Screen & System Audio Recording"

# (code, message, details) for watchdog and format events; called on a reader thread.
StatusCallback = Callable[[str, str, dict], None]
STOP_TIMEOUT_S = 5.0

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]

_live: "weakref.WeakSet[ProcessTapBackend]" = weakref.WeakSet()
_atexit_lock = threading.Lock()
_atexit_registered = False


def _stop_all_at_exit() -> None:
    for backend in list(_live):
        try:
            backend.stop()
        except Exception:
            log.exception("process tap teardown at exit failed")


def _register_atexit() -> None:
    global _atexit_registered
    with _atexit_lock:
        if not _atexit_registered:
            atexit.register(_stop_all_at_exit)
            _atexit_registered = True


def macos_version() -> Optional[tuple[int, ...]]:
    if sys.platform != "darwin":
        return None
    release = platform.mac_ver()[0]
    try:
        return tuple(int(p) for p in release.split(".")) if release else None
    except ValueError:
        return None


# The worker gets only these variables from the parent. It holds no secrets and
# needs none: measured, the worker captures with nothing but PYTHONPATH and
# PYTHONUNBUFFERED set; the rest keep paths, temp files and text encoding
# behaving as in the parent.
WORKER_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def worker_env(parent: Optional[dict] = None) -> dict:
    """Minimal environment for the capture worker; never carries API keys, tokens or secrets."""
    parent = os.environ if parent is None else parent
    env = {k: parent[k] for k in WORKER_ENV_ALLOWLIST if k in parent}
    env["PYTHONPATH"] = str(_PACKAGE_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def spawn_worker() -> subprocess.Popen:
    env = worker_env()
    return subprocess.Popen(
        [sys.executable, "-m", "hermeneus_engine.capture._tap_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, close_fds=True)


class _Run:
    def __init__(self, source: CaptureSource, sink: FrameSink, proc) -> None:
        self.source = source
        self.sink = sink
        self.proc = proc
        self.ready = threading.Event()
        self.error: str = ""
        self.stopping = False
        self.final_stats: Optional[dict] = None
        self.sink_errors = 0
        self.stderr_tail: deque = deque(maxlen=20)
        self.reader: Optional[threading.Thread] = None
        self.stderr_reader: Optional[threading.Thread] = None


class ProcessTapBackend:
    name = BACKEND_NAME

    def __init__(self, hal_factory: Optional[Callable[[], object]] = None, *,
                 spawn: Callable[[], object] = spawn_worker, own_pid: Optional[int] = None,
                 start_timeout: float = START_TIMEOUT_S, on_status: Optional[StatusCallback] = None,
                 responsible_app: Optional[Callable[[], Optional[str]]] = None,
                 reader: Optional[str] = None) -> None:
        self._hal_factory = hal_factory
        self._hal = None
        self._spawn = spawn
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._start_timeout = start_timeout
        self._lock = threading.RLock()
        self._run: Optional[_Run] = None
        self.on_status = on_status
        self._reader = reader  # None lets the worker choose (PortAudio when sounddevice is present)
        self._responsible_app = responsible_app or responsible_app_name
        self.last_stats: Optional[dict] = None
        self.last_status: Optional[tuple[str, str]] = None
        self.status_message: str = ""  # latest non-fatal watchdog note; the session shows it
        self.failure: str = ""  # set when the worker stops on its own during capture

    # ------------------------------------------------------------------ helpers

    def _get_hal(self):
        if self._hal is None:
            if self._hal_factory is not None:
                self._hal = self._hal_factory()
            else:
                from ._coreaudio import CoreAudioHAL

                self._hal = CoreAudioHAL()
        return self._hal

    # ------------------------------------------------------------------ protocol

    def available(self) -> tuple[bool, str]:
        if self._hal_factory is None:
            version = macos_version()
            if version is None:
                return False, "Process taps need macOS; this is not a Mac."
            if version < MIN_MACOS:
                shown = ".".join(str(v) for v in version)
                return False, f"Process taps need macOS 14.2 or later; this Mac runs {shown}."
            try:
                import CoreAudio
            except ImportError:
                return False, "PyObjC is missing. Install pyobjc-framework-CoreAudio."
            if not (hasattr(CoreAudio, "AudioHardwareCreateProcessTap") and hasattr(CoreAudio, "CATapDescription")):
                return False, ("The installed PyObjC does not bridge Core Audio taps. "
                               "Install pyobjc-framework-CoreAudio 13 or later.")
        try:
            self._get_hal()
        except Exception as exc:
            return False, f"Core Audio could not be loaded: {exc}"
        return True, "Core Audio process taps are available."

    def list_sources(self) -> list[CaptureSource]:
        sources = [CaptureSource(backend=self.name, source_id=SYSTEM_SOURCE_ID, label=SYSTEM_SOURCE_LABEL)]
        apps = self._app_groups()
        for bundle_id in sorted(apps, key=lambda b: (apps[b]["name"].lower(), b)):
            group = apps[bundle_id]
            sources.append(CaptureSource(backend=self.name, source_id=f"app:{bundle_id}", label=group["name"],
                                         detail="playing now" if group["playing"] else ""))
        return sources

    def start(self, source: CaptureSource, sink: FrameSink) -> None:
        with self._lock:
            if self._run is not None:
                raise CaptureError("Capture is already running. Stop it before starting another source.")
            spec = self._spec_for(source)
            if self._reader:
                spec["reader"] = self._reader
            self.failure = ""
            try:
                proc = self._spawn()
            except OSError as exc:
                raise CaptureError(f"Could not start the capture worker: {exc}") from exc
            run = _Run(source, sink, proc)
            run.reader = threading.Thread(target=self._read, args=(run,), name="hermeneus-tap-reader", daemon=True)
            run.stderr_reader = threading.Thread(target=self._read_stderr, args=(run,),
                                                 name="hermeneus-tap-stderr", daemon=True)
            run.reader.start()
            run.stderr_reader.start()
            try:
                proc.stdin.write((json.dumps(spec) + "\n").encode())
                proc.stdin.flush()
            except OSError:
                pass  # the reader reports why the worker went away
            if not run.ready.wait(self._start_timeout):
                self._finish(run)
                raise CaptureError(
                    f"Capture from {source.label} did not start within {self._start_timeout:.0f} s. "
                    "If macOS is asking for permission to record system audio, allow it and try again.")
            if run.error:
                self._finish(run)
                raise CaptureError(run.error)
            self._run = run
            self.status_message = ""
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

    def _process_infos(self):
        hal = self._get_hal()
        infos = []
        for obj in hal.process_objects():
            try:
                infos.append(hal.process_info(obj))
            except Exception:  # the process exited between listing and query
                continue
        return infos

    def _app_groups(self) -> dict:
        """Apps with a Dock presence that have opened audio, keyed by bundle id."""
        hal = self._get_hal()
        infos = [i for i in self._process_infos() if i.pid != self._own_pid]
        roots: dict = {}
        for info in infos:
            if not info.bundle_id or info.bundle_id in roots:
                continue
            app = hal.app_info(info.pid)
            if app is not None and app.regular:
                roots[info.bundle_id] = {"name": app.name or info.bundle_id, "playing": False}
        for info in infos:
            root = _root_for(info.bundle_id, roots)
            if root is not None:
                roots[root]["playing"] = roots[root]["playing"] or info.running_output
        return roots

    def _spec_for(self, source: CaptureSource) -> dict:
        if source.backend != self.name:
            raise CaptureError(f"{source.label} belongs to the {source.backend} backend, not {self.name}.")
        sid = source.source_id
        if sid == SYSTEM_SOURCE_ID:
            # Being a HAL client gives this process its Core Audio process object, which the
            # worker needs to exclude it. The query also fails early if Core Audio is unusable.
            try:
                self._get_hal().process_object_for_pid(self._own_pid)
            except Exception as exc:
                raise CaptureError(f"Core Audio is not usable: {exc}") from exc
            return {"mode": "system", "exclude_pids": [self._own_pid]}
        if sid.startswith("app:") and len(sid) > 4:
            return {"mode": "app", "bundle_id": sid[4:], "exclude_pids": [self._own_pid]}
        if sid.startswith("pid:"):
            try:
                return {"mode": "pid", "pid": int(sid[4:])}
            except ValueError:
                raise CaptureError(f"{sid} is not a valid process id.") from None
        raise CaptureError(f"Unknown process tap source {sid}.")

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
                run.ready.set()
            elif kind == KIND_ERROR:
                run.error = _message(payload) or "The capture worker reported an error."
                run.ready.set()
            elif kind == KIND_STATUS:
                self._handle_status(run, payload)
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

    def _handle_status(self, run: _Run, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
            code, text, details = str(msg["code"]), str(msg.get("message", "")), dict(msg.get("details") or {})
        except (ValueError, KeyError, TypeError):
            return
        if code == STATUS_NO_AUDIO:
            text = permission_message(self._responsible_app())
        elif code == STATUS_STALLED:
            text = f"Capture from {run.source.label} stopped receiving audio. Stop and start translation again."
        self.last_status = (code, text)
        if code in (STATUS_NO_AUDIO, STATUS_STALLED):
            self.failure = text
            self.status_message = text
            log.error("%s", text)
        elif code == STATUS_RECOVERED:
            self.status_message = ""
            log.info("process tap: %s", text)
        else:
            self.status_message = text
            log.info("process tap: %s: %s", code, text)
        if self.on_status is not None:
            try:
                self.on_status(code, text, details)
            except Exception:
                log.exception("on_status callback failed")

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
        """Ask the worker to tear down, wait for it, and escalate if it hangs. Never raises."""
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
            except subprocess.TimeoutExpired:
                log.warning("capture worker did not exit; escalating")
        for t in (run.reader, run.stderr_reader):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        stats = run.final_stats or {}
        stats["sink_errors"] = run.sink_errors
        stats["worker_exit_code"] = proc.returncode
        self.last_stats = stats
        if stats.get("teardown_failures"):
            log.error("process tap teardown reported failures: %s", stats["teardown_failures"])


def verify_system_audio_capture(timeout_s: float = 30.0) -> tuple[bool, str]:
    """Doctor seam (components "capture.verify_tap_audio"): one real capture of a test tone.

    Plays a short quiet tone with afplay in a separate process, captures it through the
    full backend and worker, and returns (ok, plain-language detail). The first run on a
    Mac shows the System Audio Recording prompt.
    """
    from .tap_doctor import check_process_tap

    result = check_process_tap(timeout_s=timeout_s)
    return result.ok, result.message


def permission_message(app: Optional[str]) -> str:
    who = f"'{app}'" if app else "the app that runs Hermes"
    return (f"macOS is giving Hermeneus silence instead of audio. Open {PERMISSION_PANE}, "
            f"turn on {who} under System Audio Recording, then start translation again.")


def responsible_app_name(pid: Optional[int] = None) -> Optional[str]:
    """Best guess at the app macOS asks about: the nearest ancestor that runs from an .app bundle.

    Returns None when no ancestor is an app (for example a gateway started by launchd).
    """
    pid = os.getpid() if pid is None else pid
    for _ in range(32):
        try:
            out = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        ppid_text, _, comm = out.partition(" ")
        marker = ".app/Contents/MacOS/"
        if marker in comm:
            # the outermost .app wins for helpers nested inside an app bundle
            return Path(comm.split(".app/")[0]).name
        try:
            pid = int(ppid_text)
        except ValueError:
            return None
        if pid <= 1:
            return None
    return None


def _message(payload: bytes) -> str:
    try:
        return str(json.loads(payload).get("message", ""))
    except (ValueError, AttributeError):
        return payload.decode(errors="replace")


def _root_for(bundle_id: str, roots: dict) -> Optional[str]:
    if not bundle_id:
        return None
    best = None
    for root in roots:
        if bundle_id == root or bundle_id.startswith(root + "."):
            if best is None or len(root) < len(best):
                best = root
    return best
