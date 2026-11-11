"""First-run setup for the Desktop pane: what is still missing, the audio check and the defaults.

Nothing here blocks (catalog rule 12). ``state()`` reads config and the secret
scope and answers at once. The audio check runs in a worker thread under its
own deadline; ``start()`` returns at once with ``running`` and the pane polls
``state()`` until the check ends. Every failure comes back as a state with a
plain message and can be run again.

The ElevenLabs key is never read here beyond "is it set": the value stays in
Hermes's secret scope (catalog rule 11) and is entered through Desktop's own
key form, not through this plugin.
"""

from __future__ import annotations

import asyncio
import json
import locale
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .hermeneus_engine import languages

log = logging.getLogger("hermeneus.onboarding")

SECRET_NAME = "ELEVENLABS_API_KEY"
# Where the key is created, and the access a restricted key needs. Hermeneus calls
# Speech to Text (Scribe v3 Live, wss://api.elevenlabs.io/v1/speech-to-text/realtime),
# Text to Speech (Flash v2.5, only with spoken translation) and GET /v2/voices
# (the voice picker, only with spoken translation).
KEY_URL = "https://elevenlabs.io/app/settings/api-keys"
KEY_PERMISSIONS = (
    {"id": "speech_to_text", "label": "Speech to Text", "access": "Access", "required": True},
    {"id": "text_to_speech", "label": "Text to Speech", "access": "Access", "required": False},
    {"id": "voices", "label": "Voices", "access": "Read", "required": False},
)
# Screen & System Audio Recording in System Settings (macOS 14.2 and later).
PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
PRIVACY_PANE = "System Settings > Privacy & Security > Screen & System Audio Recording"

# The check itself waits up to CHECK_TIMEOUT_S for capture to start (the macOS prompt is
# answered inside that time). The deadline adds room for the player and the analysis.
CHECK_TIMEOUT_S = 30.0
CHECK_DEADLINE_S = CHECK_TIMEOUT_S + 15.0
STATE_FILE = "setup.json"

# Audio check states the pane shows.
AUDIO_UNKNOWN = "unknown"      # never run on this Hermes profile
AUDIO_RUNNING = "running"
AUDIO_OK = "ok"
AUDIO_DENIED = "denied"        # macOS gave silence: the permission is off
AUDIO_FAILED = "failed"        # anything else; the message says what
AUDIO_NOT_NEEDED = "not_needed"  # no process tap here (virtual device or Windows)

FALLBACK_LANGUAGE = "en"

# Hermes installs plugin dependencies when the plugin is turned on; a process that started before
# that cannot import them until it restarts (hermes_cli/plugins_activation.py:75-79).
RESTART_MESSAGE = ("Hermeneus's audio libraries ({module}) are not loaded in this Hermes yet. Quit and "
                   "reopen Hermes, then press Check again.")


def missing_module_message(exc: BaseException) -> Optional[str]:
    """The restart message for an import failure, else None."""
    if isinstance(exc, ImportError):
        return RESTART_MESSAGE.format(module=getattr(exc, "name", None) or str(exc))
    return None


def _now() -> float:
    return time.time()


# -- language ---------------------------------------------------------------------------

def language_code(tag: Any) -> Optional[str]:
    """The subtitle target code for a locale tag (``tr-TR``, ``pt_BR.UTF-8``, ``zh-Hans``), else None."""
    if not isinstance(tag, str) or not tag.strip():
        return None
    head = tag.strip().split(".", 1)[0].split("@", 1)[0]
    if head.upper() in ("C", "POSIX"):
        return None
    try:
        lang = languages.get(head)
    except languages.UnknownLanguageError:
        return None
    return lang.iso639_1 or lang.iso639_3


def system_locale() -> str:
    """The locale of the process that serves the pane, from the environment or Python's locale."""
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key, "")
        if value and value.split(".", 1)[0].upper() not in ("C", "POSIX"):
            return value
    try:
        return locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        return ""


