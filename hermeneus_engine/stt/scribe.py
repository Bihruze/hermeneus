"""Streaming client for ElevenLabs Scribe v3 Live (realtime speech to text).

Protocol references (official ElevenLabs docs, fetched 2026-11-11):

* API reference, query parameters and message schemas:
  https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime
* Event and error types:
  https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/event-reference
* Commit strategies, chunk size guidance, previous_text, idle close:
  https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies
* Diarization, segment_index, speakers_merged:
  https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/diarization
* language_code, secondary_languages, multilang_mode:
  https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/language-options

Threading: ``send_audio`` may be called from any thread. It hands the PCM to
the event loop with ``loop.call_soon_threadsafe``; every other method and all
internal state live on the loop that ran ``start()``.

Identifiers exposed to consumers:

* ``segment_index`` on emitted events is a client-wide counter that starts at
  0 and grows by one per non-empty committed segment across every session.
  Scribe restarts its own ``segment_index`` in each session; the client keeps a
  per-session map from the server index to the global one.
* ``speaker_id`` values are namespaced per session as ``s<N>:<server id>``,
  for example ``s2:speaker_0``. Scribe restarts speaker numbering in every
  session, so ``s1:speaker_0`` and ``s2:speaker_0`` are different labels and
  may or may not be the same person. ``null`` speakers stay ``None``.
"""

from __future__ import annotations

import array
import asyncio
import base64
import math
import sys
import json
import logging
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlencode

from websockets.asyncio.client import connect as _ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from ..contracts import (
    ENGINE_SAMPLE_RATE,
    ENGINE_SAMPLE_WIDTH,
    CommittedTranscript,
    DiarizedTranscript,
    PartialTranscript,
    SecretGetter,
    SpeakersMerged,
    SttConfig,
    SttEvent,
    SttState,
    SttStatus,
    Word,
)
from ..redaction import KeyRedactor

log = logging.getLogger("hermeneus.stt.scribe")

SECRET_NAME = "ELEVENLABS_API_KEY"

# Error message types that retrying with the same key and settings cannot fix.
# input_error and chunk_size_exceeded mean this client sent something the
# server rejects; a reconnect would send the same thing again.
FATAL_ERRORS = frozenset({
    "auth_error",
    "quota_exceeded",
    "invalid_request",
    "unaccepted_terms",
    "input_error",
    "chunk_size_exceeded",
})

# Error message types that a new session can recover from.
RETRYABLE_ERRORS = frozenset({
    "rate_limited",
    "resource_exhausted",
    "queue_overflow",
    "transcriber_error",
    "session_time_limit_exceeded",
    "error",
})

# The server closed the session because no audio arrived. Reconnect only once
# audio is pending again, so a paused source does not open sessions in a loop.
IDLE_ERRORS = frozenset({"insufficient_audio_activity"})

# Reported but not session ending on their own.
NOTICE_TYPES = frozenset({"commit_throttled", "warning"})

ALL_ERROR_TYPES = FATAL_ERRORS | RETRYABLE_ERRORS | IDLE_ERRORS | {"commit_throttled"}

_FATAL_MESSAGES = {
    "auth_error": "ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY.",
    "permission": "The ElevenLabs API key does not have Speech to Text permission.",
    "quota_exceeded": "Your ElevenLabs quota is used up. Live transcription stopped.",
    "invalid_request": "ElevenLabs rejected the transcription settings",
    "unaccepted_terms": "Accept the Speech to Text terms in the ElevenLabs dashboard, then start again.",
    "input_error": "ElevenLabs could not read the audio stream",
    "chunk_size_exceeded": "ElevenLabs rejected an audio chunk as too large",
    "missing_key": "ELEVENLABS_API_KEY is not set.",
}

