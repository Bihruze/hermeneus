"""Windows process loopback backend, driven on any platform with the fake WASAPI HAL.

The cases follow tests/test_process_tap.py: conversion, teardown, app and
system targeting, the watchdog, the worker protocol and the backend.
"""

import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from fake_wasapi import FakeWasapi, WasapiError
from hermeneus_engine import subproc
from hermeneus_engine.capture import _loopback_engine, _loopback_worker, _tap_worker, process_loopback
from hermeneus_engine.capture._loopback_engine import (LoopbackCapture, LoopbackSetupError, LoopbackSpec,
                                                       app_roots, process_tree)
from hermeneus_engine.capture.process_loopback import ProcessLoopbackBackend
from hermeneus_engine.contracts import CaptureBackend, CaptureError, CaptureSource

OWN_PID = 4242


def sine(freq, n, rate=48000, amp=0.5):
    t = np.arange(n) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def chrome_hal(**kw):
    hal = FakeWasapi(**kw)
    hal.add_process(1, "explorer.exe", 0, 1.0)
    hal.add_process(100, "chrome.exe", 1, 10.0)          # browser root
    hal.add_process(101, "chrome.exe", 100, 11.0)        # audio service child
    hal.add_process(200, "discord.exe", 1, 20.0)
    hal.add_process(300, "hermes.exe", 1, 30.0)          # Hermes Desktop, parent of Hermeneus
    hal.add_process(OWN_PID, "python.exe", 300, 31.0)
    hal.add_session(101, active=True)
    hal.add_session(200, active=False, name="Discord")
    hal.add_session(OWN_PID, active=True)
    hal.add_session(300, active=False)
    return hal


def collect():
    got = []
    return got, (lambda pcm, ts: got.append((pcm, ts)))


# ---------------------------------------------------------------- process trees

def test_process_tree_follows_children_and_ignores_reused_parent_ids():
    hal = chrome_hal()
    hal.add_process(102, "chrome.exe", 101, 12.0)
    hal.add_process(103, "notepad.exe", 100, 5.0)  # created before pid 100 existed: an older parent's child
    assert process_tree(hal.procs, 100) == {100, 101, 102}


def test_app_roots_pick_the_oldest_root_and_skip_ancestors_of_hermeneus():
    hal = chrome_hal()
    hal.add_process(400, "chrome.exe", 1, 40.0)  # a second chrome instance
    assert [p.pid for p in app_roots(hal.procs, "chrome.exe", OWN_PID)] == [100, 400]
    assert app_roots(hal.procs, "hermes.exe", OWN_PID) == []
    assert app_roots(hal.procs, "python.exe", OWN_PID) == []


# ---------------------------------------------------------------- capture

def test_capture_converts_to_engine_pcm_and_tears_down():
    hal = chrome_hal()
    got, sink = collect()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="app", exe="chrome.exe", own_pid=OWN_PID), sink)
    cap.open(start_pump=False)
    assert hal.calls[0] == ("open_loopback", 100, True) and hal.stream.started
    hal.feed(sine(440, 4800))
    cap.pump_once()
    assert len(got) == 1 and len(got[0][0]) == 1600 * 2
    pcm = np.frombuffer(got[0][0], dtype="<i2").astype(np.float32) / 32768
    assert 0.3 < np.sqrt(np.mean(pcm[400:] ** 2)) / 0.5 * np.sqrt(2) < 1.1
    assert cap.close() == [] and hal.clean
    assert cap.close() == []  # idempotent


def test_int16_stream_is_scaled_and_stereo_is_mixed_to_mono():
    hal = chrome_hal(fmt="int16", rate=44100)
    got, sink = collect()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), sink)
    cap.open(start_pump=False)
    left = sine(1000, 4410, 44100)
    hal.feed(np.stack([left, -left], axis=1))  # opposite channels cancel in the mono mix
    cap.pump_once()
    pcm = np.frombuffer(got[0][0], dtype="<i2")
    assert np.max(np.abs(pcm)) <= 2
    assert cap.stats.sample_format == "int16" and cap.stats.sample_rate == 44100
    cap.close()


def test_silent_and_discontinuous_packets_keep_the_stream_length():
    hal = chrome_hal()
    got, sink = collect()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), sink)
    cap.open(start_pump=False)
    hal.feed(np.zeros(480), silent=True)
    hal.feed(sine(440, 480), discontinuity=True)
    cap.pump_once()
    assert sum(len(p) for p, _ in got) // 2 == cap.stats.samples_out
    assert cap.stats.silent_packets == 1 and cap.stats.discontinuities == 1
    cap.close()


