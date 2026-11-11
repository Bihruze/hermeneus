"""One process loopback session: target, stream, conversion and watchdog.

Platform neutral. It runs inside the Windows capture worker
(``_loopback_worker``) against ``_wasapi.WasapiHAL``, and in the tests against a
fake with the same methods:

* ``processes() -> list[ProcInfo]``, ``sessions() -> list[SessionInfo]``
* ``open_loopback(target_pid, include_tree) -> stream`` where the stream has
  ``sample_rate``, ``channels``, ``sample_format`` ("float32" or "int16"),
  ``start()``, ``wait(timeout_s) -> bool``, ``read() -> list[Packet]`` and
  ``close() -> list[str]``
* ``qpc_now_100ns() -> int``

One pump thread reads packets as their event fires, downmixes to mono,
resamples with ``virtual_device.PolyphaseResampler`` and hands engine PCM to
``on_pcm``. It does nothing else, so a slow process or session listing can
never delay a read. A separate tick thread, once per ``refresh_interval``:

* follows the app: when the root process of ``app:<exe>`` changes (the app
  was restarted), the stream is rebuilt on the new root;
* runs the silence watchdog with the same codes and back-off as the macOS
  process tap (``_tap_engine`` on the W02 branch): a stream that delivers only
  silence while a covered process has an active audio session is rebuilt,
  with a capped number of consecutive attempts, and the status is reported;
* reopens a stream that failed (device invalidated, audio service restarted).

Process loopback is not tied to an output device, so a change of the default
output device needs no rebuild, and the format is fixed by AUTOCONVERTPCM, so a
device rate change needs none either. Both are assumptions until a Windows run
confirms them; the watchdog covers them if they turn out wrong.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import numpy as np

from ..contracts import ENGINE_SAMPLE_RATE
from .virtual_device import PolyphaseResampler, float_to_s16le

log = logging.getLogger(__name__)

SILENCE_REBUILD_S = 3.0
MAX_REBUILDS = 5
WAIT_S = 0.1

STATUS_REBUILT = "rebuilt"
STATUS_NO_AUDIO = "no-audio"
STATUS_STALLED = "stalled"
STATUS_RECOVERED = "recovered"
STATUS_RETARGETED = "retargeted"

# Shown when a stream delivers silence while a covered app plays. Windows has no
# documented permission for process loopback; the microphone privacy switch is
# the one setting that could plausibly silence it (UNVERIFIED, see the W24 report).
NO_AUDIO_MESSAGE = ("Windows is delivering silence while the captured audio is playing. If Settings > "
                    "Privacy & security > Microphone blocks desktop apps, allow them and start again.")

PcmCallback = Callable[[bytes, int], None]
StatusCallback = Callable[[str, str, dict], None]


class LoopbackSetupError(RuntimeError):
    """The capture could not be built. The message is shown to the user."""


@dataclass(frozen=True)
class LoopbackSpec:
    """What to capture.

    mode "system": every process except the tree rooted at ``exclude_pid`` (Hermeneus).
    mode "app": the process tree of the oldest running ``exe`` that is not an ancestor of ``own_pid``.
    mode "pid": the process tree rooted at ``pid``, refused when that tree holds ``own_pid``.
    """

    mode: str
    exe: str = ""
    pid: int = 0
    exclude_pid: int = 0
    own_pid: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "LoopbackSpec":
        spec = cls(mode=str(d["mode"]), exe=str(d.get("exe", "")).lower(), pid=int(d.get("pid", 0)),
                   exclude_pid=int(d.get("exclude_pid", 0)), own_pid=int(d.get("own_pid", 0)))
        if spec.mode not in ("system", "app", "pid"):
            raise ValueError(f"unknown capture mode {spec.mode!r}")
        if spec.mode == "system" and spec.exclude_pid <= 0:
            raise ValueError("system capture needs exclude_pid")
        if spec.mode == "app" and not spec.exe:
            raise ValueError("app capture needs exe")
        if spec.mode == "pid" and spec.pid <= 0:
            raise ValueError("pid capture needs pid")
        return spec


@dataclass
class LoopbackStats:
    packets: int = 0
    frames_in: int = 0
    samples_out: int = 0
    silent_packets: int = 0
    discontinuities: int = 0
    rebuilds: int = 0
    stream_errors: int = 0
    timestamp_source: str = ""     # "qpc" or "arrival"
    timestamp_errors: int = 0      # packets flagged AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR (arrival time used)
    tick_ns_max: int = 0           # longest periodic tick (process and session reads, any rebuild)
    tick_ns_total: int = 0
    ticks: int = 0
    sample_rate: int = 0
    channels: int = 0
    sample_format: str = ""
    target_pid: int = 0
    last_status: str = ""
    teardown_failures: list = field(default_factory=list)


# ---------------------------------------------------------------------------- process trees

def _children(procs) -> dict:
    by_pid = {p.pid: p for p in procs}
    kids: dict = {}
    for p in procs:
        parent = by_pid.get(p.ppid)
        # Windows reuses PIDs and keeps a dead parent's id in ppid; a child
        # created before its "parent" belongs to an earlier process with that id.
        if parent is not None and p.pid != p.ppid and p.create_time >= parent.create_time:
            kids.setdefault(p.ppid, []).append(p.pid)
    return kids


def process_tree(procs, root: int) -> set[int]:
    kids = _children(procs)
    seen, todo = set(), [root]
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        todo.extend(kids.get(pid, ()))
    return seen


def app_roots(procs, exe: str, own_pid: int) -> list:
    """Processes named ``exe`` whose parent is not, oldest first, leaving out ancestors of ``own_pid``.

    Process loopback includes a whole tree, so an app whose tree holds
    Hermeneus cannot be captured without capturing Hermeneus's own speech.
    """
    exe = exe.lower()
    by_pid = {p.pid: p for p in procs}
    roots = []
    for p in procs:
        if p.name != exe:
            continue
        parent = by_pid.get(p.ppid)
        if parent is not None and parent.name == exe and p.create_time >= parent.create_time:
            continue
        if own_pid and own_pid in process_tree(procs, p.pid):
            continue
        roots.append(p)
    return sorted(roots, key=lambda p: (p.create_time, p.pid))


def resolve_target(hal, spec: LoopbackSpec) -> tuple[int, bool]:
    """(target pid, include tree) for ``spec`` right now. Raises LoopbackSetupError."""
    if spec.mode == "system":
        return spec.exclude_pid, False
    procs = hal.processes()
    if spec.mode == "pid":
        if not any(p.pid == spec.pid for p in procs):
            raise LoopbackSetupError(f"Process {spec.pid} is not running.")
        if spec.own_pid and spec.own_pid in process_tree(procs, spec.pid):
            # INCLUDE takes the whole tree, so this would capture Hermeneus's own spoken translation.
            raise LoopbackSetupError(f"Process {spec.pid} is Hermeneus or one of its parent processes; capturing "
                                     "it would capture Hermeneus's own speech. Use --source system instead.")
        return spec.pid, True
    roots = app_roots(procs, spec.exe, spec.own_pid)
    if not roots:
        raise LoopbackSetupError(f"{spec.exe} is not running.")
    return roots[0].pid, True


def audio_expected(hal, spec: LoopbackSpec, target_pid: int) -> bool:
    """Whether a process the capture covers has an active audio session."""
    active = [s for s in hal.sessions() if s.active]
    if not active:
        return False
    tree = process_tree(hal.processes(), target_pid)
    if spec.mode == "system":
        return any(s.pid not in tree for s in active)
    return any(s.pid in tree for s in active)


# ---------------------------------------------------------------------------- capture

class LoopbackCapture:
    def __init__(self, hal, spec: LoopbackSpec, on_pcm: PcmCallback, *,
                 on_status: Optional[StatusCallback] = None, refresh_interval: float = 1.0,
                 silence_rebuild_s: float = SILENCE_REBUILD_S, max_rebuilds: int = MAX_REBUILDS,
                 clock: Callable[[], float] = time.monotonic,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._hal = hal
        self._spec = spec
        self._on_pcm = on_pcm
        self._on_status = on_status
        self._refresh_interval = refresh_interval
        self._silence_rebuild_s = silence_rebuild_s
        self._max_rebuilds = max_rebuilds
        self._clock = clock
        self._clock_ns = clock_ns
        self._stream = None
        self._resampler: Optional[PolyphaseResampler] = None
        self._target = 0
        self._stopping = threading.Event()
        self._pump: Optional[threading.Thread] = None
        self._ticker: Optional[threading.Thread] = None
        # Held while the pump reads and converts, and while a rebuild swaps the stream.
        # The tick thread reads processes and sessions without it.
        self._lock = threading.RLock()
        self.stats = LoopbackStats()
        # watchdog state, touched only by the tick thread (and by tick() in tests); the pump writes _window_peak
        self._window_peak = 0.0
        self._heard_ever = False
        self._silent_since: Optional[float] = None
        self._attempts = 0
        self._reported = ""
        self._broken_since: Optional[float] = None

    # ------------------------------------------------------------------ lifecycle

    def open(self, *, start_pump: bool = True) -> None:
        """Build and start the stream. Raises LoopbackSetupError; nothing is left open on failure."""
        target, include = resolve_target(self._hal, self._spec)
        try:
            self._build(target, include)
        except LoopbackSetupError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise LoopbackSetupError(f"Windows refused the process loopback capture: {exc}") from exc
        if start_pump:
            self._pump = threading.Thread(target=self._with_com, args=(self._pump_loop,),
                                          name="hermeneus-loopback-pump", daemon=True)
            self._ticker = threading.Thread(target=self._with_com, args=(self._tick_loop,),
                                            name="hermeneus-loopback-tick", daemon=True)
            self._pump.start()
            self._ticker.start()

    def _build(self, target: int, include: bool) -> None:
        stream = self._hal.open_loopback(target, include)
        if self._stopping.is_set():
            # close() ran while the activation was in flight: never start a stream after teardown.
            self.stats.teardown_failures.extend(stream.close())
            raise LoopbackSetupError("capture is stopping")
        try:
            if stream.sample_format not in ("float32", "int16") or stream.channels < 1 or stream.sample_rate <= 0:
                raise LoopbackSetupError("The loopback stream reported a format this backend does not read.")
            stream.start()
        except BaseException:
            self.stats.teardown_failures.extend(stream.close())
            raise
        self._stream = stream
        self._target = target
        self._resampler = PolyphaseResampler(int(stream.sample_rate), ENGINE_SAMPLE_RATE)
        self.stats.sample_rate = int(stream.sample_rate)
        self.stats.channels = int(stream.channels)
        self.stats.sample_format = stream.sample_format
        self.stats.target_pid = target

    def _release(self) -> list[str]:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return []
        try:
            return list(stream.close())
        except Exception as exc:
            return [f"close stream: {exc}"]

    def close(self) -> list[str]:
        """Stop the pump and release the stream. Safe to call more than once; never raises."""
        self._stopping.set()
        for t in (self._pump, self._ticker):
            if t is not None and t is not threading.current_thread():
                t.join(timeout=2.0)
        self._pump = self._ticker = None
        failures = self._release()
        self.stats.teardown_failures.extend(failures)
        return failures

    # ------------------------------------------------------------------ pump

    def _with_com(self, body: Callable[[], None]) -> None:
        enter = getattr(self._hal, "thread_enter", None)
        if enter is not None:
            enter()
        try:
            body()
        finally:
            leave = getattr(self._hal, "thread_leave", None)
            if leave is not None:
                leave()

    def _pump_loop(self) -> None:
        while not self._stopping.is_set():
            self.pump_once()

    def _tick_loop(self) -> None:
        while not self._stopping.wait(self._refresh_interval):
            t0 = time.perf_counter_ns()
            try:
                self.tick()
            except Exception:
                log.exception("process loopback: periodic check failed")
            dt = time.perf_counter_ns() - t0
            self.stats.ticks += 1
            self.stats.tick_ns_total += dt
            self.stats.tick_ns_max = max(self.stats.tick_ns_max, dt)

    def pump_once(self) -> None:
        stream = self._stream
        if stream is None:
            self._stopping.wait(WAIT_S)
            return
        stream.wait(WAIT_S)
        with self._lock:
            if self._stream is not stream:
                return  # rebuilt while waiting
            try:
                packets = stream.read()
            except Exception as exc:
                self.stats.stream_errors += 1
                log.warning("process loopback: %s; the stream will be reopened", exc)
                self.stats.teardown_failures.extend(self._release())
                if self._broken_since is None:
                    self._broken_since = self._clock()
                return
            now_ns = self._clock_ns()
            qpc_now = self._hal.qpc_now_100ns() if packets else 0
            for p in packets:
                self._convert(p, now_ns, qpc_now)

    def _convert(self, p, now_ns: int, qpc_now: int) -> None:
        stream, resampler = self._stream, self._resampler
        if stream is None or resampler is None or p.frames <= 0:
            return
        stats = self.stats
        channels = int(stream.channels)
        rate = int(stream.sample_rate)
        if p.silent or not p.data:
            mono = np.zeros(p.frames, dtype=np.float32)
            stats.silent_packets += 1
        else:
            if stream.sample_format == "float32":
                x = np.frombuffer(p.data, dtype="<f4")
            else:
                x = np.frombuffer(p.data, dtype="<i2").astype(np.float32) / np.float32(32768.0)
            x = x[: (x.shape[0] // channels) * channels]
            mono = x.reshape(-1, channels).mean(axis=1, dtype=np.float32) if channels > 1 else x
            if mono.shape[0]:
                peak = float(np.max(np.abs(mono)))
                if peak > self._window_peak:
                    self._window_peak = peak
        if p.discontinuity:
            stats.discontinuities += 1
        stats.packets += 1
        stats.frames_in += int(mono.shape[0])
        # Age of the first frame from the performance counter the packet carries;
        # the block duration when that value is missing or implausible.
        timestamp_error = bool(getattr(p, "timestamp_error", False))
        if timestamp_error:
            stats.timestamp_errors += 1
        age_ns = (qpc_now - p.qpc_100ns) * 100 if p.qpc_100ns and qpc_now and not timestamp_error else -1
        if 0 <= age_ns < 1_000_000_000:
            if not stats.timestamp_source:
                stats.timestamp_source = "qpc"
        else:
            age_ns = int(mono.shape[0] * 1e9 / rate)
            if not stats.timestamp_source:
                stats.timestamp_source = "arrival"
        block_start = resampler.input_count
        k0 = resampler.output_count
        out = resampler.process(mono)
        if out.shape[0]:
            ts = now_ns - age_ns + int(round((resampler.output_time(k0) - block_start) * 1e9 / rate))
            stats.samples_out += int(out.shape[0])
            self._on_pcm(float_to_s16le(out), ts)

    # ------------------------------------------------------------------ periodic work

    def tick(self, now: Optional[float] = None) -> None:
        if self._stopping.is_set():
            return
        now = self._clock() if now is None else now
        if self._stream is None:
            self._reopen(now)
            return
        if self._spec.mode == "app" and self._follow_app():
            return
        self._watchdog(now)

    def _status(self, code: str, message: str, **details) -> None:
        self.stats.last_status = code
        if self._on_status is not None:
            try:
                self._on_status(code, message, details)
            except Exception:
                log.exception("status callback failed")

    def rebuild(self, reason: str) -> bool:
        try:
            target, include = resolve_target(self._hal, self._spec)  # process list read outside the lock
        except Exception as exc:
            log.warning("process loopback: rebuild after %s failed: %s", reason, exc)
            with self._lock:
                self.stats.teardown_failures.extend(self._release())
            return False
        with self._lock:
            self.stats.teardown_failures.extend(self._release())
            try:
                self._build(target, include)
            except Exception as exc:
                log.warning("process loopback: rebuild after %s failed: %s", reason, exc)
                return False
        self.stats.rebuilds += 1
        return True

    def _reopen(self, now: float) -> None:
        since = self._broken_since if self._broken_since is not None else now
        self._broken_since = since
        # The first reopen is immediate; later ones back off like the silence watchdog, capped at a minute.
        if self._attempts and now - since < min(self._silence_rebuild_s * (2 ** self._attempts), 60.0):
            return
        self._attempts += 1
        if self.rebuild("stream error"):
            self._broken_since = None
            self._status(STATUS_REBUILT, "The capture stream failed and was reopened.", attempt=self._attempts)
            return
        self._broken_since = now
        if self._attempts >= self._max_rebuilds and self._reported != STATUS_STALLED:
            self._reported = STATUS_STALLED
            self._status(STATUS_STALLED, f"The capture stream failed and {self._attempts} attempts to reopen it "
                         "did not succeed.", attempts=self._attempts)

    def _follow_app(self) -> bool:
        try:
            target, _ = resolve_target(self._hal, self._spec)
        except LoopbackSetupError:
            return False  # the app is not running; keep the stream, it delivers silence
        except Exception as exc:
            log.warning("process loopback: reading the process list failed: %s", exc)
            return False
        if target == self._target:
            return False
        old = self._target
        if self.rebuild("app restart"):
            self._status(STATUS_RETARGETED, f"{self._spec.exe} was restarted; capture follows the new process.",
                         old_pid=old, new_pid=target)
        return True

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
            playing = audio_expected(self._hal, self._spec, self._target)
        except Exception as exc:
            log.warning("process loopback watchdog: reading audio sessions failed: %s", exc)
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
                    self._status(code, NO_AUDIO_MESSAGE, attempts=self._attempts)
                else:
                    self._status(code, f"Capture stopped receiving audio and {self._attempts} rebuilds did not "
                                 "bring it back.", attempts=self._attempts)
            self._silent_since = now
            return
        self._attempts += 1
        ok = self.rebuild("silence")
        self._silent_since = now
        self._status(STATUS_REBUILT, "Capture delivered silence while audio was playing; the stream was rebuilt.",
                     attempt=self._attempts, ok=ok)
        if not self._heard_ever and self._attempts == 2 and self._reported != STATUS_NO_AUDIO:
            self._reported = STATUS_NO_AUDIO
            self._status(STATUS_NO_AUDIO, NO_AUDIO_MESSAGE, attempts=self._attempts)

    def stats_dict(self) -> dict:
        return asdict(self.stats)
