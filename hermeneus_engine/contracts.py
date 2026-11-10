"""Shared types and interfaces for the Hermeneus engine.

Every module in the engine codes against these definitions. The engine never
imports Hermes modules; the plugin layer (root ``__init__.py`` and
``dashboard/plugin_api.py``) injects Hermes services through the callables
declared here.

Audio contract: all audio that leaves a capture backend is mono, signed
16-bit little endian PCM at ``ENGINE_SAMPLE_RATE`` Hz.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol, Sequence, runtime_checkable

ENGINE_SAMPLE_RATE = 16000
ENGINE_SAMPLE_WIDTH = 2  # bytes per sample, s16le
ENGINE_CHANNELS = 1

# --------------------------------------------------------------------------
# Hermes services injected into the engine
# --------------------------------------------------------------------------

# Returns the secret value or None when it is not configured.
SecretGetter = Callable[[str], Optional[str]]

# One chat message in OpenAI style: {"role": "system" | "user" | "assistant", "content": str}
ChatMessage = dict

# Runs one bounded LLM call for the registered auxiliary task and returns the reply text.
# Raises an exception on failure; the caller owns timeouts.
LlmCall = Callable[[Sequence[ChatMessage]], Awaitable[str]]


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureSource:
    """Something a backend can capture from: a virtual input device or an app."""

    backend: str           # backend name, for example "virtual-device" or "process-tap"
    source_id: str         # stable id the backend understands (device name, bundle id, pid)
    label: str             # human readable label for pickers and doctor output
    detail: str = ""       # extra info such as channel count or sample rate


# Called from the backend's audio thread with one chunk of engine-format PCM
# and the monotonic capture timestamp (time.monotonic_ns()) of its first sample.
FrameSink = Callable[[bytes, int], None]


class CaptureError(RuntimeError):
    """Capture could not start or stopped unexpectedly. The message is shown to the user."""


@runtime_checkable
class CaptureBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]:
        """Whether this backend can run on this machine, with a plain-language reason."""

    def list_sources(self) -> list[CaptureSource]:
        """Sources the user can pick. Never prompts, never blocks on user input."""

    def start(self, source: CaptureSource, sink: FrameSink) -> None:
        """Begin delivering engine-format PCM to ``sink``. Raises CaptureError."""

    def stop(self) -> None:
        """Stop capture and release the device. Safe to call more than once."""


# --------------------------------------------------------------------------
# Speech to text (Scribe v3 Live)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Word:
    text: str
    start: Optional[float] = None
    end: Optional[float] = None
    speaker_id: Optional[str] = None
    type: Optional[str] = None  # "word" | "spacing" | other values Scribe sends


@dataclass(frozen=True)
class PartialTranscript:
    text: str
    words: tuple[Word, ...] = ()
    turn_probability: Optional[float] = None
    received_ns: int = 0


@dataclass(frozen=True)
class CommittedTranscript:
    text: str
    segment_index: Optional[int]
    language_code: Optional[str] = None
    received_ns: int = 0


@dataclass(frozen=True)
class DiarizedTranscript:
    segment_index: int
    text: str
    words: tuple[Word, ...]
    received_ns: int = 0


@dataclass(frozen=True)
class SpeakersMerged:
    from_speaker_id: str
    to_speaker_id: str
    received_ns: int = 0


class SttState(enum.Enum):
    CONNECTING = "connecting"
    LIVE = "live"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True)
class SttStatus:
    state: SttState
    message: str = ""        # plain-language reason, shown to the user on FAILED
    error_type: str = ""     # Scribe error message_type when there is one


SttEvent = PartialTranscript | CommittedTranscript | DiarizedTranscript | SpeakersMerged | SttStatus


class CommitStrategy(enum.Enum):
    VAD = "vad"
    TURN_PREDICTION = "turn_prediction"


@dataclass(frozen=True)
class SttConfig:
    model_id: str = "scribe_v3_live"
    commit_strategy: CommitStrategy = CommitStrategy.VAD  # final default chosen by gate G1
    diarize: bool = True
    multilang_mode: bool = True
    include_language_detection: bool = True
    language_code: Optional[str] = None
    secondary_languages: tuple[str, ...] = ()
    base_url: str = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    max_session_seconds: int = 0  # 0 means use the client default for rotation


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

class LineState(enum.Enum):
    PARTIAL = "partial"          # untranslated, still changing
    COMMITTED = "committed"      # final original text, translation pending
    TRANSLATED = "translated"    # final original text plus translation
    FAILED = "failed"            # translation failed; original text stays visible


@dataclass
class SubtitleLine:
    """One subtitle line. Updates reuse the same ``line_id``."""

    line_id: str
    state: LineState
    original: str
    translation: str = ""
    speaker_id: Optional[str] = None
    source_language: Optional[str] = None
    target_language: str = ""
    segment_index: Optional[int] = None
    error: str = ""
    timings_ns: dict = field(default_factory=dict)  # capture, commit, translated, tts_first_audio

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "state": self.state.value,
            "original": self.original,
            "translation": self.translation,
            "speaker_id": self.speaker_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "segment_index": self.segment_index,
            "error": self.error,
        }


@dataclass(frozen=True)
class SpeakerRelabel:
    """Tell subtitle consumers that every earlier ``from_speaker_id`` is now ``to_speaker_id``."""

    from_speaker_id: str
    to_speaker_id: str


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------

class TranslationError(RuntimeError):
    pass


@runtime_checkable
class Translator(Protocol):
    async def translate(self, text: str, *, source_language: Optional[str], target_language: str,
                        context: Sequence[str]) -> str:
        """Translate one committed segment. ``context`` holds recent earlier segments, oldest first."""


# --------------------------------------------------------------------------
# Spoken translation
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OutputDevice:
    device_id: str   # name as sounddevice reports it
    label: str


class FeedbackRiskError(RuntimeError):
    """The chosen output device would feed the capture input."""


@runtime_checkable
class Speaker(Protocol):
    async def speak(self, text: str, *, language: str) -> None:
        """Synthesize and play ``text``. Returns when playback has been queued or finished."""

    async def close(self) -> None:
        ...
