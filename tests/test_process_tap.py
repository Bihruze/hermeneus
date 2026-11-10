import io
import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from fake_hal import FakeHAL
from hermeneus_engine.capture import _tap_engine, _tap_worker, process_tap
from hermeneus_engine.capture._coreaudio import AppInfo, HALError
from hermeneus_engine.capture._resample import StreamingResampler
from hermeneus_engine.capture._tap_engine import TapCapture, TapSetupError, TapSpec
from hermeneus_engine.capture.process_tap import ProcessTapBackend
from hermeneus_engine.contracts import CaptureBackend, CaptureError, CaptureSource

OWN_PID = 4242
OPEN_ORDER = ["create_tap", "tap_format", "create_aggregate", "create_ioproc", "start"]
CLOSE_ORDER = ["stop", "destroy_ioproc", "destroy_aggregate", "destroy_tap"]


def sine(freq, n, rate=48000, amp=0.5, start=0):
    t = (np.arange(n) + start) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def chrome_hal():
    hal = FakeHAL()
    hal.add_process(10, 100, "com.google.Chrome", app=AppInfo("Google Chrome", True))
    hal.add_process(11, 101, "com.google.Chrome.helper", playing=True)
    hal.add_process(12, 102, "com.apple.avconferenced")
    hal.add_process(13, OWN_PID, "")
    hal.add_process(14, 103, "com.hnc.Discord", app=AppInfo("Discord", True))
    hal.add_process(15, 104, "com.apple.universalaccessd", app=AppInfo("universalaccessd", False))
    return hal


# ---------------------------------------------------------------- resampler

@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_resampler_chunked_equals_whole_and_keeps_passband(rate):
    x = sine(440, rate * 2, rate)
    whole = StreamingResampler(rate, 16000).process(x)
    r = StreamingResampler(rate, 16000)
    chunked = np.concatenate([r.process(x[i:i + 512]) for i in range(0, x.size, 512)])
    assert chunked.size == whole.size == 32000
    assert np.max(np.abs(chunked - whole)) < 1e-6
    rms = np.sqrt(np.mean(chunked[4000:].astype(np.float64) ** 2))
    assert abs(20 * np.log10(rms / (0.5 / np.sqrt(2)))) < 0.1


@pytest.mark.parametrize("rate", [44100, 48000])
def test_resampler_rejects_aliases(rate):
    r = StreamingResampler(rate, 16000)
    x = sine(9000, rate * 2, rate)
    y = np.concatenate([r.process(x[i:i + 512]) for i in range(0, x.size, 512)])
    rms = np.sqrt(np.mean(y[4000:].astype(np.float64) ** 2))
    assert 20 * np.log10(rms / (0.5 / np.sqrt(2))) < -80


# ---------------------------------------------------------------- tap engine

def test_capture_converts_to_engine_pcm_and_tears_down_in_order():
    hal = FakeHAL()
    hal.add_process(1, 77)
    got = []
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: got.append((pcm, ts)))
    cap.open()
    assert hal.names() == OPEN_ORDER
    for i in range(94):  # about 1 s of 512-frame buffers
        hal.feed(i * 512, sine(440, 512, start=i * 512), host_time=1_000_000 + i * 10_666_667)
    assert wait_for(lambda: cap.stats.buffers == 94)
    hal.calls.clear()
    assert cap.close() == []
    assert hal.names() == CLOSE_ORDER
    assert hal.clean
    pcm = np.frombuffer(b"".join(p for p, _ in got), dtype="<i2").astype(np.float64) / 32768
    assert pcm.size == -(-94 * 512 // 3)
    assert abs(np.sqrt(np.mean(pcm[2000:] ** 2)) - 0.5 / np.sqrt(2)) < 0.005
    stamps = [ts for _, ts in got]
    assert stamps == sorted(stamps) and stamps[0] == 1_000_000
    assert cap.stats.gaps == 0
    assert cap.close() == []  # second close is a no-op


def test_capture_counts_sample_time_gaps():
    hal = FakeHAL()
    hal.add_process(1, 77)
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None)
    cap.open()
    hal.feed(0, sine(440, 512))
    hal.feed(512, sine(440, 512))
    hal.feed(2048, sine(440, 512))
    assert wait_for(lambda: cap.stats.buffers == 3)
    cap.close()
    assert cap.stats.gaps == 1 and cap.stats.gap_frames == 1024


