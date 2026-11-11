"""User-facing failure messages.

Every message names its subject (speech to text, spoken translation, audio
capture, translation), says what happened and names the fix. The session
shows them in ``status.message``; line errors keep the scheduler's own text.
The full list, with where each one appears, is ``docs/failures.md``.

This module has no third-party imports; the stages pass in their own raw
texts and error types.
"""

from __future__ import annotations

import re
from typing import Optional

KEY_NAME = "ELEVENLABS_API_KEY"
KEYS_PAGE = "https://elevenlabs.io/app/settings/api-keys"
PLAN_PAGE = "https://elevenlabs.io/app/subscription"
MODEL_FIX = "hermes model (Configure auxiliary models > Live translation)"
DOCTOR = "hermes hermeneus doctor"

# -- speech to text (W03's SttStatus error types, stt/scribe.py) ------------------

_STT_FATAL = {
    "missing_key": (f"Speech to text: no ElevenLabs API key is set. Add {KEY_NAME} to Hermes "
                    f"(see: {DOCTOR}), then start translation again."),
    "auth_error": (f"Speech to text: ElevenLabs rejected the API key. Check {KEY_NAME} in Hermes "
                   f"(a new key is at {KEYS_PAGE}), then start translation again."),
    "http_401": (f"Speech to text: ElevenLabs rejected the API key. Check {KEY_NAME} in Hermes "
                 f"(a new key is at {KEYS_PAGE}), then start translation again."),
    "http_403": (f"Speech to text: the ElevenLabs API key has no Speech to Text permission. Allow it for "
                 f"the key at {KEYS_PAGE}, then start translation again."),
    "quota_exceeded": (f"Speech to text: the ElevenLabs quota is used up, so transcription stopped. "
                       f"Add credits or change the plan at {PLAN_PAGE}, then start translation again."),
    "unaccepted_terms": ("Speech to text: the ElevenLabs Speech to Text terms are not accepted yet. "
                         "Accept them in the ElevenLabs dashboard, then start translation again."),
}

# Retryable types (stt/scribe.py RETRYABLE_ERRORS and connection errors).
_STT_RATE = frozenset({"rate_limited", "resource_exhausted", "queue_overflow", "http_429"})
_STT_NETWORK = frozenset({"connect_failed", "connection_closed", "session_start_timeout", "open_failed"})


_ATTEMPT = re.compile(r"\s*\((attempt \d+, (?:next in \d+ s|retrying now))\)\s*$")


def stt_reconnecting(raw: str, error_type: str = "") -> str:
    """status.message while the STT client reconnects.

    The client ends its raw text with "(attempt N, next in S s)"; that part is
    kept, so the user sees how far the retries are.
    """
    match = _ATTEMPT.search(raw or "")
    progress = f" ({match.group(1)})" if match else ""
    raw = _ATTEMPT.sub("", raw or "")
    if error_type in _STT_RATE:
        return (f"Speech to text: ElevenLabs is rate limiting this account; reconnecting{progress}. "
                "Subtitles resume when it accepts audio again.")
    if error_type in _STT_NETWORK or error_type.startswith("http_5"):
        return (f"Speech to text: the connection to ElevenLabs was lost; reconnecting{progress}. "
                "Check the network connection if this does not clear.")
    if error_type == "insufficient_audio_activity":
        return "Speech to text: no sound for a while; it reconnects when audio plays again."
    if error_type == "session_time_limit_exceeded":
        return "Speech to text: opening a new ElevenLabs session (time limit reached)."
    return f"Speech to text: {raw}; reconnecting{progress}." if raw else f"Speech to text: reconnecting{progress}."


