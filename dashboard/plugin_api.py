"""Backend routes for the Hermeneus desktop pane, mounted by Hermes at ``/api/plugins/hermeneus/``.

The routes run in the process Desktop is connected to. When that process
owns the session they read it directly; when another process owns it (a
``hermes hermeneus run`` in a terminal, for example) they relay its Unix
socket. Event shapes: ``docs/events.md``.

Hermes imports this file on its own, outside the plugin package, so the
package is loaded here from this file's parent directory with the standard
importlib recipe (https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

_PACKAGE_NAME = "hermeneus_dashboard_package"
_ROOT = Path(__file__).resolve().parent.parent


def _load_package() -> Any:
    loaded = sys.modules.get(_PACKAGE_NAME)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_PACKAGE_NAME, _ROOT / "__init__.py",
                                                  submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PACKAGE_NAME, None)
        raise
    return module


_load_package()
_commands = importlib.import_module(f"{_PACKAGE_NAME}.commands")
_glue = importlib.import_module(f"{_PACKAGE_NAME}.glue")
_components = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.components")
_doctor = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.doctor")
_options = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.options")
_runtime = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.runtime")
_contracts = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.contracts")
_ipc = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.ipc")
_voice_catalog = importlib.import_module(f"{_PACKAGE_NAME}.hermeneus_engine.tts.voice_catalog")
_onboarding = importlib.import_module(f"{_PACKAGE_NAME}.onboarding")

router = APIRouter()
plugin = _commands.Plugin(None, owner="gateway")
# The first-run audio check. It reads the data dir through ``plugin`` at call time, so a test's plugin is used.
audio_check = _onboarding.AudioCheck(_onboarding.real_check, lambda: plugin.controller.data_dir,
                                     available=_onboarding.process_tap_available)


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    # ``detail`` is FastAPI's own error key; ``error`` says the same for readers that look for it.
    return JSONResponse({"ok": False, "detail": message, "error": message, **extra}, status_code=status)


@router.get("/status")
async def status() -> dict:
    return await plugin.controller.status()


@router.get("/lines")
async def lines(after: Optional[int] = Query(None, ge=0)) -> dict:
    return await plugin.controller.lines(after)


# Headers a reverse proxy adds (RFC 7239 ``Forwarded`` and the de facto X-Forwarded-*/X-Real-IP).
_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")


def _surface(request: Request) -> str:
    """``desktop`` for a Desktop on this machine, ``remote`` otherwise: the overlay opens only on the Mac
    whose screen the request comes from.

    Hermes v0.23.1 has no per-request local/remote flag for plugin routes. Its own peer address is
    ``request.client.host`` (hermes_cli/dashboard_auth/request_utils.py:20-27), which uvicorn rewrites
    from forwarding headers only for trusted proxies under the auth gate (hermes_cli/web_server.py:1083,
    web-dashboard.md "trusted_proxies"). A proxy on this machine in front of an ungated dashboard still
    shows a loopback peer, so a loopback request that carries proxy headers counts as remote.
    """
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return "remote"
    if any(h in request.headers for h in _PROXY_HEADERS):
        return "remote"
    return "desktop"


@router.post("/start")
async def start(request: Request, body: Optional[dict] = Body(default=None)):
    choices = _options.Choices.from_mapping(body or {})
    try:
        return await plugin.start(choices, platform=_surface(request))
    except _commands.RemoteStartRefused as exc:
        return _error(403, str(exc))
    except _runtime.SessionBusy as exc:
        return _error(409, str(exc), session=await plugin.controller.status())
    except _options.UsageError as exc:
        return _error(400, str(exc))
    except _contracts.FeedbackRiskError as exc:
        return _error(409, f"spoken translation would be captured again: {exc}")
    except _commands.START_ERRORS as exc:
        return _error(422, f"could not start: {exc}")


@router.post("/stop")
async def stop() -> dict:
    return await plugin.controller.stop()


@router.post("/settings")
async def settings(request: Request, body: Optional[dict] = Body(default=None)):
    """Change the running session. Answers at once with the accepted change or a refusal."""
    try:
        changes = _options.changes_from_mapping(body or {})
        return await plugin.set(changes, platform=_surface(request))
    except _commands.RemoteStartRefused as exc:
        return _error(403, str(exc))
    except _options.UsageError as exc:
        return _error(400, str(exc))
    except _ipc.SettingsRefused as exc:
        return _error(409, str(exc), session=await plugin.controller.status())


@router.get("/voices")
async def voices(refresh: bool = Query(False)):
    try:
        return await plugin.voices(refresh=refresh)
    except _voice_catalog.VoiceListError as exc:
        return _error(502, str(exc))


@router.get("/overlay")
async def overlay_status() -> dict:
    return plugin.overlay.status()


@router.post("/overlay")
async def overlay(request: Request, body: Optional[dict] = Body(default=None)):
    visible = (body or {}).get("visible")
    if not isinstance(visible, bool):
        return _error(400, 'body must be {"visible": true} or {"visible": false}')
    try:
        return await plugin.overlay.set_visible(visible, _surface(request))
    except _commands.OverlayUnavailable as exc:
        return _error(409, str(exc), **plugin.overlay.status())


@router.post("/overlay/apply")
async def overlay_apply() -> dict:
    return await asyncio.to_thread(plugin.overlay.apply)


@router.post("/overlay/theme")
async def overlay_theme(body: Optional[dict] = Body(default=None)):
    try:
        return await asyncio.to_thread(plugin.overlay.set_theme, body)
    except _commands.ThemeError as exc:
        return _error(400, str(exc))


@router.get("/setup")
async def setup(locale: list[str] = Query(default=[])) -> dict:
    """What the pane's first-run setup shows. ``locale`` is the Desktop app's system languages, in order.

    Answers at once: it reads config and whether the key is set (never its value), and the last audio check.
    """
    settings = plugin.settings()
    language = _onboarding.default_language(saved=settings.get("target_language"),
                                            hermes_language=_glue.display_language(),
                                            client_locales=locale, system=_onboarding.system_locale())
    return _onboarding.setup_state(key_set=_onboarding.key_is_set(_glue.get_secret),
                                   audio=await asyncio.to_thread(audio_check.state),
                                   model=_onboarding.model_route(_glue.aux_config(), _glue.main_config()),
                                   language=language)


@router.post("/setup/audio-check")
async def setup_audio_check(request: Request):
    """Start and stop the real capture once (the macOS prompt appears the first time). Answers at once;
    GET /setup reports the result."""
    if _surface(request) != "desktop":
        return _error(403, _commands.REMOTE_START_REFUSED)
    status = await plugin.controller.status()
    if status.get("state") in ("starting", "live", "reconnecting", "stopping"):
        return _error(409, "a session is running; stop it first, then check the audio again")
    return audio_check.start()


@router.post("/setup/open-privacy-settings")
async def setup_open_privacy(request: Request):
    """Open Screen & System Audio Recording in System Settings on this Mac."""
    if _surface(request) != "desktop":
        return _error(403, _commands.REMOTE_START_REFUSED)
    try:
        await asyncio.to_thread(_onboarding.open_privacy_settings)
    except Exception as exc:
        return _error(500, f"System Settings did not open ({exc}). Open {_onboarding.PRIVACY_PANE} yourself.")
    return {"ok": True}


@router.get("/devices")
async def devices(backend: Optional[str] = Query(None), source_id: Optional[str] = Query(None)) -> dict:
    return await asyncio.to_thread(_commands.devices_payload, backend, source_id)


@router.get("/doctor")
async def doctor() -> dict:
    report = await asyncio.to_thread(_doctor.run, get_secret=_glue.get_secret, aux_config=_glue.aux_config())
    report.checks.append(_commands.overlay_check(plugin.settings(), await plugin.controller.overlay()))
    return report.to_dict()


@router.websocket("/events")
async def events(websocket: WebSocket, after: Optional[int] = Query(None, ge=0)) -> None:
    await websocket.accept()
    stream = plugin.controller.subscribe(after)
    try:
        async for event in stream:
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        return  # the pane went away; RuntimeError is Starlette's "send after close"
    finally:
        await stream.aclose()
