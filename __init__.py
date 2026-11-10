"""Hermeneus: live translation for Hermes.

Hermes imports this directory as a plugin package and calls ``register(ctx)``.
Registration is filled in by the pipeline workstream; this file only wires the
package so the plugin loads.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Called once by the Hermes plugin loader."""
    logger.debug("hermeneus: register called")
