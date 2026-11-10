"""Capture from a virtual loopback input device such as BlackHole or VB-Cable.

The user routes system sound into the virtual driver (on macOS through a
Multi-Output Device that also includes the speakers). This backend reads the
driver's input side with sounddevice, downmixes to mono, resamples to
``ENGINE_SAMPLE_RATE`` with a stateful polyphase FIR and hands s16le chunks to
the engine's ``FrameSink``.

Threads: the PortAudio callback downmixes, resamples, converts to s16le and
calls the sink, so the sink runs on PortAudio's audio thread and must return
quickly (put the bytes on a queue). The whole step costs about 0.4 ms of CPU
per 10 ms block on an M-series MacBook Air, and a hand-off to a worker thread
measured 2 to 6 ms worse at p99 on BlackHole. A supervisor thread outside the audio
path watches for stream end, stalls and sink errors.

Timestamps: each chunk carries ``time.monotonic_ns()`` for its first sample.
The callback reads ``time.monotonic_ns()`` once and subtracts the age of the
first input sample, taken from PortAudio's ``currentTime - inputBufferAdcTime``
when both are present and plausible. Otherwise it subtracts the block duration
from the callback time. The callback maps each output sample back through the
resampler's group delay, so a chunk's timestamp is the capture time of the
audio content it holds.

Failures after ``start()`` (stream finished by PortAudio, no audio for
``stall_timeout`` seconds, sink raised) close the stream, are stored in
``last_error`` and are passed to the optional ``on_error`` callback from the
supervisor thread.
"""

from __future__ import annotations

import ctypes
import json
import math
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import numpy as np

from ..contracts import (
    ENGINE_SAMPLE_RATE,
    CaptureError,
    CaptureSource,
    FrameSink,
)

BACKEND_NAME = "virtual-device"

# Case-insensitive substrings of input device names that belong to known
# virtual loopback drivers. Windows names VB-CABLE's input
# "CABLE Output (VB-Audio Virtual Cable)"; macOS names it "VB-Cable".
DEFAULT_DRIVER_PATTERNS: tuple[str, ...] = ("blackhole", "vb-audio", "vb-cable")

ErrorCallback = Callable[[CaptureError], None]


# --------------------------------------------------------------------------
# Resampler
# --------------------------------------------------------------------------

