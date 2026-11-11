"""In-memory stand-in for ``hermeneus_engine.capture._coreaudio.CoreAudioHAL``."""

from __future__ import annotations

import threading

import numpy as np

from hermeneus_engine.capture._coreaudio import (
    FLAG_IS_FLOAT, FORMAT_LINEAR_PCM, AppInfo, HALError, ProcessInfo, TapFormat, TapHandle,
)


class FakeHAL:
    def __init__(self, rate: float = 48000.0, channels: int = 1) -> None:
        self.processes: dict[int, ProcessInfo] = {}
        self.apps: dict[int, AppInfo] = {}
        self.fmt = TapFormat(rate, FORMAT_LINEAR_PCM, FLAG_IS_FLOAT | 8, channels, 32)
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        self.live_taps: set[int] = set()
        self.live_devices: set[int] = set()
        self.live_procs: set[int] = set()
        self.running: set[int] = set()
        self.callback = None
        self.tap_processes: dict[int, tuple[list[int], bool]] = {}
        self._next = 1000
        self.default_out = 50
        self.devices = [50, 51]
        self.device_names = {50: "Speakers", 51: "BlackHole 2ch", 52: "AirPods"}
        self._lock = threading.Lock()

    # helpers for tests
    def add_process(self, object_id: int, pid: int, bundle_id: str = "", playing: bool = False,
                    app: AppInfo | None = None) -> None:
        self.processes[object_id] = ProcessInfo(object_id, pid, bundle_id, playing)
        if app is not None:
            self.apps[pid] = app

    def feed(self, sample_time: float, samples: np.ndarray, host_time: int = 0) -> None:
        assert self.callback is not None
        self.callback(sample_time, host_time, samples.astype("<f4").tobytes(), self.fmt.channels)

    def _maybe_fail(self, name: str) -> None:
        exc = self.fail.get(name)
        if exc is not None:
            raise exc

    def _id(self) -> int:
        with self._lock:
            self._next += 1
            return self._next

    # HAL surface
    def process_objects(self):
        self._maybe_fail("process_objects")
        return list(self.processes)

    def process_info(self, object_id):
        if object_id not in self.processes:
            raise HALError("read process pid", 560947818)
        return self.processes[object_id]

    def process_object_for_pid(self, pid):
        for obj, info in self.processes.items():
            if info.pid == pid:
                return obj
        return 0

    def default_output_device(self):
        return self.default_out

    def device_ids(self):
        return tuple(self.devices)

    def device_name(self, device_id):
        return self.device_names.get(device_id, "")

    def app_info(self, pid):
        return self.apps.get(pid)

    def create_tap(self, objects, exclude, name, mute=False):
        self._maybe_fail("create_tap")
        self.muted = mute
        tap = self._id()
        self.live_taps.add(tap)
        self.tap_processes[tap] = (list(objects), exclude)
        self.calls.append(("create_tap", tuple(objects), exclude))
        return TapHandle(tap, f"uid-{tap}")

    def set_tap_processes(self, tap_id, objects, exclude, name, mute=False):
        self._maybe_fail("set_tap_processes")
        self.tap_processes[tap_id] = (list(objects), exclude)
        self.calls.append(("set_tap_processes", tuple(objects), exclude))

    def tap_format(self, tap_id):
        self.calls.append(("tap_format",))
        return self.fmt

    def destroy_tap(self, tap_id):
        self.calls.append(("destroy_tap",))
        self._maybe_fail("destroy_tap")
        self.live_taps.discard(tap_id)

    def create_aggregate(self, tap_uid, name, uid):
        self._maybe_fail("create_aggregate")
        dev = self._id()
        self.live_devices.add(dev)
        self.calls.append(("create_aggregate", tap_uid))
        return dev

    def destroy_aggregate(self, device_id):
        self.calls.append(("destroy_aggregate",))
        self._maybe_fail("destroy_aggregate")
        self.live_devices.discard(device_id)

    def create_ioproc(self, device_id, callback):
        self._maybe_fail("create_ioproc")
        proc = self._id()
        self.live_procs.add(proc)
        self.callback = callback
        self.calls.append(("create_ioproc",))
        return proc, object()

    def destroy_ioproc(self, device_id, proc_id):
        self.calls.append(("destroy_ioproc",))
        self._maybe_fail("destroy_ioproc")
        self.live_procs.discard(proc_id)
        self.callback = None

    def start(self, device_id, proc_id):
        self._maybe_fail("start")
        self.running.add(device_id)
        self.calls.append(("start",))

    def stop(self, device_id, proc_id):
        self.calls.append(("stop",))
        self._maybe_fail("stop")
        self.running.discard(device_id)

    def host_time_to_ns(self, host_time):
        return int(host_time)

    @property
    def clean(self) -> bool:
        return not (self.live_taps or self.live_devices or self.live_procs or self.running)

    def names(self):
        return [c[0] for c in self.calls]