def default_language(*, saved: Any, hermes_language: Any, client_locales: Any, system: Any) -> dict:
    """The subtitle language to preselect, and where it came from.

    Order: the plugin's own ``target_language`` setting, the Hermes display
    language, the locale the Desktop app reports (the system language), the
    locale of this process, then English.

    ``display.language`` defaults to ``en`` (hermes_cli/config_defaults.py:884-886)
    and the merged config does not say whether the user chose it, so ``en``
    counts as a choice only when no system language is known.
    """
    code = language_code(saved)
    if code:
        return {"code": code, "from": "setting"}
    hermes = language_code(hermes_language)
    if hermes and hermes != FALLBACK_LANGUAGE:
        return {"code": hermes, "from": "hermes"}
    for tag in client_locales if isinstance(client_locales, (list, tuple)) else [client_locales]:
        code = language_code(tag)
        if code:
            return {"code": code, "from": "system"}
    code = language_code(system)
    if code:
        return {"code": code, "from": "system"}
    if hermes:
        return {"code": hermes, "from": "hermes"}
    return {"code": FALLBACK_LANGUAGE, "from": "fallback"}


# -- translation model ------------------------------------------------------------------

def model_route(aux: Optional[dict], config: Optional[dict]) -> dict:
    """Which model translates: the live_translate pin, else the main Hermes model.

    Hermes resolves an auxiliary task with no ``auxiliary.<task>`` entry to the
    main model (``provider: auto``); install-routes.md cites the resolver. The
    suggestion is optional: translation works without it.
    """
    aux = aux if isinstance(aux, dict) else {}
    aux_model = str(aux.get("model") or "").strip()
    aux_provider = str(aux.get("provider") or "").strip()
    if aux_model:
        return {"route": "auxiliary", "provider": aux_provider or "auto", "model": aux_model, "suggest_faster": False}
    main = (config or {}).get("model")
    if isinstance(main, dict):
        model = str(main.get("default") or main.get("model") or "").strip()
        provider = str(main.get("provider") or "").strip()
    else:
        model, provider = str(main or "").strip(), ""
    return {"route": "main", "provider": provider or "auto", "model": model, "suggest_faster": True,
            "ready": bool(model)}


# -- the audio check --------------------------------------------------------------------

class AudioCheck:
    """Starts and stops the real capture once and reports whether real audio arrived.

    ``run_check(timeout_s)`` returns ``tap_doctor.DoctorResult`` (ok, code, message).
    The last answer is kept in ``<data dir>/setup.json`` so a finished setup stays
    finished after a restart; the key and the audio itself are never stored.
    """

    def __init__(self, run_check: Callable[[float], Any], data_dir: Callable[[], Path], *,
                 available: Callable[[], tuple[bool, str]],
                 deadline_s: float = CHECK_DEADLINE_S, clock: Callable[[], float] = _now) -> None:
        self._run_check = run_check
        self._data_dir = data_dir
        self._available = available
        self._deadline_s = deadline_s
        self._clock = clock
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._current: Optional[dict] = None

    # state

    def _path(self) -> Path:
        return Path(self._data_dir()) / STATE_FILE

    def _saved(self) -> Optional[dict]:
        try:
            data = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return None
        audio = data.get("audio") if isinstance(data, dict) else None
        return audio if isinstance(audio, dict) and isinstance(audio.get("state"), str) else None

    def _save(self, result: dict) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"audio": result}))
            os.replace(tmp, path)
        except OSError as exc:  # the answer still shows; it is asked again after a restart
            log.warning("could not save the setup state: %s", exc)

    def state(self) -> dict:
        with self._lock:
            if self._current is not None:
                return dict(self._current)
        try:
            ok, why = self._available()
        except ImportError as exc:  # never "not needed": the check itself cannot load yet
            return {"state": AUDIO_FAILED, "message": missing_module_message(exc), "at": None}
        except Exception as exc:
            return {"state": AUDIO_FAILED, "message": f"Could not check the capture backend: {exc}", "at": None}
        if not ok:
            return {"state": AUDIO_NOT_NEEDED, "message": why, "at": None}
        saved = self._saved()
        if saved is not None:
            return {"state": saved["state"], "message": str(saved.get("message") or ""), "at": saved.get("at")}
        return {"state": AUDIO_UNKNOWN, "message": "", "at": None}

    def _set(self, result: Optional[dict]) -> None:
        with self._lock:
            self._current = result

    # running

    def start(self) -> dict:
        """Begin a check unless one runs; returns the state at once. Call from the event loop."""
        if self._task is not None and not self._task.done():
            return self.state()
        self._set({"state": AUDIO_RUNNING, "message": "", "at": self._clock()})
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self.state()

    async def _run(self) -> None:
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._run_check, CHECK_TIMEOUT_S),
                                            timeout=self._deadline_s)
            outcome = classify(result)
        except asyncio.TimeoutError:
            outcome = {"state": AUDIO_FAILED,
                       "message": (f"The audio check did not finish within {int(self._deadline_s)} seconds. "
                                   f"If macOS is showing a prompt, answer it, then press Check again.")}
        except Exception as exc:
            outcome = {"state": AUDIO_FAILED,
                       "message": missing_module_message(exc) or f"The audio check failed: {exc}"}
        outcome["at"] = self._clock()
        if outcome["state"] in (AUDIO_OK, AUDIO_DENIED):
            self._save(outcome)
        self._set(outcome)

    async def wait(self) -> None:
        """For tests and shutdown: wait for a running check to end."""
        if self._task is not None:
            await asyncio.shield(self._task)


