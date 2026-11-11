"""One-shot check of process tap capture, for ``hermes hermeneus doctor``.

``check_process_tap()`` starts a small player process that holds an output
stream open and plays silence, taps that process through the real backend and
worker with the tap set to mute the process on its output device, then asks
the player to play a quiet tone and checks that the tone arrived. The player
is a separate process because Hermeneus's own processes are excluded from
system capture. The player is stopped before the tap, so the tone never plays
unmuted. The first run on a Mac triggers the macOS System Audio Recording
prompt; the start waits for the answer up to ``timeout_s``.
"""

from __future__ import annotations

import math
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ..contracts import ENGINE_SAMPLE_RATE, CaptureError, CaptureSource
from .process_tap import PERMISSION_PANE, ProcessTapBackend, permission_message, responsible_app_name, worker_env

TONE_HZ = 440.0
TONE_AMPLITUDE = 0.1
LISTEN_SECONDS = 1.5
SETTLE_SECONDS = 0.15   # tone audio captured after "go" is analysed from this point on
# A captured tone at TONE_AMPLITUDE is about -23 dBFS RMS. Anything under
# -60 dBFS, or a tone peak that does not stand out, is not the test sound.
MIN_RMS_DBFS = -60.0
MIN_TONE_RATIO_DB = 20.0

OK = "ok"
UNAVAILABLE = "unavailable"
NO_TEST_PLAYER = "no-test-player"
START_FAILED = "start-failed"
PERMISSION_DENIED = "permission-denied"
WRONG_AUDIO = "wrong-audio"

PLAYER_SCRIPT = f"""
import sys, threading
import numpy as np, sounddevice as sd
go = threading.Event(); n0 = [0]
def cb(out, frames, t, st):
    if not go.is_set():
        out.fill(0); return
    n = np.arange(frames) + n0[0]; n0[0] += frames
    out[:, 0] = {TONE_AMPLITUDE} * np.sin(2 * np.pi * {TONE_HZ} * n / 48000)
s = sd.OutputStream(device=(sys.argv[1] if len(sys.argv) > 1 else None), samplerate=48000, channels=1,
                    dtype="float32", callback=cb)
s.start()
print("ready", flush=True)
for line in sys.stdin:
    if line.strip() == "go":
        go.set()
    elif line.strip() == "stop":
        break
s.abort(); s.close()
"""


@dataclass(frozen=True)
class DoctorResult:
    ok: bool
    code: str            # one of the module constants above
    message: str         # plain language, shown to the user
    details: dict = field(default_factory=dict)


class TonePlayer:
    """A separate process that plays silence until ``begin()``, then a quiet tone until killed."""

    def __init__(self, device: Optional[str] = None, ready_timeout_s: float = 10.0) -> None:
        args = [sys.executable, "-c", PLAYER_SCRIPT] + ([device] if device else [])
        self._proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=worker_env())
        self.pid = self._proc.pid
        line = [""]
        reader = threading.Thread(target=lambda: line.__setitem__(0, self._proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(ready_timeout_s)
        if line[0].strip() != "ready":
            err = ""
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or "").strip().splitlines()[-1:] or [""]
                err = err[0]
            self.kill()
            raise RuntimeError(f"the test player did not start{': ' + err if err else ''}")

    def begin(self) -> None:
        self._proc.stdin.write("go\n")
        self._proc.stdin.flush()

    def kill(self) -> None:
        try:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass


def analyse(pcm: np.ndarray, rate: int = ENGINE_SAMPLE_RATE) -> dict:
    """RMS in dBFS and how far the tone bin stands above the median spectrum, in dB."""
    if pcm.size == 0:
        return {"samples": 0, "rms_dbfs": -math.inf, "tone_ratio_db": 0.0, "all_zero": True}
    x = pcm.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(x ** 2)))
    w = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * w))
    freqs = np.fft.rfftfreq(x.size, 1 / rate)
    k = int(np.argmin(np.abs(freqs - TONE_HZ)))
    tone = float(spec[max(0, k - 2):k + 3].max())
    floor = float(np.median(spec)) or 1e-12
    return {"samples": int(x.size), "rms_dbfs": 20 * math.log10(rms) if rms > 0 else -math.inf,
            "tone_ratio_db": 20 * math.log10(max(tone, 1e-12) / floor), "all_zero": not np.any(pcm)}


def check_process_tap(*, timeout_s: float = 30.0,
                      backend_factory: Optional[Callable[..., ProcessTapBackend]] = None,
                      player_factory: Optional[Callable[[], object]] = None,
                      app_name: Optional[Callable[[], Optional[str]]] = None) -> DoctorResult:
    """Start and stop the full capture once against a test tone and report what happened.

    Never raises. The optional arguments exist for tests.
    """
    app_name = app_name or responsible_app_name
    try:
        backend = (backend_factory or ProcessTapBackend)(start_timeout=timeout_s, mute_tapped=True)
        ok, why = backend.available()
    except Exception as exc:
        return DoctorResult(False, UNAVAILABLE, f"Capture could not be loaded: {exc}")
    if not ok:
        return DoctorResult(False, UNAVAILABLE, why)
    try:
        player = (player_factory or TonePlayer)()
    except Exception as exc:
        return DoctorResult(False, NO_TEST_PLAYER, f"No test sound could be played: {exc}")
    chunks: list[tuple[float, bytes]] = []
    lock = threading.Lock()

    def sink(pcm: bytes, _ts: int) -> None:
        with lock:
            chunks.append((time.monotonic(), pcm))

    try:
        source = CaptureSource(backend=backend.name, source_id=f"pid:{player.pid}", label="the test sound")
        started = time.monotonic()
        while True:  # the player needs a moment before Core Audio lists it
            try:
                backend.start(source, sink)
                break
            except CaptureError as exc:
                if "not running or has not opened audio" not in str(exc) or time.monotonic() - started > 3.0:
                    return DoctorResult(False, START_FAILED, f"Capture could not start: {exc}")
                time.sleep(0.1)
        start_s = time.monotonic() - started
        go_at = time.monotonic()
        player.begin()
        time.sleep(LISTEN_SECONDS)
        player.kill()          # before the tap goes away, so the tone is never heard
        backend.stop()
        with lock:
            pcm = np.frombuffer(b"".join(p for t, p in chunks if t >= go_at + SETTLE_SECONDS), dtype="<i2")
        info = analyse(pcm)
        info["start_s"] = round(start_s, 3)
        info["worker_stats"] = backend.last_stats
        if info["samples"] == 0:
            return DoctorResult(False, START_FAILED, "Capture started but delivered no audio at all.", info)
        if info["all_zero"]:
            return DoctorResult(False, PERMISSION_DENIED, permission_message(app_name()), info)
        if info["rms_dbfs"] < MIN_RMS_DBFS or info["tone_ratio_db"] < MIN_TONE_RATIO_DB:
            return DoctorResult(False, WRONG_AUDIO,
                                "Capture delivered audio, but not the test sound. Run doctor again; if it "
                                f"repeats, check {PERMISSION_PANE}.", info)
        return DoctorResult(True, OK, "System audio capture works.", info)
    except Exception as exc:  # doctor must report, never crash
        return DoctorResult(False, START_FAILED, f"Capture check failed: {exc}")
    finally:
        player.kill()
        try:
            backend.stop()
        except Exception:
            pass