def stt_failed(raw: str, error_type: str = "") -> str:
    """status.message when the STT client gave up; the session ends with this text."""
    if error_type in _STT_FATAL:
        return _STT_FATAL[error_type]
    if error_type == "permission" or (error_type in ("auth_error", "error", "invalid_request")
                                      and "permission" in (raw or "").lower()):
        return _STT_FATAL["http_403"]
    if error_type in _STT_RATE:
        return (f"Speech to text stopped: ElevenLabs kept rate limiting this account. Wait a minute or "
                f"check the plan at {PLAN_PAGE}, then start translation again.")
    if error_type in _STT_NETWORK or error_type.startswith("http_5") or "lost connection" in (raw or "").lower():
        detail = f" ({raw})" if raw else ""
        return ("Speech to text stopped: the connection to ElevenLabs was lost and did not come back"
                f"{detail}. Check the network connection, then start translation again.")
    text = raw or "speech to text failed"
    return f"Speech to text stopped: {text.rstrip('.')}. Start translation again; if it repeats, run {DOCTOR}."


# -- spoken translation (tts/elevenlabs_flash.py texts) -------------------------------

def speak_error(raw: str) -> str:
    """status.message for a spoken segment that failed. Subtitles keep running."""
    low = (raw or "").lower()
    if "no elevenlabs api key" in low:
        return (f"Spoken translation: no ElevenLabs API key is set. Add {KEY_NAME} to Hermes "
                "(subtitles continue).")
    if "rejected the api key" in low or "http 401" in low:
        return (f"Spoken translation: ElevenLabs rejected the API key. Check {KEY_NAME} in Hermes "
                "(subtitles continue).")
    if "quota" in low or "credits" in low or "http 402" in low:
        return (f"Spoken translation: the ElevenLabs quota is used up. Add credits or change the plan at "
                f"{PLAN_PAGE}, or turn speech off (subtitles continue).")
    if "http 429" in low or "rate limit" in low or "too many" in low or "concurrent" in low:
        return ("Spoken translation: ElevenLabs is rate limiting this account; some sentences are not spoken. "
                "Speech resumes when it allows more (subtitles continue).")
    if "voice" in low and ("not available" in low or "does not exist" in low):
        return f"Spoken translation: {raw.rstrip('.')}."
    if "could not connect" in low or "connection dropped" in low or "reconnect failed" in low:
        return ("Spoken translation: the connection to ElevenLabs was lost, so some sentences are not spoken. "
                "Check the network connection (subtitles continue).")
    text = (raw or "a sentence could not be spoken").rstrip(".")
    return f"Spoken translation: {text} (subtitles continue)."


def output_device_gone(device: str) -> str:
    return (f"Spoken translation stopped: the output device '{device}' is no longer available. "
            "Pick another output device and turn speech on again (subtitles continue).")


# -- translation model ------------------------------------------------------------

def model_slow(count: int, window_s: float, max_lag_s: Optional[float] = None) -> str:
    lag = f" more than {max_lag_s:g} s" if max_lag_s else ""
    return (f"Translation: the model is slower than the speech; {count} lines in the last {window_s:g} s "
            f"came{lag} late and were skipped or timed out. Pick a faster model with: {MODEL_FIX}.")


def is_slow_failure(error: str) -> bool:
    """A line error that means the model was too slow, not that it failed."""
    return error.startswith("Skipped to keep up") or error.startswith("Translation timed out") \
        or error.startswith("Translation skipped: no time left")


# -- audio capture ----------------------------------------------------------------

def capture_stopped(raw: str, source_label: str = "") -> str:
    """status.message when capture ends by itself (app quit, device removed, no permission)."""
    text = (raw or "").strip() or (f"Capture from {source_label} stopped." if source_label else "Capture stopped.")
    if "again" in text.lower():
        return text  # the backend's own text already names the fix (W02 watchdog, W01 device loss)
    if "stopped unexpectedly" in text:
        text = text.rstrip(".") + ", for example because the app quit."
    elif text[-1] not in ".!?":
        text += "."
    return (f"{text} Start translation again, or pick another source "
            "(see: hermes hermeneus devices).")


# -- refusals for live changes ------------------------------------------------------

NOT_RUNNING = "No translation session is running. Start one with /translate start or hermes hermeneus run."


def not_speakable(language_name: str) -> str:
    return (f"Spoken translation does not support {language_name}. Keep speech off for this language "
            "(--speak off), or pick a language that spoken translation supports.")


NEED_DEVICE = ("Spoken translation needs an output device. Pass --device NAME "
               "(see: hermes hermeneus devices).")