Connector = Callable[[str, dict], Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ScribeTuning:
    """Client behaviour that is not part of the Scribe query string.

    Defaults and their reasons:

    * ``chunk_ms=20``: the commit strategy guide says "Send audio chunks of
      0.1 - 1 second in length" and "Smaller chunks result in lower latency".
      20 ms is below that documented range on purpose. A chunk waits in the
      buffer until it is full, so its oldest sample is delayed by the chunk
      length. Measured against Scribe v3 Live on the same 180 s of speech (W20
      latency audit): word first seen in a partial p50 669 ms at 20 ms chunks,
      699 ms at 50 ms, 725 ms at 100 ms, with 0 errors over 15414 chunks and no
      backlog in the socket write buffer at 20 ms. In the full pipeline,
      pipeline minus a 20 ms control at p50 went from +56 ms (100 ms chunks)
      to -27 ms (run noise). 20 ms is 640 bytes of pcm_16000. No page states
      the ``chunk_size_exceeded`` threshold (UNVERIFIED).
    * ``queue_seconds=5``: audio kept while no session can take it. It covers
      the first backoff steps plus a handshake. A longer backlog would be
      replayed behind live audio and delay every later subtitle by its length.
    * ``backoff_base=0.5``, ``backoff_cap=10``, ``max_attempts=8``: full jitter
      exponential backoff. Eight attempts with these values wait at most about
      45 s in total before the client reports FAILED.
    * ``rotation_seconds=600``: no official page states the session time limit
      behind ``session_time_limit_exceeded`` (UNVERIFIED). Rotation pre-opens
      the next session, so a short interval costs only one extra handshake.
      ``SttConfig.max_session_seconds`` overrides it when non-zero.
    * ``boundary_wait=20``: how long a rotation waits for a pause in the sent
      audio (``pause_ms`` below ``pause_dbfs``) before it switches anyway.
    * ``language_wait=0.25``: how long a committed segment waits for its
      ``committed_transcript_with_timestamps`` (the only event that carries
      ``language_code``). It ends as soon as that event arrives. Live, the two
      events were at most 7 ms apart in either order over 41 commits.
    * ``drain_timeout=5.0``: a session that received ``commit: true`` from the
      client (rotation or close) is closed as soon as the server has answered
      that commit with its committed segment, the language event and, with
      diarization, the diarized segment. ``drain_timeout`` bounds that wait
      for rotations. ``drain_quiet=1.0`` is the fallback for a commit the
      server does not answer: the session closes after that much silence from
      the server.
    * ``close_budget=2.0``: total time ``close()`` may take when the caller
      passes no ``timeout``.
    * ``previous_text_chars=50``: the guide says previous_text "works best
      when it's under 50 characters long" and recommends it after reconnects.
    * ``vad_silence_threshold_secs=None`` keeps the server default (1.5 s, as
      echoed in ``session_started.config``). The guide accepts 0.192 to 3.0.
    * ``segment_soft_seconds=10``, ``segment_hard_seconds=20``, ``pause_dbfs=-40``,
      ``pause_ms=120``: segment length cap. Continuous speech without a pause
      as long as the VAD silence threshold is only committed by the server's
      own cap, about 36 s of audio (guide), which measured 35 s per segment
      live. Once a segment holds ``segment_soft_seconds`` of audio, the client
      sends ``commit: true`` as soon as the audio it has sent ends in
      ``pause_ms`` below ``pause_dbfs``; at ``segment_hard_seconds`` it commits
      without waiting for a pause. The guide calls committing every 20 to 30 s
      good practice and warns against several commits in a short sequence.
      Set either value to 0 to disable that trigger.
    """

    chunk_ms: int = 20
    queue_seconds: float = 5.0
    backoff_base: float = 0.5
    backoff_cap: float = 10.0
    max_attempts: int = 8
    rotation_seconds: float = 600.0
    rotation_retry_seconds: float = 30.0
    boundary_wait: float = 20.0
    language_wait: float = 0.25
    drain_quiet: float = 1.0
    drain_timeout: float = 5.0
    open_timeout: float = 10.0
    close_timeout: float = 2.0
    close_budget: float = 2.0
    send_previous_text: bool = True
    previous_text_chars: int = 50
    vad_silence_threshold_secs: Optional[float] = None
    segment_soft_seconds: float = 10.0
    segment_hard_seconds: float = 20.0
    pause_dbfs: float = -40.0
    pause_ms: int = 120

    @property
    def chunk_bytes(self) -> int:
        return ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH * self.chunk_ms // 1000

    @property
    def queue_bytes(self) -> int:
        n = int(ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH * self.queue_seconds)
        return n - (n % ENGINE_SAMPLE_WIDTH)


@dataclass
class ScribeStats:
    sessions_opened: int = 0
    reconnects: int = 0
    rotations: int = 0
    chunks_sent: int = 0
    audio_bytes_sent: int = 0
    drop_events: int = 0
    audio_bytes_dropped: int = 0
    languages_missed: int = 0
    pause_commits: int = 0
    hard_commits: int = 0
    close_ms: dict = field(default_factory=dict)  # phase name -> ms since close() began


def build_url(config: SttConfig, tuning: Optional[ScribeTuning] = None) -> str:
    """Return the realtime URL with every query parameter the client relies on.

    ``commit_strategy`` is always sent: the commit guide says the default is
    ``manual`` while the API reference says ``turn_prediction`` is the default
    for turn-predicting models, so the client never relies on either default.
    """

    def flag(value: bool) -> str:
        return "true" if value else "false"

    query: list[tuple[str, str]] = [
        ("model_id", config.model_id),
        ("audio_format", f"pcm_{ENGINE_SAMPLE_RATE}"),
        ("commit_strategy", config.commit_strategy.value),
        ("diarize", flag(config.diarize)),
        ("multilang_mode", flag(config.multilang_mode)),
        ("include_language_detection", flag(config.include_language_detection)),
    ]
    if config.language_code:
        query.append(("language_code", config.language_code))
    for code in config.secondary_languages:
        query.append(("secondary_languages", code))
    if tuning is not None and tuning.vad_silence_threshold_secs is not None:
        query.append(("vad_silence_threshold_secs", f"{tuning.vad_silence_threshold_secs:g}"))
    return f"{config.base_url}?{urlencode(query)}"


class _Fatal(Exception):
    def __init__(self, message: str, error_type: str = ""):
        super().__init__(message)
        self.message = message
        self.error_type = error_type


class _Retry(Exception):
    def __init__(self, message: str, error_type: str = "", immediate: bool = False, idle: bool = False):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.immediate = immediate
        self.idle = idle


@dataclass
class _Item:
    kind: str                      # partial | committed | diarized | merged
    event: Any = None              # ready contract event (partial, merged)
    text: str = ""
    local_index: Optional[int] = None
    language: Optional[str] = None
    words: tuple = ()
    received_ns: int = 0
    ready: bool = True
    empty: bool = False


@dataclass(eq=False)
class _Session:
    number: int
    ws: Any
    opened_monotonic: float
    started: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    activity: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outbox: list = field(default_factory=list)
    local_to_global: dict = field(default_factory=dict)
    skipped: set = field(default_factory=set)
    early_languages: list = field(default_factory=list)  # (text, language) seen before their commit
    commits_received: int = 0
    last_commit_index: Optional[int] = None
    diarized_seen: set = field(default_factory=set)
    flush_mark: Optional[int] = None  # commits_received when the client sent its final commit:true
    receiver: Optional[asyncio.Task] = None
    session_id: str = ""
    error_type: str = ""
    error_text: str = ""
    close_reason: str = ""
    got_transcript: bool = False
    draining: bool = False
    admitted: bool = False
    finished: bool = False
    first_chunk_sent: bool = False
    segment_bytes: int = 0      # audio sent since the last commit seen or sent
    trailing_quiet_ms: int = 0  # length of the quiet run at the end of the sent audio
    wants_previous_text: bool = False
    rotate_at: float = 0.0

    def speaker(self, raw: Any) -> Optional[str]:
        if raw is None or raw == "":
            return None
        return f"s{self.number}:{raw}"


class ScribeLiveClient:
    """One logical transcription stream over a series of Scribe sessions."""

    def __init__(self, config: SttConfig, get_secret: SecretGetter, *,
                 tuning: Optional[ScribeTuning] = None,
                 clock: Callable[[], int] = time.monotonic_ns,
                 connect: Optional[Connector] = None,
                 sleep: Sleeper = asyncio.sleep,
                 rng: Callable[[], float] = random.random,
                 secret_name: str = SECRET_NAME) -> None:
        self._config = config
        self._get_secret = get_secret
        self._secret_name = secret_name
        tuning = tuning or ScribeTuning()
        if config.max_session_seconds > 0:
            tuning = replace(tuning, rotation_seconds=float(config.max_session_seconds))
        self._tuning = tuning
        self._rotation_seconds = tuning.rotation_seconds
        self._clock = clock
        self._connect = connect or self._default_connect
        self._sleep = sleep
        self._rng = rng
        self.stats = ScribeStats()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._events: asyncio.Queue = asyncio.Queue()
        self._buf = bytearray()
        self._wake_sender = asyncio.Event()
        self._audio_arrived = asyncio.Event()
        self._close_requested = asyncio.Event()
        self._active: Optional[_Session] = None
        self._sessions: list[_Session] = []
        self._next_number = 1
        self._next_segment = 0
        self._last_text = ""
        self._keys = KeyRedactor()
        self._state: Optional[SttState] = None
        self._accepting = False
        self._closing = False
        self._terminal = False
        self._supervisor: Optional[asyncio.Task] = None
        self._sender: Optional[asyncio.Task] = None
        self._drains: set[asyncio.Task] = set()
        self._rotation_target: Optional[tuple[_Session, _Session]] = None
        self._close_started: Optional[float] = None
        self._close_deadline: Optional[float] = None
        self._rotation_switched = asyncio.Event()

    # ------------------------------------------------------------------ API

    async def start(self) -> None:
        """Begin connecting. Progress and failures arrive as SttStatus events."""
        if self._loop is not None:
            raise RuntimeError("ScribeLiveClient.start() was already called")
        self._loop = asyncio.get_running_loop()
        self._accepting = True
        self._status(SttState.CONNECTING)
        self._sender = asyncio.create_task(self._send_loop(), name="scribe-sender")
        self._supervisor = asyncio.create_task(self._run(), name="scribe-supervisor")

    def send_audio(self, pcm: bytes, captured_ns: int) -> None:
        """Queue engine-format PCM. Never blocks; safe from any thread.

        ``captured_ns`` is accepted for the capture contract; latency
        attribution is done by the harness that owns capture timestamps.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError("ScribeLiveClient.start() must run before send_audio()")
        if not pcm:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, bytes(pcm))
        except RuntimeError:
            pass  # loop already closed: the stream is over

    async def events(self) -> AsyncIterator[SttEvent]:
        """Yield contract events until a CLOSED or FAILED status."""
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, SttStatus) and event.state in (SttState.CLOSED, SttState.FAILED):
                return

    async def close(self, timeout: Optional[float] = None) -> None:
        """Commit trailing audio, wait for its transcript, send a close frame.

        Returns within ``timeout`` seconds (``ScribeTuning.close_budget`` when
        None). Text the server delivers before the deadline is emitted; the
        socket closes with code 1000. If a phase overruns, the remaining
        sockets are dropped and CLOSED is emitted at the deadline.
        """
        if self._loop is None or self._terminal:
            return
        budget = self._tuning.close_budget if timeout is None else max(0.0, timeout)
        self._close_started = time.monotonic()
        self._close_deadline = self._close_started + budget
        self.stats.close_ms = {}
        # Audio handed over by send_audio() before this call is still queued
        # on the loop; let those callbacks run before the queue is sealed.
        await asyncio.sleep(0)
        self._accepting = False
        self._close_requested.set()
        self._wake_sender.set()
        if self._supervisor is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._supervisor), budget)
        except asyncio.TimeoutError:
            await self._force_close()
        except asyncio.CancelledError:
            if not self._supervisor.done():
                raise

    def _close_mark(self, phase: str) -> None:
        if self._close_started is not None:
            self.stats.close_ms[phase] = round((time.monotonic() - self._close_started) * 1000, 1)

    async def _force_close(self) -> None:
        """Deadline reached: drop every socket now and report CLOSED."""
        self._close_mark("forced")
        log.warning("scribe close overran its budget; dropping the connection")
        self._terminal = True
        if self._supervisor is not None:
            self._supervisor.cancel()
        if self._sender is not None:
            self._sender.cancel()
        for task in list(self._drains):
            task.cancel()
        for session in list(self._sessions):
            transport = getattr(session.ws, "transport", None)
            if transport is not None:
                transport.abort()
            if session.receiver is not None:
                session.receiver.cancel()
            self._finish(session)
        self._active = None
        if self._state not in (SttState.CLOSED, SttState.FAILED):
            self._terminate(SttState.CLOSED)

    # ------------------------------------------------------------- audio in

    def _enqueue(self, pcm: bytes) -> None:
        if not self._accepting:
            return
        self._buf.extend(pcm)
        excess = len(self._buf) - self._tuning.queue_bytes
        if excess > 0:
            excess += excess % ENGINE_SAMPLE_WIDTH
            del self._buf[:excess]
            self.stats.drop_events += 1
            self.stats.audio_bytes_dropped += excess
        self._audio_arrived.set()
        self._wake_sender.set()

    def _audio_message(self, session: _Session, pcm: bytes, commit: bool) -> str:
        msg = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "commit": commit,
            "sample_rate": ENGINE_SAMPLE_RATE,
        }
        if not session.first_chunk_sent and session.wants_previous_text:
            context = self._previous_text()
            if context:
                msg["previous_text"] = context
        return json.dumps(msg)

    async def _send_on(self, session: _Session, pcm: bytes, commit: bool) -> None:
        """Send one chunk; caller holds ``session.send_lock``."""
        await session.ws.send(self._audio_message(session, pcm, commit))
        session.first_chunk_sent = True
        self.stats.chunks_sent += 1
        self.stats.audio_bytes_sent += len(pcm)
        if commit:
            session.segment_bytes = 0
        else:
            session.segment_bytes += len(pcm)
            session.trailing_quiet_ms = _trailing_quiet_ms(pcm, session.trailing_quiet_ms,
                                                           self._tuning.pause_dbfs)

    def _segment_commit_due(self, session: _Session) -> str:
        """Return "hard", "pause" or "" for the segment length cap."""
        t = self._tuning
        seconds = session.segment_bytes / (ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH)
        if t.segment_hard_seconds > 0 and seconds >= t.segment_hard_seconds:
            return "hard"
        if t.segment_soft_seconds > 0 and seconds >= t.segment_soft_seconds \
                and session.trailing_quiet_ms >= t.pause_ms:
            return "pause"
        return ""

    async def _send_loop(self) -> None:
        chunk_bytes = self._tuning.chunk_bytes
        pending: Optional[bytes] = None
        while True:
            if pending is None:
                n = len(self._buf)
                if n >= chunk_bytes or (self._closing and n > 0):
                    take = min(n, chunk_bytes)
                    pending = bytes(self._buf[:take])
                    del self._buf[:take]
                elif self._closing:
                    return
                else:
                    self._wake_sender.clear()
                    await self._wake_sender.wait()
                    continue
            session = self._active
            if session is None or session.done.is_set():
                if self._closing:
                    return
                self._wake_sender.clear()
                await self._wake_sender.wait()
                continue
            async with session.send_lock:
                if session is not self._active:
                    continue
                try:
                    await self._send_on(session, pending, commit=False)
                    pending = None
                    target = self._rotation_target
                    if target is not None and target[0] is session \
                            and session.trailing_quiet_ms >= self._tuning.pause_ms:
                        await self._switch_locked(*target)
                        continue
                    due = self._segment_commit_due(session)
                    if due:
                        await self._send_on(session, b"", commit=True)
                        if due == "hard":
                            self.stats.hard_commits += 1
                        else:
                            self.stats.pause_commits += 1
                except ConnectionClosed:
                    pass
                except Exception as exc:
                    log.warning("scribe session %d: send failed: %s", session.number,
                                self._redact(f"{type(exc).__name__}: {exc}"))
            if pending is not None:
                # The socket is closing; keep the chunk for the next session.
                await _first(session.done.wait(), timeout=self._tuning.close_timeout)

    # ----------------------------------------------------------- connecting

    async def _default_connect(self, url: str, headers: dict) -> Any:
        return await _ws_connect(url, additional_headers=headers,
                                 open_timeout=self._tuning.open_timeout,
                                 close_timeout=self._tuning.close_timeout)

    def _redact(self, text: str) -> str:
        return self._keys.redact(text)

    def _remember_key(self, key: str) -> None:
        self._keys.remember(key)

    async def _open_session(self) -> _Session:
        key = self._get_secret(self._secret_name)
        if not key:
            raise _Fatal(_FATAL_MESSAGES["missing_key"], "missing_key")
        self._remember_key(key)
        url = build_url(self._config, self._tuning)
        try:
            ws = await self._connect(url, {"xi-api-key": key})
        except InvalidStatus as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise _Fatal(_FATAL_MESSAGES["auth_error" if code == 401 else "permission"],
                             f"http_{code}") from None
            if code == 400:
                raise _Fatal(f"{_FATAL_MESSAGES['invalid_request']} (HTTP 400).", "http_400") from None
            raise _Retry(f"ElevenLabs answered HTTP {code}", f"http_{code}") from None
        except InvalidURI as exc:
            raise _Fatal(f"Invalid Scribe URL: {self._redact(str(exc))}", "invalid_uri") from None
        except (OSError, asyncio.TimeoutError, ConnectionClosed) as exc:
            raise _Retry(f"Could not reach ElevenLabs: {self._redact(str(exc)) or type(exc).__name__}",
                         "connect_failed") from None
        except Exception as exc:  # handshake errors from the websockets library
            raise _Retry(f"Could not open the Scribe session: {self._redact(str(exc)) or type(exc).__name__}",
                         "connect_failed") from None

        session = _Session(number=self._next_number, ws=ws, opened_monotonic=time.monotonic())
        self._next_number += 1
        self.stats.sessions_opened += 1
        # previous_text is computed at the first chunk, after the last commit of
        # the session this one replaces has been emitted.
        session.wants_previous_text = self._tuning.send_previous_text and session.number > 1
        self._sessions.append(session)
        session.receiver = asyncio.create_task(self._receive(session), name=f"scribe-recv-{session.number}")
        started = asyncio.create_task(session.started.wait())
        done = asyncio.create_task(session.done.wait())
        try:
            await asyncio.wait({started, done}, timeout=self._tuning.open_timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            started.cancel()
            done.cancel()
        if session.started.is_set() and not session.done.is_set():
            session.rotate_at = time.monotonic() + self._rotation_seconds
            log.debug("scribe session %d started (server id %s)", session.number, session.session_id)
            return session
        if not session.done.is_set():
            await self._abort(session)
            raise _Retry("ElevenLabs did not start the session in time", "session_start_timeout")
        await self._abort(session)
        raise self._session_outcome(session)

    def _previous_text(self) -> str:
        limit = self._tuning.previous_text_chars
        text = " ".join(self._last_text.split())
        if len(text) <= limit:
            return text
        tail = text[-limit:]
        space = tail.find(" ")
        return tail[space + 1:] if 0 <= space < len(tail) - 1 else tail

    async def _abort(self, session: _Session, budget: Optional[float] = None) -> None:
        """Close with code 1000 within ``budget`` seconds, including the receive task join."""
        total = self._tuning.close_timeout if budget is None else min(budget, self._tuning.close_timeout)
        deadline = time.monotonic() + max(0.02, total)
        # websockets waits close_timeout for the close frame reply, then drops
        # the TCP connection; keep a little time for that and the join.
        if hasattr(session.ws, "close_timeout"):
            session.ws.close_timeout = max(0.01, 0.8 * (deadline - time.monotonic()))
        try:
            await asyncio.wait_for(session.ws.close(), max(0.01, deadline - time.monotonic()))
        except (asyncio.TimeoutError, Exception):
            transport = getattr(session.ws, "transport", None)
            if transport is not None:
                transport.abort()
        if session.receiver is not None:
            try:
                await asyncio.wait_for(session.receiver, max(0.01, deadline - time.monotonic()))
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                session.receiver.cancel()
        self._finish(session)

    def _session_outcome(self, session: _Session) -> Exception:
        etype, etext = session.error_type, self._redact(session.error_text)
        if etype:
            lowered = etext.lower()
            if "permission" in lowered and etype in ("auth_error", "error", "invalid_request"):
                return _Fatal(_FATAL_MESSAGES["permission"], etype)
            if etype in FATAL_ERRORS:
                base = _FATAL_MESSAGES[etype]
                if etype in ("invalid_request", "input_error", "chunk_size_exceeded"):
                    return _Fatal(f"{base}: {etext}" if etext else f"{base}.", etype)
                return _Fatal(base, etype)
            if etype in IDLE_ERRORS:
                return _Retry("No audio reached ElevenLabs for a while", etype, idle=True)
            if etype == "session_time_limit_exceeded":
                return _Retry("Scribe session time limit reached", etype, immediate=True)
            return _Retry(f"Scribe reported {etype}", etype)
        reason = self._redact(session.close_reason) or "connection closed"
        return _Retry(f"Connection to ElevenLabs dropped ({reason})", "connection_closed")

    # -------------------------------------------------------------- receive

    async def _receive(self, session: _Session) -> None:
        try:
            async for raw in session.ws:
                now = self._clock()
                if isinstance(raw, bytes):
                    log.debug("scribe session %d: ignored binary frame", session.number)
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.debug("scribe session %d: ignored non-JSON frame", session.number)
                    continue
                if isinstance(msg, dict):
                    self._handle(session, msg, now)
            rcvd = getattr(session.ws, "close_rcvd", None)
            session.close_reason = f"code {rcvd.code}" if rcvd is not None else "closed"
        except ConnectionClosed as exc:
            session.close_reason = self._redact(str(exc))
        except Exception as exc:
            session.close_reason = self._redact(f"{type(exc).__name__}: {exc}")
        finally:
            session.done.set()
            session.activity.set()
            self._wake_sender.set()
            if session.draining or self._terminal:
                self._finish(session)

    def _handle(self, session: _Session, msg: dict, now: int) -> None:
        kind = msg.get("message_type")
        session.activity.set()
        if kind == "session_started":
            session.session_id = str(msg.get("session_id") or "")
            session.started.set()
            return
        if kind == "partial_transcript":
            session.got_transcript = True
            if session.draining:
                return
            event = PartialTranscript(
                text=str(msg.get("text") or ""),
                words=self._words(session, msg.get("words")),
                turn_probability=_float(msg.get("turn_probability")),
                received_ns=now,
            )
            session.outbox.append(_Item("partial", event=event, received_ns=now))
        elif kind == "committed_transcript":
            session.got_transcript = True
            text = str(msg.get("text") or "")
            item = _Item("committed", text=text, local_index=_int(msg.get("segment_index")),
                         received_ns=now, empty=not text.strip(),
                         ready=not self._config.include_language_detection)
            if not item.ready:
                early = _take_matching(session.early_languages, text)
                if early is not None:
                    item.language, item.ready = early[1], True
            session.outbox.append(item)
            session.segment_bytes = 0
            session.commits_received += 1
            session.last_commit_index = item.local_index
            if not item.ready:
                self._loop.call_later(self._tuning.language_wait, self._language_timeout, session, item)
        elif kind == "committed_transcript_with_timestamps":
            # Live runs show this event 2 ms after the committed_transcript for
            # VAD commits and 2 ms before it for a client commit:true, so it is
            # matched in either order.
            text = str(msg.get("text") or "")
            lang = msg.get("language_code")
            language = str(lang) if lang else None
            waiting = [i for i in session.outbox if i.kind == "committed" and not i.ready]
            match = next((i for i in waiting if i.text.strip() == text.strip()), None)
            if match is None:
                session.early_languages.append((text, language))
                del session.early_languages[:-_EARLY_LANGUAGE_LIMIT]
                return
            match.language = language
            match.ready = True
        elif kind == "diarized_transcript":
            session.diarized_seen.add(_int(msg.get("segment_index")))
            session.outbox.append(_Item(
                "diarized", text=str(msg.get("text") or ""), local_index=_int(msg.get("segment_index")),
                words=self._words(session, msg.get("words")), received_ns=now))
        elif kind == "speakers_merged":
            src, dst = session.speaker(msg.get("from_speaker_id")), session.speaker(msg.get("to_speaker_id"))
            if src and dst:
                session.outbox.append(_Item("merged", event=SpeakersMerged(src, dst, received_ns=now)))
        elif kind in ALL_ERROR_TYPES:
            text = self._redact(str(msg.get("error") or ""))
            if kind == "commit_throttled":
                log.warning("scribe session %d: commit_throttled: %s", session.number, text)
                return
            log.warning("scribe session %d: %s: %s", session.number, kind, text)
            if not session.error_type:
                session.error_type, session.error_text = kind, text
            return
        elif kind == "warning":
            log.warning("scribe session %d: warning: %s", session.number,
                        self._redact(str(msg.get("warning") or "")))
            return
        else:
            log.debug("scribe session %d: ignored message type %r", session.number, kind)
            return
        self._pump()

    def _words(self, session: _Session, raw: Any) -> tuple[Word, ...]:
        if not isinstance(raw, list):
            return ()
        out = []
        for w in raw:
            if not isinstance(w, dict):
                continue
            out.append(Word(text=str(w.get("text") or ""), start=_float(w.get("start")),
                            end=_float(w.get("end")), speaker_id=session.speaker(w.get("speaker_id")),
                            type=w.get("type") if isinstance(w.get("type"), str) else None))
        return tuple(out)

    def _language_timeout(self, session: _Session, item: _Item) -> None:
        if not item.ready:
            item.ready = True
            self.stats.languages_missed += 1
            log.debug("scribe session %d: no language event within %.2fs", session.number,
                      self._tuning.language_wait)
            self._pump()

    # ----------------------------------------------------------- emit order

    def _finish(self, session: _Session) -> None:
        if session.finished:
            return
        session.finished = True
        session.admitted = True
        for item in session.outbox:
            if not item.ready:
                item.ready = True
                self.stats.languages_missed += 1
        self._pump()

    def _pump(self) -> None:
        """Emit ready events in order.

        Within a session events leave in receive order; a commit waiting for
        its language holds everything behind it. Across sessions, a newer
        session releases only partials until every older session is finished,
        so commits stay in audio order during a rotation.
        """
        older_open = False
        for session in self._sessions:
            while session.admitted and session.outbox and session.outbox[0].ready:
                item = session.outbox[0]
                if older_open and item.kind != "partial":
                    break
                session.outbox.pop(0)
                self._emit_item(session, item)
            if not session.finished or session.outbox:
                older_open = True
        self._sessions = [s for s in self._sessions if not (s.finished and not s.outbox)]

    def _emit_item(self, session: _Session, item: _Item) -> None:
        if item.kind in ("partial", "merged"):
            self._events.put_nowait(item.event)
            return
        if item.kind == "committed":
            if item.empty:
                if item.local_index is not None:
                    session.skipped.add(item.local_index)
                return
            index = self._next_segment
            self._next_segment += 1
            if item.local_index is not None:
                session.local_to_global[item.local_index] = index
            self._last_text = item.text
            self._events.put_nowait(CommittedTranscript(text=item.text, segment_index=index,
                                                        language_code=item.language,
                                                        received_ns=item.received_ns))
            return
        if item.kind == "diarized":
            if item.local_index in session.skipped:
                return
            index = session.local_to_global.get(item.local_index)
            if index is None:
                log.debug("scribe session %d: diarized segment %r has no commit", session.number,
                          item.local_index)
                return
            self._events.put_nowait(DiarizedTranscript(segment_index=index, text=item.text,
                                                       words=item.words, received_ns=item.received_ns))

    def _status(self, state: SttState, message: str = "", error_type: str = "") -> None:
        if state == self._state and state != SttState.RECONNECTING:
            return
        self._state = state
        self._events.put_nowait(SttStatus(state=state, message=message, error_type=error_type))

    # ------------------------------------------------------------ supervise

    async def _run(self) -> None:
        try:
            await self._supervise()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("scribe client stopped on an internal error: %s", self._redact(repr(exc)))
            await self._fail("Live transcription stopped on an internal error.", "internal")

    async def _supervise(self) -> None:
        attempt = 0
        while True:
            if self._close_requested.is_set():
                await self._shutdown_without_session()
                return
            try:
                session = await self._open_session()
            except _Fatal as exc:
                await self._fail(exc.message, exc.error_type)
                return
            except _Retry as exc:
                attempt += 1
                if not await self._backoff(attempt, exc):
                    return
                continue

            self._set_active(session)
            self._status(SttState.LIVE)
            self._admit(session)
            while True:
                outcome = await self._watch(session)
                if outcome == "close":
                    await self._graceful_close(session)
                    return
                if outcome == "rotate":
                    try:
                        session = await self._rotate(session)
                    except _Fatal as exc:
                        await self._fail(exc.message, exc.error_type)
                        return
                    continue
                break

            if session.got_transcript:
                attempt = 0
            self._set_active(None)
            self._finish(session)
            exc = self._session_outcome(session)
            if isinstance(exc, _Fatal):
                await self._fail(exc.message, exc.error_type)
                return
            if exc.error_type == "session_time_limit_exceeded":
                self._learn_session_limit(session)
            attempt += 1
            self.stats.reconnects += 1
            if not await self._backoff(attempt, exc):
                return

    async def _backoff(self, attempt: int, exc: _Retry) -> bool:
        """Wait before the next attempt. Returns False when the client stopped."""
        if attempt > self._tuning.max_attempts:
            await self._fail(f"Lost connection to ElevenLabs after {self._tuning.max_attempts} attempts. "
                             f"Last error: {exc.message}.", exc.error_type)
            return False
        self._status(SttState.RECONNECTING, exc.message, exc.error_type)
        log.info("scribe reconnect %d/%d: %s", attempt, self._tuning.max_attempts, exc.message)
        if exc.idle:
            self._audio_arrived.clear()
            if not self._buf:
                await _first(self._audio_arrived.wait(), self._close_requested.wait())
            return True
        if not exc.immediate or attempt > 1:
            ceiling = min(self._tuning.backoff_cap, self._tuning.backoff_base * (2 ** (attempt - 1)))
            delay = ceiling * self._rng()
            await _first(self._sleep(delay), self._close_requested.wait())
        return True

    def _learn_session_limit(self, session: _Session) -> None:
        age = time.monotonic() - session.opened_monotonic
        learned = max(30.0, 0.8 * age - self._tuning.boundary_wait)
        if learned < self._rotation_seconds:
            log.info("scribe session limit hit after %.0fs; rotating every %.0fs from now", age, learned)
            self._rotation_seconds = learned

    def _admit(self, session: _Session) -> None:
        """Release events of a session once its LIVE status has been emitted."""
        session.admitted = True
        self._pump()

    def _set_active(self, session: Optional[_Session]) -> None:
        self._active = session
        self._wake_sender.set()

    async def _watch(self, session: _Session) -> str:
        while True:
            if self._close_requested.is_set():
                return "close"
            if session.done.is_set():
                return "ended"
            remaining = session.rotate_at - time.monotonic()
            if remaining <= 0:
                return "rotate"
            await _first(session.done.wait(), self._close_requested.wait(), timeout=remaining)

    async def _rotate(self, old: _Session) -> _Session:
        """Move the stream to a new session at a pause in the audio.

        The switch happens in the sender, right after a chunk that leaves the
        sent audio ending in ``pause_ms`` of quiet, so neither session receives
        half a word. Waiting for the server's next commit instead is too late:
        live, the commit event arrives after more speech has been sent, and
        rotating on it split words between the two sessions.
        """
        try:
            new = await self._open_session()
        except _Retry as exc:
            log.info("scribe rotation postponed: %s", exc.message)
            old.rotate_at = time.monotonic() + self._tuning.rotation_retry_seconds
            return old
        self._admit(new)
        self._rotation_switched.clear()
        self._rotation_target = (old, new)
        await _first(self._rotation_switched.wait(), old.done.wait(), self._close_requested.wait(),
                     timeout=self._tuning.boundary_wait)
        if not self._rotation_switched.is_set():
            if self._close_requested.is_set() and not old.done.is_set():
                self._rotation_target = None
                await self._abort(new)
                old.rotate_at = time.monotonic() + self._rotation_seconds
                return old
            async with old.send_lock:
                if not self._rotation_switched.is_set():
                    log.debug("scribe session %d: no pause within %.0fs, rotating anyway", old.number,
                              self._tuning.boundary_wait)
                    await self._switch_locked(old, new)
        self.stats.rotations += 1
        log.debug("scribe rotated session %d -> %d", old.number, new.number)
        task = asyncio.create_task(self._drain_and_close(old), name=f"scribe-drain-{old.number}")
        self._drains.add(task)
        task.add_done_callback(self._drains.discard)
        return new

    async def _switch_locked(self, old: _Session, new: _Session) -> None:
        """Send later audio to ``new`` and commit the tail of ``old``. Caller holds ``old.send_lock``."""
        self._rotation_target = None
        self._set_active(new)
        old.draining = True
        await self._send_flush(old)
        self._rotation_switched.set()

    def _flush_answered(self, session: _Session) -> bool:
        """The server has answered the client's final commit in full."""
        if session.flush_mark is None or session.commits_received <= session.flush_mark:
            return False
        if any(not i.ready for i in session.outbox):
            return False
        if self._config.diarize and session.last_commit_index is not None:
            return session.last_commit_index in session.diarized_seen
        return True

    async def _send_flush(self, session: _Session) -> None:
        """Send the final ``commit: true``; caller holds ``session.send_lock``."""
        if session.done.is_set():
            return
        session.flush_mark = session.commits_received
        try:
            await self._send_on(session, b"", commit=True)
        except ConnectionClosed:
            session.flush_mark = None

    async def _drain_and_close(self, session: _Session, deadline: Optional[float] = None,
                               reserve: float = 0.0) -> None:
        """Wait until the final commit is answered (or the deadline), then close."""
        if deadline is None:
            deadline = time.monotonic() + self._tuning.drain_timeout
        quiet_since = time.monotonic()
        while not session.done.is_set() and not self._flush_answered(session):
            remaining = deadline - reserve - time.monotonic()
            if remaining <= 0:
                break
            if session.flush_mark is None and time.monotonic() - quiet_since >= self._tuning.drain_quiet:
                break
            session.activity.clear()
            got = await _first(session.activity.wait(), timeout=min(remaining, self._tuning.drain_quiet))
            if got:
                quiet_since = time.monotonic()
            elif not any(not i.ready for i in session.outbox):
                break  # the server stayed silent for drain_quiet
        session.draining = True
        await self._abort(session, budget=max(0.0, deadline - time.monotonic()))

    async def _graceful_close(self, session: _Session) -> None:
        deadline = self._close_deadline or (time.monotonic() + self._tuning.close_budget)
        # Keep part of the budget for the close handshake.
        reserve = min(0.3, 0.25 * max(0.0, deadline - time.monotonic()))
        self._closing = True
        self._wake_sender.set()
        if self._sender is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._sender),
                                       max(0.0, deadline - reserve - time.monotonic()))
            except asyncio.TimeoutError:
                self._sender.cancel()
        self._close_mark("audio sent")
        async with session.send_lock:
            self._set_active(None)
            session.draining = True
            await self._send_flush(session)
        self._close_mark("final commit sent")
        drained = asyncio.create_task(self._drain_and_close(session, deadline, reserve))
        await drained
        self._close_mark("socket closed")
        if self._drains:
            await asyncio.wait(set(self._drains), timeout=max(0.0, deadline - time.monotonic()))
            for task in list(self._drains):
                task.cancel()
        self._terminate(SttState.CLOSED)
        self._close_mark("closed")

    async def _shutdown_without_session(self) -> None:
        self._closing = True
        self._wake_sender.set()
        if self._sender is not None:
            self._sender.cancel()
        if self._drains:
            await asyncio.gather(*self._drains, return_exceptions=True)
        self._terminate(SttState.CLOSED)

    async def _fail(self, message: str, error_type: str) -> None:
        log.error("scribe failed (%s): %s", error_type or "unknown", message)
        self._accepting = False
        self._closing = True
        self._terminal = True
        if self._sender is not None:
            self._sender.cancel()
        for task in list(self._drains):
            task.cancel()
        for session in list(self._sessions):
            await self._abort(session)
        self._active = None
        self._terminate(SttState.FAILED, message, error_type)

    def _terminate(self, state: SttState, message: str = "", error_type: str = "") -> None:
        self._terminal = True
        self._accepting = False
        self._buf.clear()
        self._pump()
        self._status(state, message, error_type)


