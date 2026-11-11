"""Fixes from the A4 and A5 reviews of the Windows preview (W1 to W17).

Each test fails on the code the reviewers read (w24-windows before this round)
and passes after the fix. They run on any platform with fakes.
"""

import ctypes
import json
import os
import sys
import threading
import time

import numpy as np
import pytest

from fake_wasapi import FakeWasapi
from hermeneus_engine import audio_devices, components, doctor_windows, options, platform_words
from hermeneus_engine._windows import process as winproc
from hermeneus_engine import subproc
from hermeneus_engine.capture import _loopback_engine, _wasapi, process_loopback
from hermeneus_engine.capture._loopback_engine import LoopbackCapture, LoopbackSetupError, LoopbackSpec
from hermeneus_engine.capture.process_loopback import ProcessLoopbackBackend
from hermeneus_engine.contracts import CaptureSource
from hermeneus_engine.tts import routing
from hermeneus_engine.tts.playback import PlaybackError, PlaybackQueue

OWN_PID = 4242


def sine(n, rate=48000, amp=0.5):
    return (amp * np.sin(2 * np.pi * 440 * np.arange(n) / rate)).astype(np.float32)


def hal_with_tree():
    hal = FakeWasapi()
    hal.add_process(1, "explorer.exe", 0, 1.0)
    hal.add_process(300, "hermes.exe", 1, 30.0)
    hal.add_process(OWN_PID, "python.exe", 300, 31.0)
    hal.add_process(200, "discord.exe", 1, 20.0)
    hal.add_session(200, active=True)
    return hal


# ---------------------------------------------------------------- W1 worker search path

def test_w1_worker_pythonpath_carries_the_parent_sys_path(tmp_path):
    deps = tmp_path / "site-packages"
    deps.mkdir()
    env = process_loopback.worker_env({"SystemRoot": r"C:\Windows"}, path=[str(deps), "", str(tmp_path / "gone")])
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts == [str(subproc.PACKAGE_ROOT), str(deps)]


@pytest.mark.skipif(sys.platform != "darwin", reason="compares with the macOS worker")
def test_w1_same_search_path_as_the_macos_worker(tmp_path):
    from hermeneus_engine.capture import process_tap
    paths = [str(tmp_path), "", str(tmp_path), "/nonexistent-dir"]
    assert (process_tap.worker_env({}, path=paths)["PYTHONPATH"]
            == process_loopback.worker_env({}, path=paths)["PYTHONPATH"])


# ---------------------------------------------------------------- W2 one endpoint, several host APIs

WINDOWS_DEVICES = [
    {"name": "Microsoft Sound Mapper - Output", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 0},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 0},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 1, "max_output_channels": 2, "max_input_channels": 0},
    {"name": "Speakers (Realtek(R) Audio)", "hostapi": 2, "max_output_channels": 2, "max_input_channels": 0},
    {"name": "CABLE Output (VB-Audio Virtual Cable)", "hostapi": 2, "max_output_channels": 0,
     "max_input_channels": 2},
]
WINDOWS_HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]


def test_w2_playback_opens_the_wasapi_entry_of_a_windows_endpoint():
    q = PlaybackQueue("Speakers (Realtek(R) Audio)", query_devices=lambda: WINDOWS_DEVICES,
                      query_hostapis=lambda: WINDOWS_HOSTAPIS, default_hostapi=lambda: 0)
    assert q._resolve_device() == (3, 2)


def test_w2_without_wasapi_the_default_host_api_wins_and_a_true_tie_still_fails():
    apis = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "ASIO"}]
    q = PlaybackQueue("Speakers (Realtek(R) Audio)", query_devices=lambda: WINDOWS_DEVICES,
                      query_hostapis=lambda: apis, default_hostapi=lambda: 1)
    assert q._resolve_device()[0] == 2
    q = PlaybackQueue("Speakers (Realtek(R) Audio)", query_devices=lambda: WINDOWS_DEVICES,
                      query_hostapis=lambda: apis, default_hostapi=lambda: None)
    with pytest.raises(PlaybackError, match="not found"):
        q._resolve_device()


def test_w2_a_single_match_never_asks_for_host_apis():
    devices = [{"name": "MacBook Air Speakers", "hostapi": 0, "max_output_channels": 2}]
    q = PlaybackQueue("MacBook Air Speakers", query_devices=lambda: devices,
                      query_hostapis=lambda: pytest.fail("macOS path asked for host APIs"),
                      default_hostapi=lambda: pytest.fail("macOS path asked for the default host API"))
    assert q._resolve_device() == (0, 2)