def test_timestamps_come_from_the_performance_counter(monkeypatch):
    hal = chrome_hal()
    got, sink = collect()
    now = [5_000_000_000]
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), sink, clock_ns=lambda: now[0])
    cap.open(start_pump=False)
    hal.feed(sine(440, 4800), age_100ns=200_000)  # first frame recorded 20 ms ago
    cap.pump_once()
    assert cap.stats.timestamp_source == "qpc"
    delay = int(cap._resampler.delay_seconds * 1e9)
    assert abs(got[0][1] - (now[0] - 20_000_000 - delay)) < 100_000
    cap.close()


def test_timestamps_fall_back_to_arrival_time_without_a_counter():
    hal = chrome_hal()
    got, sink = collect()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), sink, clock_ns=lambda: 1_000_000_000)
    cap.open(start_pump=False)
    hal.feed(sine(440, 4800), qpc=0)
    cap.pump_once()
    assert cap.stats.timestamp_source == "arrival" and got[0][1] < 1_000_000_000 - 99_000_000
    cap.close()


def test_open_failure_leaves_nothing_open():
    hal = chrome_hal()
    hal.fail["start"] = WasapiError("IAudioClient::Start failed (0x88890004)")
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda pcm, ts: None)
    with pytest.raises(LoopbackSetupError, match="0x88890004"):
        cap.open(start_pump=False)
    assert hal.clean


def test_activation_failure_is_a_setup_error():
    hal = chrome_hal()
    hal.fail["open_loopback"] = WasapiError("process loopback activation failed: activation returned 0x80070005")
    cap = LoopbackCapture(hal, LoopbackSpec(mode="system", exclude_pid=OWN_PID), lambda pcm, ts: None)
    with pytest.raises(LoopbackSetupError, match="refused"):
        cap.open(start_pump=False)


def test_unreadable_format_is_refused_and_closed():
    hal = chrome_hal(fmt="int24")
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda pcm, ts: None)
    with pytest.raises(LoopbackSetupError, match="format"):
        cap.open(start_pump=False)
    assert hal.clean


def test_close_reports_failures_without_raising():
    hal = chrome_hal()
    hal.close_failures = ["stop audio client: 0x88890004"]
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda pcm, ts: None)
    cap.open(start_pump=False)
    assert cap.close() == ["stop audio client: 0x88890004"]


def test_app_mode_needs_a_running_process():
    hal = chrome_hal()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="app", exe="spotify.exe", own_pid=OWN_PID), lambda p, t: None)
    with pytest.raises(LoopbackSetupError, match="spotify.exe is not running"):
        cap.open(start_pump=False)
    cap2 = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=999), lambda p, t: None)
    with pytest.raises(LoopbackSetupError, match="999 is not running"):
        cap2.open(start_pump=False)


def test_system_mode_excludes_the_hermeneus_tree():
    hal = chrome_hal()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="system", exclude_pid=OWN_PID), lambda p, t: None)
    cap.open(start_pump=False)
    assert hal.calls[0] == ("open_loopback", OWN_PID, False)
    cap.close()


def test_app_restart_moves_capture_to_the_new_process():
    hal = chrome_hal()
    statuses = []
    cap = LoopbackCapture(hal, LoopbackSpec(mode="app", exe="chrome.exe", own_pid=OWN_PID), lambda p, t: None,
                          on_status=lambda c, m, d: statuses.append((c, d)))
    cap.open(start_pump=False)
    hal.remove_process(100)
    hal.remove_process(101)
    cap.tick(1.0)  # chrome is gone: keep the stream, it delivers silence
    assert len(hal.streams) == 1 and not hal.stream.closed
    hal.add_process(500, "chrome.exe", 1, 50.0)
    cap.tick(2.0)
    assert hal.streams[0].closed and hal.stream.target == 500
    assert statuses == [("retargeted", {"old_pid": 100, "new_pid": 500})]
    cap.close()
    assert hal.clean


def test_watchdog_rebuilds_a_silent_stream_while_audio_plays_and_reports_no_audio():
    hal = chrome_hal()
    statuses = []
    cap = LoopbackCapture(hal, LoopbackSpec(mode="app", exe="chrome.exe", own_pid=OWN_PID), lambda p, t: None,
                          on_status=lambda c, m, d: statuses.append(c), silence_rebuild_s=1.0, max_rebuilds=3)
    cap.open(start_pump=False)
    for now in (0.0, 1.5, 4.0, 9.0, 18.0):
        cap.tick(now)
    assert statuses == ["rebuilt", "rebuilt", "no-audio", "rebuilt"]
    assert len(hal.streams) == 4 and all(s.closed for s in hal.streams[:-1])
    hal.feed(sine(440, 480, amp=0.2))
    cap.pump_once()
    cap.tick(19.0)
    assert statuses[-1] == "recovered"
    cap.close()


