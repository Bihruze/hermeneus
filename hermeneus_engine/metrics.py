"""Per-segment stage timestamps and latency summaries.

A segment is one committed transcript and everything derived from it: its
translation and its spoken output. Code anywhere in the pipeline records the
moment a segment reaches a stage with ``LatencyRecorder.mark``. Every
timestamp is ``time.monotonic_ns()``, the same clock that ``FrameSink`` uses
for capture timestamps, so intervals across threads and stages subtract
directly.

Frame to segment attribution
----------------------------
Scribe reports word timestamps in seconds of audio since the first sample the
current STT session received. ``StreamClock`` keeps one row per chunk sent to
the STT session: the chunk's sample offset in the session stream and the
capture timestamp of its first sample, exactly as the capture backend passed
it to the ``FrameSink``. A word time ``t`` maps to the capture clock as::

    sample = round(t * 16000)
    row    = last chunk with offset <= sample
    capture_ns = row.capture_ns + (sample - row.offset) * 1e9 / 16000

The ``capture`` stage of a segment is the mapped start of its first timed word
and ``speech_end`` is the mapped end of its last timed word. Words come from
the segment's ``DiarizedTranscript`` when one arrives (matched by
``segment_index``) and otherwise from the last ``PartialTranscript`` before
the commit. A segment with no timed words gets no ``capture`` stage and is
counted as unattributed; no estimate is substituted.

Error bound of the mapping, term by term:

* Arithmetic: integer nanoseconds, under 1 microsecond.
* Gaps in capture (a backend skipped audio): none, because every chunk keeps
  its own capture timestamp.
* Capture timestamp accuracy: whatever error the backend has in stamping the
  first sample of a chunk. For WAV replay the timestamp is exact by
  construction and the delivery deviation is reported as pacing jitter.
* Word timestamp resolution and alignment error of Scribe: inherited one to
  one. UNVERIFIED until measured against labelled speech.
* Audio the STT client drops, resamples or reorders after the sink, or audio
  it replays after a reconnect: shifts every later word by the duration
  involved, up to that duration. ``StreamClock.new_session`` must be called
  when the client starts a new server session.

The harness measures the total error against a ground truth manifest when
one is available (see ``scripts/latency_harness.py``).
"""

from __future__ import annotations

import contextlib
import threading
import time
from array import array
from bisect import bisect_right
from collections import OrderedDict
from contextvars import ContextVar
from typing import Callable, Hashable, Iterable, Iterator, Optional, Sequence

from .contracts import ENGINE_SAMPLE_RATE, Word


class Stage:
    """Stage names. Values match the keys of ``SubtitleLine.timings_ns``."""

    CAPTURE = "capture"                  # capture time of the segment's first word start
    SPEECH_END = "speech_end"            # capture time of the segment's last word end
    PARTIAL = "partial"                  # first PartialTranscript of the segment received
    COMMIT = "commit"                    # CommittedTranscript received
    TRANSLATE_START = "translate_start"  # translator call started
    TRANSLATED = "translated"            # translator call returned
    TTS_FIRST_AUDIO = "tts_first_audio"  # first synthesized audio chunk received
    PLAYBACK_START = "playback_start"    # first synthesized sample handed to the output device


STAGES: tuple[str, ...] = (
    Stage.CAPTURE, Stage.SPEECH_END, Stage.PARTIAL, Stage.COMMIT, Stage.TRANSLATE_START,
    Stage.TRANSLATED, Stage.TTS_FIRST_AUDIO, Stage.PLAYBACK_START,
)

# (name, from stage, to stage). End to end is added by ``summary`` from its ``end_stage``.
INTERVALS: tuple[tuple[str, str, str], ...] = (
    ("capture_to_first_partial", Stage.CAPTURE, Stage.PARTIAL),
    ("capture_to_committed", Stage.CAPTURE, Stage.COMMIT),
    ("speech_end_to_committed", Stage.SPEECH_END, Stage.COMMIT),
    ("committed_to_translated", Stage.COMMIT, Stage.TRANSLATED),
    ("translated_to_tts_first_audio", Stage.TRANSLATED, Stage.TTS_FIRST_AUDIO),
    ("tts_first_audio_to_playback", Stage.TTS_FIRST_AUDIO, Stage.PLAYBACK_START),
)


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (the numpy default). ``q`` is 0..100."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile of an empty sequence")
    if n == 1:
        return float(sorted_values[0])
    pos = (n - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo]) + (float(sorted_values[hi]) - float(sorted_values[lo])) * frac


