"""Windows pieces of the session (lock, process check, transport, signals, doctor, source choice),
run on any platform with fakes for msvcrt and kernel32.
"""

import asyncio
import errno
import json
import os
import signal
import sys
import types

import pytest

from pipeline_fakes import make_rig, options, remove_tmpdir, short_tmpdir, wait_for

from hermeneus_engine import components, doctor, doctor_windows, ipc, runtime, stop_signals
from hermeneus_engine._windows import ipc as winipc
from hermeneus_engine._windows import lock as winlock
from hermeneus_engine._windows import process as winproc
from hermeneus_engine.contracts import CaptureSource, CommittedTranscript, LineState
from hermeneus_engine.options import Choices, pick_source, resolve


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


class FakeMsvcrt(types.ModuleType):
    """msvcrt.locking semantics: exclusive byte ranges per open handle, starting at the file position."""

    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self):
        super().__init__("msvcrt")
        self.held = {}

    def _key(self, fd, nbytes):
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino, os.lseek(fd, 0, os.SEEK_CUR), nbytes)

    def locking(self, fd, mode, nbytes):
        key = self._key(fd, nbytes)
        if mode == self.LK_UNLCK:
            if self.held.get(key) != fd:
                raise OSError(errno.EACCES, "not locked by this handle")
            del self.held[key]
        elif mode == self.LK_NBLCK:
            if key in self.held:
                raise OSError(errno.EACCES, "Permission denied")
            self.held[key] = fd
        else:
            raise AssertionError("blocking locks are never used")


@pytest.fixture
def windows(monkeypatch):
    """Run the session code down its Windows branches on this machine."""
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(runtime, "_windows", lambda: True)
    monkeypatch.setattr(ipc, "_use_tcp", lambda: True)
    monkeypatch.setattr(winproc, "pid_alive", lambda pid, *a, **k: pid == os.getpid())
    return fake


# ---------------------------------------------------------------- lock

def test_lock_is_exclusive_and_keeps_the_owner_readable(data_dir, windows):
    lock = runtime.SessionLock(data_dir)
    lock.acquire({"pid": os.getpid(), "owner": "test"})
    assert windows.held  # one byte at LOCK_OFFSET
    assert list(windows.held)[0][2] == winlock.LOCK_OFFSET
    holder = runtime.lock_holder(data_dir)
    assert holder["pid"] == os.getpid() and holder["pid_alive"] is True
    with pytest.raises(runtime.SessionBusy, match=f"process {os.getpid()}"):
        runtime.SessionLock(data_dir).acquire({"pid": 1})
    lock.release()
    assert not windows.held and runtime.lock_holder(data_dir) is None
    again = runtime.SessionLock(data_dir)
    again.acquire({"pid": os.getpid()})
    again.release()


def test_lock_holder_reports_a_dead_owner(data_dir, windows, monkeypatch):
    lock = runtime.SessionLock(data_dir)
    lock.acquire({"pid": 123456})
    monkeypatch.setattr(winproc, "pid_alive", lambda pid, *a, **k: False)
    assert runtime.lock_holder(data_dir)["pid_alive"] is False
    lock.release()


def test_unexpected_lock_errors_propagate(data_dir, windows, monkeypatch):
    def broken(fd, mode, n):
        raise OSError(errno.EBADF, "bad handle")
    monkeypatch.setattr(windows, "locking", broken)
    with pytest.raises(OSError, match="bad handle"):
        runtime.SessionLock(data_dir).acquire({"pid": 1})


# ---------------------------------------------------------------- process check

class FakeKernel32:
    def __init__(self, handle, exit_code=259, signaled=False):
        self.handle, self.exit_code, self.signaled, self.closed = handle, exit_code, signaled, []

    def OpenProcess(self, access, inherit, pid):
        assert access == winproc.PROCESS_QUERY_LIMITED_INFORMATION | winproc.SYNCHRONIZE
        return self.handle

    def WaitForSingleObject(self, handle, ms):
        assert ms == 0
        return 0 if self.signaled else winproc.WAIT_TIMEOUT

    def GetExitCodeProcess(self, handle, ptr):
        ptr._obj.value = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def test_pid_alive_asks_the_kernel_without_signals(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill must not be used on Windows"))
    k32 = FakeKernel32(77)
    assert winproc.pid_alive(5, k32) is True and k32.closed == [77]
    assert winproc.pid_alive(5, FakeKernel32(77, exit_code=0)) is False
    assert winproc.pid_alive(5, FakeKernel32(0), last_error=lambda: winproc.ERROR_ACCESS_DENIED) is True
    assert winproc.pid_alive(5, FakeKernel32(0), last_error=lambda: 87) is False
    assert winproc.pid_alive(0, k32) is False


def test_runtime_pid_alive_uses_the_kernel_check_on_windows(monkeypatch):
    monkeypatch.setattr(runtime, "_windows", lambda: True)
    monkeypatch.setattr(winproc, "pid_alive", lambda pid: pid == 42)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("os.kill(pid, 0) sends CTRL_C_EVENT on Windows"))
    assert runtime.pid_alive(42) is True and runtime.pid_alive(43) is False


