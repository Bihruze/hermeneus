"""In-memory stand-in for ``_wasapi.WasapiHAL`` (the Windows process loopback HAL).

It records every call, hands out streams that the test feeds with packets,
and checks that every stream it opened was closed.
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from hermeneus_engine.capture._wasapi import AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, AUDCLNT_BUFFERFLAGS_SILENT
from hermeneus_engine.capture._wasapi import Packet, ProcInfo, SessionInfo, WasapiError


class FakeStream:
    def __init__(self, hal: "FakeWasapi", target: int, include: bool, rate: int, channels: int, fmt: str) -> None:
        self.hal = hal
        self.target = target
        self.include = include
        self.sample_rate = rate
        self.channels = channels
        self.sample_format = fmt
        self.started = False
        self.closed = False
        self._packets: list = []
        self._cond = threading.Condition()
        self.fail_read: Optional[Exception] = None

    def start(self) -> None:
        self.hal.calls.append(("start", self.target))
        if "start" in self.hal.fail:
            raise self.hal.fail.pop("start")
        self.started = True

    def push(self, packet: Packet) -> None:
        with self._cond:
            self._packets.append(packet)
            self._cond.notify_all()

    def wait(self, timeout_s: float) -> bool:
        with self._cond:
            if not self._packets and self.fail_read is None:
                self._cond.wait(min(timeout_s, 0.02))
            return bool(self._packets)

    def read(self) -> list:
        if self.fail_read is not None:
            exc, self.fail_read = self.fail_read, None
            raise exc
        with self._cond:
            out, self._packets = self._packets, []
        return out

    def close(self) -> list:
        self.hal.calls.append(("close", self.target))
        self.closed = True
        return list(self.hal.close_failures)


class FakeWasapi:
    def __init__(self, *, rate: int = 48000, channels: int = 2, fmt: str = "float32", build: int = 22631) -> None:
        self.rate, self.channels, self.fmt, self.build = rate, channels, fmt, build
        self.procs: list[ProcInfo] = []
        self.session_list: list[SessionInfo] = []
        self.streams: list[FakeStream] = []
        self.calls: list = []
        self.fail: dict = {}
        self.close_failures: list = []
        self.qpc = 10_000_000_000  # 100 ns units

    # -- setup -----------------------------------------------------------
    def add_process(self, pid: int, name: str, ppid: int = 0, created: float = 0.0) -> None:
        self.procs.append(ProcInfo(pid, ppid, name.lower(), created))

    def remove_process(self, pid: int) -> None:
        self.procs = [p for p in self.procs if p.pid != pid]

    def add_session(self, pid: int, active: bool = True, name: str = "") -> None:
        self.session_list.append(SessionInfo(pid, active, name))

    # -- HAL -------------------------------------------------------------
    def windows_build(self) -> int:
        return self.build

    def processes(self) -> list:
        if "processes" in self.fail:
            raise self.fail.pop("processes")
        return list(self.procs)

    def sessions(self) -> list:
        return list(self.session_list)

    def qpc_now_100ns(self) -> int:
        return self.qpc

    def open_loopback(self, target_pid: int, include_tree: bool) -> FakeStream:
        self.calls.append(("open_loopback", target_pid, include_tree))
        if "open_loopback" in self.fail:
            raise self.fail.pop("open_loopback")
        s = FakeStream(self, target_pid, include_tree, self.rate, self.channels, self.fmt)
        self.streams.append(s)
        return s

    # -- test helpers ----------------------------------------------------
    @property
    def stream(self) -> FakeStream:
        return self.streams[-1]

    @property
    def clean(self) -> bool:
        return all(s.closed for s in self.streams)

    def feed(self, samples: np.ndarray, *, age_100ns: int = 0, silent: bool = False,
             discontinuity: bool = False, qpc: Optional[int] = None) -> None:
        """Push one packet of float samples (frames x channels, or mono repeated to every channel)."""
        s = self.stream
        x = np.asarray(samples, dtype=np.float32)
        if x.ndim == 1:
            x = np.repeat(x[:, None], s.channels, axis=1)
        frames = x.shape[0]
        if s.sample_format == "float32":
            data = x.astype("<f4").tobytes()
        else:
            data = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
        flags = (AUDCLNT_BUFFERFLAGS_SILENT if silent else 0) | (
            AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY if discontinuity else 0)
        s.push(Packet(b"" if silent else data, frames, flags, self.qpc - age_100ns if qpc is None else qpc))


__all__ = ["FakeWasapi", "FakeStream", "WasapiError"]
