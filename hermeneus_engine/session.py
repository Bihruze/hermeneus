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
import inspect
import itertools
import logging
import os
import threading
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Protocol

from . import languages, messages
from .bus import SubtitleBus, wire_language
from .metrics import LatencyRecorder, Stage, StreamClock, distribution
from .sentences import DEFAULT_PAUSE_MS, DEFAULT_STABLE_MS, DEFAULT_STABLE_PARTIALS, SentenceSplitter
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
from .tts.voices import DEFAULT_VOICE_ID

logger = logging.getLogger(__name__)

BYTES_PER_SECOND = ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH * ENGINE_CHANNELS

# Audio older than this is dropped when the loop falls behind the capture
# thread. Two seconds is longer than any normal loop stall and short enough
# that subtitles do not run far behind the audio after a stall.
DEFAULT_MAX_QUEUED_SECONDS = 2.0

# stop() closes every stage in parallel under this deadline, which keeps a
# stop (including a SIGINT in ``hermes hermeneus run``) under two seconds.
STOP_TIMEOUT_S = 1.5
# The STT client's own close budget (stt/scribe.py close(timeout=)). It
# commits trailing audio and waits for that transcript; 1.2 s leaves room
# inside STOP_TIMEOUT_S for the last line to reach the bus.
STT_CLOSE_TIMEOUT_S = 1.2

METRICS_INTERVAL_S = 2.0

# A failing model fails every line. The status message names the reason at
# once and when it changes, and repeats the same reason at most this often.
FAILURE_MESSAGE_INTERVAL_S = 30.0

# The model counts as slow when this many lines are skipped or time out
# within the window. One late line is noise; two in 20 s is a pattern.
SLOW_MODEL_LINES = 2
SLOW_MODEL_WINDOW_S = 20.0

# No audio, or only digital silence, for this long while live: the status
# says so, and silent chunks are no longer sent to speech to text. Scribe
# closes a socket after about 15 s without speech (A1 logs: reconnects at
# 15.5 s with a muted source), so this is shorter than that, and much longer
# than the VAD's end-of-speech pause, so a sentence always commits first.
NO_AUDIO_S = 10.0

_ZEROS: dict[int, bytes] = {}


def _is_silent(pcm: bytes) -> bool:
    """True for digital silence (every sample zero). A memcmp; real audio differs in its first bytes."""
    if not pcm:
        return True
    zeros = _ZEROS.get(len(pcm))
    if zeros is None:
        if len(_ZEROS) > 32:
            _ZEROS.clear()
        zeros = _ZEROS.setdefault(len(pcm), bytes(len(pcm)))
    return pcm == zeros


# A spoken translation that starts (playback device, ElevenLabs socket) gets
# this long before the change is reported as failed.
SPEAKER_START_TIMEOUT_S = 15.0

# Session modes. "voice" keeps the subtitle bus running (the overlay, the
# pane and hermes hermeneus run still receive every line); it tells displays
# to hide subtitle text while the translation is spoken.
MODES = ("subtitles", "both", "voice")
_MODE_ALIASES = {"subtitles": "subtitles", "subtitles-only": "subtitles", "text": "subtitles",
                 "both": "both", "subtitles+voice": "both", "subtitles-and-voice": "both",
                 "voice": "voice", "voice-only": "voice"}
SETTING_FIELDS = frozenset({"mode", "speak", "target_language", "voice_id", "output_device", "source"})


class SettingRefused(ValueError):
    """A live change cannot be applied. The message says why and what to do instead."""


def parse_mode(value: Any) -> str:
    mode = _MODE_ALIASES.get(str(value or "").strip().lower())
    if mode is None:
        raise SettingRefused(f"Unknown mode {value!r}. Use one of: subtitles, both, voice.")
    return mode


class SttClient(Protocol):
    async def start(self) -> None: ...
    def send_audio(self, pcm: bytes, captured_ns: int) -> None: ...
    def events(self) -> AsyncIterator[SttEvent]: ...
    async def close(self, timeout: Optional[float] = None) -> None: ...


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
    # Release of a finished last sentence at a pause (sentences.py); negative turns it off.
    sentence_pause_ms: float = DEFAULT_PAUSE_MS
    # False only in mode "voice": displays hide the subtitle text.
    subtitles: bool = True

    @property
    def mode(self) -> str:
        if not self.speak:
            return "subtitles"
        return "both" if self.subtitles else "voice"

    def source_dict(self) -> dict:
        return {"backend": self.source.backend, "source_id": self.source.source_id,
                "label": self.source.label}


