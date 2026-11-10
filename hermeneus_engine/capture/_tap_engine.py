"""One process tap session: tap, private aggregate device, IOProc and conversion.

This runs inside the capture worker process (see ``_tap_worker``). The HAL IO
thread only copies each buffer into a queue; a converter thread resamples to
engine format and hands PCM to ``on_pcm``. The converter thread also follows
the processes the tap should cover, so app helpers that start later and an
exclusion target that opens audio later are picked up.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import numpy as np

from ..contracts import ENGINE_SAMPLE_RATE
from ._coreaudio import FLAG_IS_FLOAT, FORMAT_LINEAR_PCM
from ._resample import StreamingResampler, float_to_s16le

log = logging.getLogger(__name__)

TAP_NAME = "Hermeneus capture"

# Buffers waiting for the converter. At 512 frames per buffer and 48 kHz this
# is about 2.7 s. Past that the converter is stuck and new buffers are dropped
# and counted rather than growing memory without bound.
QUEUE_LIMIT = 256

# Called with engine-format PCM and the monotonic_ns timestamp of its first sample.
PcmCallback = Callable[[bytes, int], None]


class TapSetupError(RuntimeError):
    """The tap could not be built. The message is shown to the user."""


@dataclass(frozen=True)
class TapSpec:
    """What to tap.

    mode "system": everything except ``exclude_pids``.
    mode "app": every process whose bundle id is ``bundle_id`` or starts with ``bundle_id + "."``.
    mode "pid": the single process ``pid``.
    """

    mode: str
    bundle_id: str = ""
    pid: int = 0
    exclude_pids: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exclude_pids"] = list(self.exclude_pids)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TapSpec":
        return cls(mode=str(d["mode"]), bundle_id=str(d.get("bundle_id", "")), pid=int(d.get("pid", 0)),
                   exclude_pids=tuple(int(p) for p in d.get("exclude_pids", ())))


@dataclass
class TapStats:
    buffers: int = 0
    frames_in: int = 0
    samples_out: int = 0
    gaps: int = 0
    gap_frames: int = 0
    dropped_buffers: int = 0
    tap_updates: int = 0
    sample_rate: float = 0.0
    tapped_objects: list = field(default_factory=list)


def resolve_objects(hal, spec: TapSpec) -> list[int]:
    """Process objects the tap should list for ``spec`` right now."""
    if spec.mode == "system":
        objects = []
        for pid in spec.exclude_pids:
            obj = hal.process_object_for_pid(pid)
            if obj:
                objects.append(obj)
        return sorted(set(objects))
    if spec.mode == "pid":
        obj = hal.process_object_for_pid(spec.pid)
        return [obj] if obj else []
    if spec.mode == "app":
        prefix = spec.bundle_id + "."
        objects = []
        for obj in hal.process_objects():
            try:
                info = hal.process_info(obj)
            except Exception:  # the process exited between listing and query
                continue
            if info.pid in spec.exclude_pids:
                continue
            if info.bundle_id == spec.bundle_id or info.bundle_id.startswith(prefix):
                objects.append(obj)
        return sorted(set(objects))
    raise TapSetupError(f"unknown tap mode {spec.mode!r}")


class TapCapture:
    def __init__(self, hal, spec: TapSpec, on_pcm: PcmCallback, *, refresh_interval: float = 1.0) -> None:
        self._hal = hal
        self._spec = spec
        self._on_pcm = on_pcm
        self._refresh_interval = refresh_interval
        self._exclude = spec.mode == "system"
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._converter: Optional[threading.Thread] = None
        self._objects: tuple[int, ...] = ()
        self.tap_id: Optional[int] = None
        self.device_id: Optional[int] = None
        self.proc_id: Optional[int] = None
        self._block = None
        self._started = False
        self.stats = TapStats()

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Build and start the tap. On any failure everything built so far is released."""
        try:
            self._open()
        except BaseException:
            self.close()
            raise

    def _open(self) -> None:
        hal = self._hal
        objects = resolve_objects(hal, self._spec)
        if not objects and not self._exclude:
            if self._spec.mode == "app":
                raise TapSetupError(f"{self._spec.bundle_id} is not running or has not opened audio yet.")
            raise TapSetupError(f"Process {self._spec.pid} is not running or has not opened audio yet.")
        handle = hal.create_tap(objects, self._exclude, TAP_NAME)
        self.tap_id = handle.tap_id
        self._objects = tuple(objects)
        self.stats.tapped_objects = list(objects)
        fmt = hal.tap_format(self.tap_id)
        if fmt.format_id != FORMAT_LINEAR_PCM or not fmt.format_flags & FLAG_IS_FLOAT or fmt.bits_per_channel != 32:
            raise TapSetupError("The tap delivers an audio format this backend does not read (expected 32-bit float PCM).")
        if fmt.sample_rate <= 0 or fmt.channels < 1:
            raise TapSetupError("The tap reported an invalid audio format.")
        self.stats.sample_rate = fmt.sample_rate
        self.device_id = hal.create_aggregate(handle.uid, TAP_NAME, f"hermeneus-tap-{uuid.uuid4()}")
        q = self._queue
        stats = self.stats

        def on_audio(sample_time: float, host_time: int, data: bytes, channels: int) -> None:
            # HAL IO thread: copy out and return.
            if q.qsize() >= QUEUE_LIMIT:
                stats.dropped_buffers += 1
                return
            q.put((sample_time, host_time, data, channels))

        self.proc_id, self._block = hal.create_ioproc(self.device_id, on_audio)
        self._converter = threading.Thread(target=self._convert, args=(int(round(fmt.sample_rate)),),
                                           name="hermeneus-tap-convert", daemon=True)
        self._converter.start()
        hal.start(self.device_id, self.proc_id)
        self._started = True

    def close(self) -> list[str]:
        """Release the IOProc, aggregate device and tap in reverse order. Safe to call more than once.

        Returns the teardown steps that failed; it never raises.
        """
        failures = []
        with self._lock:
            hal = self._hal
            steps = []
            if self._started:
                steps.append(("stop aggregate device", hal.stop, (self.device_id, self.proc_id)))
            if self.proc_id is not None:
                steps.append(("destroy IOProc", hal.destroy_ioproc, (self.device_id, self.proc_id)))
            if self.device_id is not None:
                steps.append(("destroy aggregate device", hal.destroy_aggregate, (self.device_id,)))
            if self.tap_id is not None:
                steps.append(("destroy process tap", hal.destroy_tap, (self.tap_id,)))
            for what, fn, args in steps:
                try:
                    fn(*args)
                except Exception as exc:
                    log.error("process tap teardown: %s failed: %s", what, exc)
                    failures.append(f"{what}: {exc}")
            self._started = False
            self.proc_id = self.device_id = self.tap_id = None
            self._block = None
            self._stopping.set()
        self._queue.put(None)
        conv = self._converter
        if conv is not None and conv is not threading.current_thread():
            conv.join(timeout=2.0)
        self._converter = None
        return failures

    # ------------------------------------------------------------------ converter

    def _convert(self, rate: int) -> None:
        resampler = StreamingResampler(rate, ENGINE_SAMPLE_RATE)
        stats = self.stats
        to_ns = self._hal.host_time_to_ns
        expected: Optional[float] = None
        next_refresh = time.monotonic() + self._refresh_interval
        while True:
            try:
                item = self._queue.get(timeout=self._refresh_interval)
            except queue.Empty:
                item = ()
            if item is None:  # queued after the IOProc stopped, so every buffer before it is converted
                return
            if item:
                sample_time, host_time, data, channels = item
                x = np.frombuffer(data, dtype="<f4")
                if channels > 1:
                    x = x[: (x.shape[0] // channels) * channels].reshape(-1, channels).mean(axis=1)
                frames = int(x.shape[0])
                if expected is not None and abs(sample_time - expected) > 0.5:
                    stats.gaps += 1
                    stats.gap_frames += int(round(abs(sample_time - expected)))
                expected = sample_time + frames
                stats.buffers += 1
                stats.frames_in += frames
                offset = resampler.next_output_offset
                y = resampler.process(x)
                if y.shape[0]:
                    stats.samples_out += int(y.shape[0])
                    self._on_pcm(float_to_s16le(y), to_ns(host_time) + int(offset * 1e9 / rate))
            if time.monotonic() >= next_refresh:
                next_refresh = time.monotonic() + self._refresh_interval
                self.refresh()

    def refresh(self) -> bool:
        """Point the tap at the processes ``spec`` covers now. Returns True when the tap changed."""
        if self._spec.mode == "pid":
            return False
        try:
            objects = tuple(resolve_objects(self._hal, self._spec))
        except Exception as exc:
            log.warning("process tap refresh: reading the process list failed: %s", exc)
            return False
        if set(objects) == set(self._objects) or (not objects and not self._exclude):
            return False
        with self._lock:
            if self._stopping.is_set() or self.tap_id is None:
                return False
            try:
                self._hal.set_tap_processes(self.tap_id, list(objects), self._exclude, TAP_NAME)
            except Exception as exc:
                log.warning("process tap refresh: updating the tap failed: %s", exc)
                return False
            self._objects = objects
            self.stats.tap_updates += 1
            self.stats.tapped_objects = list(objects)
        return True
