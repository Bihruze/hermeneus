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


# ---------------------------------------------------------------- silence watchdog and format changes

from hermeneus_engine.capture import _tap_engine as eng  # noqa: E402


def watchdog_capture(hal, **kw):
    statuses = []
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None,
                     on_status=lambda c, m, d: statuses.append(c), refresh_interval=1000, **kw)
    cap.open()
    return cap, statuses


def feed_and_wait(hal, cap, samples, n=1):
    before = cap.stats.buffers
    for _ in range(n):
        hal.feed(0, samples)
    assert wait_for(lambda: cap.stats.buffers >= before + n)


def test_watchdog_reports_no_audio_when_denied_and_never_heard():
    hal = FakeHAL()
    hal.add_process(1, 77, playing=True)
    cap, statuses = watchdog_capture(hal)
    zeros = np.zeros(512, dtype=np.float32)
    feed_and_wait(hal, cap, zeros)
    cap.tick(0.0)
    feed_and_wait(hal, cap, zeros)
    cap.tick(3.0)  # 3 s of silence while playing: first rebuild
    assert statuses == [eng.STATUS_REBUILT] and cap.stats.silent_rebuilds == 1
    feed_and_wait(hal, cap, zeros)
    cap.tick(8.9)
    assert cap.stats.silent_rebuilds == 1  # the second wait is doubled to 6 s
    feed_and_wait(hal, cap, zeros)
    cap.tick(9.0)
    assert statuses == [eng.STATUS_REBUILT, eng.STATUS_REBUILT, eng.STATUS_NO_AUDIO]
    assert [c[0] for c in hal.calls].count("create_tap") == 3
    cap.close()
    assert hal.clean


def test_watchdog_ignores_silence_when_nothing_plays():
    hal = FakeHAL()
    hal.add_process(1, 77, playing=False)
    cap, statuses = watchdog_capture(hal)
    for t in (0.0, 5.0, 50.0):
        feed_and_wait(hal, cap, np.zeros(512, dtype=np.float32))
        cap.tick(t)
    assert statuses == [] and cap.stats.rebuilds == 0
    cap.close()


def test_watchdog_rebuilds_a_stalled_tap_then_reports_after_the_cap():
    hal = FakeHAL()
    hal.add_process(1, 77, playing=True)
    cap, statuses = watchdog_capture(hal, max_rebuilds=2)
    feed_and_wait(hal, cap, sine(440, 512))
    cap.tick(0.0)  # audio heard
    t = 1.0
    for _ in range(6):
        feed_and_wait(hal, cap, np.zeros(512, dtype=np.float32))
        cap.tick(t)
        t += 20.0
    assert statuses == [eng.STATUS_REBUILT, eng.STATUS_REBUILT, eng.STATUS_STALLED]
    feed_and_wait(hal, cap, sine(440, 512))
    cap.tick(t)
    assert statuses[-1] == eng.STATUS_RECOVERED
    cap.close()
    assert hal.clean


def test_rate_change_rebuilds_with_a_new_resampler():
    hal = FakeHAL()
    hal.add_process(1, 77)
    got = []
    statuses = []
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: got.append(pcm),
                     on_status=lambda c, m, d: statuses.append((c, d)), refresh_interval=1000)
    cap.open()
    hal.fmt = hal.fmt.__class__(44100.0, hal.fmt.format_id, hal.fmt.format_flags, 1, 32)
    cap.tick(0.0)
    assert statuses == [(eng.STATUS_FORMAT_CHANGED, {"old_rate": 48000, "new_rate": 44100})]
    assert cap.stats.format_rebuilds == 1
    got.clear()
    hal.feed(0, sine(440, 4410, rate=44100))
    assert wait_for(lambda: cap.stats.buffers >= 1)
    cap.close()
    assert sum(len(p) for p in got) // 2 == 1600


def test_backend_turns_no_audio_status_into_a_permission_message():
    hal = FakeHAL()
    hal.add_process(1, 77, playing=True)
    seen = []
    b = ProcessTapBackend(lambda: hal, spawn=lambda: PipeWorker(hal), own_pid=OWN_PID,
                          on_status=lambda c, m, d: seen.append((c, m)), responsible_app=lambda: "Terminal")
    b.start(CaptureSource("process-tap", "pid:77", "afplay"), lambda pcm, ts: None)
    run = b._run
    b._handle_status(run, json.dumps({"code": "no-audio", "message": "x", "details": {}}).encode())
    b.stop()
    code, message = seen[0]
    assert code == "no-audio" and b.failure == message
    assert "Screen & System Audio Recording" in message and "'Terminal'" in message


# ---------------------------------------------------------------- doctor

from hermeneus_engine.capture import tap_doctor  # noqa: E402