def test_watchdog_is_quiet_when_nothing_covered_is_playing():
    hal = chrome_hal()
    hal.session_list = [s for s in hal.session_list if s.pid != 101]
    statuses = []
    cap = LoopbackCapture(hal, LoopbackSpec(mode="app", exe="chrome.exe", own_pid=OWN_PID), lambda p, t: None,
                          on_status=lambda c, m, d: statuses.append(c), silence_rebuild_s=1.0)
    cap.open(start_pump=False)
    for now in (0.0, 5.0, 50.0):
        cap.tick(now)
    assert statuses == [] and len(hal.streams) == 1
    cap.close()


def test_system_watchdog_ignores_hermeneus_own_session():
    hal = chrome_hal()
    hal.session_list = [s for s in hal.session_list if s.pid == OWN_PID]  # only our speech is playing
    assert not _loopback_engine.audio_expected(hal, LoopbackSpec(mode="system", exclude_pid=OWN_PID), OWN_PID)


def test_stream_error_is_reopened_and_reported():
    hal = chrome_hal()
    statuses = []
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda p, t: None,
                          on_status=lambda c, m, d: statuses.append(c))
    cap.open(start_pump=False)
    hal.stream.fail_read = WasapiError("reading the loopback stream failed (0x88890004)")
    cap.pump_once()
    assert cap.stats.stream_errors == 1 and hal.streams[0].closed
    cap.tick(0.0)
    assert statuses == ["rebuilt"] and len(hal.streams) == 2 and hal.stream.started
    cap.close()


def test_pump_thread_delivers_and_stops():
    hal = chrome_hal()
    got, sink = collect()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), sink, refresh_interval=0.05)
    cap.open()
    hal.feed(sine(440, 4800))
    assert wait_for(lambda: len(got) == 1)
    cap.close()
    assert hal.clean and cap._pump is None


# ---------------------------------------------------------------- worker protocol