# ---------------------------------------------------------------- W5 / L4 status outcome

def run_backend(hal):
    from test_process_loopback import PipeWorker
    b = ProcessLoopbackBackend(spawn=lambda: PipeWorker(hal), own_pid=OWN_PID, build=lambda: 22631,
                               has_module=lambda n: True)
    return b


def test_w5_no_audio_ends_the_session_like_macos():
    b = run_backend(hal_with_tree())
    b._handle_status(json.dumps({"code": "no-audio", "message": "worker text", "details": {}}).encode())
    assert b.failure == process_loopback.NO_AUDIO_TEXT == b.status_message
    assert "then start translation again" in b.failure and "Privacy & security > Microphone" in b.failure


def test_w5_stalled_uses_the_macos_text_and_ends_the_session():
    hal = hal_with_tree()
    b = run_backend(hal)
    b.start(CaptureSource("process-loopback", "pid:200", "Discord"), lambda pcm, ts: None)
    try:
        b._handle_status(json.dumps({"code": "stalled", "message": "x", "details": {}}).encode(), b._run)
        assert b.failure == "Capture from Discord stopped receiving audio. Stop and start translation again."
    finally:
        b.stop()


def test_l4_rebuilt_and_retargeted_texts_reach_the_status_line():
    b = run_backend(hal_with_tree())
    for code, text in (("rebuilt", "The capture stream failed and was reopened."),
                       ("retargeted", "chrome.exe was restarted; capture follows the new process.")):
        b._handle_status(json.dumps({"code": code, "message": text, "details": {}}).encode())
        assert b.status_message == text and b.failure == ""


# ---------------------------------------------------------------- W7 the VB-CABLE pair

def cable_inventory():
    return routing.AudioInventory(devices=(
        routing.AudioDeviceInfo("CABLE Input (VB-Audio Virtual Cable)", output_channels=2),
        routing.AudioDeviceInfo("CABLE Output (VB-Audio Virtual Cable)", input_channels=2),
        routing.AudioDeviceInfo("Speakers (Realtek(R) Audio)", output_channels=2, is_default_output=True),
    ))


def test_w7_cable_input_feeds_cable_output():
    v = routing.check_feedback_risk("CABLE Input (VB-Audio Virtual Cable)", "CABLE Output (VB-Audio Virtual Cable)",
                                    capture_backend="virtual-device", inventory=cable_inventory())
    assert v.risky and v.code == routing.CODE_SAME_DEVICE and "other side of the same virtual cable" in v.explanation
    safe = routing.safe_output_devices("CABLE Output (VB-Audio Virtual Cable)", inventory=cable_inventory())
    assert [d.device_id for d in safe] == ["Speakers (Realtek(R) Audio)"]


def test_w7_pair_rule_covers_numbered_cables_and_nothing_else():
    assert audio_devices.same_loopback_device("CABLE-A Input (VB-Audio Cable A)", "CABLE-A Output (VB-Audio Cable A)")
    assert not audio_devices.same_loopback_device("CABLE-A Input (VB-Audio Cable A)",
                                                  "CABLE-B Output (VB-Audio Cable B)")
    assert not audio_devices.same_loopback_device("BlackHole 2ch", "BlackHole 16ch")
    assert not audio_devices.same_loopback_device("Speakers", "Speakers")


# ---------------------------------------------------------------- W9 doctor

class FakeSd:
    def __init__(self, name):
        self.name = name

    def query_devices(self, kind=None):
        return {"name": self.name}


class Backend:
    def __init__(self, name, ok):
        self.name, self._ok = name, ok

    def available(self):
        return self._ok, "r"


def doctor_checks(monkeypatch, loopback_ok, out):
    monkeypatch.setattr(components, "capture_backends",
                        lambda on_error=None: [Backend("virtual-device", True), Backend("process-loopback", loopback_ok)])
    r = doctor_windows.run(get_secret=lambda n: "k", aux_config=None, build=lambda: 22631, which=lambda n: None,
                           sd_loader=lambda: FakeSd(out))
    return {c.name: c for c in r.checks}


def test_w9_doctor_names_the_microphone_privacy_switch(monkeypatch):
    c = doctor_checks(monkeypatch, True, "Speakers")["permission: microphone (Windows privacy)"]
    assert c.ok is None and "Let desktop apps access your microphone" in c.fix


