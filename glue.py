"""Hermes services for the engine: secrets, the LLM call, settings and the data directory.

Everything here reaches Hermes only through its public plugin API
(``website/docs/developer-guide/plugins/public-api.md``): ``agent.secret_scope``,
``agent.plugin_llm``, ``plugins.plugin_storage`` and ``hermes_cli.config``.
Imports are lazy so the engine and the tests run without Hermes.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional, Sequence

from .hermeneus_engine.components import HermesServices
from .hermeneus_engine.contracts import ChatMessage, TranslationError
from .hermeneus_engine.messages import MODEL_FIX

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


def make_llm_call(llm: Any, provider: Optional[str] = None):
    """Wrap ``ctx.llm`` (or a ``PluginLlm``) into the engine's ``LlmCall``.

    Failures come back as ``TranslationError`` with a plain reason and, where
    there is one, the fix (see ``describe_llm_error``).
    """

    async def call(messages: Sequence[ChatMessage]) -> str:
        try:
            result = await llm.acomplete(messages=list(messages), task=AUX_TASK,
                                         timeout=LLM_REQUEST_TIMEOUT_S, purpose=LLM_PURPOSE)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise  # the translator reports its own timeout
        except Exception as exc:
            raise TranslationError(describe_llm_error(exc, provider or _aux_provider())) from exc
        return result.text

    return call


def _aux_provider() -> str:
    try:
        return str(aux_config().get("provider") or "auto")
    except Exception:
        return "auto"


_EXTRA_IN_MESSAGE = re.compile(r"extra '([A-Za-z0-9_-]+)'|^([A-Za-z0-9_-]+) installed;")


def describe_llm_error(exc: BaseException, provider: str = "auto") -> str:
    """A plain reason for a failed model call, with the fix when Hermes's error says what it is.

    The model call goes through Hermes's own client, which raises the
    provider's error or ``pm.package.InstallError``; these are matched by
    their text, so an unknown error keeps its own words.
    """
    text = " ".join(str(exc).split())
    low = text.lower()
    where = f" ({provider})" if provider and provider != "auto" else ""
    if type(exc).__name__ == "InstallError":
        cause = str(getattr(exc, "cause", "") or text)
        m = _EXTRA_IN_MESSAGE.search(cause)
        extra = (m.group(1) or m.group(2)) if m else ""
        if "restart" in cause.lower():
            return (f"Translation failed: Hermes' {extra!r} feature was just installed. "
                    "Restart Hermes (or this command) to use it.")
        if extra:
            return (f"Translation failed: the translation model needs Hermes' {extra!r} feature. "
                    f"Run: {_install_hint(extra)}")
        return f"Translation failed: a Hermes feature the model needs is missing ({cause})."
    if "insufficient_credits" in low or "requires available credits" in low or "insufficient credit" in low \
            or "balance is too low" in low or ("402" in low and "payment" in low):
        return (f"Translation failed: the model provider reports insufficient credits{where}. "
                f"Add credits with that provider, or pick another model with: {MODEL_FIX}")
    if "401" in low or "403" in low or "invalid api key" in low or "unauthorized" in low or "authentication" in low:
        return (f"Translation failed: the model provider rejected the credentials{where}. "
                "Check that provider's key, or pick another provider with: hermes model")
    if "429" in low or "rate limit" in low or "rate_limit" in low or "too many requests" in low:
        return (f"Translation failed: the model provider is rate limiting{where}. "
                "Lines will translate again when it allows more requests.")
    if "model_not_found" in low or "does not exist" in low or "unknown model" in low:
        return (f"Translation failed: the model provider does not offer the configured model{where}. "
                f"Pick another model with: {MODEL_FIX}")
    if len(text) > 160:
        text = text[:157] + "..."
    return f"Translation failed: {type(exc).__name__}: {text}" if text else f"Translation failed: {type(exc).__name__}."


def _install_hint(extra: str) -> str:
    try:
        from pm.extras import install_hint
        return install_hint(extra)
    except Exception:
        return f"hermes pm install --extra {extra}"


# api_mode of a provider profile -> the Hermes extra its client imports
# (agent/anthropic_adapter.py:47-48 ensure_import("anthropic"),
#  agent/bedrock_adapter.py:109-110 ensure_import("bedrock")).
_API_MODE_EXTRAS = {"anthropic_messages": "anthropic", "bedrock_converse": "bedrock"}


def missing_model_extra(provider: str) -> Optional[str]:
    """The Hermes extra the live_translate provider needs and this install lacks, else None.

    Checked before a session starts so Hermes never has to offer an install
    in the middle of a session. Hermes's own lazy install either asks on the
    terminal (a hang for a session that runs unattended) or, inside its
    non-interactive scope, installs without asking (pm/extras.py:239-258);
    the plugin wants neither, so it stops and names the command instead.
    Only providers whose profile names the api_mode are known; for ``auto``
    the route is decided per call and this check cannot see it.
    """
    if not provider or provider == "auto":
        return None
    try:
        from providers import get_provider_profile
        from pm.extras import available
    except Exception:
        return None
    profile = get_provider_profile(provider)
    extra = _API_MODE_EXTRAS.get(getattr(profile, "api_mode", "") or "")
    if extra and not available(extra):
        return extra
    return None


def model_preflight() -> None:
    """Raise TranslationError when the translation model cannot run without installing something."""
    provider = _aux_provider()
    extra = missing_model_extra(provider)
    if extra:
        raise TranslationError(f"the translation model ({provider}) needs Hermes' {extra!r} feature, "
                               f"which is not installed. Run: {_install_hint(extra)}")


async def test_model(llm: Any) -> tuple[bool, str]:
    """One tiny live call on the live_translate route, for ``hermes hermeneus doctor --test-model``."""
    try:
        result = await llm.acomplete(messages=[{"role": "user", "content": "Reply with the single word OK."}],
                                     task=AUX_TASK, timeout=20.0, max_tokens=8, purpose="hermeneus.doctor")
    except Exception as exc:
        return False, describe_llm_error(exc, _aux_provider()).replace("Translation failed: ", "")
    return True, f"{getattr(result, 'provider', '?')}/{getattr(result, 'model', '?')} answered"


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