def test_capture_mixes_multichannel_to_mono():
    hal = FakeHAL(channels=2)
    hal.add_process(1, 77)
    got = []
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: got.append(pcm))
    cap.open()
    left = sine(440, 4800)
    stereo = np.stack([left, -left], axis=1).reshape(-1)
    hal.feed(0, stereo)
    assert wait_for(lambda: cap.stats.buffers == 1)
    cap.close()
    pcm = np.frombuffer(b"".join(got), dtype="<i2")
    assert pcm.size == 1600 and np.max(np.abs(pcm)) <= 1


@pytest.mark.parametrize("step", ["create_aggregate", "create_ioproc", "start"])
def test_capture_releases_everything_when_open_fails(step):
    hal = FakeHAL()
    hal.add_process(1, 77)
    hal.fail[step] = HALError(step, -50)
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None)
    with pytest.raises(HALError):
        cap.open()
    assert hal.clean
    assert "destroy_tap" in hal.names()


def test_capture_rejects_non_float_format_and_destroys_tap():
    hal = FakeHAL()
    hal.add_process(1, 77)
    hal.fmt = hal.fmt.__class__(48000.0, hal.fmt.format_id, 0, 1, 16)
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None)
    with pytest.raises(TapSetupError, match="32-bit float"):
        cap.open()
    assert hal.clean


def test_capture_close_reports_failures_without_raising():
    hal = FakeHAL()
    hal.add_process(1, 77)
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None)
    cap.open()
    hal.fail["destroy_aggregate"] = HALError("destroy aggregate device", -50)
    failures = cap.close()
    assert len(failures) == 1 and "destroy aggregate device" in failures[0]
    assert not hal.live_taps and not hal.live_procs  # later steps still ran


def test_app_mode_needs_a_running_process():
    hal = chrome_hal()
    cap = TapCapture(hal, TapSpec(mode="app", bundle_id="com.example.missing"), lambda pcm, ts: None)
    with pytest.raises(TapSetupError, match="not running"):
        cap.open()
    assert hal.calls == []


def test_app_mode_taps_helpers_and_follows_new_ones():
    hal = chrome_hal()
    cap = TapCapture(hal, TapSpec(mode="app", bundle_id="com.google.Chrome", exclude_pids=(OWN_PID,)),
                     lambda pcm, ts: None)
    cap.open()
    assert hal.calls[0] == ("create_tap", (10, 11), False)
    hal.add_process(20, 120, "com.google.Chrome.helper.Renderer")
    assert cap.refresh() is True
    assert hal.tap_processes[cap.tap_id] == ([10, 11, 20], False)
    assert cap.refresh() is False
    cap.close()
    assert hal.clean


def test_system_mode_excludes_own_processes_and_adds_late_ones():
    hal = chrome_hal()
    cap = TapCapture(hal, TapSpec(mode="system", exclude_pids=(OWN_PID, 5555)), lambda pcm, ts: None)
    cap.open()
    assert hal.calls[0] == ("create_tap", (13,), True)
    hal.add_process(30, 5555)  # the second excluded pid opens audio later
    assert cap.refresh() is True
    assert hal.tap_processes[cap.tap_id] == ([13, 30], True)
    cap.close()


def test_full_queue_drops_and_counts(monkeypatch):
    monkeypatch.setattr(_tap_engine, "QUEUE_LIMIT", 0)
    hal = FakeHAL()
    hal.add_process(1, 77)
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None)
    cap.open()
    hal.feed(0, sine(440, 512))
    cap.close()
    assert cap.stats.dropped_buffers == 1 and cap.stats.buffers == 0


