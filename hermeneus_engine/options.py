"""Turn command flags and plugin settings into SessionOptions.

Flags win over settings. Nothing here prompts: a missing choice raises
UsageError with the flag that supplies it (catalog rule 12).
"""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from . import languages
from .contracts import CaptureSource, CommitStrategy
from .session import SessionOptions
from .tts.voices import resolve_voice

DEFAULT_CONTEXT_SEGMENTS = 3
CAPTURE_BACKEND_CHOICES = ("auto", "process-tap", "virtual-device")
# With capture_backend auto, the first backend in this order that offers a source wins.
AUTO_ORDER = ("process-tap", "virtual-device")
PROCESS_TAP_SYSTEM_SOURCE = "system"
MAX_CONTEXT_SEGMENTS = 10


class UsageError(ValueError):
    """A choice is missing or invalid. The message names the flag to pass."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would print and exit
        raise UsageError(message)


def add_session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", dest="to", metavar="LANG",
                        help="language to translate into, for example tr or de")
    parser.add_argument("--source", metavar="NAME",
                        help="audio source to capture (see: hermes hermeneus devices)")
    parser.add_argument("--speak", action="store_true",
                        help="speak the translation as well; subtitles are always shown")
    parser.add_argument("--device", metavar="OUTPUT",
                        help="output device for spoken translation")
    parser.add_argument("--voice", metavar="VOICE_ID", help="ElevenLabs voice id for spoken translation")


def parse_slash_start(raw_args: Sequence[str]) -> argparse.Namespace:
    parser = _Parser(prog="/translate start", add_help=False)
    add_session_arguments(parser)
    return parser.parse_args(list(raw_args))


def split_args(raw: str) -> list[str]:
    try:
        return shlex.split(raw or "")
    except ValueError as exc:
        raise UsageError(f"could not read the arguments: {exc}") from None


@dataclass(frozen=True)
class Choices:
    to: Optional[str] = None
    source: Any = None          # a name, or {"backend", "source_id"} from the pane
    speak: Optional[bool] = None
    device: Optional[str] = None
    voice: Optional[str] = None

    @classmethod
    def from_namespace(cls, ns: Any) -> "Choices":
        return cls(to=getattr(ns, "to", None), source=getattr(ns, "source", None),
                   speak=True if getattr(ns, "speak", False) else None,
                   device=getattr(ns, "device", None), voice=getattr(ns, "voice", None))

    @classmethod
    def from_mapping(cls, body: Mapping[str, Any]) -> "Choices":
        """From the POST /start body: ``source`` ({backend, source_id} or a name), ``target_language``,
        ``speak``, ``output_device``, ``voice_id``."""
        speak = body.get("speak")
        return cls(to=body.get("target_language") or body.get("to"), source=body.get("source"),
                   speak=None if speak is None else bool(speak),
                   device=body.get("output_device") or body.get("device"),
                   voice=body.get("voice_id") or body.get("voice"))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def match_source(wanted_source: Any, sources: Sequence[CaptureSource]) -> CaptureSource:
    if isinstance(wanted_source, Mapping):
        backend = _text(wanted_source.get("backend"))
        source_id = _text(wanted_source.get("source_id"))
        for s in sources:
            if s.source_id == source_id and (not backend or s.backend == backend):
                return s
        raise UsageError(f"the audio source {source_id!r} ({backend or 'any backend'}) is not available. "
                         + describe_sources(sources))
    name = str(wanted_source)
    wanted = name.strip().casefold()
    exact = [s for s in sources if wanted in (s.source_id.casefold(), s.label.casefold())]
    if len(exact) == 1:
        return exact[0]
    partial = [s for s in sources if wanted in s.label.casefold() or wanted in s.source_id.casefold()]
    if len(exact) > 1 or len(partial) > 1:
        names = ", ".join(s.label for s in (exact or partial))
        raise UsageError(f"--source {name!r} matches more than one source ({names}); pass the full name")
    if partial:
        return partial[0]
    raise UsageError(f"no audio source called {name!r}. " + describe_sources(sources))


def pick_source(wanted: Any, settings: Mapping[str, Any], sources: Sequence[CaptureSource]) -> CaptureSource:
    """The source to capture. An explicit choice wins; otherwise the capture_backend setting decides.

    With ``auto`` (the default) the process tap is preferred when this Mac
    offers it, and its whole-system source needs no choice. A virtual device
    is used without a choice only when exactly one is present.
    """
    backend = _text(settings.get("capture_backend")) or "auto"
    if backend not in CAPTURE_BACKEND_CHOICES:
        raise UsageError(f"capture_backend setting must be one of {', '.join(CAPTURE_BACKEND_CHOICES)}, "
                         f"not {backend!r}")
    if isinstance(wanted, Mapping):
        return match_source(wanted, sources)
    allowed = list(sources) if backend == "auto" else [s for s in sources if s.backend == backend]
    name = _text(wanted) or _text(settings.get("source"))
    if name:
        return match_source(name, allowed)
    order = AUTO_ORDER if backend == "auto" else (backend,)
    for candidate in order:
        offered = [s for s in allowed if s.backend == candidate]
        if not offered:
            continue
        if candidate == "process-tap":
            system = [s for s in offered if s.source_id == PROCESS_TAP_SYSTEM_SOURCE]
            if system:
                return system[0]
        if len(offered) == 1:
            return offered[0]
        break
    raise UsageError("no audio source chosen. Pass --source <name> or set source in the plugin settings. "
                     + describe_sources(allowed))


def describe_sources(sources: Sequence[CaptureSource]) -> str:
    if not sources:
        return ("No audio source is available. Run hermes hermeneus doctor to see what is missing.")
    return "Available sources: " + "; ".join(f"{s.label} ({s.backend})" for s in sources) + "."


def resolve(choices: Choices, settings: Mapping[str, Any], sources: Sequence[CaptureSource]) -> SessionOptions:
    to = _text(choices.to) or _text(settings.get("target_language"))
    if not to:
        raise UsageError("no target language. Pass --to <code> (for example --to tr) "
                         "or set target_language in the plugin settings.")
    try:
        lang = languages.get(to)
    except languages.UnknownLanguageError as exc:
        raise UsageError(f"--to: {exc}") from None
    target = lang.iso639_1 or lang.iso639_3

    speak = choices.speak if choices.speak is not None else bool(settings.get("speak", False))

    source = pick_source(choices.source, settings, sources)

    device = _text(choices.device) or _text(settings.get("output_device")) or None
    voice = resolve_voice(_text(choices.voice), _text(settings.get("voice_id")))
    if speak:
        if not lang.tts_flash_v2_5_supported:
            raise UsageError(f"spoken translation does not support {lang.english_name}; "
                             "start without --speak or pick another --to language")
        if not device:
            raise UsageError("spoken translation needs an output device. Pass --device <name> "
                             "(see: hermes hermeneus devices) or set output_device in the plugin settings.")

    strategy_raw = _text(settings.get("commit_strategy")) or CommitStrategy.VAD.value
    try:
        strategy = CommitStrategy(strategy_raw)
    except ValueError:
        allowed = ", ".join(c.value for c in CommitStrategy)
        raise UsageError(f"commit_strategy setting must be one of {allowed}, not {strategy_raw!r}") from None

    raw_ctx = settings.get("context_segments", DEFAULT_CONTEXT_SEGMENTS)
    try:
        context_segments = int(raw_ctx)
    except (TypeError, ValueError):
        raise UsageError(f"context_segments setting must be a whole number, not {raw_ctx!r}") from None
    if not 0 <= context_segments <= MAX_CONTEXT_SEGMENTS:
        raise UsageError(f"context_segments setting must be between 0 and {MAX_CONTEXT_SEGMENTS}")

    return SessionOptions(source=source, target_language=target, speak=speak,
                          output_device=device if speak else None, commit_strategy=strategy,
                          voice_id=voice if speak else None, context_segments=context_segments)