def _kaiser_lowpass(num_taps: int, cutoff: float, beta: float) -> np.ndarray:
    """Windowed-sinc low-pass. ``cutoff`` is in cycles per sample (0 to 0.5)."""
    n = np.arange(num_taps, dtype=np.float64) - (num_taps - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.kaiser(num_taps, beta)
    return h / h.sum()


class PolyphaseResampler:
    """Stateful rational resampler from ``in_rate`` to ``out_rate`` for mono float32.

    The anti-aliasing filter is a Kaiser-windowed sinc designed at the
    upsampled rate. Its passband ends at ``passband`` times the lower Nyquist
    frequency and it reaches ``attenuation_db`` at that Nyquist frequency.
    State carries across ``process`` calls, so splitting a signal into blocks
    gives the same output as processing it at once.
    """

    def __init__(self, in_rate: int, out_rate: int, *, passband: float = 0.9,
                 attenuation_db: float = 80.0) -> None:
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        g = math.gcd(int(in_rate), int(out_rate))
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.up = self.out_rate // g
        self.down = self.in_rate // g

        if self.up == 1 and self.down == 1:
            bank = np.ones((1, 1), dtype=np.float32)
            self.delay_up = 0.0
        else:
            up_rate = float(self.in_rate * self.up)
            nyquist = min(self.in_rate, self.out_rate) / 2.0
            f_pass = passband * nyquist
            f_stop = nyquist
            width = 2.0 * math.pi * (f_stop - f_pass) / up_rate
            num_taps = int(math.ceil((attenuation_db - 7.95) / (2.285 * width))) + 1
            if attenuation_db > 50.0:
                beta = 0.1102 * (attenuation_db - 8.7)
            elif attenuation_db >= 21.0:
                beta = 0.5842 * (attenuation_db - 21.0) ** 0.4 + 0.07886 * (attenuation_db - 21.0)
            else:
                beta = 0.0
            h = _kaiser_lowpass(num_taps, (f_pass + f_stop) / 2.0 / up_rate, beta) * self.up
            self.delay_up = (num_taps - 1) / 2.0
            taps_per_phase = -(-num_taps // self.up)
            padded = np.zeros(taps_per_phase * self.up, dtype=np.float64)
            padded[:num_taps] = h
            # bank[phase, i] = h[phase + up * i]; reversed so a window slice
            # x[base - P + 1 .. base] lines up with taps i = P - 1 .. 0.
            bank = padded.reshape(taps_per_phase, self.up).T[:, ::-1]
        self._bank = np.ascontiguousarray(bank, dtype=np.float32)
        self.taps_per_phase = self._bank.shape[1]
        self._history = np.zeros(self.taps_per_phase - 1, dtype=np.float32)
        self._in_count = 0      # input samples consumed so far
        self._out_count = 0     # output samples produced so far

    @property
    def delay_seconds(self) -> float:
        """Group delay of the filter in seconds."""
        return self.delay_up / (self.in_rate * self.up)

    @property
    def input_count(self) -> int:
        return self._in_count

    @property
    def output_count(self) -> int:
        return self._out_count

    def output_time(self, k: int) -> float:
        """Input-sample position (from stream start) of the content in output sample ``k``."""
        return (k * self.down - self.delay_up) / self.up

    def process(self, block: np.ndarray) -> np.ndarray:
        x = np.asarray(block, dtype=np.float32).reshape(-1)
        n_new = x.shape[0]
        if n_new == 0:
            return np.zeros(0, dtype=np.float32)
        p = self.taps_per_phase
        buf = np.concatenate((self._history, x)) if p > 1 else x
        buf_start = self._in_count - (p - 1)
        total = self._in_count + n_new
        # Outputs k whose newest input index floor(k*down/up) is available.
        k_end = (total * self.up - 1) // self.down + 1
        k0 = self._out_count
        if k_end > k0:
            ks = np.arange(k0, k_end, dtype=np.int64)
            t = ks * self.down
            bases = t // self.up - buf_start
            windows = np.lib.stride_tricks.sliding_window_view(buf, p)[bases - (p - 1)]
            if self.up == 1:
                y = windows @ self._bank[0]
            else:
                y = np.einsum("kp,kp->k", windows, self._bank[t % self.up])
            y = y.astype(np.float32, copy=False)
        else:
            y = np.zeros(0, dtype=np.float32)
        if p > 1:
            self._history = buf[-(p - 1):].copy()
        self._in_count = total
        self._out_count = max(k_end, k0)
        return y


def float_to_s16le(samples: np.ndarray) -> bytes:
    scaled = np.rint(np.asarray(samples, dtype=np.float32) * 32768.0)
    return np.clip(scaled, -32768, 32767).astype("<i2").tobytes()


# --------------------------------------------------------------------------
# Device matching
# --------------------------------------------------------------------------

def _input_devices(sd: Any) -> list[tuple[int, dict]]:
    out = []
    for index, info in enumerate(sd.query_devices()):
        if int(info.get("max_input_channels", 0)) > 0:
            out.append((index, dict(info)))
    return out


def _default_hostapi(sd: Any) -> Optional[int]:
    try:
        value = sd.default.hostapi
    except Exception:
        return None
    return value if isinstance(value, int) and value >= 0 else None


def resolve_input_device(sd: Any, wanted: str) -> tuple[int, dict]:
    """Find one input device by exact name, else by a unique case-insensitive substring.

    When the same name appears under several host APIs (Windows lists one
    device under MME, DirectSound and WASAPI), the default host API wins.
    """
    if not wanted or not wanted.strip():
        raise CaptureError("No capture device was chosen.")
    devices = _input_devices(sd)

    def narrow(matches: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        if len(matches) > 1:
            host = _default_hostapi(sd)
            preferred = [m for m in matches if m[1].get("hostapi") == host]
            if preferred:
                return preferred
        return matches

    exact = narrow([d for d in devices if d[1]["name"] == wanted])
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise CaptureError(
            f"More than one input device is named '{wanted}'. Remove the duplicate or rename one of them.")

    needle = wanted.casefold()
    partial = narrow([d for d in devices if needle in d[1]["name"].casefold()])
    names = sorted({d[1]["name"] for d in partial})
    if len(partial) == 1:
        return partial[0]
    if len(names) > 1 or len(partial) > 1:
        raise CaptureError(
            f"'{wanted}' matches more than one input device: {', '.join(names)}. "
            "Pick one by its full name.")
    available = ", ".join(sorted(d[1]["name"] for d in devices)) or "none"
    raise CaptureError(f"No input device named '{wanted}' was found. Input devices: {available}.")


# --------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------

@dataclass
class CaptureStats:
    """Counters read by doctor and the latency harness. Times are nanoseconds."""

    device: str = ""
    device_rate: int = 0
    device_channels: int = 0
    blocksize: int = 0
    timestamp_source: str = ""     # "adc" or "callback"; set by the first callback
    blocks: int = 0
    frames_in: int = 0
    frames_out: int = 0
    input_overflows: int = 0
    callback_ns_total: int = 0     # wall time inside the PortAudio callback, sink included
    callback_ns_max: int = 0
    callback_cpu_ns_total: int = 0  # CPU time of the callback thread for the same span
    callback_cpu_ns_max: int = 0
    resampler_delay_ns: int = 0

    def snapshot(self) -> dict:
        d = dict(self.__dict__)
        n = max(self.blocks, 1)
        d["callback_ns_mean"] = self.callback_ns_total // n
        d["callback_cpu_ns_mean"] = self.callback_cpu_ns_total // n
        return d


class VirtualDeviceBackend:
    """``CaptureBackend`` for virtual loopback drivers (BlackHole, VB-Cable).

    ``on_error`` (optional) receives a ``CaptureError`` from the supervisor
    thread when capture stops on its own. It must not call ``start()``
    synchronously; ``stop()`` is safe from it.
    """

    name = BACKEND_NAME

    def __init__(self, *, driver_patterns: Sequence[str] = DEFAULT_DRIVER_PATTERNS,
                 list_all_inputs: bool = False, block_ms: float = 10.0,
                 stall_timeout: float = 2.0, on_error: Optional[ErrorCallback] = None,
                 sd_module: Any = None) -> None:
        if not 1.0 <= block_ms <= 100.0:
            raise ValueError("block_ms must be between 1 and 100")
        if stall_timeout <= 0:
            raise ValueError("stall_timeout must be positive")
        self.driver_patterns = tuple(p.casefold() for p in driver_patterns if p)
        self.list_all_inputs = list_all_inputs
        self.block_ms = float(block_ms)
        self.stall_timeout = float(stall_timeout)
        self.on_error = on_error
        self._sd = sd_module

        self._lock = threading.Lock()          # start/stop bookkeeping
        self._stream_lock = threading.Lock()   # closing the PortAudio stream
        self._stream: Any = None
        self._supervisor: Optional[threading.Thread] = None
        self._wake = threading.Event()         # set on stop, stream end or callback error
        self._stopping = threading.Event()
        self._pending_error: Optional[CaptureError] = None
        self._last_callback_ns = 0
        self._stats = CaptureStats()
        self.last_error: Optional[CaptureError] = None

    # -- sounddevice access -------------------------------------------------

    def _sounddevice(self) -> Any:
        if self._sd is None:
            try:
                import sounddevice
            except (ImportError, OSError) as exc:
                raise CaptureError(f"The sounddevice library could not be loaded: {exc}") from exc
            self._sd = sounddevice
        return self._sd

    def _is_virtual(self, name: str) -> bool:
        lowered = name.casefold()
        return any(p in lowered for p in self.driver_patterns)

    # -- CaptureBackend -----------------------------------------------------

    def available(self) -> tuple[bool, str]:
        try:
            sources = self.list_sources()
        except CaptureError as exc:
            return False, str(exc)
        if not sources:
            return False, ("No virtual audio input such as BlackHole or VB-Cable was found. "
                           "Install BlackHole 2ch (https://github.com/ExistentialAudio/BlackHole) "
                           "and route system sound to it.")
        return True, "Virtual input found: " + ", ".join(s.label for s in sources) + "."

    def list_sources(self) -> list[CaptureSource]:
        sd = self._sounddevice()
        try:
            devices = _input_devices(sd)
        except Exception as exc:
            raise CaptureError(f"Could not list audio devices: {exc}") from exc
        sources = []
        for _, info in devices:
            name = info["name"]
            if not (self.list_all_inputs or self._is_virtual(name)):
                continue
            detail = f"{int(info['max_input_channels'])} in, {int(round(info['default_samplerate']))} Hz"
            sources.append(CaptureSource(backend=self.name, source_id=name, label=name, detail=detail))
        return sources

    def start(self, source: CaptureSource, sink: FrameSink) -> None:
        if source.backend != self.name:
            raise CaptureError(f"Source '{source.label}' belongs to the {source.backend} backend.")
        sd = self._sounddevice()
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                raise CaptureError("Capture is already running. Stop it before starting again.")
            self._supervisor = None
            index, info = resolve_input_device(sd, source.source_id)
            name = info["name"]
            rate = int(round(float(info["default_samplerate"])))
            channels = 2 if int(info["max_input_channels"]) >= 2 else 1
            blocksize = max(1, int(round(rate * self.block_ms / 1000.0)))
            resampler = PolyphaseResampler(rate, ENGINE_SAMPLE_RATE)

            self.last_error = None
            self._pending_error = None
            self._stopping.clear()
            self._wake.clear()
            self._stats = CaptureStats(
                device=name, device_rate=rate, device_channels=channels, blocksize=blocksize,
                resampler_delay_ns=int(resampler.delay_seconds * 1e9))
            callback = self._make_callback(sd, resampler, rate, channels, sink)

            def finished() -> None:
                # Runs on a PortAudio thread; only flag it, the supervisor reports.
                if not self._stopping.is_set():
                    if self._pending_error is None:
                        self._pending_error = CaptureError(
                            f"The audio stream from '{name}' ended unexpectedly. "
                            "The device may have been removed.")
                    self._wake.set()

            try:
                stream = sd.InputStream(
                    device=index, samplerate=rate, channels=channels, dtype="float32",
                    blocksize=blocksize, latency="low", callback=callback,
                    finished_callback=finished)
            except Exception as exc:
                raise CaptureError(f"Could not open '{name}': {exc}") from exc
            self._stream = stream
            self._last_callback_ns = time.monotonic_ns()
            try:
                stream.start()
            except Exception as exc:
                self._stopping.set()
                self._close_stream()
                raise CaptureError(f"Could not start '{name}': {exc}") from exc
            supervisor = threading.Thread(target=self._supervise, args=(name,),
                                          name="hermeneus-capture-vdev", daemon=True)
            self._supervisor = supervisor
            supervisor.start()

    def stop(self) -> None:
        with self._lock:
            self._stopping.set()
            supervisor = self._supervisor
            self._supervisor = None
        self._close_stream()
        self._wake.set()
        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=5.0)

    # -- extras -------------------------------------------------------------

    @property
    def running(self) -> bool:
        s = self._supervisor
        return s is not None and s.is_alive() and not self._stopping.is_set()

    def stats(self) -> dict:
        return self._stats.snapshot()

    # -- internals ----------------------------------------------------------

    def _make_callback(self, sd: Any, resampler: PolyphaseResampler, rate: int, channels: int,
                       sink: FrameSink):
        stats = self._stats
        ns_per_in = 1e9 / rate
        abort = getattr(sd, "CallbackAbort", None)

        def callback(indata, frames, time_info, status) -> None:
            t0 = time.perf_counter_ns()
            c0 = time.thread_time_ns()
            now = time.monotonic_ns()
            self._last_callback_ns = now
            if self._pending_error is not None:
                if abort is not None:
                    raise abort
                return
            age_ns = None
            try:
                adc = float(time_info.inputBufferAdcTime)
                cur = float(time_info.currentTime)
                if adc > 0.0 and cur > 0.0 and 0.0 <= cur - adc < 1.0:
                    age_ns = int((cur - adc) * 1e9)
            except (AttributeError, TypeError, ValueError):
                pass
            if age_ns is None:
                age_ns = int(frames * ns_per_in)
                if not stats.timestamp_source:
                    stats.timestamp_source = "callback"
            elif not stats.timestamp_source:
                stats.timestamp_source = "adc"
            if status and getattr(status, "input_overflow", False):
                stats.input_overflows += 1

            if channels == 2:
                mono = (indata[:, 0] + indata[:, 1]) * np.float32(0.5)
            else:
                mono = indata[:, 0]
            block_start = resampler.input_count
            k0 = resampler.output_count
            out = resampler.process(mono)
            stats.blocks += 1
            stats.frames_in += frames
            if out.shape[0]:
                ts = now - age_ns + int(round((resampler.output_time(k0) - block_start) * ns_per_in))
                try:
                    sink(float_to_s16le(out), ts)
                except Exception as exc:
                    self._pending_error = CaptureError(f"The audio consumer failed: {exc}")
                    self._wake.set()
                    if abort is not None:
                        raise abort from exc
                    return
                stats.frames_out += out.shape[0]
            dt = time.perf_counter_ns() - t0
            dc = time.thread_time_ns() - c0
            stats.callback_ns_total += dt
            stats.callback_cpu_ns_total += dc
            if dt > stats.callback_ns_max:
                stats.callback_ns_max = dt
            if dc > stats.callback_cpu_ns_max:
                stats.callback_cpu_ns_max = dc

        return callback

    def _supervise(self, device_name: str) -> None:
        poll = min(0.25, self.stall_timeout / 4.0)
        while True:
            self._wake.wait(poll)
            if self._stopping.is_set():
                return
            error = self._pending_error
            if error is None:
                silent_s = (time.monotonic_ns() - self._last_callback_ns) / 1e9
                if silent_s < self.stall_timeout:
                    self._wake.clear()
                    continue
                error = CaptureError(
                    f"No audio arrived from '{device_name}' for {self.stall_timeout:g} s. "
                    "The device may have been removed or switched off.")
            self._fail(error)
            return

    def _fail(self, error: CaptureError) -> None:
        self.last_error = error
        self._stopping.set()
        self._close_stream()
        callback = self.on_error
        if callback is not None:
            try:
                callback(error)
            except Exception:
                pass

    def _close_stream(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Multi-Output check (doctor)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OutputDeviceInfo:
    name: str
    transport: str                    # "aggregate", "virtual", "builtin", other or "unknown"
    stacked: Optional[bool] = None    # True for a Multi-Output Device, None when unknown
    members: Optional[tuple[str, ...]] = None  # sub-device names, None when unknown


@dataclass(frozen=True)
class MultiOutputReport:
    ok: bool
    virtual_device: str
    default_output: Optional[str]
    kind: str                     # "multi-output", "aggregate", "virtual-device", "device", "unknown"
    members: tuple[str, ...] = ()
    includes_virtual: bool = False
    method: str = ""              # "coreaudio" or "system_profiler"
    explanation: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "virtual_device": self.virtual_device,
            "default_output": self.default_output, "kind": self.kind,
            "members": list(self.members), "includes_virtual": self.includes_virtual,
            "method": self.method, "explanation": self.explanation,
        }


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class _CoreAudioReader:
    """Read-only CoreAudio queries through ctypes. macOS only."""

    _UTF8 = 0x08000100
    _SINT32 = 3

    class _Address(ctypes.Structure):
        _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32),
                    ("mElement", ctypes.c_uint32)]

    def __init__(self) -> None:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(self._Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(self._Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        self._ca, self._cf = ca, cf

    def _addr(self, selector: str) -> "_CoreAudioReader._Address":
        return self._Address(_fourcc(selector), _fourcc("glob"), 0)

    def _get(self, obj: int, selector: str, ctype):
        value = ctype()
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self._ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(self._addr(selector)), 0, None, ctypes.byref(size), ctypes.byref(value))
        if status != 0:
            raise OSError(f"AudioObjectGetPropertyData('{selector}') returned {status}")
        return value.value

    def _get_cfstring(self, obj: int, selector: str) -> str:
        ref = self._get(obj, selector, ctypes.c_void_p)
        if not ref:
            return ""
        try:
            buf = ctypes.create_string_buffer(1024)
            if not self._cf.CFStringGetCString(ref, buf, len(buf), self._UTF8):
                return ""
            return buf.value.decode("utf-8")
        finally:
            self._cf.CFRelease(ref)

    def _get_ids(self, obj: int, selector: str) -> list[int]:
        addr = self._addr(selector)
        size = ctypes.c_uint32(0)
        status = self._ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size))
        if status != 0:
            raise OSError(f"AudioObjectGetPropertyDataSize('{selector}') returned {status}")
        count = size.value // ctypes.sizeof(ctypes.c_uint32)
        if count == 0:
            return []
        arr = (ctypes.c_uint32 * count)()
        status = self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), arr)
        if status != 0:
            raise OSError(f"AudioObjectGetPropertyData('{selector}') returned {status}")
        return list(arr)[: size.value // ctypes.sizeof(ctypes.c_uint32)]

    def _stacked(self, device: int) -> Optional[bool]:
        try:
            comp = self._get(device, "acom", ctypes.c_void_p)
        except OSError:
            return None
        if not comp:
            return None
        key = self._cf.CFStringCreateWithCString(None, b"stacked", self._UTF8)
        try:
            num = self._cf.CFDictionaryGetValue(comp, key)
            if not num:
                return False
            out = ctypes.c_int32(0)
            if not self._cf.CFNumberGetValue(num, self._SINT32, ctypes.byref(out)):
                return None
            return bool(out.value)
        finally:
            self._cf.CFRelease(key)
            self._cf.CFRelease(comp)

    def default_output(self) -> OutputDeviceInfo:
        device = self._get(1, "dOut", ctypes.c_uint32)  # kAudioObjectSystemObject
        if not device:
            raise OSError("no default output device")
        name = self._get_cfstring(device, "lnam")
        transport_code = self._get(device, "tran", ctypes.c_uint32)
        transport = {
            _fourcc("grup"): "aggregate", _fourcc("virt"): "virtual", _fourcc("bltn"): "builtin",
            _fourcc("usb "): "usb", _fourcc("blue"): "bluetooth", _fourcc("hdmi"): "hdmi",
            _fourcc("airp"): "airplay",
        }.get(transport_code, "other")
        if transport != "aggregate":
            return OutputDeviceInfo(name=name, transport=transport)
        members = tuple(self._get_cfstring(sub, "lnam") for sub in self._get_ids(device, "agrp"))
        return OutputDeviceInfo(name=name, transport=transport, stacked=self._stacked(device),
                                members=members)


def _profiler_default_output(run: Callable[..., Any] = subprocess.run) -> OutputDeviceInfo:
    proc = run(["system_profiler", "SPAudioDataType", "-json"], capture_output=True, text=True,
               timeout=20, check=True)
    data = json.loads(proc.stdout)
    for group in data.get("SPAudioDataType", []):
        for item in group.get("_items", []):
            if item.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                raw = str(item.get("coreaudio_device_transport", ""))
                transport = "unknown"
                for word in ("aggregate", "virtual", "builtin", "usb", "bluetooth", "hdmi", "airplay"):
                    if word in raw:
                        transport = word
                        break
                return OutputDeviceInfo(name=item.get("_name", ""), transport=transport)
    raise OSError("system_profiler reported no default output device")


def detect_multi_output_setup(virtual_device: str = "BlackHole 2ch", *,
                              reader: Optional[Callable[[], OutputDeviceInfo]] = None,
                              fallback: Optional[Callable[[], OutputDeviceInfo]] = None) -> MultiOutputReport:
    """Check whether system sound reaches ``virtual_device`` while the user still hears it.

    Reads the default output device through CoreAudio (ctypes, read-only).
    When CoreAudio cannot be loaded it falls back to ``system_profiler
    SPAudioDataType -json``, which names the default output but does not list
    aggregate members. Never changes any audio setting.
    """
    method = "coreaudio"
    errors: dict = {}
    info: Optional[OutputDeviceInfo] = None
    if reader is None and sys.platform == "darwin":
        try:
            reader = _CoreAudioReader().default_output
        except OSError as exc:
            errors["coreaudio"] = str(exc)
    if reader is not None:
        try:
            info = reader()
        except OSError as exc:
            errors["coreaudio"] = str(exc)
    if info is None:
        method = "system_profiler"
        if fallback is None and sys.platform == "darwin":
            fallback = _profiler_default_output
        if fallback is not None:
            try:
                info = fallback()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors["system_profiler"] = str(exc)
    if info is None:
        reason = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "this check only runs on macOS"
        return MultiOutputReport(
            ok=False, virtual_device=virtual_device, default_output=None, kind="unknown",
            method=method, details=errors,
            explanation=f"Could not read the default sound output ({reason}).")

    wanted = virtual_device.casefold()
    name = info.name
    if name.casefold() == wanted:
        return MultiOutputReport(
            ok=False, virtual_device=virtual_device, default_output=name, kind="virtual-device",
            includes_virtual=True, method=method, details=errors,
            explanation=(f"Sound output goes straight to '{name}'. Hermeneus can hear it but you cannot. "
                         f"In Audio MIDI Setup create a Multi-Output Device with '{virtual_device}' and your "
                         "speakers or headphones ticked, then choose it in System Settings > Sound > Output."))
    if info.transport != "aggregate":
        return MultiOutputReport(
            ok=False, virtual_device=virtual_device, default_output=name, kind="device",
            method=method, details=errors,
            explanation=(f"Sound output goes to '{name}' only, so '{virtual_device}' receives silence. "
                         f"In Audio MIDI Setup create a Multi-Output Device with '{virtual_device}' and "
                         f"'{name}' ticked, then choose it in System Settings > Sound > Output."))
    if info.members is None:
        return MultiOutputReport(
            ok=False, virtual_device=virtual_device, default_output=name, kind="aggregate",
            method=method, details=errors,
            explanation=(f"Sound output goes to '{name}', a combined device, but its members could not be "
                         f"read. Open Audio MIDI Setup and check that '{virtual_device}' is ticked."))
    members = info.members
    includes = any(m.casefold() == wanted for m in members)
    listed = ", ".join(members) or "none"
    if info.stacked is False:
        return MultiOutputReport(
            ok=False, virtual_device=virtual_device, default_output=name, kind="aggregate",
            members=members, includes_virtual=includes, method=method, details=errors,
            explanation=(f"Sound output goes to '{name}', an Aggregate Device (members: {listed}). "
                         "System sound plays only on its first channels, so it may not reach "
                         f"'{virtual_device}'. Use a Multi-Output Device instead."))
    if not includes:
        return MultiOutputReport(
            ok=False, virtual_device=virtual_device, default_output=name, kind="multi-output",
            members=members, includes_virtual=False, method=method, details=errors,
            explanation=(f"Sound output goes to the Multi-Output Device '{name}' (members: {listed}), "
                         f"which does not include '{virtual_device}'. Tick '{virtual_device}' for it "
                         "in Audio MIDI Setup."))
    return MultiOutputReport(
        ok=True, virtual_device=virtual_device, default_output=name, kind="multi-output",
        members=members, includes_virtual=True, method=method, details=errors,
        explanation=(f"Sound output goes to the Multi-Output Device '{name}' (members: {listed}). "
                     f"Hermeneus hears it through '{virtual_device}' while you hear it too."))
