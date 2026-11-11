"""The palette the native overlay draws with: what the Desktop pane last sent, else the nous fallback.

Token values and their sources are in ``hermeneus_engine/overlay_tokens.py``,
the table the 127.0.0.1 overlay page reads as well.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..overlay_tokens import FALLBACK, Palette, Rgba, WEIGHT_LABEL, WEIGHT_TEXT, palette_from_json  # noqa: F401


def load(path: Path) -> Optional[Palette]:
    """The palette the pane sent (``overlay/theme.json``), or None."""
    try:
        return palette_from_json(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None


def palette(dark: bool, sent: Optional[Palette] = None) -> Palette:
    """The sent palette (its own light or dark, which is Desktop's), else nous for the system appearance."""
    return sent if sent is not None else FALLBACK[dark]


def ns_weight(css_weight: int) -> float:
    """CSS font-weight to NSFontWeight (400 regular 0.0, 500 medium 0.23, 600 semibold 0.3, 700 bold 0.4)."""
    return {400: 0.0, 500: 0.23, 600: 0.3, 700: 0.4}.get(css_weight, 0.0)
