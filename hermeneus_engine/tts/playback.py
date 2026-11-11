"""Ordered playback of synthesized speech on a named output device.

Utterances play in the order they were opened, which is the order of
``speak()`` calls, whatever order their audio arrives in. Audio is streamed:
an utterance starts playing as soon as its first chunk is here.

Drift policy (spoken output must not trail the subtitles by much):

* An utterance whose first sample would play more than ``max_start_lag_s``
  after it was opened is skipped whole. The check runs when the utterance
  reaches the head of the queue and again on every callback until its first
  sample plays, so a late network response is skipped too.
* An utterance that has started plays to its end. Cutting speech mid-word is
  harder to follow than skipping a segment, and the skip rule above bounds
  the lag to the length of the utterance in progress plus ``max_start_lag_s``.
* ``backlog_seconds()`` lets the speaker skip a segment before paying for its
  synthesis when the queue is already that far behind.

Threading: the PortAudio callback touches only ``collections.deque``
append/popleft (atomic in CPython), plain attribute assignment and numpy
copies. It takes no lock and never waits. Events for the asyncio side go to
a deque that ``drain_events()`` empties.
"""

from __future__ import annotations

import collections
import enum
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Sequence

import numpy as np

from ..audio_devices import prefer_one
from ..contracts import FeedbackRiskError
from .routing import CaptureGate, RouteVerdict, ensure_route_safe

DEFAULT_MAX_START_LAG_S = 3.0


class UtteranceState(enum.Enum):
    QUEUED = "queued"
    PLAYING = "playing"
    DONE = "done"
    DROPPED = "dropped"
    CANCELLED = "cancelled"


class Utterance:
    """One spoken segment. Created by ``PlaybackQueue.open_utterance``."""

    __slots__ = ("utterance_id", "created_ns", "chunks", "finished", "cancelled", "state",
                 "total_samples", "played_samples", "first_play_ns", "end_ns", "_cur", "_pos")

    def __init__(self, utterance_id: str, created_ns: int) -> None:
        self.utterance_id = utterance_id
        self.created_ns = created_ns
        self.chunks: collections.deque[np.ndarray] = collections.deque()
        self.finished = False
        self.cancelled = False
        self.state = UtteranceState.QUEUED
        self.total_samples = 0
        self.played_samples = 0
        self.first_play_ns: Optional[int] = None
        self.end_ns: Optional[int] = None
        self._cur: Optional[np.ndarray] = None
        self._pos = 0

    @property
    def closed(self) -> bool:
        return self.state in (UtteranceState.DONE, UtteranceState.DROPPED, UtteranceState.CANCELLED)


@dataclass(frozen=True)
class PlaybackEvent:
    kind: str            # "started", "done", "dropped", "underflow"
    utterance_id: str
    at_ns: int
    lag_ns: int = 0      # for "started" and "dropped": time since the utterance was opened


class PcmSink(Protocol):
    """What a speaker needs from playback."""

    sample_rate: int
    max_start_lag_s: float

    def open_utterance(self, utterance_id: str, created_ns: int) -> Utterance: ...
    def feed(self, utt: Utterance, pcm: bytes) -> None: ...
    def finish(self, utt: Utterance) -> None: ...
    def cancel(self, utt: Utterance) -> None: ...
    def backlog_seconds(self) -> float: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


class PlaybackError(RuntimeError):
    """The output device could not be opened. The message is shown to the user."""


def _default_stream_factory(**kwargs: Any) -> Any:
    import sounddevice as sd  # noqa: PLC0415  (PortAudio is loaded only when playback starts)
    return sd.OutputStream(**kwargs)


def _default_query_devices() -> Sequence[dict]:
    import sounddevice as sd  # noqa: PLC0415
    return [dict(d) for d in sd.query_devices()]


def _default_query_hostapis() -> Sequence[dict]:
    import sounddevice as sd  # noqa: PLC0415
    return [dict(a) for a in sd.query_hostapis()]


def _default_hostapi() -> Optional[int]:
    import sounddevice as sd  # noqa: PLC0415
    value = sd.default.hostapi
    return value if isinstance(value, int) and value >= 0 else None