# ---------------------------------------------------------------- transport

async def test_session_runs_over_the_token_guarded_loopback_transport(data_dir, windows):
    rig = make_rig()
    rt = runtime.SessionRuntime(data_dir, options(), rig.build, owner="test")
    await rt.start()
    endpoint = json.loads(runtime.socket_path(data_dir).read_text())
    assert endpoint["transport"] == "tcp" and endpoint["host"] == "127.0.0.1" and len(endpoint["token"]) == 64
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    rig.stt.emit(CommittedTranscript("first", segment_index=0))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))

    client = ipc.SessionClient(runtime.socket_path(data_dir))
    assert (await client.status())["state"] == "live"
    page = await client.lines(None)
    assert [e["line"]["translation"] for e in page["events"] if e["type"] == "line"] == ["<first>"]
    stream = client.subscribe(after=None)
    assert (await stream.__anext__())["type"] == "hello"
    await stream.aclose()

    # A process that cannot read the endpoint file has no token and gets nothing.
    for bad in ({"op": "status"}, {"op": "stop", "token": "0" * 64}):
        reader, writer = await asyncio.open_connection("127.0.0.1", endpoint["port"])
        writer.write((json.dumps(bad) + "\n").encode())
        await writer.drain()
        assert json.loads(await reader.readline()) == {"type": "error", "message": "unauthorized"}
        writer.close()
    assert rt.bus.status()["state"] == "live"

    await client.stop()
    await asyncio.wait_for(rt.wait(), 3)
    assert runtime.lock_holder(data_dir) is None and not runtime.socket_path(data_dir).exists()
    with pytest.raises(ipc.SessionUnavailable):
        await client.status()


async def test_stale_endpoint_file_from_a_dead_owner_is_replaced(data_dir, windows):
    runtime.run_dir(data_dir)
    winipc.write_endpoint(runtime.socket_path(data_dir), 1, "f" * 64)
    rig = make_rig()
    rt = runtime.SessionRuntime(data_dir, options(), rig.build, owner="test")
    await rt.start()
    assert json.loads(runtime.socket_path(data_dir).read_text())["port"] != 1
    await rt.close()


async def test_client_without_endpoint_reports_unavailable(data_dir, windows):
    with pytest.raises(ipc.SessionUnavailable):
        await ipc.SessionClient(runtime.socket_path(data_dir)).status()
    runtime.run_dir(data_dir)
    runtime.socket_path(data_dir).write_text("not json")
    with pytest.raises(ipc.SessionUnavailable):
        await ipc.SessionClient(runtime.socket_path(data_dir)).status()


def test_token_comparison():
    assert winipc.token_ok("ab" * 32, "ab" * 32)
    assert not winipc.token_ok("ab" * 32, "ab" * 31 + "ac")
    assert not winipc.token_ok("ab" * 32, None)


# ---------------------------------------------------------------- signals

async def test_stop_signals_use_signal_module_on_windows(monkeypatch):
    monkeypatch.setattr(stop_signals.sys, "platform", "win32")
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a: pytest.fail("not supported on Windows"))
    stopped = asyncio.Event()
    before = signal.getsignal(signal.SIGINT)
    restore = stop_signals.install(loop, stopped.set)
    try:
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before


async def test_stop_signals_use_the_loop_on_posix():
    if sys.platform == "win32":
        pytest.skip("POSIX branch")
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    restore = stop_signals.install(loop, stopped.set)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stopped.wait(), 2)
    finally:
        restore()


# ---------------------------------------------------------------- doctor