class SpeechMetricsRelay:
    """Hands the speaker's per-utterance metrics (elevenlabs_flash.UtteranceMetrics) to the session.

    The speaker is built before the session exists; the session sets ``target``.
    """

    def __init__(self) -> None:
        self.target: Optional[Callable[[object], None]] = None

    def __call__(self, metrics: object) -> None:
        if self.target is not None:
            self.target(metrics)


@dataclass
class SessionComponents:
    capture: CaptureBackend
    stt: SttClient
    scheduler_factory: SchedulerFactory
    speaker: Optional[Speaker] = None
    # For changes while running. Each may be None; the change is then refused.
    # speaker_factory(voice_id, output_device, source) returns a new, unstarted Speaker.
    speaker_factory: Optional[Callable[[str, str, CaptureSource], Speaker]] = None
    # capture_factory(source) returns a new, unstarted CaptureBackend for that source.
    capture_factory: Optional[Callable[[CaptureSource], CaptureBackend]] = None
    # route_check(output_device, source) raises FeedbackRiskError for a route that would be captured again.
    route_check: Optional[Callable[[str, CaptureSource], None]] = None
    # Receives the speaker's UtteranceMetrics; pass it as the speaker's on_metrics.
    speech_metrics: SpeechMetricsRelay = field(default_factory=SpeechMetricsRelay)


class _SwitchSink:
    """Sink for a capture started by a source change: drops audio until the old capture has stopped.

    The cost on the capture thread is one attribute test per chunk (measured
    in the W26 report, probe_hop.py --switch).
    """

    __slots__ = ("_put", "open")

    def __init__(self, put: Callable[[bytes, int], None]) -> None:
        self._put = put
        self.open = False

    def put(self, pcm: bytes, captured_ns: int) -> None:
        if self.open:
            self._put(pcm, captured_ns)


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


def _takes_utterance_id(speaker: object) -> bool:
    """FlashSpeaker.speak takes utterance_id (it keys its UtteranceMetrics); the Speaker contract does not."""
    try:
        return "utterance_id" in inspect.signature(speaker.speak).parameters
    except (TypeError, ValueError):
        return False


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


def _source_from(value: Any) -> Optional[CaptureSource]:
    if value is None or isinstance(value, CaptureSource):
        return value
    if isinstance(value, Mapping):
        backend = str(value.get("backend") or "").strip()
        source_id = str(value.get("source_id") or "").strip()
        if not backend or not source_id:
            raise SettingRefused("A source needs both backend and source_id (see: hermes hermeneus devices).")
        return CaptureSource(backend=backend, source_id=source_id,
                             label=str(value.get("label") or source_id), detail=str(value.get("detail") or ""))
    raise SettingRefused("Pass the source as a name (hermes hermeneus set --source NAME) "
                         "or as {backend, source_id}.")