def classify(result: Any) -> dict:
    """``tap_doctor.DoctorResult`` (or an (ok, message) pair) to the pane's audio state."""
    from .hermeneus_engine.capture import tap_doctor
    if isinstance(result, tuple) and len(result) == 2:
        ok, message, code = bool(result[0]), str(result[1]), None
    else:
        ok, message, code = bool(getattr(result, "ok", False)), str(getattr(result, "message", "")), \
            getattr(result, "code", None)
    if ok:
        return {"state": AUDIO_OK, "message": message}
    if code == tap_doctor.PERMISSION_DENIED:
        return {"state": AUDIO_DENIED, "message": message}
    return {"state": AUDIO_FAILED, "message": message}


def real_check(timeout_s: float) -> Any:
    """The doctor's capture check: the real backend and worker, one tap started and stopped."""
    from .hermeneus_engine.capture.tap_doctor import check_process_tap
    return check_process_tap(timeout_s=timeout_s)


def process_tap_available() -> tuple[bool, str]:
    """Whether this Mac captures through a process tap (the only backend with this permission)."""
    if sys.platform != "darwin":
        return False, "this computer does not use the macOS process tap"
    from .hermeneus_engine import components
    try:
        backend = components.capture_backend("process-tap")
        return backend.available()
    except components.ComponentUnavailable as exc:
        return False, str(exc)


def open_privacy_settings(run: Optional[Callable[[list], Any]] = None) -> None:
    """Open Screen & System Audio Recording in System Settings with ``open`` (never waits on the app)."""
    if sys.platform != "darwin":
        raise RuntimeError("System Settings is a macOS app")
    if run is None:
        from .hermeneus_engine import subproc
        run = lambda argv: subproc.run(argv, capture_output=True, timeout=5, check=True)  # noqa: E731
    run(["/usr/bin/open", PRIVACY_URL])


# -- the whole picture ------------------------------------------------------------------

def setup_state(*, key_set: Optional[bool], audio: dict, model: dict, language: dict) -> dict:
    """What the pane's setup shows. ``complete`` is what gates the guided setup."""
    audio_done = audio.get("state") in (AUDIO_OK, AUDIO_NOT_NEEDED)
    return {
        "key": {"set": key_set, "name": SECRET_NAME, "url": KEY_URL, "permissions": list(KEY_PERMISSIONS)},
        "audio": {**audio, "settings_url": PRIVACY_URL, "settings_pane": PRIVACY_PANE},
        "model": model,
        "language": language,
        "complete": bool(key_set) and audio_done,
    }


def key_is_set(get_secret: Callable[[str], Optional[str]]) -> Optional[bool]:
    """True or False, or None when the secret scope cannot be read. The value is never returned."""
    try:
        return bool(get_secret(SECRET_NAME))
    except Exception as exc:
        log.warning("could not read %s presence: %s", SECRET_NAME, type(exc).__name__)
        return None