def test_w9_doctor_checks_the_vb_cable_route_only_for_the_fallback(monkeypatch):
    assert doctor_checks(monkeypatch, True, "Speakers")["system sound reaches the virtual device"].ok is True
    c = doctor_checks(monkeypatch, False, "Speakers (Realtek(R) Audio)")["system sound reaches the virtual device"]
    assert c.ok is False and c.required and "CABLE Input" in c.fix
    c = doctor_checks(monkeypatch, False, "CABLE Input (VB-Audio Virtual Cable)")["system sound reaches the virtual device"]
    assert c.ok is True and "Listen to this device" in c.detail


def test_w9_no_audio_check_help_is_true_on_windows(monkeypatch):
    import argparse

    import importlib

    from plugin_loader import load_plugin_package
    commands = importlib.import_module(load_plugin_package().__name__ + ".commands")
    monkeypatch.setattr(sys, "platform", "win32")
    p = argparse.ArgumentParser()
    commands.setup_cli(p)
    top = " ".join(p.format_help().split())
    doc = " ".join(p._subparsers._group_actions[0].choices["doctor"].format_help().split())
    assert "no capture check to skip" in doc and "BlackHole" not in doc
    assert "needs on this PC" in top and "this Mac" not in top


# ---------------------------------------------------------------- W11 words and wrong-OS settings

def test_w11_machine_word_per_platform():
    assert platform_words.this_machine("darwin") == "this Mac"
    assert platform_words.this_machine("win32") == "this PC"


def test_w11_wrong_os_capture_backend_names_the_platform(monkeypatch):
    monkeypatch.setattr(options.sys, "platform", "win32")
    sources = [CaptureSource("process-loopback", "system", "All system audio except Hermeneus")]
    with pytest.raises(options.UsageError, match="macOS process tap; this system does not offer it"):
        options.pick_source(None, {"capture_backend": "process-tap"}, sources)


def test_w11_macos_keeps_its_message_for_an_unavailable_process_tap(monkeypatch):
    monkeypatch.setattr(options.sys, "platform", "darwin")
    with pytest.raises(options.UsageError, match="no audio source chosen"):
        options.pick_source(None, {"capture_backend": "process-tap"},
                            [CaptureSource("virtual-device", "BlackHole 2ch", "BlackHole 2ch")])


# ---------------------------------------------------------------- W12 / W13 Initialize

class FakeClient:
    def __init__(self, log, refuse):
        self.log, self.refuse = log, refuse

    def Initialize(self, share, flags, duration, periodicity, fmt, guid):
        wf = fmt._obj
        self.log.append((id(self), share, flags, duration, periodicity, wf.nSamplesPerSec, wf.wBitsPerSample))
        if self.refuse:
            raise OSError(-2004287480, "AUDCLNT_E_UNSUPPORTED_FORMAT")


class WaveFormat(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_uint16), ("nChannels", ctypes.c_uint16), ("nSamplesPerSec", ctypes.c_uint32),
                ("nAvgBytesPerSec", ctypes.c_uint32), ("nBlockAlign", ctypes.c_uint16),
                ("wBitsPerSample", ctypes.c_uint16), ("cbSize", ctypes.c_uint16)]


def fake_hal(refusals):
    hal = object.__new__(_wasapi.WasapiHAL)
    log, clients = [], []

    def activate(pid, include):
        c = FakeClient(log, refusals.pop(0))
        clients.append(c)
        return c

    hal._activate_client = activate
    hal._WAVEFORMATEX = WaveFormat
    hal._stream_for = lambda client, fmt: ("stream", client, fmt)
    return hal, log, clients


def test_w12_event_driven_shared_mode_passes_zero_duration_and_periodicity():
    hal, log, _ = fake_hal([False])
    hal.open_loopback(OWN_PID, False)
    (_, share, flags, duration, periodicity, rate, bits), = log
    assert (share, duration, periodicity) == (0, 0, 0)
    assert flags == 0x00020000 | 0x00040000 | 0x80000000  # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM, as the sample


def test_w13_each_format_gets_a_fresh_client():
    hal, log, clients = fake_hal([True, False])
    _, client, fmt = hal.open_loopback(OWN_PID, False)
    assert len(clients) == 2 and client is clients[1] and log[0][0] != log[1][0]
    assert [(r, b) for *_, r, b in log] == [(48000, 32), (44100, 16)] and fmt == _wasapi.CAPTURE_FORMATS[1]
    hal, _, _ = fake_hal([True, True])
    with pytest.raises(_wasapi.WasapiError, match="refused every capture format"):
        hal.open_loopback(OWN_PID, False)