class Backend:
    def __init__(self, name, ok, reason="r"):
        self.name, self._ok, self._reason = name, ok, reason

    def available(self):
        return self._ok, self._reason


class FakeSd:
    def __init__(self, name="Speakers (Realtek)"):
        self.name = name

    def query_devices(self, kind=None):
        assert kind == "output"
        if self.name is None:
            raise RuntimeError("no output")
        return {"name": self.name}


def win_doctor(monkeypatch, backends, build=22631, secret="k", sd=None):
    monkeypatch.setattr(components, "capture_backends", lambda on_error=None: backends)
    return doctor_windows.run(get_secret=lambda name: secret, aux_config={"provider": "nous", "model": "m"},
                              build=lambda: build, which=lambda name: None, sd_loader=lambda: sd or FakeSd())


def states(report):
    return {c["id"]: c["state"] for c in report.to_dict()["checks"]}


def test_windows_doctor_passes_with_process_loopback(monkeypatch):
    r = win_doctor(monkeypatch, [Backend("virtual-device", False), Backend("process-loopback", True)])
    s = states(r)
    assert r.ok and s["windows"] == "ok" and s["capture-process-loopback"] == "ok" and s["audio-source"] == "ok"
    assert s["audio-output-device"] == "ok" and s["elevenlabs-api-key"] == "ok"
    assert "macos" not in s and "permission-system-audio-recording" not in s


def test_windows_doctor_on_an_old_build_points_at_vb_cable(monkeypatch):
    r = win_doctor(monkeypatch, [Backend("virtual-device", False), Backend("process-loopback", False)], build=19045)
    s = states(r)
    assert not r.ok and s["windows"] == "warn" and s["audio-source"] == "fail"
    text = r.render()
    assert "VB-CABLE" in text and "20348" in text


def test_windows_doctor_missing_key_fails_without_printing_it(monkeypatch):
    r = win_doctor(monkeypatch, [Backend("process-loopback", True)], secret=None, sd=FakeSd(None))
    s = states(r)
    assert not r.ok and s["elevenlabs-api-key"] == "fail" and s["audio-output-device"] == "warn"


def test_doctor_entry_point_dispatches_on_platform(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor_windows, "_windows_build", lambda: 22631)
    monkeypatch.setattr(components, "capture_backends", lambda on_error=None: [Backend("process-loopback", True)])
    monkeypatch.setattr(doctor_windows, "_load_sounddevice", lambda: FakeSd())
    r = doctor.run(get_secret=lambda n: "k", aux_config=None, which=lambda n: None)
    assert states(r)["windows"] == "ok"


# ---------------------------------------------------------------- choices and routing

def loopback_sources():
    return [CaptureSource("virtual-device", "CABLE Output (VB-Audio Virtual Cable)", "CABLE Output"),
            CaptureSource("process-loopback", "system", "All system audio except Hermeneus"),
            CaptureSource("process-loopback", "app:chrome.exe", "chrome", "playing now")]


def test_auto_picks_the_whole_system_loopback_source():
    assert pick_source(None, {}, loopback_sources()).source_id == "system"
    assert pick_source("chrome", {}, loopback_sources()).source_id == "app:chrome.exe"
    assert pick_source(None, {"capture_backend": "virtual-device"}, loopback_sources()).backend == "virtual-device"


def test_same_settings_resolve_on_windows():
    opts = resolve(Choices(to="tr", speak=True, device="Speakers (Realtek)"), {}, loopback_sources())
    assert opts.source.backend == "process-loopback" and opts.output_device == "Speakers (Realtek)"


def test_loopback_capture_is_feedback_safe_on_any_output():
    from hermeneus_engine.tts import routing
    inv = routing.AudioInventory(devices=(routing.AudioDeviceInfo("Speakers (Realtek)", output_channels=2),))
    src = CaptureSource("process-loopback", "system", "All")
    v = routing.check_feedback_risk("Speakers (Realtek)", "system", inventory=inv,
                                    capture_backend="process-loopback",
                                    own_process_excluded=components.own_process_excluded(src))
    assert not v.risky
    own = CaptureSource("process-loopback", f"pid:{os.getpid()}", "me")
    assert components.own_process_excluded(own) is False
    v = routing.check_feedback_risk("Speakers (Realtek)", own.source_id, inventory=inv,
                                    capture_backend="process-loopback", own_process_excluded=False)
    assert v.risky and v.code == routing.CODE_OWN_PROCESS_CAPTURED
