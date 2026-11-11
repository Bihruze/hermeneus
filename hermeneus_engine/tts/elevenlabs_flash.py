"""Spoken translation with ElevenLabs Flash v2.5 over one WebSocket per session.

Transport: the multi-context TTS WebSocket
``wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/multi-stream-input``
(https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-multi-stream-input).
It is the stream-input protocol with a ``context_id`` on every message. Each
subtitle segment gets its own context, so the server marks the end of each
segment with ``{"isFinal": true, "contextId": ...}``. The single-context
``stream-input`` endpoint sends ``isFinal`` only when the whole stream closes,
so with one connection per session it cannot tell where one flushed segment
ends and the next begins.

Per segment the speaker sends, in order:

1. ``{"context_id": c, "text": " ", "generation_config": {...}}`` initialise the context
2. ``{"context_id": c, "text": "<segment> ", "flush": true}`` text, forced generation
3. ``{"context_id": c, "close_context": true}`` close; a flushing context finishes first

and ``{"close_socket": true}`` when the session ends.

The API key is read from a provider callable on every connect, sent only in
the ``xi-api-key`` header, never logged, and replaced by ``[redacted]`` in any
error text.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from .playback import PcmSink, Utterance
from .voices import DEFAULT_VOICE_ID, DEFAULT_VOICE_NAME, unavailable_message

log = logging.getLogger("hermeneus.tts.flash")

# The websockets library logs request headers (and so the API key) at DEBUG.
# Its connection logger is pinned to WARNING so a DEBUG root logger cannot
# print the key.
_ws_log = logging.getLogger("hermeneus.tts.flash.ws")
_ws_log.setLevel(logging.WARNING)

FLASH_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_BASE_URL = "wss://api.elevenlabs.io"

# pcm_24000: S16LE mono at 24 kHz. Output devices on this Mac run at 48 kHz,
# an exact 2:1 ratio for CoreAudio's converter; 22.05 kHz is not an integer
# ratio and 16 kHz loses speech bandwidth above 8 kHz. 24 kHz PCM is on the
# Free, Starter and Creator tiers (44.1 kHz needs Pro).
OUTPUT_FORMAT = "pcm_24000"
OUTPUT_SAMPLE_RATE = 24000

# Documented range 50 to 500, default [120, 160, 250, 290]. 50 is the lowest
# allowed first step; the rest follow the low-latency example in the
# realtime TTS guide. Every segment is flushed, so the schedule only matters
# inside segments longer than 50 characters.
CHUNK_LENGTH_SCHEDULE = (50, 120, 160, 290)

# Default 20 s, maximum 180 s. Subtitle gaps longer than 20 s are common,
# and every reconnect costs a TLS and WebSocket handshake on the next segment.
INACTIVITY_TIMEOUT_S = 180

# The server allows 5 concurrent contexts per connection. One slot stays free
# so a context that is closing never blocks a new segment.
MAX_CONTEXTS = 4

# eleven_flash_v2_5 languages, from the models page model table
# (https://elevenlabs.io/docs/overview/models): all eleven_multilingual_v2
# languages plus hu, no, vi; 32 in total, same list as eleven_turbo_v2_5.
FLASH_V2_5_LANGUAGES = frozenset({
    "en", "ja", "zh", "de", "hi", "fr", "ko", "pt", "it", "es", "id", "nl", "tr", "fil", "pl", "sv",
    "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms", "sk", "da", "ta", "uk", "ru", "hu", "no", "vi",
})
_LANGUAGE_ALIASES = {"nb": "no", "nn": "no", "tl": "fil", "in": "id"}

# Server error for a voice id the account cannot use. Observed live on
# 2026-11-11 for a made-up id: after the handshake the server sent
# {"message": "A voice with voice_id <id> does not exist.",
#  "error": "voice_id_does_not_exist", "code": 1008} and closed with 1008.
VOICE_UNAVAILABLE_ERRORS = frozenset({"voice_id_does_not_exist"})
# Message the help center quotes for library voices a free plan cannot use
# (https://elevenlabs.io/docs/help-center/product/voices/voice-library/why-cant-i-use-some-voices-from-the-voice-library).
_VOICE_UNAVAILABLE_PHRASE = "voice is not available"

KeyProvider = Callable[[], Optional[str]]


class TtsError(RuntimeError):
    """Spoken output failed. The message is plain language and never contains the key."""


def normalize_language(code: str) -> str:
    """Map a language tag such as ``en-US`` or ``nb`` to a Flash v2.5 code. Raises TtsError."""
    base = (code or "").strip().replace("_", "-").split("-")[0].lower()
    base = _LANGUAGE_ALIASES.get(base, base)
    if base not in FLASH_V2_5_LANGUAGES:
        raise TtsError(f"Spoken translation does not support language '{code}' "
                       f"(eleven_flash_v2_5 supports {len(FLASH_V2_5_LANGUAGES)} languages).")
    return base


@dataclass(frozen=True)
class FlashConfig:
    voice_id: str = DEFAULT_VOICE_ID               # resolved by voices.resolve_voice
    base_url: str = DEFAULT_BASE_URL
    output_format: str = OUTPUT_FORMAT
    sample_rate: int = OUTPUT_SAMPLE_RATE
    chunk_length_schedule: tuple[int, ...] = CHUNK_LENGTH_SCHEDULE
    inactivity_timeout_s: int = INACTIVITY_TIMEOUT_S
    voice_settings: Optional[dict] = None
    open_timeout_s: float = 10.0
    final_timeout_s: float = 15.0      # wait for isFinal after the close_context message
    max_reconnects: int = 3            # per drop, before the affected segments fail
    reconnect_backoff_s: tuple[float, ...] = (0.0, 0.25, 1.0)

    def __post_init__(self) -> None:
        if not self.voice_id or "/" in self.voice_id:
            raise ValueError("FlashConfig.voice_id must be an ElevenLabs voice id")
        if not all(50 <= v <= 500 for v in self.chunk_length_schedule):
            raise ValueError("chunk_length_schedule values must be in 50..500")
        if not 1 <= self.inactivity_timeout_s <= 180:
            raise ValueError("inactivity_timeout_s must be in 1..180")
        if not self.output_format.startswith("pcm_"):
            raise ValueError("FlashSpeaker plays raw PCM; output_format must be pcm_*")
        if int(self.output_format.split("_")[1]) != self.sample_rate:
            raise ValueError("sample_rate must match output_format")


@dataclass
class UtteranceMetrics:
    utterance_id: str
    chars: int
    language: str
    speak_ns: int                       # time.monotonic_ns() when speak() was called
    first_audio_ns: Optional[int] = None
    final_ns: Optional[int] = None
    connect_ns: int = 0                 # handshake time paid by this segment, 0 if the socket was open
    outcome: str = "pending"            # done, skipped-backlog, truncated, failed, cancelled
    error: str = ""

    @property
    def first_audio_latency_ns(self) -> Optional[int]:
        return None if self.first_audio_ns is None else self.first_audio_ns - self.speak_ns


@dataclass
class _Context:
    context_id: str
    text: str
    utt: Utterance
    metrics: UtteranceMetrics
    carry: bytes = b""
    attempts: int = 0
    sent: bool = False
    timer: Optional[asyncio.TimerHandle] = None


Connect = Callable[..., Awaitable[Any]]


def _default_connect(uri: str, **kwargs: Any) -> Awaitable[Any]:
    from websockets.asyncio.client import connect  # noqa: PLC0415
    return connect(uri, **kwargs)


class FlashSpeaker:
    """``Speaker`` implementation. One instance per translation session.

    The speaker owns ``playback``: ``start()`` opens it and ``close()`` stops it.
    """

    def __init__(self, config: FlashConfig, key_provider: KeyProvider, playback: PcmSink, *,
                 connect: Connect = _default_connect,
                 on_metrics: Optional[Callable[[UtteranceMetrics], None]] = None,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        if playback.sample_rate != config.sample_rate:
            raise ValueError("playback sample rate must match the TTS output format")
        self.config = config
        self._key_provider = key_provider
        self._playback = playback
        self._connect = connect
        self._on_metrics = on_metrics
        self._clock = clock
        self._ws: Any = None
        self._ws_language: Optional[str] = None
        self._reader: Optional[asyncio.Task] = None
        self._conn_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(MAX_CONTEXTS)
        self._contexts: dict[str, _Context] = {}
        self._ids = itertools.count(1)
        self._closing = False
        self._closed = False
        self._last_key: str = ""
        self.voice_id = config.voice_id
        self.status_message = ""     # read by the session and shown with its status
        self._voice_failed = ""
        self.last_error = ""         # latest server error for a segment; the session shows it
        self.connects = 0
        self.metrics: list[UtteranceMetrics] = []

    # ------------------------------------------------------------- public API

    async def start(self, language: Optional[str] = None) -> None:
        """Open playback (runs the feedback check) and, given a language, pre-open the socket.

        The feedback check reads the audio inventory (about 0.3 s measured with
        system_profiler), so playback opens on a worker thread and the event
        loop keeps moving audio and subtitles meanwhile.
        """
        await asyncio.to_thread(self._playback.start)
        if language:
            async with self._conn_lock:
                await self._ensure_connected(normalize_language(language))

    async def speak(self, text: str, *, language: str, utterance_id: Optional[str] = None) -> None:
        """Queue ``text`` for synthesis and playback. Returns once the text is sent.

        Playback order follows call order. Raises TtsError when the segment
        cannot be sent; other segments are unaffected.
        """
        if self._closed:
            raise TtsError("Spoken output is closed.")
        if self._voice_failed:
            raise TtsError(self._voice_failed)
        text = " ".join(text.split())
        if not text:
            return
        speak_ns = self._clock()
        lang = normalize_language(language)
        uid = utterance_id or f"u{next(self._ids)}"
        m = UtteranceMetrics(utterance_id=uid, chars=len(text), language=lang, speak_ns=speak_ns)

        if self._playback.backlog_seconds() > self._playback.max_start_lag_s:
            m.outcome = "skipped-backlog"
            self._emit(m)
            return
        if not self._playback.running:
            self._playback.start()

        utt = self._playback.open_utterance(uid, speak_ns)
        ctx = _Context(context_id=uid, text=text, utt=utt, metrics=m)
        try:
            await self._slots.acquire()
        except BaseException:
            self._playback.cancel(utt)
            raise
        self._contexts[uid] = ctx
        try:
            async with self._conn_lock:
                connect_ns = await self._ensure_connected(lang)
                m.connect_ns = connect_ns
                await self._send_context(ctx)
        except Exception as exc:
            self._fail(ctx, exc)
            raise TtsError(self._redact(str(exc))) from None

    def health(self) -> str:
        """Empty while spoken output can play; otherwise why it cannot (the output device is gone)."""
        check = getattr(self._playback, "health", None)
        return check() if callable(check) else ""

    async def close(self) -> None:
        """Close the socket and stop playback. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._closing = True
        ws = self._ws
        if ws is not None:
            # close_socket lets flushing contexts finish first, so it is sent
            # only when nothing is in flight; otherwise the socket closes now.
            if not self._contexts:
                try:
                    await ws.send(json.dumps({"close_socket": True}))
                except Exception:
                    pass
            try:
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except Exception:
                pass
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        for ctx in list(self._contexts.values()):
            self._end(ctx, "cancelled")
        self._ws = None
        self._playback.stop()

    # ------------------------------------------------------------- connection

    def _uri(self, language: str) -> str:
        q = urllib.parse.urlencode({
            "model_id": FLASH_MODEL_ID,
            "output_format": self.config.output_format,
            "inactivity_timeout": self.config.inactivity_timeout_s,
            "language_code": language,
        })
        voice = urllib.parse.quote(self.voice_id, safe="")
        return f"{self.config.base_url.rstrip('/')}/v1/text-to-speech/{voice}/multi-stream-input?{q}"

    def _redact(self, text: str) -> str:
        if self._last_key and self._last_key in text:
            text = text.replace(self._last_key, "[redacted]")
        return text

    def _ws_open(self) -> bool:
        ws = self._ws
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        return getattr(state, "name", "OPEN") == "OPEN"

    async def _ensure_connected(self, language: str) -> int:
        """Return handshake nanoseconds spent (0 if already open). Caller holds _conn_lock."""
        if self._ws_open() and self._ws_language == language:
            return 0
        if self._ws is not None:
            # language_code is a connection parameter. Segments still being
            # generated on the old socket finish before it closes.
            await self._wait_sent_drained(self.config.final_timeout_s)
            await self._drop_socket()
        key = self._key_provider()
        if not key:
            raise TtsError("No ElevenLabs API key is configured for spoken translation.")
        self._last_key = key
        t0 = self._clock()
        try:
            ws = await asyncio.wait_for(
                self._connect(self._uri(language), additional_headers={"xi-api-key": key},
                              open_timeout=self.config.open_timeout_s, max_size=2 ** 24,
                              logger=_ws_log),
                timeout=self.config.open_timeout_s + 1.0)
        except Exception as exc:
            raise TtsError(self._describe_connect_error(exc)) from None
        self.connects += 1
        self._ws = ws
        self._ws_language = language
        self._reader = asyncio.get_running_loop().create_task(self._read(ws))
        return self._clock() - t0

    def _describe_connect_error(self, exc: BaseException) -> str:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return f"ElevenLabs rejected the API key for spoken translation (HTTP {status})."
        if status == 429:
            return ("ElevenLabs is rate limiting spoken translation or the quota is used up (HTTP 429).")
        if status == 402:
            return "ElevenLabs reports no credits left for spoken translation (HTTP 402)."
        if status is not None:
            return f"ElevenLabs refused the spoken translation connection (HTTP {status})."
        return self._redact(f"Could not connect to ElevenLabs for spoken translation: "
                            f"{type(exc).__name__}: {exc}")

    async def _wait_sent_drained(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while any(c.sent for c in self._contexts.values()) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        for ctx in [c for c in self._contexts.values() if c.sent]:
            self._end(ctx, "truncated" if ctx.metrics.first_audio_ns else "failed",
                      error="language switch")

    async def _drop_socket(self) -> None:
        ws, self._ws = self._ws, None
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
        if ws is not None:
            try:
                await ws.send(json.dumps({"close_socket": True}))
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except Exception:
                pass

    async def _send_context(self, ctx: _Context) -> None:
        init: dict[str, Any] = {"context_id": ctx.context_id, "text": " ",
                                "generation_config": {"chunk_length_schedule": list(self.config.chunk_length_schedule)}}
        if self.config.voice_settings:
            init["voice_settings"] = self.config.voice_settings
        ws = self._ws
        ctx.attempts += 1
        await ws.send(json.dumps(init))
        await ws.send(json.dumps({"context_id": ctx.context_id, "text": ctx.text + " ", "flush": True}))
        await ws.send(json.dumps({"context_id": ctx.context_id, "close_context": True}))
        ctx.sent = True
        self._arm_timer(ctx)

    def _arm_timer(self, ctx: _Context) -> None:
        if ctx.timer is not None:
            ctx.timer.cancel()
        loop = asyncio.get_running_loop()
        ctx.timer = loop.call_later(self.config.final_timeout_s, self._on_final_timeout, ctx.context_id)

    def _on_final_timeout(self, context_id: str) -> None:
        ctx = self._contexts.get(context_id)
        if ctx is None:
            return
        log.warning("spoken segment %s: no isFinal within %.1f s; ending it", context_id,
                    self.config.final_timeout_s)
        self._end(ctx, "truncated" if ctx.metrics.first_audio_ns else "failed",
                  error="no end-of-segment message from ElevenLabs")

    # ----------------------------------------------------------------- reader

    async def _read(self, ws: Any) -> None:
        dropped_by_error = False
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("spoken output: non JSON message ignored")
                    continue
                self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            dropped_by_error = True
            log.warning("spoken output connection dropped: %s", self._redact(f"{type(exc).__name__}: {exc}"))
        if ws is not self._ws or self._closing:
            return
        # The socket ended on its own: inactivity timeout, server close or network drop.
        self._ws = None
        self._reader = None
        in_flight = [c for c in self._contexts.values() if c.sent]
        if not in_flight:
            return  # idle close; the next speak() reconnects
        code = getattr(ws, "close_code", None)
        log.info("spoken output socket closed (code %s, error %s) with %d segments in flight",
                 code, dropped_by_error, len(in_flight))
        asyncio.get_running_loop().create_task(self._recover(in_flight))

    def _handle(self, msg: dict) -> None:
        cid = msg.get("contextId") or msg.get("context_id")
        ctx = self._contexts.get(cid) if cid else None
        audio = msg.get("audio")
        if audio:
            if ctx is None:
                return  # audio for a context that already ended, for example after a timeout
            try:
                pcm = base64.b64decode(audio, validate=True)
            except (binascii.Error, ValueError):
                log.warning("spoken segment %s: undecodable audio chunk ignored", cid)
                return
            if ctx.metrics.first_audio_ns is None:
                ctx.metrics.first_audio_ns = self._clock()
            pcm = ctx.carry + pcm
            whole = len(pcm) - (len(pcm) % 2)
            ctx.carry = pcm[whole:]
            self._playback.feed(ctx.utt, pcm[:whole])
            self._arm_timer(ctx)
        if msg.get("isFinal"):
            if ctx is not None:
                self._end(ctx, "done")
            return
        if not audio and ("error" in msg or "message" in msg):
            text = self._redact(str(msg.get("message") or msg.get("error")))
            if self._is_voice_unavailable(msg):
                self._on_voice_unavailable(text)
                return
            log.warning("spoken output server message: %s", text)
            self.last_error = text
            if ctx is not None:
                self._end(ctx, "failed", error=text)

    @staticmethod
    def _is_voice_unavailable(msg: dict) -> bool:
        if str(msg.get("error") or "") in VOICE_UNAVAILABLE_ERRORS:
            return True
        return _VOICE_UNAVAILABLE_PHRASE in str(msg.get("message") or "").lower()

    def _on_voice_unavailable(self, server_text: str) -> None:
        """Switch this session to the default voice; segments in flight are resent on reconnect."""
        bad = self.voice_id
        if bad != DEFAULT_VOICE_ID:
            self.voice_id = DEFAULT_VOICE_ID
            self.status_message = unavailable_message(bad)
            log.warning("%s (server: %s)", self.status_message, server_text)
        else:
            self._voice_failed = (f"The default voice {DEFAULT_VOICE_NAME} ({DEFAULT_VOICE_ID}) is not available "
                                  "on this ElevenLabs account. Set voice_id to a voice from your account.")
            self.status_message = self._voice_failed
            log.warning("%s (server: %s)", self._voice_failed, server_text)
            for ctx in list(self._contexts.values()):
                if ctx.sent:
                    self._end(ctx, "failed", error=self._voice_failed)
        ws = self._ws
        if ws is not None:
            # The server closes the socket after this error; closing from our
            # side as well makes the reader run recovery with the new voice.
            asyncio.get_running_loop().create_task(ws.close())

    # --------------------------------------------------------------- recovery

    async def _recover(self, in_flight: Sequence[_Context]) -> None:
        resend = []
        for ctx in in_flight:
            if ctx.metrics.first_audio_ns is None and ctx.attempts <= self.config.max_reconnects:
                resend.append(ctx)
            else:
                # Some audio already played or is queued; repeating the segment
                # would speak its start twice.
                self._end(ctx, "truncated" if ctx.metrics.first_audio_ns else "failed",
                          error="connection dropped")
        if not resend:
            return
        lang = resend[0].metrics.language
        last_error = ""
        for delay in self.config.reconnect_backoff_s[: self.config.max_reconnects]:
            if self._closing:
                return
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self._conn_lock:
                    await self._ensure_connected(lang)
                    for ctx in resend:
                        if ctx.context_id in self._contexts and not ctx.utt.closed:
                            await self._send_context(ctx)
                return
            except Exception as exc:
                last_error = self._redact(str(exc))
                log.warning("spoken output reconnect failed: %s", last_error)
        for ctx in resend:
            self._end(ctx, "failed", error=last_error or "reconnect failed")

    # ------------------------------------------------------------ bookkeeping

    def _end(self, ctx: _Context, outcome: str, error: str = "") -> None:
        if self._contexts.pop(ctx.context_id, None) is None:
            return
        if ctx.timer is not None:
            ctx.timer.cancel()
        if outcome == "cancelled":
            self._playback.cancel(ctx.utt)
        self._playback.finish(ctx.utt)
        ctx.metrics.final_ns = self._clock()
        ctx.metrics.outcome = outcome
        ctx.metrics.error = error
        self._slots.release()
        self._emit(ctx.metrics)

    def _fail(self, ctx: _Context, exc: BaseException) -> None:
        if ctx.context_id in self._contexts:
            self._playback.cancel(ctx.utt)
            self._end(ctx, "failed", error=self._redact(str(exc)))

    def _emit(self, m: UtteranceMetrics) -> None:
        self.metrics.append(m)
        if len(self.metrics) > 512:
            del self.metrics[:256]
        if self._on_metrics is not None:
            try:
                self._on_metrics(m)
            except Exception:
                log.exception("on_metrics callback failed")