def distribution(values: Iterable[float]) -> dict:
    """count, min, mean, p50, p90, p95, p99 and max of ``values``. Empty input gives count 0 only."""
    data = sorted(values)
    if not data:
        return {"count": 0}
    return {
        "count": len(data),
        "min": data[0],
        "mean": sum(data) / len(data),
        "p50": percentile(data, 50),
        "p90": percentile(data, 90),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
        "max": data[-1],
    }


class LatencyRecorder:
    """Thread-safe store of stage timestamps per segment key.

    ``mark`` keeps the first timestamp for a stage; later marks of the same
    stage are ignored and return False. ``set`` overwrites, for stages whose
    value improves later (attribution from diarized words). The recorder
    keeps at most ``max_segments`` segments and evicts the oldest first.
    """

    def __init__(self, max_segments: int = 10000, clock: Callable[[], int] = time.monotonic_ns):
        if max_segments < 1:
            raise ValueError("max_segments must be at least 1")
        self._clock = clock
        self._max = max_segments
        self._lock = threading.Lock()
        self._segments: OrderedDict[Hashable, dict[str, int]] = OrderedDict()
        self._meta: dict[Hashable, dict] = {}
        self.evicted = 0

    def _segment(self, key: Hashable) -> dict[str, int]:
        seg = self._segments.get(key)
        if seg is None:
            seg = self._segments[key] = {}
            if len(self._segments) > self._max:
                old, _ = self._segments.popitem(last=False)
                self._meta.pop(old, None)
                self.evicted += 1
        return seg

    def mark(self, key: Hashable, stage: str, ts_ns: Optional[int] = None) -> bool:
        if ts_ns is None:
            ts_ns = self._clock()
        with self._lock:
            seg = self._segment(key)
            if stage in seg:
                return False
            seg[stage] = ts_ns
            return True

    def set(self, key: Hashable, stage: str, ts_ns: int) -> None:
        with self._lock:
            self._segment(key)[stage] = ts_ns

    def annotate(self, key: Hashable, **meta) -> None:
        with self._lock:
            self._segment(key)
            self._meta.setdefault(key, {}).update(meta)

    def record_timings(self, key: Hashable, timings_ns: dict) -> None:
        """Copy the known stages of a ``SubtitleLine.timings_ns`` dict, first value wins."""
        with self._lock:
            seg = self._segment(key)
            for stage, ts in timings_ns.items():
                if stage in STAGES and stage not in seg and isinstance(ts, int):
                    seg[stage] = ts

    def get(self, key: Hashable) -> dict[str, int]:
        with self._lock:
            return dict(self._segments.get(key, {}))

    def snapshot(self) -> list[tuple[Hashable, dict[str, int], dict]]:
        with self._lock:
            return [(k, dict(v), dict(self._meta.get(k, {}))) for k, v in self._segments.items()]

    def summary(self, end_stage: str = Stage.TRANSLATED, start_stage: str = Stage.CAPTURE) -> dict:
        """Distributions in milliseconds for every interval in ``INTERVALS`` plus end to end.

        A segment contributes to an interval only when both stages are present.
        ``negative`` counts intervals below zero, which point at an attribution
        or clock problem and are kept in the distribution rather than hidden.
        """
        pairs = list(INTERVALS) + [("end_to_end", start_stage, end_stage)]
        values: dict[str, list[float]] = {name: [] for name, _, _ in pairs}
        snap = self.snapshot()
        for _, seg, _ in snap:
            for name, a, b in pairs:
                if a in seg and b in seg:
                    values[name].append((seg[b] - seg[a]) / 1e6)
        out = {}
        for name, a, b in pairs:
            d = distribution(values[name])
            d["from"] = a
            d["to"] = b
            d["negative"] = sum(1 for v in values[name] if v < 0)
            out[name] = d
        return {
            "unit": "ms",
            "segments": len(snap),
            "evicted": self.evicted,
            "unattributed": sum(1 for _, seg, _ in snap if Stage.COMMIT in seg and Stage.CAPTURE not in seg),
            "intervals": out,
        }