async def _first(*aws: Awaitable, timeout: Optional[float] = None) -> bool:
    """Wait until the first awaitable finishes. Returns False on timeout."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return bool(done)
    finally:
        for t in tasks:
            t.cancel()


_EARLY_LANGUAGE_LIMIT = 8
_FRAME_BYTES = ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH // 100  # 10 ms


def _trailing_quiet_ms(pcm: bytes, previous_ms: int, threshold_dbfs: float) -> int:
    """Length of the quiet run at the end of the audio sent so far.

    ``previous_ms`` is the run carried over from earlier chunks; it continues
    only if this whole chunk is quiet.
    """
    limit = (32768.0 * 10 ** (threshold_dbfs / 20)) ** 2
    usable = len(pcm) - len(pcm) % ENGINE_SAMPLE_WIDTH
    samples = array.array("h", pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    per_frame = _FRAME_BYTES // ENGINE_SAMPLE_WIDTH
    frames = [samples[i:i + per_frame] for i in range(0, len(samples), per_frame)]
    run = 0
    for frame in reversed(frames):
        if not frame or math.fsum(x * x for x in frame) / len(frame) >= limit:
            return run
        run += 10
    return previous_ms + run




def _take_matching(early: list, text: str) -> Optional[tuple]:
    """Remove and return the early language event whose text equals ``text``.

    Text must match: an early event left over from a commit that already timed
    out must not lend its language to a different commit.
    """
    for n, entry in enumerate(early):
        if entry[0].strip() == text.strip():
            return early.pop(n)
    return None


def _float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
