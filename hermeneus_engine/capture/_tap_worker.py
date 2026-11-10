"""Capture worker process for the process tap backend.

The Core Audio IOProc is a Python callable, so it needs the GIL on every
buffer. In a busy interpreter that wait starves the audio thread (measured in
the W02 spike: two CPU-bound Python threads in the same process lost a quarter
of all buffers). The backend therefore runs the tap in this separate,
otherwise idle interpreter and reads finished PCM through a pipe.

Protocol. The parent writes one JSON line with the ``TapSpec`` to stdin. The
worker answers on stdout with frames: a 13 byte header (kind: 1 byte,
payload length: uint32 LE, timestamp: int64 LE) followed by the payload.

* ``R`` ready, JSON payload with the tap format and tapped process objects
* ``A`` audio, engine-format PCM; timestamp is monotonic_ns of the first sample
* ``E`` error, JSON payload ``{"message": ...}``; the worker exits after it
* ``T`` status, JSON payload ``{"code", "message", "details"}`` from the silence watchdog
  and format-change handling (see ``_tap_engine.STATUS_*``)
* ``S`` final stats, JSON payload, sent after teardown

The worker stops when stdin reaches end of file or delivers a ``stop`` line,
and on SIGTERM, SIGINT or SIGHUP. Every path releases the tap and the private
aggregate device before the process exits.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import struct
import sys
import threading
from dataclasses import asdict
from typing import BinaryIO, Callable, Optional

HEADER = struct.Struct("<cIq")
KIND_READY = b"R"
KIND_AUDIO = b"A"
KIND_ERROR = b"E"
KIND_STATS = b"S"
KIND_STATUS = b"T"

log = logging.getLogger("hermeneus.capture.tap_worker")


class FrameWriter:
    def __init__(self, out: BinaryIO) -> None:
        self._out = out
        self._lock = threading.Lock()
        self.broken = False

    def send(self, kind: bytes, timestamp: int, payload: bytes) -> bool:
        if self.broken:
            return False
        data = HEADER.pack(kind, len(payload), timestamp) + payload
        with self._lock:
            try:
                self._out.write(data)
                self._out.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.broken = True
                return False
        return True

    def send_json(self, kind: bytes, obj: dict) -> bool:
        return self.send(kind, 0, json.dumps(obj).encode())


def read_frame(inp: BinaryIO) -> Optional[tuple[bytes, int, bytes]]:
    """Read one frame. Returns None at end of stream."""
    head = _read_exact(inp, HEADER.size)
    if head is None:
        return None
    kind, length, timestamp = HEADER.unpack(head)
    payload = _read_exact(inp, length) if length else b""
    if payload is None:
        return None
    return kind, timestamp, payload


def _read_exact(inp: BinaryIO, n: int) -> Optional[bytes]:
    chunks = []
    while n:
        chunk = inp.read(n)
        if not chunk:
            return None
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def serve(inp: BinaryIO, out: BinaryIO, hal_factory: Callable[[], object], *, own_pid: int,
          refresh_interval: float = 1.0, engine_options: Optional[dict] = None) -> int:
    """Run one capture session. Returns the process exit code."""
    from ._tap_engine import TapCapture, TapSetupError, TapSpec

    writer = FrameWriter(out)
    line = inp.readline()
    if not line:
        return 0
    try:
        spec = TapSpec.from_dict(json.loads(line))
    except (ValueError, KeyError, TypeError) as exc:
        writer.send_json(KIND_ERROR, {"message": f"invalid tap request: {exc}"})
        return 2
    if spec.mode == "system" and own_pid not in spec.exclude_pids:
        spec = TapSpec(mode=spec.mode, bundle_id=spec.bundle_id, pid=spec.pid,
                       exclude_pids=spec.exclude_pids + (own_pid,))

    capture: Optional[TapCapture] = None

    def on_pcm(pcm: bytes, timestamp: int) -> None:
        writer.send(KIND_AUDIO, timestamp, pcm)

    def on_status(code: str, message: str, details: dict) -> None:
        writer.send_json(KIND_STATUS, {"code": code, "message": message, "details": details})

    try:
        capture = TapCapture(hal_factory(), spec, on_pcm, on_status=on_status, refresh_interval=refresh_interval,
                             **(engine_options or {}))
        atexit.register(capture.close)
        capture.open()
    except TapSetupError as exc:
        writer.send_json(KIND_ERROR, {"message": str(exc)})
        return 2
    except Exception as exc:
        writer.send_json(KIND_ERROR, {"message": f"Core Audio refused the tap: {exc}"})
        return 3

    failures: list[str] = []
    try:
        writer.send_json(KIND_READY, {"sample_rate": capture.stats.sample_rate,
                                      "tapped_objects": capture.stats.tapped_objects})
        while True:
            line = inp.readline()
            if not line or line.strip() == b"stop" or writer.broken:
                break
    finally:
        failures = capture.close()
        stats = asdict(capture.stats)
        stats["teardown_failures"] = failures
        writer.send_json(KIND_STATS, stats)
    return 1 if failures else 0


def _raise_exit(signum, _frame):
    raise SystemExit(128 + signum)


def main() -> int:
    # Frames go to a private copy of stdout; anything else printed lands on stderr.
    proto = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="hermeneus tap worker: %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _raise_exit)

    from ._coreaudio import CoreAudioHAL

    return serve(sys.stdin.buffer, proto, CoreAudioHAL, own_pid=os.getpid())


if __name__ == "__main__":
    sys.exit(main())