# ---------------------------------------------------------------- W14 pid sources that hold Hermeneus

def test_w14_pid_of_an_ancestor_is_refused():
    hal = hal_with_tree()
    for pid in (300, OWN_PID):
        cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=pid, own_pid=OWN_PID), lambda p, t: None)
        with pytest.raises(LoopbackSetupError, match="Hermeneus or one of its parent processes"):
            cap.open(start_pump=False)
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200, own_pid=OWN_PID), lambda p, t: None)
    cap.open(start_pump=False)
    cap.close()


def test_w14_backend_passes_own_pid_and_routing_refuses_a_parent(monkeypatch):
    b = ProcessLoopbackBackend(own_pid=OWN_PID, build=lambda: 22631, has_module=lambda n: True)
    assert b._spec_for(CaptureSource("process-loopback", "pid:300", "x"))["own_pid"] == OWN_PID
    monkeypatch.setattr(process_loopback, "own_ancestry", lambda own_pid=None: {OWN_PID, 300, 1})
    assert components.own_process_excluded(CaptureSource("process-loopback", "pid:300", "Hermes")) is False
    assert components.own_process_excluded(CaptureSource("process-loopback", "pid:200", "Discord")) is True


# ---------------------------------------------------------------- W15 slow checks never delay reads

def test_w15_a_slow_tick_does_not_delay_the_pump():
    hal = hal_with_tree()
    real = hal.processes

    def slow():
        time.sleep(0.6)
        return real()

    got = []
    cap = LoopbackCapture(hal, LoopbackSpec(mode="app", exe="discord.exe", own_pid=OWN_PID),
                          lambda pcm, ts: got.append(time.monotonic()), refresh_interval=0.05)
    cap.open()
    hal.processes = slow
    try:
        time.sleep(0.15)  # the tick thread is now inside a 0.6 s process listing
        fed = time.monotonic()
        hal.feed(sine(4800))
        deadline = fed + 0.3
        while not got and time.monotonic() < deadline:
            time.sleep(0.005)
        assert got and got[0] - fed < 0.2
    finally:
        cap.close()
    assert cap.stats.ticks >= 1 and cap.stats.tick_ns_max >= 500_000_000


# ---------------------------------------------------------------- W16 free-threaded marshaler

def test_w16_imarshal_goes_to_the_ftm_and_everything_else_to_the_object():
    calls = []
    assert _wasapi.marshal_query(True, lambda: calls.append("ftm") or 0, lambda: calls.append("own") or 1) == 0
    assert _wasapi.marshal_query(False, lambda: calls.append("ftm") or 0, lambda: calls.append("own") or 7) == 7
    assert calls == ["ftm", "own"]

    def refuse():
        raise OSError("E_NOINTERFACE")
    assert _wasapi.marshal_query(True, refuse, lambda: 0) == _wasapi.E_NOINTERFACE
    assert _wasapi.IID_IMARSHAL == "{00000003-0000-0000-C000-000000000046}"


# ---------------------------------------------------------------- W17

def test_w17_timestamp_error_packets_use_arrival_time():
    hal = hal_with_tree()
    got = []
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda pcm, ts: got.append(ts),
                          clock_ns=lambda: 1_000_000_000)
    cap.open(start_pump=False)
    s = hal.stream
    x = np.repeat(sine(4800)[:, None], 2, axis=1).astype("<f4").tobytes()
    s.push(_wasapi.Packet(x, 4800, _wasapi.AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, hal.qpc - 10))
    cap.pump_once()
    assert cap.stats.timestamp_errors == 1 and cap.stats.timestamp_source == "arrival"
    assert got[0] < 1_000_000_000 - 99_000_000  # block duration subtracted, not the 1 us the counter claimed
    cap.close()


def test_w17_a_stream_activated_during_teardown_is_closed_not_started():
    hal = hal_with_tree()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda pcm, ts: None)
    cap.open(start_pump=False)
    real_open = hal.open_loopback

    def open_then_stop(pid, include):
        s = real_open(pid, include)
        cap._stopping.set()  # close() ran while this activation was in flight
        return s

    hal.open_loopback = open_then_stop
    assert cap.rebuild("silence") is False
    assert hal.stream.closed and not hal.stream.started
    cap.close()
    assert hal.clean


def test_w17_exit_code_259_is_not_alive_once_the_handle_is_signaled():
    from test_windows_platform import FakeKernel32
    assert winproc.pid_alive(5, FakeKernel32(77, exit_code=259, signaled=True)) is False
    assert winproc.pid_alive(5, FakeKernel32(77, exit_code=259, signaled=False)) is True