class PlaybackQueue:
    """Plays mono s16le PCM utterances on one output device, in order."""

    def __init__(self, device_name: str, *, sample_rate: int = 24000,
                 max_start_lag_s: float = DEFAULT_MAX_START_LAG_S,
                 route_check: Optional[Callable[[], RouteVerdict]] = None,
                 gate_mode: bool = False, gate: Optional[CaptureGate] = None,
                 stream_factory: Callable[..., Any] = _default_stream_factory,
                 query_devices: Callable[[], Sequence[dict]] = _default_query_devices,
                 query_hostapis: Callable[[], Sequence[dict]] = _default_query_hostapis,
                 default_hostapi: Callable[[], Optional[int]] = _default_hostapi,
                 clock: Callable[[], int] = time.monotonic_ns,
                 latency: str | float = "low") -> None:
        if gate_mode and gate is None:
            raise ValueError("gate_mode needs a CaptureGate shared with the capture backend")
        self.device_name = device_name
        self.sample_rate = sample_rate
        self.max_start_lag_s = max_start_lag_s
        self._max_lag_ns = int(max_start_lag_s * 1e9)
        self._route_check = route_check
        self._gate_mode = gate_mode
        self._gate = gate
        self._stream_factory = stream_factory
        self._query_devices = query_devices
        self._query_hostapis = query_hostapis
        self._default_hostapi = default_hostapi
        self._clock = clock
        self._latency = latency
        self._pending: collections.deque[Utterance] = collections.deque()
        self._current: Optional[Utterance] = None
        self._events: collections.deque[PlaybackEvent] = collections.deque(maxlen=1024)
        self._stream: Any = None
        self._channels = 1
        self.verdict: Optional[RouteVerdict] = None
        self.underflows = 0
        self.callback_ns_max = 0     # slowest callback seen, for the latency harness

    # ---------------------------------------------------------------- control

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        """Check the route, then open the device. Raises FeedbackRiskError or PlaybackError."""
        if self._stream is not None:
            return
        if self._route_check is not None:
            self.verdict = self._route_check()
            ensure_route_safe(self.verdict, gate_mode=self._gate_mode)
        index, channels = self._resolve_device()
        self._channels = channels
        try:
            stream = self._stream_factory(device=index, samplerate=self.sample_rate, channels=channels,
                                          dtype="int16", latency=self._latency, callback=self._callback)
            stream.start()
        except FeedbackRiskError:
            raise
        except Exception as exc:
            raise PlaybackError(f"Could not open '{self.device_name}' for spoken output: {exc}") from exc
        self._stream = stream

    def health(self) -> str:
        """Empty while the device plays; otherwise why it stopped.

        PortAudio ends a stream whose device disappears (unplugged headphones,
        a Bluetooth device that went away); ``active`` is then False although
        this queue never stopped it.
        """
        stream = self._stream
        if stream is None or getattr(stream, "active", True):
            return ""
        return f"Output device '{self.device_name}' stopped playing; it may have been unplugged or switched off."

    def stop(self) -> None:
        """Stop the device and cancel everything queued. Safe to call more than once."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        now = self._clock()
        cur, self._current = self._current, None
        for utt in ([cur] if cur else []) + list(self._pending):
            if not utt.closed:
                utt.state = UtteranceState.CANCELLED
                utt.end_ns = now
        self._pending.clear()

    def _resolve_device(self) -> tuple[int, int]:
        devices = list(self._query_devices())
        exact = [i for i, d in enumerate(devices)
                 if d.get("name") == self.device_name and d.get("max_output_channels", 0) > 0]
        if not exact:
            folded = self.device_name.casefold()
            exact = [i for i, d in enumerate(devices)
                     if str(d.get("name", "")).casefold() == folded and d.get("max_output_channels", 0) > 0]
        if len(exact) > 1:
            # Windows lists one endpoint per host API under one name (audio_devices.prefer_one).
            exact = prefer_one(exact, devices, self._query_hostapis, self._default_hostapi)
        if len(exact) != 1:
            names = ", ".join(d["name"] for d in devices if d.get("max_output_channels", 0) > 0)
            raise PlaybackError(f"Output device '{self.device_name}' not found. Available outputs: {names}")
        idx = exact[0]
        # Mono speech is copied to both channels of a stereo device; a mono
        # stream on a stereo device plays on the first channel only.
        return idx, min(2, int(devices[idx]["max_output_channels"]))

    # ------------------------------------------------------- producer (asyncio)

    def open_utterance(self, utterance_id: str, created_ns: int) -> Utterance:
        utt = Utterance(utterance_id, created_ns)
        self._pending.append(utt)
        return utt

    def feed(self, utt: Utterance, pcm: bytes) -> None:
        """Append s16le mono PCM. ``pcm`` must hold whole samples."""
        if utt.closed or utt.cancelled or not pcm:
            return
        arr = np.frombuffer(pcm, dtype="<i2")
        utt.chunks.append(arr)
        utt.total_samples += arr.shape[0]

    def finish(self, utt: Utterance) -> None:
        utt.finished = True

    def cancel(self, utt: Utterance) -> None:
        utt.cancelled = True

    def backlog_seconds(self) -> float:
        """Seconds of audio already received and not yet played, across the queue."""
        total = 0
        cur = self._current
        if cur is not None:
            total += cur.total_samples - cur.played_samples
        for utt in tuple(self._pending):
            total += utt.total_samples - utt.played_samples
        return total / self.sample_rate

    def drain_events(self) -> list[PlaybackEvent]:
        out = []
        while True:
            try:
                out.append(self._events.popleft())
            except IndexError:
                return out

    # --------------------------------------------------------- audio callback

    def _callback(self, outdata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        t0 = self._clock()
        if status and getattr(status, "output_underflow", False):
            self.underflows += 1
        filled = 0
        now = t0
        while filled < frames:
            utt = self._current
            if utt is None:
                try:
                    utt = self._pending.popleft()
                except IndexError:
                    break
                self._current = utt
            if utt.cancelled:
                utt.state = UtteranceState.CANCELLED
                utt.end_ns = now
                self._current = None
                continue
            if utt.first_play_ns is None:
                lag = now - utt.created_ns
                if lag > self._max_lag_ns:
                    utt.state = UtteranceState.DROPPED
                    utt.end_ns = now
                    self._events.append(PlaybackEvent("dropped", utt.utterance_id, now, lag))
                    self._current = None
                    continue
            cur = utt._cur
            if cur is None or utt._pos >= cur.shape[0]:
                try:
                    cur = utt.chunks.popleft()
                except IndexError:
                    if utt.finished:
                        utt.state = UtteranceState.DONE
                        utt.end_ns = now
                        self._events.append(PlaybackEvent("done", utt.utterance_id, now))
                        self._current = None
                        continue
                    break  # waiting for more audio of this utterance: silence, keep order
                utt._cur = cur
                utt._pos = 0
            if utt.first_play_ns is None:
                utt.first_play_ns = now
                utt.state = UtteranceState.PLAYING
                self._events.append(PlaybackEvent("started", utt.utterance_id, now, now - utt.created_ns))
            n = min(frames - filled, cur.shape[0] - utt._pos)
            outdata[filled:filled + n, :] = cur[utt._pos:utt._pos + n, None]
            utt._pos += n
            utt.played_samples += n
            filled += n
        if filled < frames:
            outdata[filled:, :] = 0
        if filled and self._gate is not None:
            start = t0 + self._dac_offset_ns(time_info)
            self._gate.mark_audible(start, start + int(filled * 1e9 / self.sample_rate))
        spent = self._clock() - t0
        if spent > self.callback_ns_max:
            self.callback_ns_max = spent

    @staticmethod
    def _dac_offset_ns(time_info: Any) -> int:
        try:
            off = float(time_info.outputBufferDacTime) - float(time_info.currentTime)
        except (AttributeError, TypeError, ValueError):
            return 0
        return int(off * 1e9) if 0.0 <= off < 1.0 else 0