# ---------------------------------------------------------------- worker protocol

class PipeWorker:
    """Runs ``_tap_worker.serve`` on a thread with real pipes, shaped like subprocess.Popen."""

    def __init__(self, hal, own_pid=9999):
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.stdin = os.fdopen(in_w, "wb")
        self.stdout = os.fdopen(out_r, "rb")
        self.stderr = None
        self._in = os.fdopen(in_r, "rb")
        self._out = os.fdopen(out_w, "wb")
        self.returncode = None
        self.pid = own_pid
        self.signals = []

        def run():
            try:
                self.returncode = _tap_worker.serve(self._in, self._out, lambda: hal, own_pid=own_pid,
                                                    refresh_interval=0.05)
            finally:
                self._out.close()
                self._in.close()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("fake worker", timeout)
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


def test_worker_protocol_round_trip():
    hal = FakeHAL()
    hal.add_process(1, 77)
    w = PipeWorker(hal)
    w.stdin.write(json.dumps({"mode": "pid", "pid": 77}).encode() + b"\n")
    w.stdin.flush()
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_READY and json.loads(payload)["sample_rate"] == 48000.0
    hal.feed(0, sine(440, 480), host_time=123)
    kind, ts, pcm = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_AUDIO and ts == 123 and len(pcm) == 160 * 2
    w.stdin.close()
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_STATS and json.loads(payload)["teardown_failures"] == []
    assert w.wait(2) == 0 and hal.clean


def test_worker_adds_itself_to_system_exclusion():
    hal = FakeHAL()
    hal.add_process(1, 9999)  # the worker's own pid
    hal.add_process(2, OWN_PID)
    w = PipeWorker(hal, own_pid=9999)
    w.stdin.write(json.dumps({"mode": "system", "exclude_pids": [OWN_PID]}).encode() + b"\n")
    w.stdin.flush()
    assert _tap_worker.read_frame(w.stdout)[0] == _tap_worker.KIND_READY
    assert hal.calls[0] == ("create_tap", (1, 2), True)
    w.stdin.close()
    assert w.wait(2) == 0


def test_worker_reports_setup_errors():
    hal = FakeHAL()
    w = PipeWorker(hal)
    w.stdin.write(json.dumps({"mode": "pid", "pid": 1}).encode() + b"\n")
    w.stdin.flush()
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_ERROR and "not running" in json.loads(payload)["message"]
    assert w.wait(2) == 2


# ---------------------------------------------------------------- backend

def make_backend(hal, **kw):
    return ProcessTapBackend(lambda: hal, spawn=lambda: PipeWorker(hal), own_pid=OWN_PID, **kw)


def test_backend_satisfies_protocol_and_reports_available():
    b = make_backend(FakeHAL())
    assert isinstance(b, CaptureBackend)
    assert b.name == "process-tap"
    assert b.available() == (True, "Core Audio process taps are available.")


def test_list_sources_groups_helpers_and_hides_daemons():
    sources = make_backend(chrome_hal()).list_sources()
    assert [(s.source_id, s.label, s.detail) for s in sources] == [
        ("system", "All system audio except Hermeneus", ""),
        ("app:com.hnc.Discord", "Discord", ""),
        ("app:com.google.Chrome", "Google Chrome", "playing now"),
    ]


def test_backend_start_delivers_pcm_and_stop_tears_down():
    hal = chrome_hal()
    b = make_backend(hal)
    got = []
    src = CaptureSource("process-tap", "app:com.google.Chrome", "Google Chrome")
    b.start(src, lambda pcm, ts: got.append((pcm, ts)))
    with pytest.raises(CaptureError, match="already running"):
        b.start(src, lambda pcm, ts: None)
    hal.feed(0, sine(440, 4800), host_time=55)
    assert wait_for(lambda: len(got) == 1)
    assert got[0][1] == 55 and len(got[0][0]) == 3200
    b.stop()
    assert hal.clean
    assert b.last_stats["buffers"] == 1 and b.last_stats["worker_exit_code"] == 0
    b.stop()  # safe to call again


