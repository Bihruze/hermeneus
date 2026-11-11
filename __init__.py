"""Hermeneus: live translation for Hermes.

Hermes imports this directory as a plugin package and calls ``register(ctx)``.
The plugin modules are imported inside ``register`` so that importing this
file on its own (pytest does that for the directory's ``__init__.py``) has no
side effects. Registration touches nothing heavy: audio, network and the LLM
are used only when a session starts.
"""

from __future__ import annotations

SLASH_COMMAND = "translate"
CLI_COMMAND = "hermeneus"


def register(ctx) -> None:
    """Called once by the Hermes plugin loader."""
    from . import glue
    from .commands import SLASH_USAGE, Plugin, cli_handler, setup_cli
    from .hermeneus_engine.platform_words import this_machine

    plugin = Plugin(ctx, owner="hermes")
    ctx.register_auxiliary_task(glue.AUX_TASK, display_name=glue.AUX_DISPLAY_NAME,
                                description=glue.AUX_DESCRIPTION)
    ctx.register_command(SLASH_COMMAND, plugin.slash,
                         description=f"Live translation of audio playing on {this_machine()}",
                         args_hint="start|set|stop|status|voices|overlay")
    ctx.register_cli_command(CLI_COMMAND, help=f"live translation of audio playing on {this_machine()}",
                             setup_fn=setup_cli, handler_fn=cli_handler(plugin),
                             description=SLASH_USAGE)
    ctx.on_unload(plugin.unload)