# ---------------------------------------------------------------- W18 / W19 (A4 re-audit N1 to N3)

class WaitTrackingStream:
    """A stream whose wait() blocks until the test releases it, and which records a close during a wait."""

    def __init__(self, hal, target, include):
        self.hal, self.target, self.include = hal, target, include
        self.sample_rate, self.channels, self.sample_format = 48000, 2, "float32"
        self.started = self.closed = False
        self.in_wait = threading.Event()
        self.release = threading.Event()
        self.violations = []
        self.closed_by = None

    def start(self):
        self.started = True

    def wait(self, timeout_s):
        self.in_wait.set()
        self.release.wait(2.0)
        self.in_wait.clear()
        return False

    def read(self):
        return []

    def close(self):
        if self.in_wait.is_set():
            self.violations.append("closed while a wait was pending")
        self.closed = True
        self.closed_by = threading.current_thread().name
        self.release.set()
        return []


def test_w18_only_the_pump_closes_a_stream_it_may_be_waiting_on():
    hal = hal_with_tree()
    streams = []

    def open_loopback(pid, include):
        s = WaitTrackingStream(hal, pid, include)
        streams.append(s)
        return s

    hal.open_loopback = open_loopback
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda p, t: None, refresh_interval=60)
    cap.open()
    try:
        first = streams[0]
        assert first.in_wait.wait(1.0)          # the pump is inside WaitForSingleObject on the first stream
        assert cap.rebuild("silence") is True    # the tick thread's rebuild, run from this thread
        assert not first.closed                  # handed over, not closed under the waiting pump
        first.release.set()                      # the wait returns
        deadline = time.monotonic() + 1.0
        while not first.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert first.closed and first.closed_by == "hermeneus-loopback-pump"
        assert first.violations == []
    finally:
        for s in streams:
            s.release.set()
        cap.close()
    assert all(s.closed for s in streams) and all(not s.violations for s in streams)


def test_w19_activation_runs_outside_the_lock():
    hal = hal_with_tree()
    real_open = hal.open_loopback
    inside = threading.Event()
    go_on = threading.Event()

    def slow_open(pid, include):
        if inside.is_set() is False and len(hal.streams) >= 1:
            inside.set()
            go_on.wait(2.0)
        return real_open(pid, include)

    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda p, t: None)
    cap.open(start_pump=False)
    hal.open_loopback = slow_open
    t = threading.Thread(target=cap.rebuild, args=("silence",))
    t.start()
    assert inside.wait(1.0)
    got = cap._lock.acquire(timeout=0.2)  # the pump could take the lock during a slow activation
    if got:
        cap._lock.release()
    go_on.set()
    t.join(2.0)
    cap.close()
    assert got and hal.clean


def test_w19_the_peak_is_swapped_under_the_lock():
    hal = hal_with_tree()
    cap = LoopbackCapture(hal, LoopbackSpec(mode="pid", pid=200), lambda p, t: None)
    cap.open(start_pump=False)
    cap._window_peak = 0.5
    done = threading.Event()
    with cap._lock:
        t = threading.Thread(target=lambda: (cap._watchdog(0.0), done.set()))
        t.start()
        assert not done.wait(0.1)  # the watchdog waits for the lock the pump holds while converting
    assert done.wait(1.0)
    t.join()
    cap.close()


# ---------------------------------------------------------------- W22 wording (A5 re-audit)

def test_w22_remote_start_refusal_names_the_machine(monkeypatch):
    import importlib

    from plugin_loader import load_plugin_package
    commands = importlib.import_module(load_plugin_package().__name__ + ".commands")
    assert commands.REMOTE_START_REFUSED == ("Hermeneus captures audio on " + platform_words.this_machine() +
                                             ", so start it from " + platform_words.the_machine() +
                                             " (terminal or Hermes Desktop).")
    assert platform_words.the_machine("darwin") == "the Mac" and platform_words.the_machine("win32") == "the PC"
    if sys.platform == "darwin":  # macOS text unchanged, byte for byte
        assert commands.REMOTE_START_REFUSED == ("Hermeneus captures audio on this Mac, so start it from the Mac "
                                                 "(terminal or Hermes Desktop).")


def test_w22_plugin_yaml_settings_are_os_neutral():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
    block = text[text.index("  allow_remote_start:"):]
    description = next(line for line in block.splitlines() if line.strip().startswith("description:"))
    assert "Mac" not in description and "this computer" in description


