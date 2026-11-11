"""Where the session finds its real parts.

Each part is looked up by module and attribute name when it is first used,
so the pipeline imports nothing it does not run (sounddevice, websockets) and
a build without one part reports a plain error instead of failing at import.
Tests pass their own builder to the runtime and never come here.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .contracts import CaptureError, CaptureSource, LlmCall, OutputDevice, SecretGetter, SttConfig
from .session import SessionComponents, SessionOptions

SECRET_NAME = "ELEVENLABS_API_KEY"

# name -> (module relative to this package, attribute)
REGISTRY: dict[str, tuple[str, str]] = {
    "capture.virtual-device": (".capture.virtual_device", "VirtualDeviceBackend"),
    "capture.process-tap": (".capture.process_tap", "ProcessTapBackend"),
    "capture.process-loopback": (".capture.process_loopback", "ProcessLoopbackBackend"),
    "capture.multi_output": (".capture.virtual_device", "detect_multi_output_setup"),
    # W02 seam: starts and stops a tap once while another process plays a test
    # sound, and reports whether real non-zero audio arrived. The lead sets the
    # final name at merge; until then the doctor says the check is not in this build.
    "capture.verify_tap_audio": (".capture.process_tap", "verify_system_audio_capture"),
    "stt": (".stt.scribe", "ScribeLiveClient"),
    "translator": (".translate.llm_translator", "LlmTranslator"),
    "scheduler": (".translate.scheduler", "TranslationScheduler"),
    "speaker": (".tts.elevenlabs_flash", "FlashSpeaker"),
    "speaker.config": (".tts.elevenlabs_flash", "FlashConfig"),
    "speaker.playback": (".tts.playback", "PlaybackQueue"),
    "routing.check": (".tts.routing", "check_feedback_risk"),
    "routing.ensure_safe": (".tts.routing", "ensure_route_safe"),
    "routing.safe_outputs": (".tts.routing", "safe_output_devices"),
}

# Backends in the order the doctor and the source picker try them. Each
# platform loads only its own driverless backend, so no macOS module is
# imported on Windows and the reverse.
CAPTURE_BACKENDS = (("capture.virtual-device", "capture.process-loopback") if sys.platform == "win32"
                    else ("capture.virtual-device", "capture.process-tap"))
# Backends that capture processes rather than a device; spoken translation is
# safe on any output when they leave out this process.
PROCESS_BACKENDS = ("process-tap", "process-loopback")

CaptureErrorCallback = Callable[[CaptureError], None]


class ComponentUnavailable(RuntimeError):
    """This build does not contain the named part."""


def load(name: str) -> Any:
    try:
        module_name, attr = REGISTRY[name]
    except KeyError:
        raise ComponentUnavailable(f"unknown component {name!r}") from None
    try:
        module = importlib.import_module(module_name, package=__package__)
    except ModuleNotFoundError as exc:
        raise ComponentUnavailable(f"{name} is not part of this build ({exc.name} is missing)") from None
    try:
        return getattr(module, attr)
    except AttributeError:
        raise ComponentUnavailable(f"{name}: {module.__name__} has no {attr}") from None


def available(name: str) -> bool:
    try:
        load(name)
        return True
    except ComponentUnavailable:
        return False


def _accepts_on_error(cls: Any) -> bool:
    try:
        return "on_error" in inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return False


def capture_backends(on_error: Optional[CaptureErrorCallback] = None) -> list:
    """Instances of every capture backend this build has.

    ``on_error`` goes to backends whose constructor takes it (virtual-device
    does; process-tap reports through its ``failure`` attribute, which the
    session polls).
    """
    backends = []
    for name in CAPTURE_BACKENDS:
        try:
            cls = load(name)
        except ComponentUnavailable:
            continue
        backends.append(cls(on_error=on_error) if on_error is not None and _accepts_on_error(cls) else cls())
    return backends


def capture_backend(backend_name: str, on_error: Optional[CaptureErrorCallback] = None):
    for backend in capture_backends(on_error):
        if backend.name == backend_name:
            return backend
    raise ComponentUnavailable(f"capture backend {backend_name!r} is not part of this build")


def list_sources() -> tuple[list[CaptureSource], list[str]]:
    """Every source the available backends offer, plus the reasons for backends that are not available."""
    sources: list[CaptureSource] = []
    problems: list[str] = []
    for backend in capture_backends():
        ok, reason = backend.available()
        if ok:
            sources.extend(backend.list_sources())
        else:
            problems.append(f"{backend.name}: {reason}")
    return sources, problems


def detect_multi_output_setup() -> Any:
    """``capture.virtual_device.MultiOutputReport`` for the current default output."""
    return load("capture.multi_output")()


def own_process_excluded(source: CaptureSource) -> bool:
    """Whether the capture leaves out audio this process plays.

    The process tap's ``system`` and ``app:`` sources exclude the PID of the
    process that created the backend (process_tap.py ``_spec_for``), and the
    session plays spoken translation from that same process (PlaybackQueue
    runs here), so its own speech is not captured. A ``pid:`` source excludes
    nothing; it is safe only when it names another process.
    """
    if source.backend not in PROCESS_BACKENDS:
        return True
    if source.source_id.startswith("pid:"):
        return source.source_id[4:] != str(os.getpid())
    return True


def _route_kwargs(source: CaptureSource) -> dict:
    return {"capture_backend": source.backend, "own_process_excluded": own_process_excluded(source)}


def verify_tap_audio() -> tuple[bool, str]:
    """Run W02's real-audio permission check. Returns (ok, plain-language detail)."""
    result = load("capture.verify_tap_audio")()
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return bool(getattr(result, "ok", result)), str(getattr(result, "explanation", "") or getattr(result, "detail", ""))


