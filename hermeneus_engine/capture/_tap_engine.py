"""One process tap session: tap, private aggregate device, IOProc and conversion.

This runs inside the capture worker process (see ``_tap_worker``). Two readers
exist. The default, ``ioproc``, installs an IOProc through PyObjC whose Python
block only copies each buffer into a queue; the worker is otherwise idle, so the
block gets the GIL at once. The ``portaudio`` reader opens the private aggregate
as a PortAudio input and drains it without a Python callback (see
``_pa_reader``); it measured worse in the worker (see ``_tap_worker.main``) and
stays available on request. Either way a converter thread resamples to engine
format and hands PCM to ``on_pcm``. The converter runs at the macOS
time-constraint priority (see ``_rt``) and does nothing else, so it never sits
preempted while holding the GIL the IOProc needs. A separate housekeeping
thread at normal priority, once per ``refresh_interval``:

* follows the processes the tap should cover (app helpers that start later,
  an excluded process that opens audio later);
* rebuilds the tap when its sample rate changes (the output device changed
  rate or the default output moved to a device with another rate);
* runs the silence watchdog. A tap that delivers exact zeros while a tapped
  process reports running output is either denied by macOS (no error is
  returned in that case) or stalled. The watchdog tears down and rebuilds the
  tap and aggregate, with a capped number of consecutive attempts, and
  reports a status the parent shows to the user.
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

# Watchdog. Seconds of exact digital silence, while a tapped process reports
# running output, before the tap is rebuilt. Each failed attempt doubles the
# wait. After MAX_REBUILDS consecutive attempts without audio the watchdog
# stops rebuilding and reports.
SILENCE_REBUILD_S = 3.0
MAX_REBUILDS = 5

# Status codes sent to the parent.
STATUS_REBUILT = "rebuilt"            # informational: the tap was rebuilt
STATUS_NO_AUDIO = "no-audio"          # never received audio while a process played: permission likely missing
STATUS_STALLED = "stalled"            # audio stopped and rebuilding did not bring it back
STATUS_RECOVERED = "recovered"        # audio is back after a no-audio or stalled report
STATUS_FORMAT_CHANGED = "format-changed"
STATUS_DEVICES_CHANGED = "devices-changed"   # default output or device list changed; tap rebuilt
STATUS_REATTACHED = "reattached"             # the tapped app restarted and the tap follows its new processes

READER_PORTAUDIO = "portaudio"
READER_IOPROC = "ioproc"

PcmCallback = Callable[[bytes, int], None]
StatusCallback = Callable[[str, str, dict], None]


class TapSetupError(RuntimeError):
    """The tap could not be built. The message is shown to the user."""


@dataclass(frozen=True)
class TapSpec:
    """What to tap.

    mode "system": everything except ``exclude_pids``.
    mode "app": every process whose bundle id is ``bundle_id`` or starts with ``bundle_id + "."``.
    mode "pid": the single process ``pid``.
    ``mute``: the tapped processes are silent on their output device while tapped. Only for
    "app" and "pid"; the doctor uses it so its test tone is never heard.
    """

    mode: str
    bundle_id: str = ""
    pid: int = 0
    exclude_pids: tuple[int, ...] = ()
    mute: bool = False

    def __post_init__(self) -> None:
        if self.mute and self.mode == "system":
            raise TapSetupError("a system-wide tap is never muted")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exclude_pids"] = list(self.exclude_pids)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TapSpec":
        return cls(mode=str(d["mode"]), bundle_id=str(d.get("bundle_id", "")), pid=int(d.get("pid", 0)),
                   exclude_pids=tuple(int(p) for p in d.get("exclude_pids", ())), mute=bool(d.get("mute", False)))


@dataclass
class TapStats:
    buffers: int = 0
    frames_in: int = 0
    samples_out: int = 0
    gaps: int = 0
    gap_frames: int = 0
    overflows: int = 0
    reader: str = ""
    dropped_buffers: int = 0
    tap_updates: int = 0
    rebuilds: int = 0
    silent_rebuilds: int = 0
    format_rebuilds: int = 0
    device_rebuilds: int = 0
    reattaches: int = 0
    converter_policy: str = ""
    sample_rate: float = 0.0
    tapped_objects: list = field(default_factory=list)
    last_status: str = ""


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


def audio_expected(hal, spec: TapSpec, objects) -> bool:
    """Whether some process the tap covers reports running output right now."""
    if spec.mode == "system":
        excluded = set(objects)
        candidates = [o for o in hal.process_objects() if o not in excluded]
    else:
        candidates = list(objects)
    for obj in candidates:
        try:
            info = hal.process_info(obj)
        except Exception:
            continue
        if spec.mode == "system" and info.pid in spec.exclude_pids:
            continue
        if info.running_output:
            return True
    return False


class TapCapture:
    def __init__(self, hal, spec: TapSpec, on_pcm: PcmCallback, *, on_status: Optional[StatusCallback] = None,
                 refresh_interval: float = 1.0, silence_rebuild_s: float = SILENCE_REBUILD_S,
                 max_rebuilds: int = MAX_REBUILDS, clock: Callable[[], float] = time.monotonic,
                 reader: str = READER_IOPROC, pa_factory: Optional[Callable[[], object]] = None) -> None:
        if reader not in (READER_PORTAUDIO, READER_IOPROC):
            raise ValueError(f"unknown reader {reader!r}")
        self._reader_kind = reader
        self._pa_factory = pa_factory
        self._pa = None
        self._pa_stream = None
        self._pa_thread: Optional[threading.Thread] = None
        self._pa_stop = threading.Event()
        self._hal = hal
        self._spec = spec
        self._on_pcm = on_pcm
        self._on_status = on_status
        self._refresh_interval = refresh_interval
        self._silence_rebuild_s = silence_rebuild_s
        self._max_rebuilds = max_rebuilds
        self._clock = clock
        self._exclude = spec.mode == "system"
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._converter: Optional[threading.Thread] = None
        self._housekeeper: Optional[threading.Thread] = None
        self._objects: tuple[int, ...] = ()
        self.tap_id: Optional[int] = None
        self.device_id: Optional[int] = None
        self.proc_id: Optional[int] = None
        self._block = None
        self._started = False
        self._generation = 0
        self._rate = 0
        # watchdog state, touched only by the converter thread (and by tick() in tests)
        self._window_peak = 0.0
        self._heard_ever = False
        self._silent_since: Optional[float] = None
        self._attempts = 0
        self._reported = ""
        self._devices: Optional[tuple] = None  # (default output, device list) seen at the last build
        self.stats = TapStats(reader=reader)

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Build and start the tap and the converter. On failure everything built so far is released."""
        try:
            objects = resolve_objects(self._hal, self._spec)
            if not objects and not self._exclude:
                if self._spec.mode == "app":
                    raise TapSetupError(f"{self._spec.bundle_id} is not running or has not opened audio yet.")
                raise TapSetupError(f"Process {self._spec.pid} is not running or has not opened audio yet.")
            with self._lock:
                self._build(objects)
            self._converter = threading.Thread(target=self._convert, name="hermeneus-tap-convert", daemon=True)
            self._converter.start()
            self._housekeeper = threading.Thread(target=self._housekeep, name="hermeneus-tap-housekeeping",
                                                 daemon=True)
            self._housekeeper.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> list[str]:
        """Release the IOProc, aggregate device and tap in reverse order. Safe to call more than once.

        Returns the teardown steps that failed; it never raises.
        """
        self._stopping.set()
        keeper = self._housekeeper
        if keeper is not None and keeper is not threading.current_thread():
            keeper.join(timeout=5.0)  # it may be mid-rebuild; rebuild() checks _stopping before touching the HAL
        self._housekeeper = None
        with self._lock:
            failures = self._release()
        self._queue.put(None)
        conv = self._converter
        if conv is not None and conv is not threading.current_thread():
            conv.join(timeout=2.0)
        self._converter = None
        return failures

    # ------------------------------------------------------------------ HAL objects (caller holds the lock)

    def _build(self, objects) -> None:
        hal = self._hal
        try:
            handle = (hal.create_tap(list(objects), self._exclude, TAP_NAME, mute=True) if self._spec.mute
                      else hal.create_tap(list(objects), self._exclude, TAP_NAME))
            self.tap_id = handle.tap_id
            self._objects = tuple(objects)
            self.stats.tapped_objects = list(objects)
            fmt = hal.tap_format(self.tap_id)
            if (fmt.format_id != FORMAT_LINEAR_PCM or not fmt.format_flags & FLAG_IS_FLOAT
                    or fmt.bits_per_channel != 32):
                raise TapSetupError("The tap delivers an audio format this backend does not read "
                                    "(expected 32-bit float PCM).")
            if fmt.sample_rate <= 0 or fmt.channels < 1:
                raise TapSetupError("The tap reported an invalid audio format.")
            self.stats.sample_rate = fmt.sample_rate
            self._rate = int(round(fmt.sample_rate))
            tag = uuid.uuid4().hex[:8]
            device_name = f"{TAP_NAME} {tag}"
            self.device_id = hal.create_aggregate(handle.uid, device_name, f"hermeneus-tap-{tag}")
            self._generation += 1
            generation, rate, q, stats = self._generation, self._rate, self._queue, self.stats
            if self._reader_kind == READER_PORTAUDIO:
                self._open_portaudio(device_name, generation, rate)
                return

            def on_audio(sample_time: float, host_time: int, data: bytes, channels: int) -> None:
                # HAL IO thread: copy out and return.
                if q.qsize() >= QUEUE_LIMIT:
                    stats.dropped_buffers += 1
                    return
                q.put((generation, rate, sample_time, host_time, False, data, channels))

            self.proc_id, self._block = hal.create_ioproc(self.device_id, on_audio)
            hal.start(self.device_id, self.proc_id)
            self._started = True
        except BaseException:
            self._release()
            raise

    def _open_portaudio(self, device_name: str, generation: int, rate: int) -> None:
        if self._pa is None:
            if self._pa_factory is None:
                from ._pa_reader import PortAudioReader

                self._pa = PortAudioReader()
            else:
                self._pa = self._pa_factory()
        stream = self._pa.open(device_name, rate)
        self._pa_stream = stream
        stop = threading.Event()
        self._pa_stop = stop
        q, stats = self._queue, self.stats
        latency_ns = int(getattr(stream, "latency_s", 0.0) * 1e9)

        def drain() -> None:
            sample_time = 0.0
            while not stop.is_set():
                try:
                    x, overflowed = stream.read(stop=stop)
                except Exception:
                    if not stop.is_set():
                        log.warning("PortAudio read failed; the watchdog will rebuild the tap")
                    return
                now = time.monotonic_ns()
                frames = int(x.shape[0])
                if overflowed:
                    stats.overflows += 1
                if not frames:
                    continue
                # Capture time of the first sample: the read returned once the block was complete,
                # and PortAudio reports how long input takes to reach the ring buffer.
                stamp = now - int(frames * 1e9 / rate) - latency_ns
                if q.qsize() >= QUEUE_LIMIT:
                    stats.dropped_buffers += 1
                else:
                    q.put((generation, rate, sample_time, stamp, True, x.astype("<f4").tobytes(), 1))
                sample_time += frames

        self._pa_thread = threading.Thread(target=drain, name="hermeneus-tap-portaudio", daemon=True)
        self._pa_thread.start()

    def _release(self) -> list[str]:
        hal = self._hal
        failures = []
        if self._pa_stream is not None:
            # Stop the drain thread first (it polls, so it returns within one poll interval),
            # then close the stream it was reading.
            self._pa_stop.set()
            thread = self._pa_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
                if thread.is_alive():
                    log.error("process tap teardown: the PortAudio drain thread did not stop")
                    failures.append("PortAudio drain thread did not stop")
            try:
                self._pa_stream.close()
            except Exception as exc:
                log.error("process tap teardown: closing the PortAudio stream failed: %s", exc)
                failures.append(f"close PortAudio stream: {exc}")
            self._pa_stream = None
            self._pa_thread = None
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
        return failures

    def rebuild(self, reason: str) -> bool:
        """Tear down and recreate tap, aggregate and IOProc. Returns False when the rebuild failed."""
        if self._stopping.is_set():
            return False
        with self._lock:
            if self._stopping.is_set():
                return False
            self._release()
            try:
                objects = resolve_objects(self._hal, self._spec) or list(self._objects)
                self._build(objects)
            except Exception as exc:
                log.warning("process tap rebuild (%s) failed: %s", reason, exc)
                return False
            self.stats.rebuilds += 1
            if reason == "silence":
                self.stats.silent_rebuilds += 1
            elif reason == "format":
                self.stats.format_rebuilds += 1
            elif reason == "devices":
                self.stats.device_rebuilds += 1
        return True

    # ------------------------------------------------------------------ converter

    def _housekeep(self) -> None:
        while not self._stopping.wait(self._refresh_interval):
            try:
                self.tick()
            except Exception:
                log.exception("process tap housekeeping failed")

    def _convert(self) -> None:
        from ._rt import promote_time_constraint

        # One 512-frame buffer at 48 kHz is 10.67 ms; converting it measured p99 0.64 ms at this
        # priority (W20), so 2 ms of computation per period leaves room without starving others.
        period_ns = int(512 * 1e9 / (self._rate or 48000))
        self.stats.converter_policy = promote_time_constraint(period_ns, 2_000_000, period_ns)
        stats = self.stats
        to_ns = self._hal.host_time_to_ns
        resampler: Optional[StreamingResampler] = None
        current = (0, 0)  # (generation, rate) the resampler belongs to
        expected: Optional[float] = None
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:  # queued after the IOProc stopped, so every buffer before it is converted
                return
            if item:
                generation, rate, sample_time, stamp, stamp_is_ns, data, channels = item
                if generation != self._generation:
                    pass  # left over from a tap that was rebuilt
                else:
                    if (generation, rate) != current:
                        if resampler is None or rate != current[1]:
                            resampler = StreamingResampler(rate, ENGINE_SAMPLE_RATE)
                        current = (generation, rate)
                        expected = None
                    x = np.frombuffer(data, dtype="<f4")
                    if channels > 1:
                        x = x[: (x.shape[0] // channels) * channels].reshape(-1, channels).mean(axis=1)
                    frames = int(x.shape[0])
                    if frames:
                        peak = float(np.max(np.abs(x)))
                        if peak > self._window_peak:
                            self._window_peak = peak
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
                        base = stamp if stamp_is_ns else to_ns(stamp)
                        # First output sample: its input position after the chunk start, less the
                        # filter's group delay (0.99 ms at 48 kHz).
                        shift = (offset / rate - resampler.delay_seconds) * 1e9
                        self._on_pcm(float_to_s16le(y), base + int(round(shift)))

    def tick(self, now: Optional[float] = None) -> None:
        """Periodic work: follow processes, notice format changes, run the silence watchdog."""
        if self._stopping.is_set():
            return
        now = self._clock() if now is None else now
        self.refresh()
        self._check_devices()
        self._check_format()
        self._watchdog(now)

    def _status(self, code: str, message: str, **details) -> None:
        self.stats.last_status = code
        if self._on_status is not None:
            try:
                self._on_status(code, message, details)
            except Exception:
                log.exception("status callback failed")

    def _device_snapshot(self) -> Optional[tuple]:
        try:
            return (self._hal.default_output_device(), tuple(sorted(self._hal.device_ids())))
        except Exception as exc:
            log.warning("process tap: reading the device list failed: %s", exc)
            return None

    def _check_devices(self) -> None:
        """Rebuild when the default output or the set of audio devices changed.

        Polled once per tick (two property reads) instead of a HAL listener block, because a
        listener block registered through PyObjC cannot be removed reliably (removal needs the
        same block object, which PyObjC does not guarantee). The first tick only records the state.
        """
        snap = self._device_snapshot()
        if snap is None:
            return
        if self._devices is None:
            self._devices = snap
            return
        if snap == self._devices:
            return
        old = self._devices
        self._devices = snap
        if self.rebuild("devices"):
            name = self._hal.device_name(snap[0]) if snap[0] != old[0] else ""
            text = (f"The output device changed to {name}; capture was rebuilt." if name
                    else "The list of audio devices changed; capture was rebuilt.")
            self._status(STATUS_DEVICES_CHANGED, text, default_output_changed=snap[0] != old[0],
                         devices_added=len(set(snap[1]) - set(old[1])),
                         devices_removed=len(set(old[1]) - set(snap[1])))

    def _check_format(self) -> None:
        tap_id = self.tap_id
        if tap_id is None:
            return
        try:
            rate = int(round(self._hal.tap_format(tap_id).sample_rate))
        except Exception as exc:
            log.warning("process tap: reading the tap format failed: %s", exc)
            return
        if rate and rate != self._rate:
            old = self._rate
            if self.rebuild("format"):
                self._status(STATUS_FORMAT_CHANGED, f"The output sample rate changed from {old} Hz to {rate} Hz; "
                             "capture was rebuilt.", old_rate=old, new_rate=rate)

    def _watchdog(self, now: float) -> None:
        peak, self._window_peak = self._window_peak, 0.0
        if peak > 0.0:
            if self._reported in (STATUS_NO_AUDIO, STATUS_STALLED):
                self._status(STATUS_RECOVERED, "Audio is arriving again.")
            self._heard_ever = True
            self._silent_since = None
            self._attempts = 0
            self._reported = ""
            return
        try:
            playing = audio_expected(self._hal, self._spec, self._objects)
        except Exception as exc:
            log.warning("process tap watchdog: reading process state failed: %s", exc)
            return
        if not playing:
            self._silent_since = None
            return
        if self._silent_since is None:
            self._silent_since = now
            return
        wait = self._silence_rebuild_s * (2 ** self._attempts)
        if now - self._silent_since < wait:
            return
        if self._attempts >= self._max_rebuilds:
            code = STATUS_STALLED if self._heard_ever else STATUS_NO_AUDIO
            if self._reported != code:
                self._reported = code
                if code == STATUS_NO_AUDIO:
                    self._status(code, "macOS is delivering silence while the captured audio is playing. "
                                 "Audio recording permission is probably not granted.", attempts=self._attempts)
                else:
                    self._status(code, f"Capture stopped receiving audio and {self._attempts} rebuilds did not "
                                 "bring it back.", attempts=self._attempts)
            self._silent_since = now
            return
        self._attempts += 1
        ok = self.rebuild("silence")
        self._silent_since = now
        self._status(STATUS_REBUILT, "Capture delivered silence while audio was playing; the tap was rebuilt.",
                     attempt=self._attempts, ok=ok)
        if not self._heard_ever and self._attempts == 2 and self._reported != STATUS_NO_AUDIO:
            # Two fresh taps in a row gave nothing at all: tell the user now instead of after the cap.
            self._reported = STATUS_NO_AUDIO
            self._status(STATUS_NO_AUDIO, "macOS is delivering silence while the captured audio is playing. "
                         "Audio recording permission is probably not granted.", attempts=self._attempts)

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
                if self._spec.mute:
                    self._hal.set_tap_processes(self.tap_id, list(objects), self._exclude, TAP_NAME, mute=True)
                else:
                    self._hal.set_tap_processes(self.tap_id, list(objects), self._exclude, TAP_NAME)
            except Exception as exc:
                log.warning("process tap refresh: updating the tap failed: %s", exc)
                return False
            previous = self._objects
            self._objects = objects
            self.stats.tap_updates += 1
            self.stats.tapped_objects = list(objects)
        if self._spec.mode == "app" and previous and objects and not set(previous) & set(objects):
            self.stats.reattaches += 1
            self._status(STATUS_REATTACHED, f"{self._spec.bundle_id} restarted; capture follows it again.",
                         processes=len(objects))
        return True