class FakePlayer:
    def __init__(self, hal, feed):
        self.pid = 77
        self.killed = False
        self.began = False
        self._hal, self._feed = hal, feed

    def begin(self):
        self.began = True
        threading.Thread(target=lambda: (time.sleep(0.2), self._feed(self._hal)), daemon=True).start()

    def kill(self):
        self.killed = True


def doctor_with(hal, feed):
    hal.add_process(1, 77, playing=True)
    players = []

    def factory():
        players.append(FakePlayer(hal, feed))
        return players[-1]

    result = tap_doctor.check_process_tap(
        backend_factory=lambda **kw: ProcessTapBackend(lambda: hal, spawn=lambda: PipeWorker(hal), own_pid=OWN_PID, **kw),
        player_factory=factory, app_name=lambda: "Terminal")
    assert players and players[0].killed
    return result


def test_doctor_passes_on_the_test_tone():
    r = doctor_with(FakeHAL(), lambda hal: [hal.feed(i * 4800, sine(440, 4800, amp=0.1, start=i * 4800))
                                            for i in range(10)])
    assert r.ok and r.code == "ok", r
    assert -25 < r.details["rms_dbfs"] < -21


def test_doctor_reports_denied_permission_with_the_pane():
    r = doctor_with(FakeHAL(), lambda hal: [hal.feed(i * 4800, np.zeros(4800, dtype=np.float32)) for i in range(10)])
    assert not r.ok and r.code == "permission-denied"
    assert "System Settings > Privacy & Security > Screen & System Audio Recording" in r.message
    assert "'Terminal'" in r.message


def test_doctor_rejects_audio_that_is_not_the_tone():
    rng = np.random.default_rng(1)
    r = doctor_with(FakeHAL(), lambda hal: [hal.feed(i * 4800, (rng.standard_normal(4800) * 0.1).astype(np.float32))
                                            for i in range(10)])
    assert not r.ok and r.code == "wrong-audio"


def test_doctor_mutes_the_tapped_player():
    hal = FakeHAL()
    doctor_with(hal, lambda h: [h.feed(i * 4800, sine(440, 4800, amp=0.1, start=i * 4800)) for i in range(10)])
    assert hal.muted is True


def test_doctor_reports_start_failure():
    hal = FakeHAL()
    hal.fail["create_tap"] = HALError("create process tap", 2003332927)
    r = doctor_with(hal, lambda hal: None)
    assert not r.ok and r.code == "start-failed" and "create process tap" in r.message


# ---------------------------------------------------------------- worker environment and doctor seam

def test_worker_env_carries_no_secrets():
    parent = {"PATH": "/usr/bin", "HOME": "/Users/x", "TMPDIR": "/tmp/x", "LANG": "en_US.UTF-8",
              "ELEVENLABS_API_KEY": "sk-1", "OPENROUTER_API_KEY": "sk-2", "GITHUB_TOKEN": "t", "HERMES_SECRET": "s",
              "ANTHROPIC_AUTH_TOKEN": "a", "AWS_SECRET_ACCESS_KEY": "k", "PYTHONPATH": "/elsewhere"}
    env = process_tap.worker_env(parent, path=[])
    assert not [k for k in env if k.endswith(("_KEY", "_TOKEN", "_SECRET")) or "SECRET" in k or "TOKEN" in k]
    assert set(env) <= set(process_tap.WORKER_ENV_ALLOWLIST) | {"PYTHONPATH", "PYTHONUNBUFFERED"}
    assert env["PYTHONPATH"] == str(process_tap._PACKAGE_ROOT) and env["PATH"] == "/usr/bin"


def test_worker_path_carries_the_parent_sys_path(tmp_path):
    deps = tmp_path / "plugin-env" / "site-packages"
    deps.mkdir(parents=True)
    env = process_tap.worker_env({}, path=["", str(deps), str(tmp_path / "missing")])
    assert env["PYTHONPATH"].split(os.pathsep) == [str(process_tap._PACKAGE_ROOT), str(deps)]


