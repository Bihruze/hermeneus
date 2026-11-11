#!/usr/bin/env python3
"""First run of the Windows process loopback path on a real Windows machine.

Prints one JSON line per step and exits 0 only when every step that the
machine can support passed. It plays nothing audible unless --tone is given;
with --tone it plays a quiet 440 Hz tone on the default output twice: from a
child process, which a capture of that child must hear, and from this
process, which the system capture (all audio except this process tree) must
not hear. Close other apps that play sound first.

  python scripts/windows_smoke.py            # build, packages, app list, 2 s system capture
  python scripts/windows_smoke.py --tone     # also checks capture with a test tone

It never reads or prints a secret.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TONE = r"""
import numpy as np, sounddevice as sd, time
rate = 48000
t = np.arange(rate * 2) / rate
sd.play((0.05 * np.sin(2 * np.pi * 440 * t)).astype("float32"), rate)
sd.wait()
"""


def emit(step: str, ok, **info) -> None:
    print(json.dumps({"step": step, "ok": ok, **info}), flush=True)


def capture(backend, source, seconds: float) -> dict:
    import numpy as np
    chunks = []
    backend.start(source, lambda pcm, ts: chunks.append(pcm))
    time.sleep(seconds)
    backend.stop()
    pcm = np.frombuffer(b"".join(chunks), dtype="<i2").astype(np.float32) / 32768
    return {"samples": int(pcm.size), "peak": float(np.max(np.abs(pcm))) if pcm.size else 0.0,
            "stats": backend.last_stats, "status": backend.last_status}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tone", action="store_true", help="play a quiet test tone and check capture hears it")
    args = ap.parse_args()
    failed = False

    from hermeneus_engine.capture import process_loopback
    from hermeneus_engine.contracts import CaptureSource

    emit("platform", sys.platform == "win32", platform=sys.platform, build=process_loopback.windows_build(),
         python=sys.version.split()[0])
    backend = process_loopback.ProcessLoopbackBackend()
    ok, reason = backend.available()
    emit("available", ok, reason=reason)
    if not ok:
        return 1
    try:
        sources = backend.list_sources()
        emit("list_sources", True, sources=[s.source_id for s in sources])
    except Exception as exc:
        emit("list_sources", False, error=repr(exc))
        failed = True
    system = CaptureSource(process_loopback.BACKEND_NAME, "system", "All system audio except Hermeneus")
    try:
        result = capture(backend, system, 2.0)
        emit("system_capture", result["samples"] > 16000, **result)
        failed = failed or result["samples"] <= 16000
    except Exception as exc:
        emit("system_capture", False, error=repr(exc))
        return 1
    if args.tone:
        import numpy as np
        import sounddevice as sd
        # 1. A tone played by another process reaches a capture of that process.
        from hermeneus_engine import subproc

        player = subproc.python_popen(["-c", "import time; time.sleep(1.0)\n" + TONE])
        try:
            app = CaptureSource(process_loopback.BACKEND_NAME, f"pid:{player.pid}", "tone player")
            heard = capture(backend, app, 2.5)
        finally:
            player.wait(timeout=15)
        emit("tone_heard_in_player_capture", heard["peak"] > 0.01, peak=heard["peak"])
        failed = failed or heard["peak"] <= 0.01
        # 2. A tone played by this process (as Hermeneus plays spoken translation)
        #    does not reach the system capture, which leaves this process tree out.
        rate = 48000
        t = np.arange(rate * 2) / rate
        sd.play((0.05 * np.sin(2 * np.pi * 440 * t)).astype("float32"), rate)
        try:
            mine = capture(backend, system, 1.5)
        finally:
            sd.wait()
        emit("own_tone_left_out_of_system_capture", mine["peak"] < 0.001, peak=mine["peak"],
             note="other apps playing at the same time also raise the peak")
        failed = failed or mine["peak"] >= 0.001
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
