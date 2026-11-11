"""One live translation session: capture, speech to text, translation, subtitles.

Data flow::

    CaptureBackend (audio thread)
        -> FrameQueue (bounded, drops oldest audio, counts drops)
        -> STT client send_audio (event loop)
    STT client events()
        -> SubtitleLine updates on the SubtitleBus
        -> committed lines to the translation scheduler
    scheduler on_line
        -> translated or failed line on the bus
        -> optional Speaker

``TranslationSession`` runs on one asyncio loop and does not know which
process or thread owns it; ``runtime.SessionRuntime`` adds the lock file and
the Unix socket around it.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import threading
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import AsyncIterator, Awaitable, Callable, Optional, Protocol

from .bus import SubtitleBus, wire_language
from .sentences import DEFAULT_STABLE_MS, DEFAULT_STABLE_PARTIALS, SentenceSplitter
from .contracts import (
    ENGINE_CHANNELS,
    ENGINE_SAMPLE_RATE,
    ENGINE_SAMPLE_WIDTH,
    CaptureBackend,
    CaptureError,
    CaptureSource,
    CommitStrategy,
    CommittedTranscript,
    DiarizedTranscript,
    LineState,
    PartialTranscript,
    Speaker,
    SpeakersMerged,
    SttEvent,
    SttState,
    SttStatus,
    SubtitleLine,
)

logger = logging.getLogger(__name__)

BYTES_PER_SECOND = ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH * ENGINE_CHANNELS

# Audio older than this is dropped when the loop falls behind the capture
# thread. Two seconds is longer than any normal loop stall and short enough
# that subtitles do not run far behind the audio after a stall.
DEFAULT_MAX_QUEUED_SECONDS = 2.0

# stop() closes every stage in parallel under this deadline, which keeps a
# stop (including a SIGINT in ``hermes hermeneus run``) under two seconds.
STOP_TIMEOUT_S = 1.5

METRICS_INTERVAL_S = 2.0

# A failing model fails every line. The status message names the reason at
# once and when it changes, and repeats the same reason at most this often.
FAILURE_MESSAGE_INTERVAL_S = 30.0


class SttClient(Protocol):
    async def start(self) -> None: ...
    def send_audio(self, pcm: bytes, captured_ns: int) -> None: ...
    def events(self) -> AsyncIterator[SttEvent]: ...
    async def close(self) -> None: ...


class LineScheduler(Protocol):
    """What the session needs from the translation scheduler."""

    def submit(self, line: SubtitleLine) -> None: ...
    def relabel_speaker(self, from_speaker_id: str, to_speaker_id: str) -> None: ...
    @property
    def backlog(self) -> int: ...
    async def aclose(self) -> None: ...


# Builds the scheduler once the session knows where finished lines go.
SchedulerFactory = Callable[[Callable[[SubtitleLine], None]], LineScheduler]


@dataclass(frozen=True)
class SessionOptions:
    source: CaptureSource
    target_language: str
    speak: bool = False
    output_device: Optional[str] = None
    commit_strategy: CommitStrategy = CommitStrategy.VAD
    voice_id: Optional[str] = None
    context_segments: int = 3
    max_lines: int = 200
    max_queued_seconds: float = DEFAULT_MAX_QUEUED_SECONDS
    # Early translation of stable sentences (sentences.py). A negative
    # stable_ms turns it off: lines are then whole Scribe segments.
    sentence_stable_ms: float = DEFAULT_STABLE_MS
    sentence_stable_partials: int = DEFAULT_STABLE_PARTIALS

    def source_dict(self) -> dict:
        return {"backend": self.source.backend, "source_id": self.source.source_id,
                "label": self.source.label}


@dataclass
class SessionComponents:
    capture: CaptureBackend
    stt: SttClient
    scheduler_factory: SchedulerFactory
    speaker: Optional[Speaker] = None


class FrameQueue:
    """Bounded hand-off of PCM chunks from the capture thread to the event loop.

    ``put`` never blocks the capture thread. When more than ``max_bytes`` of
    audio is waiting, the oldest chunks are dropped and counted. Waiting
    chunks go to ``consumer`` in one loop callback per empty to non-empty
    transition, so the hand-off costs one ``call_soon_threadsafe``, the same
    as calling the STT client's thread-safe ``send_audio`` directly
    (measured: see the W06 report, probe_hop.py).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, max_bytes: int,
                 consumer: Callable[[bytes, int], None]) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._loop = loop
        self._max_bytes = max_bytes
        self._consumer = consumer
        self._lock = threading.Lock()
        self._chunks: deque = deque()
        self._bytes = 0
        self._closed = False
        self.chunks_in = 0
        self.bytes_in = 0
        self.chunks_dropped = 0
        self.bytes_dropped = 0

    def put(self, pcm: bytes, captured_ns: int) -> None:
        """Called from the capture thread."""
        if not pcm:
            return
        with self._lock:
            if self._closed:
                return
            was_empty = not self._chunks
            self._chunks.append((pcm, captured_ns))
            self._bytes += len(pcm)
            self.chunks_in += 1
            self.bytes_in += len(pcm)
            while self._bytes > self._max_bytes and len(self._chunks) > 1:
                old, _ns = self._chunks.popleft()
                self._bytes -= len(old)
                self.chunks_dropped += 1
                self.bytes_dropped += len(old)
        if was_empty:
            try:
                self._loop.call_soon_threadsafe(self.flush)
            except RuntimeError:
                pass  # loop already closed during shutdown

    def flush(self) -> None:
        """On the loop: hand every waiting chunk to the consumer, oldest first."""
        with self._lock:
            if self._closed:
                self._chunks.clear()
                self._bytes = 0
                return
            batch = list(self._chunks)
            self._chunks.clear()
            self._bytes = 0
        for pcm, captured_ns in batch:
            try:
                self._consumer(pcm, captured_ns)
            except Exception as exc:  # one bad chunk must not stop the audio path
                logger.warning("hermeneus: audio hand-off failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._bytes


_STT_TO_SESSION = {
    SttState.CONNECTING: "starting",
    SttState.LIVE: "live",
    SttState.RECONNECTING: "reconnecting",
}


def _majority_speaker(words) -> Optional[str]:
    counts = Counter(w.speaker_id for w in words if w.speaker_id and (w.type in (None, "word")))
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _in_daemon_thread(fn: Callable[[], object], name: str) -> "asyncio.Future":
    """Run a blocking call on its own daemon thread.

    Unlike ``asyncio.to_thread`` this does not use the loop's default
    executor, which ``asyncio.run`` waits for at exit: a device that hangs
    in close must not hold up the process past its stop deadline.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def settle(result=None, error=None) -> None:
        if fut.done():
            return
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    def body() -> None:
        try:
            result = fn()
        except BaseException as exc:
            error, result = exc, None
        else:
            error = None
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:
            pass  # the loop is gone; nobody waits any more

    threading.Thread(target=body, name=name, daemon=True).start()
    return fut


def _ms(nbytes: int) -> int:
    return int(nbytes * 1000 / BYTES_PER_SECOND)


class TranslationSession:
    def __init__(self, options: SessionOptions, components: SessionComponents, bus: SubtitleBus, *,
                 clock: Callable[[], int] = time.monotonic_ns,
                 metrics_interval_s: float = METRICS_INTERVAL_S) -> None:
        self.options = options
        self._c = components
        self.bus = bus
        self._clock = clock
        self._metrics_interval = metrics_interval_s
        self._state = "idle"
        self._message = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._frames: Optional[FrameQueue] = None
        self._scheduler: Optional[LineScheduler] = None
        self._tasks: list[asyncio.Task] = []
        self._speech: Optional[asyncio.Queue] = None
        self._done: Optional[asyncio.Event] = None
        self._stop_lock: Optional[asyncio.Lock] = None
        self._capture_started = False
        self._prefix = f"{os.getpid():x}{int(time.time()):x}"
        self._ids = itertools.count(1)
        self._partial_id: Optional[str] = None
        self._lines: "OrderedDict[str, SubtitleLine]" = OrderedDict()
        self._started_ns = 0
        self._detected: Optional[str] = None
        self._capture_note = ""
        self._failure_message = ""
        self._splitter = SentenceSplitter(stable_ms=options.sentence_stable_ms,
                                          stable_partials=options.sentence_stable_partials)
        self._segment_lines: list[str] = []      # lines of the segment being spoken
        self._segments: "OrderedDict[int, list[str]]" = OrderedDict()  # segment_index -> its lines
        self._failure_published_ns = 0
        self.stats: dict[str, int] = {"lines_committed": 0, "lines_translated": 0,
                                      "lines_failed": 0, "speak_errors": 0,
                                      "sentences_released_early": 0}

    # -- status -----------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def metrics(self) -> dict:
        m = dict(self.stats)
        fq = self._frames
        if fq is not None:
            m.update(audio_chunks_in=fq.chunks_in, audio_chunks_dropped=fq.chunks_dropped,
                     audio_dropped_ms=_ms(fq.bytes_dropped), audio_queued_ms=_ms(fq.queued_bytes))
        if self._scheduler is not None:
            try:
                m["translation_backlog"] = int(self._scheduler.backlog)
            except Exception:  # a scheduler without the property must not break status
                pass
        if self._started_ns:
            m["uptime_s"] = round((self._clock() - self._started_ns) / 1e9, 1)
        return m

    def _publish_status(self, state: Optional[str] = None, message: Optional[str] = None) -> None:
        if state is not None:
            self._state = state
        if message is not None:
            self._message = message
        self.bus.publish_status(state=self._state, message=self._message,
                                source=self.options.source_dict(),
                                target_language=self.options.target_language,
                                speak=self.options.speak, detected_language=self._detected,
                                metrics=self.metrics())

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start every stage. Raises CaptureError or the STT start error after cleaning up."""
        if self._loop is not None:
            raise RuntimeError("a TranslationSession starts once")
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._started_ns = self._clock()
        self._frames = FrameQueue(self._loop, int(self.options.max_queued_seconds * BYTES_PER_SECOND),
                                  self._c.stt.send_audio)
        self._publish_status("starting", "connecting to speech to text")
        try:
            self._scheduler = self._c.scheduler_factory(self._on_translated)
            await self._c.stt.start()
            self._tasks.append(asyncio.create_task(self._stt_loop(), name="hermeneus:stt-events"))
            self._tasks.append(asyncio.create_task(self._metrics_loop(), name="hermeneus:metrics"))
            if self.options.speak and self._c.speaker is not None:
                start_speaker = getattr(self._c.speaker, "start", None)
                if start_speaker is not None:
                    await start_speaker(self.options.target_language)
                self._speech = asyncio.Queue()
                self._tasks.append(asyncio.create_task(self._speech_loop(), name="hermeneus:speech"))
            await asyncio.to_thread(self._c.capture.start, self.options.source, self._frames.put)
            self._capture_started = True
        except BaseException as exc:
            message = str(exc) if isinstance(exc, CaptureError) else f"could not start: {exc}"
            await self._shutdown(final_state="failed", message=message)
            raise

    async def stop(self, message: str = "stopped") -> None:
        await self._shutdown(final_state="idle", message=message)

    async def wait_closed(self) -> None:
        assert self._done is not None, "start() was not called"
        await self._done.wait()

    @property
    def closed(self) -> bool:
        return self._done is not None and self._done.is_set()

    async def _shutdown(self, *, final_state: str, message: str) -> None:
        assert self._stop_lock is not None
        async with self._stop_lock:
            if self._done.is_set():
                return
            self._publish_status("stopping", message if final_state == "failed" else "stopping")
            if self._frames is not None:
                self._frames.close()
            current = asyncio.current_task()
            for task in self._tasks:
                if task is not current:
                    task.cancel()
            # The stages are independent once audio stops, so they close in
            # parallel under one deadline. A step that overruns is logged and
            # left behind; the session still reports its final state in time.
            steps: dict[str, Awaitable] = {}
            if self._capture_started:
                self._capture_started = False
                steps["capture stop"] = _in_daemon_thread(self._c.capture.stop, "hermeneus-capture-stop")
            steps["speech to text close"] = self._c.stt.close()
            if self._scheduler is not None:
                steps["translation close"] = self._scheduler.aclose()
            if self._c.speaker is not None:
                steps["speaker close"] = self._c.speaker.close()
            pending = {asyncio.ensure_future(aw): what for what, aw in steps.items()}
            for task in self._tasks:
                if task is not current:
                    pending[task] = task.get_name()
            done, late = await asyncio.wait(pending, timeout=STOP_TIMEOUT_S)
            for fut in done:
                if not fut.cancelled() and fut.exception() is not None and pending[fut] in steps:
                    logger.warning("hermeneus: %s failed: %s", pending[fut], fut.exception())
            for fut in late:
                logger.warning("hermeneus: %s took longer than %.1fs; continuing shutdown", pending[fut],
                               STOP_TIMEOUT_S)
            self._publish_status(final_state, message)
            self._done.set()

    def capture_failed(self, error: BaseException) -> None:
        """Capture stopped on its own. Call on the session's loop."""
        self._fail_soon(str(error) or "audio capture stopped")

    def _fail_soon(self, message: str) -> None:
        """Shut down from inside a stage task without awaiting itself."""
        if self._done is not None and not self._done.is_set():
            asyncio.get_running_loop().create_task(self._shutdown(final_state="failed", message=message))

    # -- stages ---------------------------------------------------------------

    async def _metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(self._metrics_interval)
            # A backend without an error callback (process-tap) records why it
            # stopped in ``failure``.
            failure = getattr(self._c.capture, "failure", "") if self._capture_started else ""
            if failure:
                self._fail_soon(str(failure))
                return
            # W02 seam: the tap's zero-buffer watchdog reports what it is doing
            # (for example rebuilding a silent tap) in ``status_message``.
            note = str(getattr(self._c.capture, "status_message", "") or "") if self._capture_started else ""
            # The speaker reports a voice fallback the same way.
            voice_note = str(getattr(self._c.speaker, "status_message", "") or "") if self._c.speaker else ""
            note = " ".join(n for n in (note, voice_note) if n)
            if note != self._capture_note:
                self._capture_note = note
                if self._state in ("live", "reconnecting"):
                    self._publish_status(message=note)
                continue
            if self._state in ("live", "reconnecting"):
                self._publish_status()

    async def _speech_loop(self) -> None:
        assert self._speech is not None and self._c.speaker is not None
        while True:
            text = await self._speech.get()
            try:
                await self._c.speaker.speak(text, language=self.options.target_language)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["speak_errors"] += 1
                logger.warning("hermeneus: spoken translation failed: %s", exc)

    async def _stt_loop(self) -> None:
        try:
            async for event in self._c.stt.events():
                self._on_stt_event(event)
                if self.closed:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("hermeneus: speech to text stream ended with an error: %s", exc)
            self._fail_soon(f"speech to text stopped: {exc}")
            return
        if self._state not in ("stopping", "idle", "failed"):
            self._fail_soon("speech to text stream ended")

    # -- event handling ---------------------------------------------------------

    def _new_id(self) -> str:
        return f"{self._prefix}-{next(self._ids)}"

    def _store(self, line: SubtitleLine) -> None:
        self._lines.pop(line.line_id, None)
        self._lines[line.line_id] = line
        while len(self._lines) > self.options.max_lines:
            self._lines.popitem(last=False)
        self.bus.publish_line(line)

    def _on_stt_event(self, event: SttEvent) -> None:
        if isinstance(event, PartialTranscript):
            self._on_partial(event)
        elif isinstance(event, CommittedTranscript):
            self._on_committed(event)
        elif isinstance(event, DiarizedTranscript):
            self._on_diarized(event)
        elif isinstance(event, SpeakersMerged):
            self._on_merged(event)
        elif isinstance(event, SttStatus):
            self._on_stt_status(event)

    def _on_partial(self, event: PartialTranscript) -> None:
        text = event.text.strip()
        if not text:
            return
        speaker = _majority_speaker(event.words)
        now = event.received_ns or self._clock()
        for sentence in self._splitter.on_partial(text, now):
            # The sentence takes over the partial line's id, so the display
            # keeps one line per sentence in speaking order.
            self._release(sentence, speaker=speaker, source_language=self._detected,
                          segment_index=None, received_ns=now, early=True)
        rest = self._splitter.remainder(text)
        if not rest:
            return
        if self._partial_id is None:
            self._partial_id = self._new_id()
            line = SubtitleLine(line_id=self._partial_id, state=LineState.PARTIAL, original=rest,
                                speaker_id=speaker, target_language=self.options.target_language,
                                timings_ns={"partial": now})
        else:
            prev = self._lines[self._partial_id]
            if prev.original == rest and (speaker is None or speaker == prev.speaker_id):
                return
            line = replace(prev, original=rest, speaker_id=speaker or prev.speaker_id)
        self._store(line)

    def _release(self, text: str, *, speaker: Optional[str], source_language: Optional[str],
                 segment_index: Optional[int], received_ns: int, early: bool) -> SubtitleLine:
        """Turn the current partial line (or a new one) into a committed line and send it to translation."""
        if self._partial_id is not None:
            prev = self._lines[self._partial_id]
            timings = dict(prev.timings_ns)
            speaker = prev.speaker_id or speaker
            line_id = self._partial_id
        else:
            timings, line_id = {}, self._new_id()
        self._partial_id = None
        timings["commit"] = received_ns
        if early:
            timings["sentence_released"] = received_ns
        line = SubtitleLine(line_id=line_id, state=LineState.COMMITTED, original=text,
                            speaker_id=speaker, source_language=source_language,
                            target_language=self.options.target_language,
                            segment_index=segment_index, timings_ns=timings)
        self._store(line)
        self._segment_lines.append(line_id)
        self.stats["lines_committed"] += 1
        if early:
            self.stats["sentences_released_early"] += 1
        assert self._scheduler is not None
        try:
            self._scheduler.submit(replace(line, timings_ns=dict(timings)))
        except Exception as exc:
            self._on_translated(replace(line, state=LineState.FAILED, error=f"translation unavailable: {exc}"))
        return line

    def _on_committed(self, event: CommittedTranscript) -> None:
        text = event.text.strip()
        source_language = wire_language(event.language_code)
        if source_language and source_language != self._detected:
            self._detected = source_language
            self._publish_status()
        received = event.received_ns or self._clock()
        pieces = self._splitter.on_commit(text) if text else []
        for piece in pieces:
            self._release(piece, speaker=None, source_language=source_language,
                          segment_index=event.segment_index, received_ns=received, early=False)
        if not pieces and self._partial_id is not None:
            # The commit holds nothing beyond what was already released, so
            # the partial line on screen had words Scribe dropped at the end.
            # The event schema has no removal; the line ends empty.
            prev = self._lines[self._partial_id]
            self._partial_id = None
            self._store(replace(prev, state=LineState.TRANSLATED, original="", translation=""))
        if event.segment_index is not None:
            self._segments[event.segment_index] = list(self._segment_lines)
            while len(self._segments) > 64:
                self._segments.pop(next(iter(self._segments)))
        self._segment_lines = []

    def _on_diarized(self, event: DiarizedTranscript) -> None:
        """Give each line of the segment the main speaker of its own words."""
        line_ids = [i for i in self._segments.get(event.segment_index, []) if i in self._lines]
        words = [w for w in event.words if w.type in (None, "word")]
        if not line_ids or not words:
            return
        pos = 0
        for i, line_id in enumerate(line_ids):
            line = self._lines[line_id]
            n = len(line.original.split())
            chunk = words[pos:pos + n] if i < len(line_ids) - 1 else words[pos:]
            pos += n
            speaker = _majority_speaker(chunk)
            if speaker is not None and line.speaker_id != speaker:
                self._store(replace(line, speaker_id=speaker))

    def _on_merged(self, event: SpeakersMerged) -> None:
        for line_id, line in list(self._lines.items()):
            if line.speaker_id == event.from_speaker_id:
                self._lines[line_id] = replace(line, speaker_id=event.to_speaker_id)
        self.bus.publish_relabel(event.from_speaker_id, event.to_speaker_id)
        if self._scheduler is not None:
            self._scheduler.relabel_speaker(event.from_speaker_id, event.to_speaker_id)

    def _on_stt_status(self, event: SttStatus) -> None:
        if event.state == SttState.FAILED:
            self._fail_soon(event.message or "speech to text failed")
            return
        if event.state == SttState.CLOSED:
            if self._state not in ("stopping", "idle", "failed"):
                self._fail_soon(event.message or "speech to text closed the connection")
            return
        mapped = _STT_TO_SESSION.get(event.state)
        if mapped and self._state not in ("stopping", "idle", "failed"):
            self._publish_status(mapped, event.message or ("live" if mapped == "live" else ""))

    def _note_translation_failure(self, error: str) -> None:
        """Show why translation fails in the status, without one status per failed line."""
        if not error or error.startswith("Skipped to keep up"):
            return  # backlog skips are a choice, not a fault
        now = self._clock()
        if (error != self._failure_message
                or now - self._failure_published_ns >= FAILURE_MESSAGE_INTERVAL_S * 1e9):
            self._failure_message = error
            self._failure_published_ns = now
            if self._state in ("live", "reconnecting"):
                self._publish_status(message=error)

    def _on_translated(self, done: SubtitleLine) -> None:
        """Scheduler callback, on the loop thread, once per committed line."""
        current = self._lines.get(done.line_id)
        original = current.original if current is not None else done.original
        speaker = current.speaker_id if current is not None else done.speaker_id
        timings = dict(current.timings_ns if current is not None else done.timings_ns)
        timings.update(done.timings_ns)
        if done.state == LineState.TRANSLATED:
            timings.setdefault("translated", self._clock())
            self.stats["lines_translated"] += 1
            if self._failure_message:
                self._failure_message = ""
                if self._state in ("live", "reconnecting"):
                    self._publish_status(message="")
        else:
            self.stats["lines_failed"] += 1
            self._note_translation_failure(done.error)
        line = replace(done, original=original, speaker_id=speaker, timings_ns=timings)
        self._store(line)
        if (line.state == LineState.TRANSLATED and line.translation and self._speech is not None
                and not self.closed):
            self._speech.put_nowait(line.translation)