def test_backend_start_error_is_capture_error_and_cleans_up():
    hal = chrome_hal()
    hal.fail["create_aggregate"] = HALError("create aggregate device", 1852797029)
    b = make_backend(hal)
    with pytest.raises(CaptureError, match="create aggregate device"):
        b.start(CaptureSource("process-tap", "system", "All"), lambda pcm, ts: None)
    assert hal.clean


def test_backend_start_timeout_kills_worker():
    class Silent(PipeWorker):
        def __init__(self):
            in_r, in_w = os.pipe()
            out_r, out_w = os.pipe()
            self.stdin, self.stdout, self.stderr = os.fdopen(in_w, "wb"), os.fdopen(out_r, "rb"), None
            self._keep = (os.fdopen(in_r, "rb"), os.fdopen(out_w, "wb"))
            self.returncode, self.signals = None, []

        def wait(self, timeout=None):
            if "kill" in self.signals:
                self._keep[1].close()
                self.returncode = -9
                return -9
            raise subprocess.TimeoutExpired("silent", timeout)

    worker = Silent()
    b = ProcessTapBackend(lambda: FakeHAL(), spawn=lambda: worker, own_pid=OWN_PID, start_timeout=0.2)
    old = process_tap.STOP_TIMEOUT_S
    process_tap.STOP_TIMEOUT_S = 0.05
    try:
        with pytest.raises(CaptureError, match="permission"):
            b.start(CaptureSource("process-tap", "pid:1", "x"), lambda pcm, ts: None)
    finally:
        process_tap.STOP_TIMEOUT_S = old
    assert worker.signals == ["terminate", "kill"]


def test_backend_rejects_foreign_and_unknown_sources():
    b = make_backend(FakeHAL())
    with pytest.raises(CaptureError, match="virtual-device"):
        b.start(CaptureSource("virtual-device", "BlackHole 2ch", "BlackHole"), lambda pcm, ts: None)
    with pytest.raises(CaptureError, match="Unknown"):
        b.start(CaptureSource("process-tap", "bogus", "?"), lambda pcm, ts: None)


def test_sink_exceptions_are_counted_not_fatal():
    hal = FakeHAL()
    hal.add_process(1, 77)
    b = make_backend(hal)

    def bad_sink(pcm, ts):
        raise ValueError("boom")

    b.start(CaptureSource("process-tap", "pid:77", "afplay"), bad_sink)
    hal.feed(0, sine(440, 480))
    hal.feed(480, sine(440, 480))
    b.stop()
    assert b.last_stats["sink_errors"] == 2 and hal.clean


def test_atexit_handler_stops_live_backends():
    hal = FakeHAL()
    hal.add_process(1, 77)
    b = make_backend(hal)
    b.start(CaptureSource("process-tap", "pid:77", "afplay"), lambda pcm, ts: None)
    process_tap._stop_all_at_exit()
    assert hal.clean and b not in process_tap._live


def test_unexpected_worker_exit_sets_failure():
    hal = FakeHAL()
    hal.add_process(1, 77)
    workers = []

    def spawn():
        workers.append(PipeWorker(hal))
        return workers[-1]

    b = ProcessTapBackend(lambda: hal, spawn=spawn, own_pid=OWN_PID)
    b.start(CaptureSource("process-tap", "pid:77", "afplay"), lambda pcm, ts: None)
    workers[0].stdin.close()  # the worker sees end of input and exits on its own
    assert wait_for(lambda: b.failure != "")
    assert "stopped unexpectedly" in b.failure
    b.stop()


@pytest.mark.skipif(sys.platform != "darwin", reason="spawns the real worker module")
def test_real_worker_module_starts_and_exits_on_empty_input():
    proc = process_tap.spawn_worker()
    out, err = proc.communicate(b"", timeout=30)
    assert proc.returncode == 0, err.decode()
    assert out == b""