def test_spawn_worker_passes_the_minimal_env(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    monkeypatch.setenv("SOME_PROVIDER_TOKEN", "tok")
    seen = {}

    def fake_popen(args, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(process_tap.subprocess, "Popen", fake_popen)
    process_tap.spawn_worker()
    assert "ELEVENLABS_API_KEY" not in seen["env"] and "SOME_PROVIDER_TOKEN" not in seen["env"]


@pytest.mark.skipif(sys.platform != "darwin", reason="spawns the real worker module")
def test_real_worker_sees_no_secrets(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    env = process_tap.worker_env()
    out = subprocess.run([sys.executable, "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
                         env=env, capture_output=True, text=True, timeout=30).stdout
    names = json.loads(out)
    assert not [k for k in names if k.endswith(("_KEY", "_TOKEN", "_SECRET"))]


def test_status_message_and_failure_follow_worker_status():
    hal = FakeHAL()
    hal.add_process(1, 77)
    b = make_backend(hal)
    b.start(CaptureSource("process-tap", "pid:77", "afplay"), lambda pcm, ts: None)
    b._handle_status(b._run, json.dumps({"code": "rebuilt", "message": "tap rebuilt", "details": {}}).encode())
    assert b.status_message == "tap rebuilt" and b.failure == ""
    b._handle_status(b._run, json.dumps({"code": "recovered", "message": "back", "details": {}}).encode())
    assert b.status_message == ""
    b._handle_status(b._run, json.dumps({"code": "stalled", "message": "x", "details": {}}).encode())
    assert "stopped receiving audio" in b.failure
    b.stop()


def test_verify_seam_is_wired(monkeypatch):
    from hermeneus_engine import components
    fn = components.load("capture.verify_tap_audio")
    monkeypatch.setattr(tap_doctor, "check_process_tap",
                        lambda timeout_s=30.0: tap_doctor.DoctorResult(False, "permission-denied", "allow it"))
    assert fn() == (False, "allow it")


# ---------------------------------------------------------------- PortAudio reader path

class FakePAStream:
    def __init__(self, rate):
        self.rate = rate
        self.latency_s = 0.05
        self.blocks = queue_mod.Queue()
        self.closed = False

    def read(self, frames=512, stop=None):
        while True:
            try:
                item = self.blocks.get(timeout=0.01)
            except queue_mod.Empty:
                if stop is not None and stop.is_set():
                    return np.zeros(0, dtype=np.float32), False
                continue
            if item is None:
                raise RuntimeError("stream closed")
            return item

    def close(self):
        self.closed = True
        self.blocks.put(None)


class FakePA:
    def __init__(self):
        self.opened = []

    def open(self, name, rate):
        s = FakePAStream(rate)
        self.opened.append((name, rate, s))
        return s


import queue as queue_mod  # noqa: E402


def pa_capture(hal, got, **kw):
    pa = FakePA()
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: got.append((pcm, ts)),
                     reader=eng.READER_PORTAUDIO, pa_factory=lambda: pa, refresh_interval=1000, **kw)
    cap.open()
    return cap, pa


def test_portaudio_reader_converts_and_counts_overflows():
    hal = FakeHAL()
    hal.add_process(1, 77)
    got = []
    cap, pa = pa_capture(hal, got)
    name, rate, stream = pa.opened[0]
    assert name.startswith("Hermeneus capture ") and rate == 48000
    assert "create_ioproc" not in hal.names()
    t0 = time.monotonic_ns()
    for i in range(10):
        stream.blocks.put((sine(440, 4800, start=i * 4800), i == 3))
    assert wait_for(lambda: cap.stats.buffers == 10)
    assert cap.close() == []
    assert stream.closed and hal.clean
    pcm = np.frombuffer(b"".join(p for p, _ in got), dtype="<i2")
    assert pcm.size == 16000 and cap.stats.overflows == 1
    assert got[0][1] < t0  # stamped before arrival: block length plus reported latency


def test_portaudio_reader_is_reopened_on_rebuild():
    hal = FakeHAL()
    hal.add_process(1, 77, playing=True)
    got = []
    cap, pa = pa_capture(hal, got)
    assert cap.rebuild("silence")
    assert len(pa.opened) == 2 and pa.opened[0][2].closed and not pa.opened[1][2].closed
    assert pa.opened[0][0] != pa.opened[1][0]  # a fresh aggregate name each time
    pa.opened[0][2].blocks.put((sine(440, 4800), False))  # late block from the old stream is ignored
    pa.opened[1][2].blocks.put((sine(440, 4800), False))
    assert wait_for(lambda: cap.stats.buffers == 1)
    cap.close()
    assert hal.clean


# ---------------------------------------------------------------- device changes and app restarts

def test_default_output_change_rebuilds_and_reports():
    hal = FakeHAL()
    hal.add_process(1, 77)
    statuses = []
    cap = TapCapture(hal, TapSpec(mode="pid", pid=77), lambda pcm, ts: None,
                     on_status=lambda c, m, d: statuses.append((c, m)), refresh_interval=1000)
    cap.open()
    cap.tick(0.0)  # records the device state
    assert cap.stats.rebuilds == 0
    hal.default_out = 51
    cap.tick(1.0)
    assert statuses == [(eng.STATUS_DEVICES_CHANGED, "The output device changed to BlackHole 2ch; capture was rebuilt.")]
    assert cap.stats.device_rebuilds == 1
    cap.tick(2.0)
    assert cap.stats.device_rebuilds == 1  # no change, no rebuild
    hal.devices.append(52)  # headphones connected
    cap.tick(3.0)
    assert statuses[-1] == (eng.STATUS_DEVICES_CHANGED, "The list of audio devices changed; capture was rebuilt.")
    cap.close()
    assert hal.clean and [c[0] for c in hal.calls].count("create_tap") == 3


def test_app_restart_reattaches_with_a_status():
    hal = chrome_hal()
    statuses = []
    cap = TapCapture(hal, TapSpec(mode="app", bundle_id="com.hnc.Discord", exclude_pids=(OWN_PID,)),
                     lambda pcm, ts: None, on_status=lambda c, m, d: statuses.append(c), refresh_interval=1000)
    cap.open()
    del hal.processes[14]  # Discord quits
    assert cap.refresh() is False  # nothing to tap: the tap keeps its last processes
    hal.add_process(40, 140, "com.hnc.Discord", app=AppInfo("Discord", True))  # and starts again
    hal.add_process(41, 141, "com.hnc.Discord.helper")
    assert cap.refresh() is True
    assert statuses == [eng.STATUS_REATTACHED] and cap.stats.reattaches == 1
    assert hal.tap_processes[cap.tap_id] == ([40, 41], False)
    cap.close()


def test_backend_shows_device_and_reattach_notes_without_failing():
    hal = FakeHAL()
    hal.add_process(1, 77)
    b = make_backend(hal)
    b.start(CaptureSource("process-tap", "pid:77", "afplay"), lambda pcm, ts: None)
    for code in ("devices-changed", "reattached"):
        b._handle_status(b._run, json.dumps({"code": code, "message": f"note {code}", "details": {}}).encode())
        assert b.status_message == f"note {code}" and b.failure == ""
    b.stop()


# ---------------------------------------------------------------- doctor tone routing

def test_doctor_prefers_a_two_channel_virtual_output():
    devs = [{"name": "MacBook Air Speakers", "max_output_channels": 2}, {"name": "BlackHole 16ch", "max_output_channels": 16},
            {"name": "BlackHole 2ch", "max_output_channels": 2}, {"name": "Microphone", "max_output_channels": 0}]
    assert tap_doctor.virtual_output_device(lambda: devs) == "BlackHole 2ch"
    assert tap_doctor.virtual_output_device(lambda: devs[:2]) == "BlackHole 16ch"
    assert tap_doctor.virtual_output_device(lambda: [devs[0], {"name": "VB-Cable", "max_output_channels": 2}]) == "VB-Cable"
    assert tap_doctor.virtual_output_device(lambda: devs[:1]) is None


def test_default_player_uses_the_quiet_tone_without_a_virtual_device(monkeypatch):
    made = []
    monkeypatch.setattr(tap_doctor, "TonePlayer", lambda device=None, amplitude=None: made.append((device, amplitude)) or object())
    monkeypatch.setattr(tap_doctor, "virtual_output_device", lambda: None)
    tap_doctor.default_player()
    monkeypatch.setattr(tap_doctor, "virtual_output_device", lambda: "BlackHole 2ch")
    tap_doctor.default_player()
    assert made == [(None, tap_doctor.AUDIBLE_AMPLITUDE), ("BlackHole 2ch", tap_doctor.VIRTUAL_AMPLITUDE)]
    assert 20 * np.log10(tap_doctor.AUDIBLE_AMPLITUDE / np.sqrt(2)) > tap_doctor.MIN_RMS_DBFS + 6


def test_doctor_passes_on_the_quiet_tone_and_says_it_may_be_heard():
    r = doctor_with(FakeHAL(), lambda hal: [hal.feed(i * 4800, sine(440, 4800, amp=tap_doctor.AUDIBLE_AMPLITUDE, start=i * 4800))
                                            for i in range(10)])
    assert r.ok and "may have heard it" in r.message and "--no-audio-check" in r.message


def test_doctor_run_skips_the_audio_check_on_request():
    from hermeneus_engine import doctor
    called = []
    report = doctor.run(get_secret=lambda n: None, aux_config=None, mac_ver=lambda: ("27.0.1", "", ""),
                        which=lambda n: None, verify_tap_audio=lambda: called.append(1) or (True, "x"), audio_check=False)
    perm = [c for c in report.checks if c.name == "permission: system audio recording"]
    assert called == [] and perm and perm[0].ok is None and "--no-audio-check" in perm[0].detail


def test_doctor_failure_messages_also_mention_the_tone():
    r = doctor_with(FakeHAL(), lambda hal: [hal.feed(i * 4800, np.zeros(4800, dtype=np.float32)) for i in range(10)])
    assert r.code == "permission-denied" and "test tone" in r.message
