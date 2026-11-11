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

from fastapi import APIRouter, Body, Query, WebSocket, WebSocketDisconnect
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

router = APIRouter()
plugin = _commands.Plugin(None, owner="gateway")


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    # ``detail`` is FastAPI's own error key; ``error`` says the same for readers that look for it.
    return JSONResponse({"ok": False, "detail": message, "error": message, **extra}, status_code=status)


@router.get("/status")
async def status() -> dict:
    return await plugin.controller.status()


@router.get("/lines")
async def lines(after: Optional[int] = Query(None, ge=0)) -> dict:
    return await plugin.controller.lines(after)


@router.post("/start")
async def start(body: Optional[dict] = Body(default=None)):
    choices = _options.Choices.from_mapping(body or {})
    try:
        return await plugin.start(choices)
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
