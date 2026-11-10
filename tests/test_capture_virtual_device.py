"""VirtualDeviceBackend tests against a fake sounddevice module. No real device is opened."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from hermeneus_engine.capture.virtual_device import (
    OutputDeviceInfo,
    PolyphaseResampler,
    VirtualDeviceBackend,
    detect_multi_output_setup,
    resolve_input_device,
)
from hermeneus_engine.contracts import CaptureBackend, CaptureError, CaptureSource


# --------------------------------------------------------------------------
# Fake sounddevice
# --------------------------------------------------------------------------

def _dev(name, ins, outs=0, rate=48000.0, hostapi=0):
    return {"name": name, "max_input_channels": ins, "max_output_channels": outs,
            "default_samplerate": rate, "hostapi": hostapi}


MAC_DEVICES = [
    _dev("BlackHole 16ch", 16, 16),
    _dev("BlackHole 2ch", 2, 2),
    _dev("MacBook Air Microphone", 1),
    _dev("MacBook Air Speakers", 0, 2),
    _dev("VB-Cable", 2, 2),
]


class FakeCallbackAbort(Exception):
    pass


class FakeStream:
    def __init__(self, sd, **kwargs):
        self.sd = sd
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.finished_callback = kwargs.get("finished_callback")
        self.started = False
        self.stop_calls = 0
        self.close_calls = 0
        sd.streams.append(self)

    def start(self):
        if self.sd.fail_start:
            raise RuntimeError("PortAudio error -9986")
        self.started = True

    def stop(self):
        self.stop_calls += 1
        if self.started and self.finished_callback:
            self.finished_callback()
        self.started = False

    def close(self):
        self.close_calls += 1

    def feed(self, block, adc=None, cur=None):
        block = np.asarray(block, dtype=np.float32)
        if block.ndim == 1:
            block = block[:, None]
        info = SimpleNamespace(inputBufferAdcTime=adc if adc is not None else 0.0,
                               currentTime=cur if cur is not None else 0.0)
        try:
            self.callback(block, block.shape[0], info, SimpleNamespace(input_overflow=False))
        except FakeCallbackAbort:
            self.started = False
            if self.finished_callback:
                self.finished_callback()


class FakeSD:
    CallbackAbort = FakeCallbackAbort

    def __init__(self, devices=MAC_DEVICES, hostapi=0):
        self.devices = devices
        self.default = SimpleNamespace(hostapi=hostapi)
        self.streams: list[FakeStream] = []
        self.fail_start = False

    def query_devices(self):
        return list(self.devices)

    def InputStream(self, **kwargs):  # noqa: N802 (mirrors sounddevice)
        return FakeStream(self, **kwargs)


class Collector:
    def __init__(self):
        self.chunks: list[tuple[bytes, int]] = []
        self.lock = threading.Lock()

    def __call__(self, data: bytes, ts: int) -> None:
        with self.lock:
            self.chunks.append((data, ts))

    def samples(self) -> np.ndarray:
        return np.frombuffer(b"".join(c[0] for c in self.chunks), dtype="<i2").astype(np.float64) / 32768.0


def _source(name):
    return CaptureSource(backend="virtual-device", source_id=name, label=name)


# --------------------------------------------------------------------------
# Device listing and matching
# --------------------------------------------------------------------------

def test_satisfies_protocol():
    assert isinstance(VirtualDeviceBackend(sd_module=FakeSD()), CaptureBackend)
    assert VirtualDeviceBackend.name == "virtual-device"


def test_list_sources_filters_virtual_drivers():
    names = [s.source_id for s in VirtualDeviceBackend(sd_module=FakeSD()).list_sources()]
    assert names == ["BlackHole 16ch", "BlackHole 2ch", "VB-Cable"]


def test_list_sources_matches_windows_vb_audio_name():
    sd = FakeSD([_dev("CABLE Output (VB-Audio Virtual Cable)", 2), _dev("Microphone (Realtek)", 2)])
    assert [s.source_id for s in VirtualDeviceBackend(sd_module=sd).list_sources()] == [
        "CABLE Output (VB-Audio Virtual Cable)"]


def test_list_sources_custom_patterns_and_all_inputs():
    sd = FakeSD()
    custom = VirtualDeviceBackend(sd_module=sd, driver_patterns=["microphone"]).list_sources()
    assert [s.source_id for s in custom] == ["MacBook Air Microphone"]
    every = VirtualDeviceBackend(sd_module=sd, list_all_inputs=True).list_sources()
    assert len(every) == 4 and all(s.backend == "virtual-device" for s in every)
    assert every[1].detail == "2 in, 48000 Hz"


def test_available_reports_missing_driver():
    ok, reason = VirtualDeviceBackend(sd_module=FakeSD([_dev("Mic", 1)])).available()
    assert not ok and "BlackHole" in reason
    ok, reason = VirtualDeviceBackend(sd_module=FakeSD()).available()
    assert ok and "BlackHole 2ch" in reason


def test_resolve_exact_name_beats_substring():
    index, info = resolve_input_device(FakeSD(), "BlackHole 2ch")
    assert index == 1 and info["name"] == "BlackHole 2ch"


def test_resolve_unique_substring_case_insensitive():
    index, _ = resolve_input_device(FakeSD(), "vb-cable")
    assert index == 4
    index, _ = resolve_input_device(FakeSD(), "2CH")
    assert index == 1


def test_resolve_ambiguous_substring_lists_candidates():
    with pytest.raises(CaptureError) as err:
        resolve_input_device(FakeSD(), "blackhole")
    assert "BlackHole 16ch, BlackHole 2ch" in str(err.value)


def test_resolve_missing_device():
    with pytest.raises(CaptureError) as err:
        resolve_input_device(FakeSD(), "Loopback Audio")
    assert "No input device named 'Loopback Audio'" in str(err.value)


def test_resolve_ignores_output_only_devices():
    with pytest.raises(CaptureError):
        resolve_input_device(FakeSD(), "MacBook Air Speakers")


def test_resolve_duplicate_names_prefers_default_hostapi():
    sd = FakeSD([_dev("CABLE Output (VB-Audio)", 2, hostapi=0), _dev("CABLE Output (VB-Audio)", 2, hostapi=2)],
                hostapi=2)
    assert resolve_input_device(sd, "CABLE Output (VB-Audio)")[0] == 1


def test_start_missing_device_raises():
    backend = VirtualDeviceBackend(sd_module=FakeSD())
    with pytest.raises(CaptureError):
        backend.start(_source("Soundflower"), Collector())
    backend.stop()


def test_start_rejects_foreign_source():
    backend = VirtualDeviceBackend(sd_module=FakeSD())
    with pytest.raises(CaptureError):
        backend.start(CaptureSource(backend="process-tap", source_id="x", label="x"), Collector())


# --------------------------------------------------------------------------
# Resampler
# --------------------------------------------------------------------------

def _tone_level_db(y, rate, freq):
    n = len(y)
    t = np.arange(n) / rate
    # Least-squares amplitude of a known frequency.
    basis = np.stack([np.sin(2 * np.pi * freq * t), np.cos(2 * np.pi * freq * t)], axis=1)
    coef, *_ = np.linalg.lstsq(basis, y, rcond=None)
    return 20 * np.log10(np.hypot(*coef) + 1e-20)


def _dominant_freq(y, rate):
    w = y * np.hanning(len(y))
    spec = np.abs(np.fft.rfft(w))
    return np.fft.rfftfreq(len(y), 1 / rate)[np.argmax(spec)]


@pytest.mark.parametrize("in_rate", [48000, 44100])
def test_resampler_keeps_1khz_tone(in_rate):
    r = PolyphaseResampler(in_rate, 16000)
    t = np.arange(in_rate) / in_rate
    y = r.process((0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32))
    assert abs(len(y) - 16000) <= 1
    steady = y[200:]
    assert abs(_dominant_freq(steady, 16000) - 1000) < 2
    level = _tone_level_db(steady, 16000, 1000)
    assert abs(level - 20 * np.log10(0.5)) < 1.0


def test_resampler_suppresses_12khz_alias():
    r = PolyphaseResampler(48000, 16000)
    t = np.arange(48000) / 48000
    y = r.process((0.5 * np.sin(2 * np.pi * 12000 * t)).astype(np.float32))[200:]
    # 12 kHz folds to 4 kHz at a 16 kHz output rate.
    alias = _tone_level_db(y, 16000, 4000)
    assert alias - 20 * np.log10(0.5) < -40.0


def test_resampler_block_split_matches_one_shot():
    rng = np.random.default_rng(7)
    x = rng.standard_normal(48000 * 2).astype(np.float32) * 0.2
    whole = PolyphaseResampler(48000, 16000).process(x)
    r = PolyphaseResampler(48000, 16000)
    parts, pos = [], 0
    for size in rng.integers(1, 1500, size=2000):
        if pos >= len(x):
            break
        parts.append(r.process(x[pos:pos + size]))
        pos += size
    split = np.concatenate(parts)
    assert len(split) == len(whole)
    assert np.max(np.abs(split - whole)) < 1e-5


def test_resampler_44k_block_split_matches_one_shot():
    rng = np.random.default_rng(3)
    x = rng.standard_normal(44100).astype(np.float32) * 0.2
    whole = PolyphaseResampler(44100, 16000).process(x)
    r = PolyphaseResampler(44100, 16000)
    split = np.concatenate([r.process(x[i:i + 441]) for i in range(0, len(x), 441)])
    assert np.max(np.abs(split - whole)) < 1e-5


def test_resampler_sine_has_no_clicks_at_block_edges():
    r = PolyphaseResampler(48000, 16000)
    t = np.arange(48000) / 48000
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    y = np.concatenate([r.process(x[i:i + 480]) for i in range(0, len(x), 480)])[200:]
    # A 440 Hz sine at 16 kHz changes at most 2*pi*440/16000*0.5 per sample.
    assert np.max(np.abs(np.diff(y))) < 2 * np.pi * 440 / 16000 * 0.5 * 1.05


def test_resampler_passthrough_at_16k():
    r = PolyphaseResampler(16000, 16000)
    x = np.linspace(-1, 1, 160, dtype=np.float32)
    assert np.array_equal(r.process(x), x)


# --------------------------------------------------------------------------
# Streaming through the backend
# --------------------------------------------------------------------------

def test_start_opens_native_rate_stereo_stream_and_downmixes():
    sd = FakeSD()
    sink = Collector()
    backend = VirtualDeviceBackend(sd_module=sd, block_ms=10)
    backend.start(_source("BlackHole 2ch"), sink)
    stream = sd.streams[0]
    assert stream.kwargs["samplerate"] == 48000
    assert stream.kwargs["channels"] == 2
    assert stream.kwargs["blocksize"] == 480
    assert stream.kwargs["dtype"] == "float32"
    t = np.arange(48000) / 48000
    left = 0.6 * np.sin(2 * np.pi * 1000 * t)
    right = np.zeros_like(left)
    stereo = np.stack([left, right], axis=1).astype(np.float32)
    for i in range(0, 48000, 480):
        stream.feed(stereo[i:i + 480])
    backend.stop()
    y = sink.samples()
    assert abs(len(y) - 16000) <= 1
    assert abs(_tone_level_db(y[200:], 16000, 1000) - 20 * np.log10(0.3)) < 1.0
    assert all(len(c[0]) % 2 == 0 for c in sink.chunks)
    stats = backend.stats()
    assert stats["blocks"] == 100 and stats["frames_in"] == 48000 and stats["frames_out"] == 16000


def test_sixteen_channel_device_reads_first_two_channels():
    sd = FakeSD()
    backend = VirtualDeviceBackend(sd_module=sd)
    backend.start(_source("BlackHole 16ch"), Collector())
    assert sd.streams[0].kwargs["channels"] == 2
    backend.stop()


def test_timestamps_follow_adc_time_and_are_continuous(monkeypatch):
    sd = FakeSD()
    sink = Collector()
    backend = VirtualDeviceBackend(sd_module=sd, block_ms=10)
    backend.start(_source("BlackHole 2ch"), sink)
    stream = sd.streams[0]
    clock = {"ns": 10_000_000_000}
    monkeypatch.setattr(time, "monotonic_ns", lambda: clock["ns"])
    for i in range(50):
        # The callback runs 2 ms after the block's first sample hit the ADC.
        clock["ns"] = 10_000_000_000 + i * 10_000_000 + 2_000_000
        stream.feed(np.zeros((480, 2)), adc=100.0 + i * 0.01, cur=100.0 + i * 0.01 + 0.002)
    monkeypatch.undo()
    backend.stop()
    assert backend.stats()["timestamp_source"] == "adc"
    # The first output sample holds content from (303 - 1) / 2 = 151 input samples
    # before the first input sample, the filter's group delay at 48 kHz.
    expected = 10_000_000_000 - 151 / 48000 * 1e9
    assert abs(sink.chunks[0][1] - expected) < 1000
    # Each chunk starts where the previous one ended, at 62.5 us per output sample.
    for (d0, t0), (_, t1) in zip(sink.chunks, sink.chunks[1:]):
        assert abs((t1 - t0) - (len(d0) // 2) * 62_500) < 1000


def test_timestamps_fall_back_to_callback_time(monkeypatch):
    sd = FakeSD()
    sink = Collector()
    backend = VirtualDeviceBackend(sd_module=sd)
    backend.start(_source("BlackHole 2ch"), sink)
    monkeypatch.setattr(time, "monotonic_ns", lambda: 5_000_000_000)
    sd.streams[0].feed(np.zeros((480, 2)))
    monkeypatch.undo()
    backend.stop()
    assert backend.stats()["timestamp_source"] == "callback"
    expected = 5_000_000_000 - 10_000_000 - 151 / 48000 * 1e9
    assert abs(sink.chunks[0][1] - expected) < 1000


def test_stop_is_idempotent_and_releases_stream():
    sd = FakeSD()
    backend = VirtualDeviceBackend(sd_module=sd)
    backend.stop()
    backend.start(_source("BlackHole 2ch"), Collector())
    assert backend.running
    backend.stop()
    backend.stop()
    stream = sd.streams[0]
    assert stream.close_calls == 1 and not backend.running
    assert not [t for t in threading.enumerate() if t.name == "hermeneus-capture-vdev"]
    assert backend.last_error is None


def test_restart_after_stop():
    sd = FakeSD()
    backend = VirtualDeviceBackend(sd_module=sd)
    backend.start(_source("BlackHole 2ch"), Collector())
    backend.stop()
    sink = Collector()
    backend.start(_source("VB-Cable"), sink)
    sd.streams[1].feed(np.zeros((480, 2)))
    backend.stop()
    assert len(sd.streams) == 2 and sink.chunks


def test_double_start_raises():
    backend = VirtualDeviceBackend(sd_module=FakeSD())
    backend.start(_source("BlackHole 2ch"), Collector())
    with pytest.raises(CaptureError):
        backend.start(_source("BlackHole 2ch"), Collector())
    backend.stop()


def test_stream_start_failure_raises_and_cleans_up():
    sd = FakeSD()
    sd.fail_start = True
    backend = VirtualDeviceBackend(sd_module=sd)
    with pytest.raises(CaptureError) as err:
        backend.start(_source("BlackHole 2ch"), Collector())
    assert "Could not start 'BlackHole 2ch'" in str(err.value)
    assert sd.streams[0].close_calls == 1
    assert not backend.running


def _wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_stream_finished_by_portaudio_reports_error():
    sd = FakeSD()
    errors = []
    backend = VirtualDeviceBackend(sd_module=sd, on_error=errors.append)
    backend.start(_source("BlackHole 2ch"), Collector())
    sd.streams[0].finished_callback()  # PortAudio ended the stream, for example after device loss
    assert _wait_for(lambda: errors)
    assert "ended unexpectedly" in str(errors[0])
    assert backend.last_error is errors[0]
    assert sd.streams[0].close_calls == 1
    backend.stop()
    assert sd.streams[0].close_calls == 1


def test_silent_stall_reports_error():
    sd = FakeSD()
    errors = []
    backend = VirtualDeviceBackend(sd_module=sd, stall_timeout=0.1, on_error=errors.append)
    backend.start(_source("BlackHole 2ch"), Collector())
    assert _wait_for(lambda: errors)
    assert "No audio arrived from 'BlackHole 2ch'" in str(errors[0])
    backend.stop()


def test_sink_exception_aborts_stream_and_reports_once():
    sd = FakeSD()
    errors = []
    calls = []

    def bad_sink(data, ts):
        calls.append(ts)
        raise ValueError("queue closed")

    backend = VirtualDeviceBackend(sd_module=sd, on_error=errors.append)
    backend.start(_source("BlackHole 2ch"), bad_sink)
    sd.streams[0].feed(np.zeros((480, 2)))
    sd.streams[0].feed(np.zeros((480, 2)))
    assert _wait_for(lambda: errors)
    assert len(calls) == 1 and len(errors) == 1
    assert "queue closed" in str(errors[0])
    assert sd.streams[0].close_calls == 1
    backend.stop()


def test_on_error_may_call_stop():
    sd = FakeSD()
    holder = {}

    def on_error(err):
        holder["backend"].stop()

    backend = VirtualDeviceBackend(sd_module=sd, stall_timeout=0.1, on_error=on_error)
    holder["backend"] = backend
    backend.start(_source("BlackHole 2ch"), Collector())
    assert _wait_for(lambda: backend.last_error is not None)
    assert _wait_for(lambda: not [t for t in threading.enumerate() if t.name == "hermeneus-capture-vdev"])


def test_callback_cost_is_recorded():
    sd = FakeSD()
    backend = VirtualDeviceBackend(sd_module=sd)
    backend.start(_source("BlackHole 2ch"), Collector())
    for _ in range(10):
        sd.streams[0].feed(np.zeros((480, 2)))
    backend.stop()
    st = backend.stats()
    assert st["blocks"] == 10 and st["callback_ns_max"] >= st["callback_ns_mean"] > 0
    assert st["resampler_delay_ns"] == int(151 / 48000 * 1e9)


# --------------------------------------------------------------------------
# Multi-Output check
# --------------------------------------------------------------------------

def _report(info):
    return detect_multi_output_setup("BlackHole 2ch", reader=lambda: info)


def test_multi_output_with_blackhole_is_ok():
    r = _report(OutputDeviceInfo("Multi-Output Device", "aggregate", True,
                                 ("MacBook Air Speakers", "BlackHole 2ch")))
    assert r.ok and r.kind == "multi-output" and r.includes_virtual and r.method == "coreaudio"


def test_speakers_only_explains_silence():
    r = _report(OutputDeviceInfo("MacBook Air Speakers", "builtin"))
    assert not r.ok and r.kind == "device"
    assert "receives silence" in r.explanation and "Multi-Output Device" in r.explanation


def test_output_straight_to_blackhole():
    r = _report(OutputDeviceInfo("BlackHole 2ch", "virtual"))
    assert not r.ok and r.kind == "virtual-device" and "you cannot" in r.explanation


def test_multi_output_without_blackhole():
    r = _report(OutputDeviceInfo("Multi-Output Device", "aggregate", True, ("MacBook Air Speakers", "VB-Cable")))
    assert not r.ok and not r.includes_virtual and "does not include 'BlackHole 2ch'" in r.explanation


def test_plain_aggregate_is_flagged():
    r = _report(OutputDeviceInfo("Aggregate Device", "aggregate", False, ("BlackHole 2ch", "MacBook Air Speakers")))
    assert not r.ok and r.kind == "aggregate" and r.includes_virtual


def test_falls_back_to_system_profiler():
    def broken():
        raise OSError("status -1")

    r = detect_multi_output_setup("BlackHole 2ch", reader=broken,
                                  fallback=lambda: OutputDeviceInfo("MacBook Air Speakers", "builtin"))
    assert r.method == "system_profiler" and r.default_output == "MacBook Air Speakers"
    assert "coreaudio" in r.details


def test_unreadable_output_is_reported():
    def broken():
        raise OSError("status -1")

    r = detect_multi_output_setup("BlackHole 2ch", reader=broken, fallback=broken)
    assert not r.ok and r.kind == "unknown" and "Could not read" in r.explanation
    assert set(r.to_dict()) >= {"ok", "explanation", "members"}
