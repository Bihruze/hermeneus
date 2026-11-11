"""Capture worker process for the Windows process loopback backend.

Same reason and same framing as the macOS tap worker (``_tap_worker``): the
capture runs in an otherwise idle interpreter, so a busy Hermes process never
delays the thread that drains WASAPI, and COM state (the multithreaded
apartment this worker needs) stays out of the Hermes process.

The parent writes one JSON line to stdin:

* a ``LoopbackSpec`` (``{"mode": "system" | "app" | "pid", ...}``) to capture;
* ``{"mode": "list", "own_pid": N}`` to list apps with audio sessions.

Frames on stdout use ``_tap_worker.HEADER``:

* ``R`` ready, JSON with the stream format and the target pid
* ``A`` audio, engine-format PCM; timestamp is monotonic_ns of the first sample
* ``E`` error, JSON ``{"message": ...}``; the worker exits after it
* ``T`` status, JSON ``{"code", "message", "details"}`` from the watchdog
* ``L`` list reply, JSON ``{"build": int, "apps": [...]}``; the worker exits after it
* ``S`` final stats, JSON, after teardown

The worker stops when stdin reaches end of file or delivers a ``stop`` line,
and on SIGINT, SIGTERM or SIGBREAK. Windows has no SIGHUP; a parent that
cannot stop it through stdin ends it with TerminateProcess, after which the
audio service drops the process's client.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
from typing import BinaryIO, Callable, Optional

from ._tap_worker import KIND_AUDIO, KIND_ERROR, KIND_READY, KIND_STATS, FrameWriter

KIND_STATUS = b"T"   # same value as the macOS worker's status frame
KIND_LIST = b"L"

log = logging.getLogger("hermeneus.capture.loopback_worker")


def list_apps(hal, own_pid: int) -> dict:
    """Apps that have an audio session on the default output, grouped by executable."""
    from ._loopback_engine import app_roots

    procs = hal.processes()
    names = {p.pid: p.name for p in procs}
    apps: dict = {}
    for s in hal.sessions():
        exe = names.get(s.pid, "")
        if not exe or s.pid in (own_pid, os.getpid()):
            continue
        entry = apps.setdefault(exe, {"exe": exe, "label": "", "playing": False})
        entry["playing"] = entry["playing"] or s.active
        if s.display_name and not s.display_name.startswith("@"):
            entry["label"] = entry["label"] or s.display_name
    out = []
    for exe, entry in apps.items():
        if not app_roots(procs, exe, own_pid):
            continue  # every instance is an ancestor of Hermeneus; see app_roots
        if not entry["label"]:
            entry["label"] = exe[:-4] if exe.endswith(".exe") else exe
        out.append(entry)
    return {"build": int(getattr(hal, "windows_build", lambda: 0)()), "apps": out}


def serve(inp: BinaryIO, out: BinaryIO, hal_factory: Callable[[], object], *, own_pid: int,
          refresh_interval: float = 1.0) -> int:
    """Run one request. Returns the process exit code."""
    from ._loopback_engine import LoopbackCapture, LoopbackSetupError, LoopbackSpec

    writer = FrameWriter(out)
    line = inp.readline()
    if not line:
        return 0
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("not an object")
        if request.get("mode") == "list":
            try:
                reply = list_apps(hal_factory(), int(request.get("own_pid", 0)))
            except Exception as exc:
                writer.send_json(KIND_ERROR, {"message": f"Windows audio sessions could not be read: {exc}"})
                return 3
            writer.send_json(KIND_LIST, reply)
            return 0
        spec = LoopbackSpec.from_dict(request)
    except (ValueError, KeyError, TypeError) as exc:
        writer.send_json(KIND_ERROR, {"message": f"invalid capture request: {exc}"})
        return 2

    def on_pcm(pcm: bytes, timestamp: int) -> None:
        writer.send(KIND_AUDIO, timestamp, pcm)

    def on_status(code: str, message: str, details: dict) -> None:
        writer.send_json(KIND_STATUS, {"code": code, "message": message, "details": details})

    capture: Optional[LoopbackCapture] = None
    try:
        capture = LoopbackCapture(hal_factory(), spec, on_pcm, on_status=on_status,
                                  refresh_interval=refresh_interval)
        atexit.register(capture.close)
        capture.open()
    except LoopbackSetupError as exc:
        writer.send_json(KIND_ERROR, {"message": str(exc)})
        return 2
    except Exception as exc:
        writer.send_json(KIND_ERROR, {"message": f"Windows audio could not be opened: {exc}"})
        return 3

    failures: list[str] = []
    try:
        s = capture.stats
        writer.send_json(KIND_READY, {"sample_rate": s.sample_rate, "channels": s.channels,
                                      "format": s.sample_format, "target_pid": s.target_pid})
        while True:
            line = inp.readline()
            if not line or line.strip() == b"stop" or writer.broken:
                break
    finally:
        failures = capture.close()
        stats = capture.stats_dict()
        stats["teardown_failures"] = list(stats.get("teardown_failures") or [])
        writer.send_json(KIND_STATS, stats)
    return 1 if failures else 0


def _raise_exit(signum, _frame):
    raise SystemExit(128 + int(signum))


def main() -> int:
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="hermeneus loopback worker: %(levelname)s %(message)s")
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _raise_exit)
    if sys.platform != "win32":
        FrameWriter(proto).send_json(KIND_ERROR, {"message": "process loopback capture needs Windows"})
        return 2
    # comtypes initializes COM on import with sys.coinit_flags; the completion
    # handler is called from an MTA thread, so this process lives in the MTA.
    sys.coinit_flags = 0  # type: ignore[attr-defined]  # COINIT_MULTITHREADED
    from ._wasapi import WasapiHAL

    return serve(sys.stdin.buffer, proto, WasapiHAL, own_pid=os.getpid())


if __name__ == "__main__":
    sys.exit(main())
