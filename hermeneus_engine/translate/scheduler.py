"""Ordered, bounded translation of committed subtitle lines.

The pipeline hands every committed line to :meth:`TranslationScheduler.submit`
as soon as speech to text commits it. The scheduler translates up to
``max_concurrency`` lines at once and calls ``on_line`` exactly once per
submitted line, in submission order, with the line in state TRANSLATED or
FAILED. A FAILED line keeps its original text and carries a plain error.

Backlog policy (lag bound)
--------------------------
Every line has a deadline: its commit time plus ``max_lag_s``.

* A waiting line starts only when it still has at least ``min_call_budget_s``
  before its deadline. Otherwise it is delivered at once as FAILED with a
  "skipped" error. Waiting lines are taken oldest first, so when the model is
  slower than speech the oldest lines are the ones skipped and the newest
  speech keeps being translated.
* A started call gets ``min(call timeout, time left before the deadline)``.

Lines start in submission order, so every line ahead of a line has an earlier
or equal deadline and finishes by it. Each line is therefore delivered no
later than its own deadline, apart from event loop scheduling delay.

Merging adjacent backlog lines into one call is not implemented: the
``SubtitleLine`` contract has no state for a line whose translation was
delivered on another line. The report requests that contract change.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections import deque
from typing import Callable, Optional, Protocol, Sequence

from hermeneus_engine.contracts import LineState, SubtitleLine, TranslationError
from hermeneus_engine.translate.prompt import ContextLine

logger = logging.getLogger(__name__)

# One call in flight plus one lets the next line start without waiting a full
# round trip of the previous one, which hides most of the model latency when
# lines arrive every few seconds. More slots mostly multiply rate limit risk
# and spend when the model is already slower than speech.
DEFAULT_MAX_CONCURRENCY = 2

# About two typical spoken segments. A translation that arrives later than
# this sits under speech the viewer has already moved past.
DEFAULT_MAX_LAG_S = 8.0

# A call with less than one second left rarely returns in time; skipping it
# frees the slot for newer speech.
DEFAULT_MIN_CALL_BUDGET_S = 1.0

LineCallback = Callable[[SubtitleLine], None]
Clock = Callable[[], int]  # monotonic nanoseconds


class ContextualTranslator(Protocol):
    timeout_s: float
    context_segments: int

    async def translate(self, text: str, *, source_language: Optional[str], target_language: str,
                        context: Sequence[ContextLine], speaker_id: Optional[str] = None,
                        timeout_s: Optional[float] = None) -> str:
        ...


@dataclasses.dataclass
class _Entry:
    seq: int
    line: SubtitleLine
    deadline_ns: int
    translation: Optional[str] = None
    result: Optional[SubtitleLine] = None


class TranslationScheduler:
    def __init__(self, translator: ContextualTranslator, *, target_language: str, on_line: LineCallback,
                 max_concurrency: int = DEFAULT_MAX_CONCURRENCY, max_lag_s: float = DEFAULT_MAX_LAG_S,
                 min_call_budget_s: float = DEFAULT_MIN_CALL_BUDGET_S, call_timeout_s: Optional[float] = None,
                 context_segments: Optional[int] = None, clock: Clock = time.monotonic_ns) -> None:
        """``call_timeout_s`` and ``context_segments`` default to the translator's own values."""
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_lag_s <= 0 or min_call_budget_s < 0 or min_call_budget_s >= max_lag_s:
            raise ValueError("need max_lag_s > min_call_budget_s >= 0")
        self._translator = translator
        self._target = target_language
        self._on_line = on_line
        self.max_concurrency = max_concurrency
        self.max_lag_s = float(max_lag_s)
        self.min_call_budget_s = float(min_call_budget_s)
        self.call_timeout_s = float(call_timeout_s if call_timeout_s is not None else translator.timeout_s)
        self.context_segments = int(context_segments if context_segments is not None else translator.context_segments)
        self._clock = clock
        self._next_seq = 0
        self._next_delivery = 0
        self._waiting: deque[_Entry] = deque()
        self._finished: dict[int, _Entry] = {}
        self._history: deque[_Entry] = deque(maxlen=max(self.context_segments, 0) + self.max_concurrency + 64)
        self._tasks: set[asyncio.Task] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    # -- public API ---------------------------------------------------------

    def submit(self, line: SubtitleLine) -> None:
        """Queue a committed line. Never blocks. Call from the event loop thread."""
        if self._closed:
            raise RuntimeError("TranslationScheduler is closed")
        commit_ns = line.timings_ns.get("commit") or self._clock()
        entry = _Entry(seq=self._next_seq, line=line,
                       deadline_ns=int(commit_ns + self.max_lag_s * 1e9))
        self._next_seq += 1
        self._idle.clear()
        self._waiting.append(entry)
        self._history.append(entry)
        self._pump()

    def relabel_speaker(self, from_speaker_id: str, to_speaker_id: str) -> None:
        """Apply a Scribe speaker merge to lines that are still waiting or kept as context."""
        for entry in list(self._history) + list(self._waiting):
            if entry.line.speaker_id == from_speaker_id:
                entry.line = dataclasses.replace(entry.line, speaker_id=to_speaker_id)

    @property
    def backlog(self) -> int:
        """Lines submitted but not yet delivered."""
        return self._next_seq - self._next_delivery

    async def drain(self) -> None:
        """Wait until every submitted line has been delivered."""
        await self._idle.wait()

    async def aclose(self) -> None:
        """Cancel running calls and drop waiting lines. No callback runs after this returns."""
        self._closed = True
        self._waiting.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._idle.set()

    # -- internals ----------------------------------------------------------

    def _pump(self) -> None:
        while self._waiting and len(self._tasks) < self.max_concurrency and not self._closed:
            entry = self._waiting.popleft()
            left_s = (entry.deadline_ns - self._clock()) / 1e9
            if left_s < self.min_call_budget_s:
                self._finish(entry, failed=(
                    f"Skipped to keep up: the translation would have been more than {self.max_lag_s:g} s behind."))
                continue
            task = asyncio.get_running_loop().create_task(self._run(entry, min(self.call_timeout_s, left_s)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _context_for(self, entry: _Entry) -> list[ContextLine]:
        if self.context_segments <= 0:
            return []
        earlier = [e for e in self._history if e.seq < entry.seq and e.line.original.strip()]
        return [ContextLine(text=e.line.original, speaker_id=e.line.speaker_id, translation=e.translation)
                for e in earlier[-self.context_segments:]]

    async def _run(self, entry: _Entry, budget_s: float) -> None:
        line = entry.line
        try:
            translation = await self._translator.translate(
                line.original, source_language=line.source_language, target_language=self._target,
                context=self._context_for(entry), speaker_id=line.speaker_id, timeout_s=budget_s)
        except asyncio.CancelledError:
            raise
        except TranslationError as exc:
            self._finish(entry, failed=str(exc) or "Translation failed.")
        except Exception as exc:  # a translator that breaks its contract must not stall the queue
            logger.exception("translator raised outside TranslationError")
            self._finish(entry, failed=f"Translation failed: {type(exc).__name__}.")
        else:
            entry.translation = translation
            self._finish(entry, translation=translation)
        # Free this slot before refilling: the done callback runs only after this returns.
        self._tasks.discard(asyncio.current_task())
        self._pump()

    def _finish(self, entry: _Entry, *, translation: str = "", failed: str = "") -> None:
        timings = dict(entry.line.timings_ns)
        timings["translated"] = self._clock()
        entry.result = dataclasses.replace(
            entry.line, state=LineState.FAILED if failed else LineState.TRANSLATED,
            translation="" if failed else translation, error=failed, target_language=self._target,
            timings_ns=timings)
        self._finished[entry.seq] = entry
        self._deliver_ready()

    def _deliver_ready(self) -> None:
        while self._next_delivery in self._finished and not self._closed:
            entry = self._finished.pop(self._next_delivery)
            self._next_delivery += 1
            try:
                self._on_line(entry.result)
            except Exception:
                logger.exception("subtitle line callback failed")
        if self._next_delivery == self._next_seq:
            self._idle.set()
