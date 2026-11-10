"""Hermes services for the engine: secrets, the LLM call, settings and the data directory.

Everything here reaches Hermes only through its public plugin API
(``website/docs/developer-guide/plugins/public-api.md``): ``agent.secret_scope``,
``agent.plugin_llm``, ``plugins.plugin_storage`` and ``hermes_cli.config``.
Imports are lazy so the engine and the tests run without Hermes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from .hermeneus_engine.components import HermesServices
from .hermeneus_engine.contracts import ChatMessage

PLUGIN_NAME = "hermeneus"
AUX_TASK = "live_translate"
AUX_DISPLAY_NAME = "Live translation"
AUX_DESCRIPTION = "Translates live subtitle segments. A small, fast model keeps subtitles close to the speech."
# Per request. The translator bounds the whole call (host retries included) with its own timeout.
LLM_REQUEST_TIMEOUT_S = 6.0
LLM_PURPOSE = "hermeneus.live_translate"

SETTING_DEFAULTS: dict[str, Any] = {
    "target_language": "",
    "capture_backend": "auto",
    "source": "",
    "speak": False,
    "commit_strategy": "vad",
    "output_device": "",
    "voice_id": "",
    "context_segments": 3,
}


def get_secret(name: str) -> Optional[str]:
    """Catalog rule 11: credentials come through Hermes's profile-aware secret scope."""
    from agent.secret_scope import get_secret as _get_secret
    return _get_secret(name)


def data_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/hermeneus``, the documented home for plugin runtime state."""
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_NAME)


def make_llm_call(llm: Any):
    """Wrap ``ctx.llm`` (or a ``PluginLlm``) into the engine's ``LlmCall``."""

    async def call(messages: Sequence[ChatMessage]) -> str:
        result = await llm.acomplete(messages=list(messages), task=AUX_TASK,
                                     timeout=LLM_REQUEST_TIMEOUT_S, purpose=LLM_PURPOSE)
        return result.text

    return call


def standalone_llm() -> Any:
    """The LLM facade for code that has no ``ctx`` (the dashboard router)."""
    from agent.plugin_llm import PluginLlm
    return PluginLlm(plugin_id=PLUGIN_NAME)


def services(llm: Any) -> HermesServices:
    return HermesServices(get_secret=get_secret, llm=make_llm_call(llm))


def _config() -> dict:
    from hermes_cli.config import load_config_readonly
    return load_config_readonly() or {}


def settings(ctx: Any = None) -> dict:
    """This plugin's settings (``plugins.entries.hermeneus.settings``), defaults filled in."""
    out = dict(SETTING_DEFAULTS)
    if ctx is not None:
        for key, default in SETTING_DEFAULTS.items():
            out[key] = ctx.get_config(key, default)
        return out
    entry = ((_config().get("plugins") or {}).get("entries") or {}).get(PLUGIN_NAME) or {}
    stored = entry.get("settings") or {}
    if isinstance(stored, dict):
        out.update({k: v for k, v in stored.items() if k in SETTING_DEFAULTS})
    return out


def aux_config() -> dict:
    cfg = (_config().get("auxiliary") or {}).get(AUX_TASK) or {}
    return cfg if isinstance(cfg, dict) else {}