async def _close_quietly(obj: Any, what: str) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        await asyncio.wait_for(close(), STOP_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("hermeneus: %s close failed: %s", what, exc)


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
        # Stage times of every line (metrics.LatencyRecorder) and the map from
        # Scribe word times to the capture clock (metrics.StreamClock).
        self.latency = LatencyRecorder(max_segments=2000)
        self._stream_clock = StreamClock()
        self._stt_sessions_seen: Optional[int] = None
        self._clock_reliable = True
        self._pause_timer: Optional[asyncio.TimerHandle] = None
        self._tts_first_audio_ms: deque = deque(maxlen=500)
        self._splitter = SentenceSplitter(stable_ms=options.sentence_stable_ms,
                                          stable_partials=options.sentence_stable_partials,
                                          pause_ms=options.sentence_pause_ms)
        self._segment_lines: list[str] = []      # lines of the segment being spoken
        self._segments: "OrderedDict[int, list[str]]" = OrderedDict()  # segment_index -> its lines
        self._failure_published_ns = 0
        self._speak_message = ""
        self._speak_message_ns = 0
        self._speaker_error_seen = ""
        self._slow_ns: deque = deque()
        self._speech_task: Optional[asyncio.Task] = None
        self._stt_task: Optional[asyncio.Task] = None
        self._speaking = False
        self._want_speech = False
        self._settings_lock: Optional[asyncio.Lock] = None
        self._quiet_since_ns = 0
        self._last_chunks_in = -1
        self._no_audio = False      # the no-audio message is showing
        self._gated = False         # silent chunks are held back from speech to text
        self._sampled_silent: Optional[bool] = None  # the last sampled chunk was digital silence
        self._applying: list[str] = []
        self._jobs: set[asyncio.Task] = set()
        self.stats: dict[str, int] = {"lines_committed": 0, "lines_translated": 0,
                                      "lines_failed": 0, "speak_errors": 0,
                                      "sentences_released_early": 0, "sentences_released_at_pause": 0, "settings_changes": 0,
                                      "silent_chunks_held": 0}

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
        m["latency_ms"] = self.latency_summary()
        return m

    def latency_summary(self) -> dict:
        """p50 and p95 per stage interval, in ms, over the lines this session kept (up to 2000)."""
        def brief(values) -> dict:
            d = distribution(values)
            return {"count": d["count"], **({"p50": round(d["p50"]), "p95": round(d["p95"])} if d["count"] else {})}

        pairs = {"end_of_sentence_to_subtitle": (Stage.SPEECH_END, Stage.TRANSLATED),
                 "end_of_sentence_to_released": (Stage.SPEECH_END, "released"),
                 "released_to_translated": ("released", Stage.TRANSLATED),
                 "translated_to_first_tts_audio": (Stage.TRANSLATED, Stage.TTS_FIRST_AUDIO)}
        snap = [seg for _key, seg, _meta in self.latency.snapshot()]
        out = {name: brief([(seg[b] - seg[a]) / 1e6 for seg in snap if a in seg and b in seg])
               for name, (a, b) in pairs.items()}
        out["tts_request_to_first_audio"] = brief(list(self._tts_first_audio_ms))
        return out

    def _publish_status(self, state: Optional[str] = None, message: Optional[str] = None) -> None:
        if state is not None:
            self._state = state
        if message is not None:
            self._message = message
        opts = self.options
        self.bus.publish_status(state=self._state, message=self._message,
                                source=opts.source_dict(),
                                target_language=opts.target_language,
                                speak=opts.speak, detected_language=self._detected,
                                metrics=self.metrics(),
                                extra={"mode": opts.mode, "subtitles": opts.subtitles,
                                       "voice_id": opts.voice_id if opts.speak else None,
                                       "output_device": opts.output_device if opts.speak else None,
                                       "applying": list(self._applying)})

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start every stage. Raises CaptureError or the STT start error after cleaning up."""
        if self._loop is not None:
            raise RuntimeError("a TranslationSession starts once")
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._started_ns = self._clock()
        self.bus.epoch_ns = self._started_ns
        self._c.speech_metrics.target = self._on_speech_metrics
        self._frames = FrameQueue(self._loop, int(self.options.max_queued_seconds * BYTES_PER_SECOND),
                                  self._send_audio)
        self._publish_status("starting", "connecting to speech to text")
        try:
            self._scheduler = self._c.scheduler_factory(self._on_translated)
            await self._c.stt.start()
            self._stt_task = asyncio.create_task(self._stt_loop(), name="hermeneus:stt-events")
            self._tasks.append(self._stt_task)
            self._tasks.append(asyncio.create_task(self._metrics_loop(), name="hermeneus:metrics"))
            if self.options.speak and self._c.speaker is not None:
                start_speaker = getattr(self._c.speaker, "start", None)
                if start_speaker is not None:
                    await start_speaker(self.options.target_language)
                self._ensure_speech_loop()
                self._speaking = self._want_speech = True
            elif self.options.speak:
                self.options = replace(self.options, speak=False, subtitles=True)
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
            if self._pause_timer is not None:
                self._pause_timer.cancel()
                self._pause_timer = None
            current = asyncio.current_task()
            self._speaking = self._want_speech = False
            for task in list(self._jobs):
                if task is not current:
                    task.cancel()
            if self._speech_task is not None and self._speech_task not in self._tasks:
                self._tasks.append(self._speech_task)
            # The STT event loop keeps running: the client emits the transcript
            # of the trailing audio while it closes, and that last sentence
            # must reach the bus. It ends with the client's CLOSED event, or
            # is cancelled at the deadline below.
            for task in self._tasks:
                if task is not current and task is not self._stt_task:
                    task.cancel()
            # The stages are independent once audio stops, so they close in
            # parallel under one deadline. A step that overruns is logged and
            # left behind; the session still reports its final state in time.
            steps: dict[str, Awaitable] = {}
            if self._capture_started:
                self._capture_started = False
                steps["capture stop"] = _in_daemon_thread(self._c.capture.stop, "hermeneus-capture-stop")
            steps["speech to text close"] = self._c.stt.close(timeout=STT_CLOSE_TIMEOUT_S)
            if self._scheduler is not None:
                steps["translation close"] = self._scheduler.aclose()
            if self._c.speaker is not None:
                steps["speaker close"] = self._c.speaker.close()
            pending = {asyncio.ensure_future(aw): what for what, aw in steps.items()}
            for task in list(self._jobs):
                if task is not current:
                    pending[task] = task.get_name()
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
                if fut is self._stt_task:
                    fut.cancel()
            self._publish_status(final_state, message)
            self._done.set()

    def capture_failed(self, error: BaseException) -> None:
        """Capture stopped on its own. Call on the session's loop."""
        self._fail_soon(messages.capture_stopped(str(error), self.options.source.label))

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
                self._fail_soon(messages.capture_stopped(str(failure), self.options.source.label))
                return
            self._check_speaker_health()
            self._check_audio()
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

    def _check_audio(self) -> None:
        """Every metrics tick: has the capture delivered sound in the last NO_AUDIO_S seconds?

        No audio path cost while sound flows: once per tick the FrameQueue
        consumer is swapped for ``_sample_audio``, which looks at one chunk,
        swaps itself back and passes the chunk on. Once digital silence has lasted
        NO_AUDIO_S, the FrameQueue consumer becomes ``_gate_audio``, which
        drops silent chunks and hands the first chunk with sound straight
        back to speech to text.
        """
        fq = self._frames
        if fq is None or not self._capture_started or self._gated or self._state not in ("live", "reconnecting"):
            return
        count = fq.chunks_in
        if count == self._last_chunks_in:
            kind = "none"
        elif self._sampled_silent is None:
            kind = "unknown"  # no chunk sampled since the last tick yet
        else:
            kind = "silent" if self._sampled_silent else ""
        self._last_chunks_in = count
        self._sampled_silent = None
        fq._consumer = self._sample_audio
        if kind == "unknown":
            return
        if not kind:
            self._quiet_since_ns = 0
            if self._no_audio:
                self._audio_back()
            return
        now = self._clock()
        if not self._quiet_since_ns:
            self._quiet_since_ns = now
            return
        if now - self._quiet_since_ns < NO_AUDIO_S * 1e9:
            return
        if kind == "silent":
            self._gated = True
            fq._consumer = self._gate_audio
        if not self._no_audio:
            self._no_audio = True
            self._publish_status("live", messages.no_audio(self.options.source.label))

    def _sample_audio(self, pcm: bytes, captured_ns: int) -> None:
        """FrameQueue consumer for one chunk per metrics tick."""
        self._sampled_silent = _is_silent(pcm)
        if self._frames is not None and self._frames._consumer == self._sample_audio:
            self._frames._consumer = self._c.stt.send_audio
        self._c.stt.send_audio(pcm, captured_ns)

    def _gate_audio(self, pcm: bytes, captured_ns: int) -> None:
        """FrameQueue consumer while the source is silent."""
        if _is_silent(pcm):
            self.stats["silent_chunks_held"] += 1
            return
        self._gated = False
        if self._frames is not None:
            self._frames._consumer = self._c.stt.send_audio
        self._c.stt.send_audio(pcm, captured_ns)
        self._audio_back()

    def _audio_back(self) -> None:
        self._no_audio = False
        self._quiet_since_ns = 0
        if self._state in ("live", "reconnecting") and self._message.startswith("No audio from"):
            self._publish_status("live", "live")

    def _ensure_speech_loop(self) -> None:
        if self._speech is None:
            self._speech = asyncio.Queue()
        if self._speech_task is None or self._speech_task.done():
            self._speech_task = asyncio.create_task(self._speech_loop(), name="hermeneus:speech")

    async def _speech_loop(self) -> None:
        assert self._speech is not None
        while True:
            text, language, line_id = await self._speech.get()
            speaker = self._c.speaker
            if speaker is None or not self._speaking:
                continue
            try:
                if _takes_utterance_id(speaker):
                    await speaker.speak(text, language=language, utterance_id=line_id)
                else:
                    await speaker.speak(text, language=language)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["speak_errors"] += 1
                logger.warning("hermeneus: spoken translation failed: %s", exc)
                self._note_speak_failure(messages.speak_error(str(exc)))

    def _on_speech_metrics(self, m: object) -> None:
        """UtteranceMetrics from the speaker: first spoken audio of a line (E5)."""
        latency = getattr(m, "first_audio_latency_ns", None)
        first = getattr(m, "first_audio_ns", None)
        if latency is None or first is None:
            return
        self._tts_first_audio_ms.append(latency / 1e6)
        line_id = str(getattr(m, "utterance_id", "") or "")
        line = self._lines.get(line_id)
        if line is not None and Stage.TTS_FIRST_AUDIO not in line.timings_ns:
            self._store(replace(line, timings_ns={**line.timings_ns, Stage.TTS_FIRST_AUDIO: int(first)}))

    def _note_speak_failure(self, message: str) -> None:
        """Show a spoken translation failure in the status, at most once per reason per 30 s."""
        now = self._clock()
        if message != self._speak_message or now - self._speak_message_ns >= FAILURE_MESSAGE_INTERVAL_S * 1e9:
            self._speak_message = message
            self._speak_message_ns = now
            if self._state in ("live", "reconnecting"):
                self._publish_status(message=message)

    def _check_speaker_health(self) -> None:
        """Server-side speech errors and a vanished output device, read every metrics tick."""
        speaker = self._c.speaker
        if speaker is None or not self._speaking:
            return
        error = str(getattr(speaker, "last_error", "") or "")
        if error and error != self._speaker_error_seen:
            self._speaker_error_seen = error
            self._note_speak_failure(messages.speak_error(error))
        health = getattr(speaker, "health", None)
        problem = health() if callable(health) else ""
        if problem and not self._applying:
            device = self.options.output_device or ""
            logger.warning("hermeneus: spoken output stopped: %s", problem)
            self._turn_speech_off(messages.output_device_gone(device))

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

    def _send_audio(self, pcm: bytes, captured_ns: int) -> None:
        """On the loop: note the chunk in the stream clock, then hand it to the STT client."""
        stats = getattr(self._c.stt, "stats", None)
        opened = getattr(stats, "sessions_opened", None)
        if opened is not None:
            if self._stt_sessions_seen is not None and opened != self._stt_sessions_seen:
                # A rotation or reconnect started a new Scribe session. The
                # client switches sessions inside its own sender, so where the
                # new session's sample 0 falls is not visible here. Word times
                # are no longer mapped rather than mapped wrongly (contract
                # request: a per-session offset from the STT client).
                self._clock_reliable = False
            self._stt_sessions_seen = opened
        self._stream_clock.on_frame(len(pcm), captured_ns)
        self._c.stt.send_audio(pcm, captured_ns)

    def _word_capture_ns(self, t_s: Optional[float]) -> Optional[int]:
        """Capture time of a Scribe word time, or None when it cannot be mapped reliably."""
        if t_s is None or not self._clock_reliable:
            return None
        mapped = self._stream_clock.to_capture_ns(float(t_s))
        if mapped is None or mapped[1]:
            return None
        return mapped[0]

    def _store(self, line: SubtitleLine) -> None:
        self.latency.record_timings(line.line_id, line.timings_ns)
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
        last = event.words[-1] if event.words and event.words[-1].type in (None, "word") else None
        last_end_ns = self._word_capture_ns(last.end) if last is not None else None
        for sentence in self._splitter.on_partial(text, now, last_end_ns):
            # The sentence takes over the partial line's id, so the display
            # keeps one line per sentence in speaking order.
            self._release(sentence, speaker=speaker, source_language=self._detected,
                          segment_index=None, received_ns=now, early=True)
        self._arm_pause_timer()
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

    def _arm_pause_timer(self) -> None:
        """Wake up when the finished last sentence is due at a pause (sentences.py pause rule)."""
        if self._pause_timer is not None:
            self._pause_timer.cancel()
            self._pause_timer = None
        deadline = self._splitter.pause_deadline_ns()
        if deadline is None or self._loop is None or self.closed:
            return
        self._pause_timer = self._loop.call_later(max(0.0, (deadline - self._clock()) / 1e9), self._on_pause)

    def _on_pause(self) -> None:
        self._pause_timer = None
        if self.closed or self._state not in ("live", "reconnecting", "starting"):
            return
        now = self._clock()
        for sentence in self._splitter.on_tick(now):
            self._release(sentence, speaker=None, source_language=self._detected,
                          segment_index=None, received_ns=now, early=True)
            self.stats["sentences_released_at_pause"] += 1

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
        timings["released"] = received_ns
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
        if self._state == "stopping":
            # The last sentence arrived while the session closes; translation is already shutting down.
            self._store(replace(line, state=LineState.FAILED,
                                error="Not translated: the session stopped while this sentence arrived."))
            return line
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
        self._arm_pause_timer()
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
            changed = replace(line, speaker_id=speaker) if speaker is not None and line.speaker_id != speaker else line
            timed = [w for w in chunk if w.start is not None]
            if timed:
                first = self._word_capture_ns(timed[0].start)
                last_w = timed[-1]
                end = self._word_capture_ns(last_w.end if last_w.end is not None else last_w.start)
                timings = dict(changed.timings_ns)
                if first is not None and Stage.CAPTURE not in timings:
                    timings[Stage.CAPTURE] = first
                if end is not None and Stage.SPEECH_END not in timings:
                    timings[Stage.SPEECH_END] = end
                if timings != changed.timings_ns:
                    changed = replace(changed, timings_ns=timings)
            if changed is not line:
                self._store(changed)

    def _on_merged(self, event: SpeakersMerged) -> None:
        for line_id, line in list(self._lines.items()):
            if line.speaker_id == event.from_speaker_id:
                self._lines[line_id] = replace(line, speaker_id=event.to_speaker_id)
        self.bus.publish_relabel(event.from_speaker_id, event.to_speaker_id)
        if self._scheduler is not None:
            self._scheduler.relabel_speaker(event.from_speaker_id, event.to_speaker_id)

    def _on_stt_status(self, event: SttStatus) -> None:
        if event.state == SttState.FAILED:
            self._fail_soon(messages.stt_failed(event.message, event.error_type))
            return
        if event.state == SttState.CLOSED:
            if self._state not in ("stopping", "idle", "failed"):
                self._fail_soon(messages.stt_failed(event.message or "the connection closed", event.error_type))
            return
        mapped = _STT_TO_SESSION.get(event.state)
        if mapped and self._state not in ("stopping", "idle", "failed") and self._no_audio:
            # Scribe closes an idle socket; with no sound that is expected, not a lost network.
            self._publish_status("live", messages.no_audio(self.options.source.label))
            return
        if mapped and self._state not in ("stopping", "idle", "failed"):
            if mapped == "reconnecting":
                message = messages.stt_reconnecting(event.message, event.error_type)
            else:
                message = event.message or ("live" if mapped == "live" else "")
            self._publish_status(mapped, message)

    def _note_translation_failure(self, error: str) -> None:
        """Show why translation fails in the status, without one status per failed line."""
        if not error:
            return
        now = self._clock()
        if messages.is_slow_failure(error):
            # One late line is noise; several within the window mean the model is too slow.
            self._slow_ns.append(now)
            while self._slow_ns and now - self._slow_ns[0] > SLOW_MODEL_WINDOW_S * 1e9:
                self._slow_ns.popleft()
            if len(self._slow_ns) < SLOW_MODEL_LINES:
                return
            error = messages.model_slow(len(self._slow_ns), SLOW_MODEL_WINDOW_S)
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
            # A call that succeeded ends a slow-model or failure message at once.
            self._slow_ns.clear()
            if self._failure_message:
                self._failure_message = ""
                if self._state in ("live", "reconnecting"):
                    self._publish_status(message="")
        else:
            self.stats["lines_failed"] += 1
            self._note_translation_failure(done.error)
        line = replace(done, original=original, speaker_id=speaker, timings_ns=timings)
        self._store(line)
        if (line.state == LineState.TRANSLATED and line.translation and self._speaking
                and self._speech is not None and not self.closed):
            self._speech.put_nowait((line.translation, line.target_language or self.options.target_language,
                                     line.line_id))

    # -- changes while running -----------------------------------------------------

    def apply_settings(self, changes: Mapping[str, Any]) -> dict:
        """Accept or refuse a change of mode, target language, voice, output device or source.

        Call on the session's loop. Returns at once: changes that need no I/O
        (target language, showing or hiding subtitles, speech off) are in
        effect when this returns; the rest (speech on, a new voice or output
        device, a new source) run in the background, listed in
        ``applying``, and the status reports how they ended. Raises
        SettingRefused with the reason and the fix.
        """
        if self._loop is None or self._done is None or self._done.is_set() \
                or self._state in ("stopping", "idle", "failed"):
            raise SettingRefused(messages.NOT_RUNNING)
        unknown = sorted(set(changes) - SETTING_FIELDS)
        if unknown:
            raise SettingRefused(f"Unknown setting {unknown[0]!r}. These can change while running: "
                                 "mode, speak, target_language, voice_id, output_device, source.")
        opts = self.options
        accepted: dict[str, Any] = {}

        speak, subtitles = opts.speak, opts.subtitles
        if changes.get("mode") is not None:
            mode = parse_mode(changes["mode"])
            speak, subtitles = mode != "subtitles", mode != "voice"
        if changes.get("speak") is not None:
            want = bool(changes["speak"])
            if changes.get("mode") is not None and want != speak:
                raise SettingRefused(f"--speak {'on' if want else 'off'} does not fit mode "
                                     f"{parse_mode(changes['mode'])!r}; pass only one of them.")
            if want != speak:
                speak, subtitles = want, True

        target = opts.target_language
        raw_target = str(changes.get("target_language") or "").strip()
        if raw_target:
            try:
                lang = languages.get(raw_target)
            except languages.UnknownLanguageError as exc:
                raise SettingRefused(f"Subtitle language: {exc}") from None
            target = lang.iso639_1 or lang.iso639_3
        if speak:
            lang = languages.get(target)
            if not lang.tts_flash_v2_5_supported:
                raise SettingRefused(messages.not_speakable(lang.english_name))

        voice = str(changes.get("voice_id") or "").strip() or opts.voice_id or DEFAULT_VOICE_ID
        if "/" in voice or any(ch.isspace() for ch in voice):
            raise SettingRefused(f"{voice!r} is not an ElevenLabs voice id. List the voices with: "
                                 "hermes hermeneus voices")
        device = str(changes.get("output_device") or "").strip() or opts.output_device or ""
        if speak and not device:
            raise SettingRefused(messages.NEED_DEVICE)

        source = _source_from(changes.get("source"))
        if source is not None and (source.backend, source.source_id) == (opts.source.backend, opts.source.source_id):
            source = None  # already capturing it
        if source is not None and self._c.capture_factory is None:
            raise SettingRefused("This session cannot change its source while running. Stop translation and "
                                 "start it again with --source.")

        cur_voice, cur_device = opts.voice_id or DEFAULT_VOICE_ID, opts.output_device or ""
        speaker_needed = speak and (not self._speaking or self._c.speaker is None
                                    or voice != cur_voice or device != cur_device)
        if speaker_needed and self._c.speaker_factory is None:
            raise SettingRefused("This session cannot start or change spoken translation while running. "
                                 "Stop translation and start it again with --speak, --voice or --device.")

        # In effect now: the target language, showing or hiding subtitles while
        # speech stays on, and speech off.
        if target != opts.target_language:
            accepted["target_language"] = target
        new_subtitles = subtitles if (opts.speak and speak) else opts.subtitles
        if opts.speak and not speak:
            accepted["mode"] = "subtitles"
        elif opts.speak and speak and new_subtitles != opts.subtitles:
            accepted["mode"] = "both" if new_subtitles else "voice"
        self.options = replace(self.options, target_language=target, subtitles=new_subtitles)
        if not speak:
            # Speech stays off: a new voice or output device is kept for the next time speech turns on (E7).
            if str(changes.get("voice_id") or "").strip() and voice != cur_voice:
                accepted["voice_id"] = voice
            if str(changes.get("output_device") or "").strip() and device != cur_device:
                accepted["output_device"] = device
            self.options = replace(self.options, voice_id=voice, output_device=device or None)
        self._want_speech = speak
        stop_speech = opts.speak and not speak
        if stop_speech:
            self._stop_speaking_now()

        # In the background, one request after another.
        applying: list[str] = []
        if source is not None:
            applying.append("source")
        if speaker_needed:
            if not self._speaking or self._c.speaker is None:
                applying.append("speak")
            else:
                if voice != cur_voice:
                    applying.append("voice_id")
                if device != cur_device:
                    applying.append("output_device")
        if applying or stop_speech:
            self._applying.extend(f for f in applying if f not in self._applying)
            self._start_job(self._apply_job(source=source, speaker=(voice, device, subtitles) if speaker_needed else None,
                                            close_speaker=stop_speech, fields=applying))
        if accepted or applying:
            self.stats["settings_changes"] += 1
        self._publish_status()
        return {"accepted": accepted, "applying": applying, "status": self.bus.status()}

    def _stop_speaking_now(self) -> None:
        """No new sentence is queued or spoken from here on; the speaker closes in the background."""
        self._speaking = False
        self.options = replace(self.options, speak=False, subtitles=True)
        if self._speech is not None:
            while not self._speech.empty():
                self._speech.get_nowait()

    def _start_job(self, coro: Awaitable) -> None:
        job = asyncio.get_running_loop().create_task(coro, name="hermeneus:settings")
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    def _turn_speech_off(self, message: str) -> None:
        if not self._speaking:
            return
        self._want_speech = False
        self._stop_speaking_now()
        self._start_job(self._apply_job(source=None, speaker=None, close_speaker=True, fields=[]))
        self._publish_status(message=message)

    async def _apply_job(self, *, source: Optional[CaptureSource], speaker: Optional[tuple],
                         close_speaker: bool, fields: list[str]) -> None:
        assert self._settings_lock is not None
        problems: list[str] = []
        try:
            async with self._settings_lock:
                if close_speaker and not self._want_speech:
                    await self._close_speaker()
                if source is not None:
                    try:
                        await self._switch_source(source)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        problems.append(f"Source not changed: {exc} Still capturing "
                                        f"{self.options.source.label}.")
                if speaker is not None and not self.closed:
                    try:
                        await self._switch_speaker(*speaker)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        what = "Spoken translation not changed" if self._speaking else "Spoken translation not started"
                        problems.append(f"{what}: {str(exc).rstrip('.')}." )
        finally:
            for f in fields:
                if f in self._applying:
                    self._applying.remove(f)
        if self.closed:
            return
        if problems:
            self._publish_status(message=" ".join(problems))
        elif fields:
            self._publish_status(message="")

    async def _close_speaker(self) -> None:
        task, self._speech_task = self._speech_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        old, self._c.speaker = self._c.speaker, None
        if old is not None:
            await _close_quietly(old, "speaker")

    async def _switch_speaker(self, voice: str, device: str, subtitles: bool) -> None:
        """Start a speaker with the new voice or device, then retire the old one. The old one keeps
        speaking until the new one is ready, and stays when the new one cannot start."""
        source = self.options.source
        assert self._c.speaker_factory is not None
        speaker = self._c.speaker_factory(voice, device, source)
        try:
            start = getattr(speaker, "start", None)
            if start is not None:
                await asyncio.wait_for(start(self.options.target_language), SPEAKER_START_TIMEOUT_S)
        except BaseException:
            await _close_quietly(speaker, "new speaker")
            raise
        if not self._want_speech or self.closed:
            await _close_quietly(speaker, "new speaker")  # speech was turned off while this one started
            return
        old = self._c.speaker
        task, self._speech_task = self._speech_task, None
        if task is not None:
            # The sentence the old speaker is sending is cut; the queue keeps its order.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._c.speaker = speaker
        self._speaker_error_seen = ""
        self.options = replace(self.options, speak=True, subtitles=subtitles, voice_id=voice, output_device=device)
        self._speaking = True
        self._ensure_speech_loop()
        if old is not None:
            await _close_quietly(old, "previous speaker")

    async def _switch_source(self, source: CaptureSource) -> None:
        """Start capture of ``source`` next to the old one, then stop the old one. Same STT session.

        Audio of the new source is dropped until the old capture has
        stopped, so the two never mix; the gap is the old capture's stop
        time. When the new source cannot start, the old one keeps running.
        """
        assert self._c.capture_factory is not None and self._frames is not None
        if self._speaking and self._c.route_check is not None and self.options.output_device:
            # Spoken translation must not be captured again from the new source.
            await asyncio.to_thread(self._c.route_check, self.options.output_device, source)
        backend = self._c.capture_factory(source)
        sink = _SwitchSink(self._frames.put)
        starting = asyncio.ensure_future(asyncio.to_thread(backend.start, source, sink.put))
        try:
            await asyncio.shield(starting)
        except asyncio.CancelledError:
            # The session is stopping while the new capture starts; stop it once it is up.
            starting.add_done_callback(
                lambda f: None if f.cancelled() or f.exception() is not None
                else threading.Thread(target=backend.stop, daemon=True).start())
            raise
        old = self._c.capture
        self._c.capture = backend
        self.options = replace(self.options, source=source)
        self._capture_note = ""
        try:
            await asyncio.wait_for(_in_daemon_thread(old.stop, "hermeneus-capture-switch"), STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("hermeneus: the previous capture took longer than %.1fs to stop", STOP_TIMEOUT_S)
        except Exception as exc:
            logger.warning("hermeneus: the previous capture failed to stop: %s", exc)
        sink.open = True