# ---------------------------------------------------------------- W21 truncated MME cable names

CABLE_NAMES = {
    "MME": ("CABLE Input (VB-Audio Virtual C", "CABLE Output (VB-Audio Virtual "),
    "Windows DirectSound": ("CABLE Input (VB-Audio Virtual Cable)", "CABLE Output (VB-Audio Virtual Cable)"),
    "Windows WASAPI": ("CABLE Input (VB-Audio Virtual Cable)", "CABLE Output (VB-Audio Virtual Cable)"),
}


@pytest.mark.parametrize("out_api", sorted(CABLE_NAMES))
@pytest.mark.parametrize("cap_api", sorted(CABLE_NAMES))
def test_w21_cable_pair_matches_across_host_api_names(out_api, cap_api):
    out_name, cap_name = CABLE_NAMES[out_api][0], CABLE_NAMES[cap_api][1]
    assert len(CABLE_NAMES["MME"][0]) == 31 and len(CABLE_NAMES["MME"][1]) == 31
    assert audio_devices.same_loopback_device(out_name, cap_name)
    inv = routing.AudioInventory(devices=(
        routing.AudioDeviceInfo(out_name, output_channels=2),
        routing.AudioDeviceInfo(cap_name, input_channels=2),
        routing.AudioDeviceInfo("Speakers (Realtek(R) Audio)", output_channels=2),
    ))
    v = routing.check_feedback_risk(out_name, cap_name, capture_backend="virtual-device", inventory=inv)
    assert v.risky and v.code == routing.CODE_SAME_DEVICE


def test_w21_numbered_and_truncated_cables_stay_apart():
    assert audio_devices.same_loopback_device("CABLE-A Input (VB-Audio Cable A)", "CABLE-A Output (VB-Audio Cable")
    assert not audio_devices.same_loopback_device("CABLE-A Input (VB-Audio Cable A)", "CABLE Output (VB-Audio Virtual ")
    assert not audio_devices.same_loopback_device("Cable Modem Output", "Cable Modem Input")


# ---------------------------------------------------------------- W20 WASAPI shared mode at 24 kHz

class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def playback_for(name, devices, apis, *, asked=None):
    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return FakeStream(**kwargs)

    def hostapis():
        if asked is not None:
            asked.append(1)
        return apis

    q = PlaybackQueue(name, sample_rate=24000, stream_factory=factory, query_devices=lambda: devices,
                      query_hostapis=hostapis, default_hostapi=lambda: 0,
                      wasapi_settings=lambda: ("WasapiSettings", {"auto_convert": True}))
    return q, made


def test_w20_wasapi_output_gets_auto_convert_on_windows(monkeypatch):
    from hermeneus_engine.tts import playback
    monkeypatch.setattr(playback.sys, "platform", "win32")
    q, made = playback_for("Speakers (Realtek(R) Audio)", WINDOWS_DEVICES, WINDOWS_HOSTAPIS)
    q.start()
    assert made[0]["device"] == 3 and made[0]["samplerate"] == 24000
    assert made[0]["extra_settings"] == ("WasapiSettings", {"auto_convert": True})
    q.stop()


def test_w20_other_host_apis_on_windows_get_no_extra_settings(monkeypatch):
    from hermeneus_engine.tts import playback
    monkeypatch.setattr(playback.sys, "platform", "win32")
    devices = [{"name": "Speakers (MME only)", "hostapi": 0, "max_output_channels": 2}]
    q, made = playback_for("Speakers (MME only)", devices, WINDOWS_HOSTAPIS)
    q.start()
    assert "extra_settings" not in made[0]
    q.stop()


def test_w20_macos_stream_arguments_are_unchanged(monkeypatch):
    from hermeneus_engine.tts import playback
    monkeypatch.setattr(playback.sys, "platform", "darwin")
    asked = []
    devices = [{"name": "MacBook Air Speakers", "hostapi": 0, "max_output_channels": 2}]
    q, made = playback_for("MacBook Air Speakers", devices, [{"name": "Core Audio"}], asked=asked)
    q.start()
    assert set(made[0]) == {"device", "samplerate", "channels", "dtype", "latency", "callback"} and asked == []
    q.stop()


def test_w20_real_sounddevice_has_wasapi_settings_with_auto_convert():
    import inspect

    import sounddevice
    assert "auto_convert" in inspect.signature(sounddevice.WasapiSettings).parameters
