"""The overlay's commands, shared by ``/translate overlay``, ``hermes hermeneus overlay`` and ``POST /overlay``."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..overlay_settings import from_settings
from ..overlay_tokens import ThemeError, palette_from_payload  # noqa: F401  (callers catch ThemeError)
from ..runtime import lock_holder
from . import control
from .control import OverlayUnavailable  # noqa: F401  (callers catch it from here)

RUNNING = frozenset({"starting", "live", "reconnecting"})


class OverlayService:
    def __init__(self, data_dir: Callable[[], Path], settings: Callable[[], dict],
                 status: Callable[[], Awaitable[dict]], *, show_fn=control.show, hide_fn=control.hide,
                 status_fn=control.status) -> None:
        self._data_dir = data_dir
        self._settings = settings
        self._status = status
        self._show = show_fn
        self._hide = hide_fn
        self._status_fn = status_fn

    @property
    def data_dir(self) -> Path:
        return Path(self._data_dir())

    async def session_running(self) -> bool:
        status = await self._status()
        return status.get("state") in RUNNING or lock_holder(self.data_dir) is not None

    async def show(self, platform: Optional[str] = None) -> dict:
        """Open the overlay (or apply new settings to the open one). Raises OverlayUnavailable."""
        control.check_platform(platform)
        running = await self.session_running()
        settings = from_settings(self._settings())
        return await asyncio.to_thread(self._show, self.data_dir, settings, session_running=running)

    async def hide(self) -> dict:
        return await asyncio.to_thread(self._hide, self.data_dir)

    def status(self) -> dict:
        """Open or not, and ``armed``: whether the ``overlay`` setting opens it with the next session."""
        look = from_settings(self._settings())
        settings = {k: v for k, v in asdict(look).items() if k != "auto"}
        return {**self._status_fn(self.data_dir), "armed": look.auto, "settings": settings}

    def apply(self) -> dict:
        """Hand the current settings to an open overlay; it redraws within a second, without closing."""
        if control.running_pid(self.data_dir) is not None:
            control.write_settings(self.data_dir, from_settings(self._settings()))
        return self.status()

    def set_theme(self, payload) -> dict:
        """Store the Desktop theme's token values (raises ThemeError); an open overlay repaints with them."""
        control.write_theme(self.data_dir, palette_from_payload(payload))
        return self.status()

    async def set_visible(self, visible: bool, platform: Optional[str] = None) -> dict:
        return await (self.show(platform) if visible else self.hide())

    async def auto(self, platform: Optional[str] = None) -> str:
        """After a session starts: open the overlay when the ``overlay`` setting is on.

        Returns a sentence for the user, empty when the setting is off. A
        failure never fails the session start; it is reported instead.
        """
        if not from_settings(self._settings()).auto:
            return ""
        try:
            await self.show(platform)
        except OverlayUnavailable as exc:
            return f"The subtitle overlay did not open. {exc}"
        return "The subtitle overlay is open."


def describe(result: dict) -> str:
    message = result.get("message") or ("the overlay is open" if result.get("visible") else "the overlay is closed")
    return message[:1].upper() + message[1:] + "."