class PipeWorker:
    """Runs ``_loopback_worker.serve`` on a thread with real pipes, shaped like subprocess.Popen."""

    def __init__(self, hal, own_pid=9999):
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.stdin = os.fdopen(in_w, "wb")
        self.stdout = os.fdopen(out_r, "rb")
        self.stderr = None
        self._in = os.fdopen(in_r, "rb")
        self._out = os.fdopen(out_w, "wb")
        self.returncode = None
        self.signals = []

        def run():
            try:
                self.returncode = _loopback_worker.serve(self._in, self._out, lambda: hal, own_pid=own_pid,
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


def send(w, obj):
    w.stdin.write(json.dumps(obj).encode() + b"\n")
    w.stdin.flush()


def test_worker_protocol_round_trip():
    hal = chrome_hal()
    w = PipeWorker(hal)
    send(w, {"mode": "pid", "pid": 200})
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_READY
    assert json.loads(payload) == {"sample_rate": 48000, "channels": 2, "format": "float32", "target_pid": 200}
    hal.feed(sine(440, 480))
    kind, _, pcm = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_AUDIO and len(pcm) > 0
    w.stdin.close()
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_STATS and json.loads(payload)["teardown_failures"] == []
    assert w.wait(2) == 0 and hal.clean


def test_worker_lists_apps_and_leaves_out_hermeneus_and_its_ancestors():
    hal = chrome_hal()
    w = PipeWorker(hal)
    send(w, {"mode": "list", "own_pid": OWN_PID})
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _loopback_worker.KIND_LIST
    reply = json.loads(payload)
    assert reply["build"] == 22631
    assert sorted((a["exe"], a["label"], a["playing"]) for a in reply["apps"]) == [
        ("chrome.exe", "chrome", True), ("discord.exe", "Discord", False)]
    assert w.wait(2) == 0


def test_worker_reports_setup_and_request_errors():
    w = PipeWorker(chrome_hal())
    send(w, {"mode": "app", "exe": "spotify.exe", "own_pid": OWN_PID})
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_ERROR and "not running" in json.loads(payload)["message"]
    assert w.wait(2) == 2
    w = PipeWorker(chrome_hal())
    send(w, {"mode": "system"})
    kind, _, payload = _tap_worker.read_frame(w.stdout)
    assert kind == _tap_worker.KIND_ERROR and "exclude_pid" in json.loads(payload)["message"]
    assert w.wait(2) == 2


def test_worker_main_refuses_to_run_off_windows():
    if sys.platform == "win32":
        pytest.skip("checks the guard that only runs off Windows")
    proc = subprocess.run([sys.executable, "-m", "hermeneus_engine.capture._loopback_worker"], input=b"",
                          capture_output=True, timeout=30, env=process_loopback.worker_env(),
                          cwd=str(subproc.PACKAGE_ROOT))
    assert proc.returncode == 2
    kind, _, payload = _tap_worker.read_frame(__import__("io").BytesIO(proc.stdout))
    assert kind == _tap_worker.KIND_ERROR and "needs Windows" in json.loads(payload)["message"]


# ---------------------------------------------------------------- backend

def make_backend(hal, **kw):
    kw.setdefault("build", lambda: 22631)
    kw.setdefault("has_module", lambda name: True)
    return ProcessLoopbackBackend(spawn=lambda: PipeWorker(hal), own_pid=OWN_PID, **kw)


def test_backend_satisfies_protocol_and_reports_available():
    b = make_backend(chrome_hal())
    assert isinstance(b, CaptureBackend)
    assert b.name == "process-loopback"
    assert b.available() == (True, "WASAPI process loopback is available.")


@pytest.mark.parametrize("build,missing,expected", [
    (None, (), "needs Windows; this is not Windows"),
    (19045, (), "build 20348 or later; this is build 19045"),
    (22631, ("pycaw",), "Missing Python packages: pycaw"),
])
def test_backend_availability_reasons(build, missing, expected):
    b = ProcessLoopbackBackend(build=lambda: build, has_module=lambda name: name not in missing)
    ok, reason = b.available()
    assert not ok and expected in reason


def test_list_sources_offers_system_and_apps():
    sources = make_backend(chrome_hal()).list_sources()
    assert [(s.source_id, s.label, s.detail) for s in sources] == [
        ("system", "All system audio except Hermeneus", ""),
        ("app:chrome.exe", "chrome", "playing now"),
        ("app:discord.exe", "Discord", ""),
    ]


def test_list_sources_falls_back_to_system_when_listing_fails():
    def spawn():
        raise OSError("no python")
    b = ProcessLoopbackBackend(spawn=spawn, own_pid=OWN_PID, build=lambda: 22631, has_module=lambda n: True)
    assert [s.source_id for s in b.list_sources()] == ["system"]


def test_backend_start_delivers_pcm_and_stop_tears_down():
    hal = chrome_hal()
    b = make_backend(hal)
    got, sink = collect()
    src = CaptureSource("process-loopback", "app:chrome.exe", "chrome")
    b.start(src, sink)
    with pytest.raises(CaptureError, match="already running"):
        b.start(src, lambda pcm, ts: None)
    assert hal.calls[0] == ("open_loopback", 100, True)
    hal.feed(sine(440, 4800))
    assert wait_for(lambda: len(got) == 1)
    b.stop()
    assert hal.clean
    assert b.last_stats["packets"] == 1 and b.last_stats["worker_exit_code"] == 0
    b.stop()


def test_backend_system_source_excludes_this_process():
    hal = chrome_hal()
    b = make_backend(hal)
    b.start(CaptureSource("process-loopback", "system", "All"), lambda pcm, ts: None)
    assert hal.calls[0] == ("open_loopback", OWN_PID, False)
    b.stop()


def test_backend_start_error_is_capture_error_and_cleans_up():
    hal = chrome_hal()
    hal.fail["start"] = WasapiError("IAudioClient::Start failed (0x88890004)")
    b = make_backend(hal)
    with pytest.raises(CaptureError, match="0x88890004"):
        b.start(CaptureSource("process-loopback", "system", "All"), lambda pcm, ts: None)
    assert hal.clean


def test_backend_start_timeout_terminates_worker(monkeypatch):
    class Silent(PipeWorker):
        def __init__(self):
            in_r, in_w = os.pipe()
            out_r, out_w = os.pipe()
            self.stdin, self.stdout, self.stderr = os.fdopen(in_w, "wb"), os.fdopen(out_r, "rb"), None
            self._keep = (os.fdopen(in_r, "rb"), os.fdopen(out_w, "wb"))
            self.returncode, self.signals = None, []

        def wait(self, timeout=None):
            if "terminate" in self.signals:
                self._keep[1].close()
                self.returncode = 1
                return 1
            raise subprocess.TimeoutExpired("silent", timeout)

    worker = Silent()
    monkeypatch.setattr(process_loopback, "STOP_TIMEOUT_S", 0.05)
    b = ProcessLoopbackBackend(spawn=lambda: worker, own_pid=OWN_PID, start_timeout=0.2, build=lambda: 22631,
                               has_module=lambda n: True)
    with pytest.raises(CaptureError, match="did not start"):
        b.start(CaptureSource("process-loopback", "pid:1", "x"), lambda pcm, ts: None)
    assert worker.signals == ["terminate"]


def test_backend_rejects_foreign_and_unknown_sources():
    b = make_backend(chrome_hal())
    with pytest.raises(CaptureError, match="virtual-device"):
        b.start(CaptureSource("virtual-device", "CABLE Output", "CABLE"), lambda pcm, ts: None)
    with pytest.raises(CaptureError, match="Unknown"):
        b.start(CaptureSource("process-loopback", "bogus", "?"), lambda pcm, ts: None)
    with pytest.raises(CaptureError, match="valid process id"):
        b.start(CaptureSource("process-loopback", "pid:-3", "?"), lambda pcm, ts: None)


def test_sink_exceptions_are_counted_not_fatal():
    hal = chrome_hal()
    b = make_backend(hal)

    def bad_sink(pcm, ts):
        raise ValueError("boom")

    b.start(CaptureSource("process-loopback", "pid:200", "discord"), bad_sink)
    hal.feed(sine(440, 4800))
    assert wait_for(lambda: hal.stream._packets == [])
    time.sleep(0.05)
    hal.feed(sine(440, 4800))
    time.sleep(0.1)
    b.stop()
    assert b.last_stats["sink_errors"] == 2 and hal.clean


def test_status_frames_reach_status_message():
    hal = chrome_hal()
    seen = []
    b = make_backend(hal, on_status=lambda c, m, d: seen.append(c))
    b._handle_status(json.dumps({"code": "rebuilt", "message": "the stream was rebuilt", "details": {}}).encode())
    assert b.status_message == "the stream was rebuilt" and b.failure == ""
    b._handle_status(json.dumps({"code": "recovered", "message": "back", "details": {}}).encode())
    assert b.status_message == "" and seen == ["rebuilt", "recovered"]


def test_atexit_handler_stops_live_backends():
    hal = chrome_hal()
    b = make_backend(hal)
    b.start(CaptureSource("process-loopback", "pid:200", "discord"), lambda pcm, ts: None)
    process_loopback._stop_all_at_exit()
    assert hal.clean and b not in process_loopback._live


def test_unexpected_worker_exit_sets_failure():
    hal = chrome_hal()
    workers = []

    def spawn():
        workers.append(PipeWorker(hal))
        return workers[-1]

    b = ProcessLoopbackBackend(spawn=spawn, own_pid=OWN_PID, build=lambda: 22631, has_module=lambda n: True)
    b.start(CaptureSource("process-loopback", "pid:200", "discord"), lambda pcm, ts: None)
    workers[0].stdin.close()
    assert wait_for(lambda: b.failure != "")
    assert "stopped unexpectedly" in b.failure
    b.stop()


def test_worker_env_is_minimal_and_keeps_systemroot():
    env = process_loopback.worker_env({"SystemRoot": r"C:\Windows", "Path": r"C:\Python", "TEMP": r"C:\t",
                                       "ELEVENLABS_API_KEY": "secret", "OPENROUTER_API_KEY": "x"}, path=[])
    assert env["SYSTEMROOT"] == r"C:\Windows" and env["PATH"] == r"C:\Python" and env["TEMP"] == r"C:\t"
    assert "ELEVENLABS_API_KEY" not in env and "OPENROUTER_API_KEY" not in env
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(subproc.PACKAGE_ROOT)


@pytest.mark.skipif(__import__("ctypes").sizeof(__import__("ctypes").c_void_p) != 8, reason="64-bit layout")
def test_activation_structures_match_the_windows_x64_layout():
    import ctypes
    from hermeneus_engine.capture import _wasapi as w
    # PROPVARIANT is 24 bytes on 64-bit Windows with the BLOB {ULONG cbSize; BYTE *pBlobData} at offset 8.
    assert ctypes.sizeof(w._PropVariant) == 24 and w._PropVariant.data.offset == 8
    assert w._Blob.cbSize.offset == 0 and w._Blob.pBlobData.offset == 8
    # AUDIOCLIENT_ACTIVATION_PARAMS: ActivationType, then {TargetProcessId, ProcessLoopbackMode}.
    params = w._LoopbackParams(w.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, 1234,
                               w.PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
    assert bytes(params).hex() == "01000000" "d2040000" "01000000"