# --------------------------------------------------------------------------
# Context binding, so stages deep in a call (TTS first audio) reach the right segment
# --------------------------------------------------------------------------

_BINDING: ContextVar[Optional[tuple[LatencyRecorder, Hashable]]] = ContextVar(
    "hermeneus_metrics_segment", default=None)


@contextlib.contextmanager
def bind_segment(recorder: LatencyRecorder, key: Hashable) -> Iterator[None]:
    """Make ``mark_current`` and ``stage_marker`` record into ``key`` inside this block.

    asyncio tasks created inside the block inherit the binding. Threads do not;
    take a ``stage_marker`` before handing work to a thread.
    """
    token = _BINDING.set((recorder, key))
    try:
        yield
    finally:
        _BINDING.reset(token)


def mark_current(stage: str, ts_ns: Optional[int] = None) -> bool:
    """Mark ``stage`` for the bound segment. Returns False when nothing is bound."""
    binding = _BINDING.get()
    if binding is None:
        return False
    return binding[0].mark(binding[1], stage, ts_ns)


def stage_marker(stage: str) -> Optional[Callable[[Optional[int]], bool]]:
    """A callable that marks ``stage`` for the currently bound segment from any thread.

    Returns None when no segment is bound, so uninstrumented runs pay nothing.
    """
    binding = _BINDING.get()
    if binding is None:
        return None
    recorder, key = binding

    def _mark(ts_ns: Optional[int] = None) -> bool:
        return recorder.mark(key, stage, ts_ns)

    return _mark


# --------------------------------------------------------------------------
# Stream time to capture clock
# --------------------------------------------------------------------------

class StreamClock:
    """Maps STT stream time (seconds since the session's first sample) to capture ns.

    ``on_frame`` must see every chunk in the order it is sent to the STT
    session. It is called from the audio thread; ``to_capture_ns`` from any
    thread.
    """

    def __init__(self, sample_rate: int = ENGINE_SAMPLE_RATE, sample_width: int = 2):
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self._lock = threading.Lock()
        self._offsets = array("q")
        self._capture = array("q")
        self._next_offset = 0
        self._reset_pending = False
        self.sessions = 1

    def new_session(self) -> None:
        """The next frame is sample 0 of a new STT session."""
        with self._lock:
            self._reset_pending = True

    def on_frame(self, nbytes: int, capture_ns: int) -> None:
        with self._lock:
            if self._reset_pending:
                self._offsets = array("q")
                self._capture = array("q")
                self._next_offset = 0
                self._reset_pending = False
                self.sessions += 1
            self._offsets.append(self._next_offset)
            self._capture.append(capture_ns)
            self._next_offset += nbytes // self.sample_width

    @property
    def samples_sent(self) -> int:
        with self._lock:
            return self._next_offset

    def to_capture_ns(self, t_s: float) -> Optional[tuple[int, bool]]:
        """``(capture_ns, extrapolated)``. ``extrapolated`` is True when ``t_s`` lies past the
        last sample sent. None when nothing has been sent or ``t_s`` is negative."""
        if t_s < 0:
            return None
        sample = round(t_s * self.sample_rate)
        with self._lock:
            if not self._offsets:
                return None
            i = bisect_right(self._offsets, sample) - 1
            if i < 0:
                return None
            offset = self._offsets[i]
            base = self._capture[i]
            extrapolated = sample > self._next_offset
        return base + (sample - offset) * 1_000_000_000 // self.sample_rate, extrapolated


def timed_span(words: Sequence[Word]) -> Optional[tuple[float, float]]:
    """Stream time of the first word start and the last word end, ignoring spacing tokens."""
    starts = [w.start for w in words if w.start is not None and w.type in (None, "word")]
    if not starts:
        return None
    ends = [w.end if w.end is not None else w.start for w in words
            if w.start is not None and w.type in (None, "word")]
    return min(starts), max(ends)
