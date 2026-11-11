"""The palette the native overlay draws with: what the Desktop pane last sent, else the nous fallback.

Token values and their sources are in ``hermeneus_engine/overlay_tokens.py``,
the table the 127.0.0.1 overlay page reads as well.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from ..overlay_tokens import BANNER, FALLBACK, Palette, Rgba, WEIGHT_LABEL, WEIGHT_TEXT, palette_from_json  # noqa: F401


def load(path: Path) -> Optional[Palette]:
    """The palette the pane sent (``overlay/theme.json``), or None."""
    try:
        return palette_from_json(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return None


def palette(dark: bool, sent: Optional[Palette] = None, style: str = "theme") -> Palette:
    """With ``overlay_style`` banner, the banner palette. Otherwise the sent palette (its own light or dark,
    which is Desktop's), else nous for the system appearance."""
    if style == "banner":
        return BANNER
    return sent if sent is not None else FALLBACK[dark]


def ns_weight(css_weight: int) -> float:
    """CSS font-weight to NSFontWeight (400 regular 0.0, 500 medium 0.23, 600 semibold 0.3, 700 bold 0.4)."""
    return {400: 0.0, 500: 0.23, 600: 0.3, 700: 0.4}.get(css_weight, 0.0)


# -- legibility over video ------------------------------------------------------------

SEE_THROUGH_BELOW = 50  # background opacity (percent) under which the text carries its own edge


@dataclass(frozen=True)
class TextEdge:
    """How text keeps its edge over video. ``outline_percent`` is NSStrokeWidthAttributeName (percent of the
    font size, stroke only, centred on the glyph edge, so half of it shows outside the fill); 0 means none."""

    outline_percent: float
    halo_alpha: float
    halo_blur: float  # blur radius as a fraction of the font size
    solid: bool  # draw the fill opaque (its colour composited over the surface), so the outline never shows through


# 50% background and up: the soft halo alone, as before.
EDGE_ON_SURFACE = TextEdge(outline_percent=0.0, halo_alpha=0.9, halo_blur=0.14, solid=False)
# Under 50%: a thin outline strictly behind an opaque fill (5% of the size, half of it outside the glyph, so letters
# keep their spacing and counters) and a lighter halo on the fill only, like broadcast subtitles.
EDGE_SEE_THROUGH = TextEdge(outline_percent=10.0, halo_alpha=0.75, halo_blur=0.10, solid=True)


def see_through(p: Palette, opacity: int) -> Palette:
    """Under 50% background the video shows through, so text is light with a thin dark edge in every theme,
    as TV subtitles are (DCMP: white text with a drop or rim shadow). A light palette swaps its surface and text
    for the dark fallback's and its speaker colours for the banner's, which keep AA on a dark ground."""
    if opacity >= SEE_THROUGH_BELOW or p.dark:
        return p
    dark = FALLBACK[True]
    return replace(p, dark=True, surface=dark.surface, text=dark.text, text_secondary=dark.text_secondary,
                   text_tertiary=dark.text_tertiary, speakers=BANNER.speakers, failed=None)


def text_edge(opacity: int) -> TextEdge:
    return EDGE_SEE_THROUGH if opacity < SEE_THROUGH_BELOW else EDGE_ON_SURFACE


def ink(color: Rgba, surface: Rgba, edge: TextEdge) -> Rgba:
    """The colour a run is drawn in: as the token says, or composited over the opaque surface when solid."""
    return color.over(surface.with_alpha(1.0)) if edge.solid else color