def safe_output_devices(source: Optional[CaptureSource]) -> list[OutputDevice]:
    fn = load("routing.safe_outputs")
    if source is None:
        return list(fn(None))
    return list(fn(source.source_id, **_route_kwargs(source)))


def route_verdict(output_device: str, source: CaptureSource) -> Any:
    return load("routing.check")(output_device, source.source_id, **_route_kwargs(source))


def check_feedback_risk(output_device: str, source: CaptureSource) -> None:
    """Raises contracts.FeedbackRiskError when playing to ``output_device`` would be captured again."""
    load("routing.ensure_safe")(route_verdict(output_device, source))


@dataclass(frozen=True)
class HermesServices:
    """What the plugin layer injects: Hermes's secret lookup and the bounded LLM call."""

    get_secret: SecretGetter
    llm: LlmCall


def build(options: SessionOptions, services: HermesServices,
          on_capture_error: Optional[CaptureErrorCallback] = None) -> SessionComponents:
    """Real components for one session. Raises FeedbackRiskError, ComponentUnavailable or ValueError.

    The factories in the result let the session start spoken translation,
    change its voice or output device, and change its source while it runs.
    """

    def speaker_factory(voice_id: str, output_device: str, source: CaptureSource) -> Any:
        # options.resolve fills voice_id (flag, setting, default); FlashConfig has the same default.
        config = load("speaker.config")(**({"voice_id": voice_id} if voice_id else {}))

        def route_check():
            return route_verdict(output_device or "", source)

        playback = load("speaker.playback")(output_device, sample_rate=config.sample_rate, route_check=route_check)
        return load("speaker")(config, lambda: services.get_secret(SECRET_NAME), playback)

    speaker: Optional[Any] = None
    if options.speak:
        # Checked before anything opens a device or a socket.
        check_feedback_risk(options.output_device or "", options.source)
        speaker = speaker_factory(options.voice_id or "", options.output_device or "", options.source)

    capture = capture_backend(options.source.backend, on_capture_error)
    stt = load("stt")(SttConfig(commit_strategy=options.commit_strategy), services.get_secret)
    translator = load("translator")(services.llm, context_segments=options.context_segments)
    scheduler_cls = load("scheduler")

    def scheduler_factory(on_line: Callable) -> Any:
        return scheduler_cls(translator, target_language=options.target_language, on_line=on_line)

    def capture_factory(source: CaptureSource) -> Any:
        return capture_backend(source.backend, on_capture_error)

    return SessionComponents(capture=capture, stt=stt, scheduler_factory=scheduler_factory, speaker=speaker,
                             speaker_factory=speaker_factory,
                             capture_factory=capture_factory, route_check=check_feedback_risk)
