"""One-shot check of process tap capture, for ``hermes hermeneus doctor``.

``check_process_tap()`` plays a short quiet tone with ``afplay`` (a separate
process, because Hermeneus's own audio is excluded from system capture;
the tone keeps playing while a first-run permission prompt is open),
captures that process through the real backend and worker, and checks that
real audio arrived. The first run on a Mac triggers the macOS System Audio
Recording prompt; the start waits for the answer up to ``timeout_s``.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ..contracts import ENGINE_SAMPLE_RATE, CaptureError, CaptureSource
from .process_tap import PERMISSION_PANE, ProcessTapBackend, permission_message, responsible_app_name

AFPLAY = "/usr/bin/afplay"
TONE_HZ = 440.0
TONE_AMPLITUDE = 0.1
LISTEN_SECONDS = 1.5
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


@dataclass(frozen=True)
class DoctorResult:
    ok: bool
    code: str            # one of the module constants above
    message: str         # plain language, shown to the user
    details: dict = field(default_factory=dict)


def _write_tone(path: str, seconds: float, rate: int = 48000) -> None:
    t = np.arange(int(rate * seconds)) / rate
    fade = np.minimum(1.0, np.minimum(t, seconds - t) / 0.05)
    x = (TONE_AMPLITUDE * fade * np.sin(2 * np.pi * TONE_HZ * t) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.tobytes())


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
                      player: Optional[Callable[[str], subprocess.Popen]] = None,
                      app_name: Optional[Callable[[], Optional[str]]] = None) -> DoctorResult:
    """Start and stop the full capture once against a test tone and report what happened.

    Never raises. The optional arguments exist for tests.
    """
    app_name = app_name or responsible_app_name
    backend = (backend_factory or ProcessTapBackend)(start_timeout=timeout_s)
    ok, why = backend.available()
    if not ok:
        return DoctorResult(False, UNAVAILABLE, why)
    if player is None and not os.path.exists(AFPLAY):
        return DoctorResult(False, NO_TEST_PLAYER, "afplay was not found, so no test sound can be played.")
    tmp = tempfile.mkdtemp(prefix="hermeneus-doctor-")
    proc = None
    chunks: list[bytes] = []
    lock = threading.Lock()
    try:
        tone = os.path.join(tmp, "tone.wav")
        # Long enough to still play after a permission prompt that holds start() for timeout_s.
        _write_tone(tone, timeout_s + LISTEN_SECONDS + 2.0)
        proc = (player or (lambda p: subprocess.Popen([AFPLAY, p], stdout=subprocess.DEVNULL,
                                                      stderr=subprocess.DEVNULL)))(tone)
        source = CaptureSource(backend=backend.name, source_id=f"pid:{proc.pid}", label="the test sound")
        started = time.monotonic()
        last_error = ""
        while True:  # afplay needs a moment before Core Audio lists it
            try:
                def sink(pcm: bytes, _ts: int) -> None:
                    with lock:
                        chunks.append(pcm)
                backend.start(source, sink)
                break
            except CaptureError as exc:
                last_error = str(exc)
                if "not running or has not opened audio" not in last_error or time.monotonic() - started > 2.0:
                    return DoctorResult(False, START_FAILED, f"Capture could not start: {last_error}")
                time.sleep(0.1)
        start_s = time.monotonic() - started
        time.sleep(LISTEN_SECONDS)
        backend.stop()
        with lock:
            pcm = np.frombuffer(b"".join(chunks), dtype="<i2")
        info = analyse(pcm)
        info["start_s"] = round(start_s, 3)
        info["worker_stats"] = backend.last_stats
        if info["all_zero"] and info["samples"] > 0:
            return DoctorResult(False, PERMISSION_DENIED, permission_message(app_name()), info)
        if info["samples"] == 0:
            return DoctorResult(False, START_FAILED, "Capture started but delivered no audio at all.", info)
        if info["rms_dbfs"] < MIN_RMS_DBFS or info["tone_ratio_db"] < MIN_TONE_RATIO_DB:
            return DoctorResult(False, WRONG_AUDIO,
                                "Capture delivered audio, but not the test sound. Run doctor again; if it "
                                f"repeats, check {PERMISSION_PANE}.", info)
        return DoctorResult(True, OK, "System audio capture works.", info)
    except Exception as exc:  # doctor must report, never crash
        return DoctorResult(False, START_FAILED, f"Capture check failed: {exc}")
    finally:
        try:
            backend.stop()
        except Exception:
            pass
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
