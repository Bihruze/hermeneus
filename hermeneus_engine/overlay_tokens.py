"""One token table for both subtitle overlays: the native window and the 127.0.0.1 page.

Hermes Desktop is the source. The Desktop pane resolves the active theme's
tokens on its own element, as the SDK guide says to for canvas drawing
(``website/docs/developer-guide/desktop-plugin-sdk.md:2119-2122``,
``getComputedStyle(...).getPropertyValue``), and sends them with
``POST /overlay/theme``. They are kept in ``<data dir>/overlay/theme.json``.
Until a pane has sent them, the overlays use ``FALLBACK``: the default skin
``nous`` (``apps/desktop/src/themes/presets.ts:462``), with the colours from
``apps/shared/src/theme-presets.ts:125-170`` and the mixes from
``apps/desktop/src/styles.css`` (paths in the hermes-agent checkout, v0.23.1).

Token to overlay role, the same on both surfaces:

==========================  ===================================================
``--ui-bg-elevated``        strip surface (the popover primitive's fill,
                            ``components/ui/popover.tsx:31``)
``--ui-text-primary``       translation (``styles.css:364``, base at 94%)
``--ui-text-secondary``     original and a line still being spoken (74%, :365)
``--ui-text-tertiary``      notices (54%, :366)
``--ui-red``                "not translated" marker on a failed line, as the pane
                            marks it (``styles.css:243``, dark ``:613``)
``--ui-stroke-secondary``   hairline, as on the popover (``popover.tsx:31``)
``--ui-blue`` ... ``--ui-yellow``  speaker colours, in the pane's order
                            (``styles.css:243-249``, dark ``:613-615``)
``--radius-lg``             corner radius, as on the popover (``popover.tsx:24``)
``--dt-font-sans``          font family (``styles.css:476-478``)
==========================  ===================================================

Weights are shared too: text 400, speaker label 600.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

SPEAKER_TOKENS = ("--ui-blue", "--ui-green", "--ui-orange", "--ui-purple", "--ui-cyan", "--ui-yellow")
COLOR_TOKENS = {
    "surface": "--ui-bg-elevated",
    "text": "--ui-text-primary",
    "text_secondary": "--ui-text-secondary",
    "text_tertiary": "--ui-text-tertiary",
    "stroke": "--ui-stroke-secondary",
}
WEIGHT_TEXT = 400
WEIGHT_LABEL = 600
# The Desktop shell sets html font-size to var(--dt-base-size, .875rem) with
# --dt-base-size 1rem, so 1rem is 16 px; --radius-lg is calc(--radius-scalar
# 0.2 * 0.75rem) = 2.4 px. Both come from the built stylesheet
# (apps/desktop/dist/assets/main-*.css: "--radius-scalar:.2",
# ".rounded-lg{border-radius:calc(var(--radius-scalar) * .75rem)}").
FALLBACK_RADIUS_PX = 2.4  # 0.2 x 0.75rem x 16 px
FALLBACK_FONT = "-apple-system"


@dataclass(frozen=True)
class Rgba:
    r: float
    g: float
    b: float
    a: float = 1.0

    @classmethod
    def hex(cls, value: str, alpha: float = 1.0) -> "Rgba":
        value = value.lstrip("#")
        return cls(int(value[0:2], 16) / 255, int(value[2:4], 16) / 255, int(value[4:6], 16) / 255, alpha)

    def with_alpha(self, alpha: float) -> "Rgba":
        return Rgba(self.r, self.g, self.b, alpha)

    def css(self) -> str:
        return f"rgba({round(self.r * 255)}, {round(self.g * 255)}, {round(self.b * 255)}, {round(self.a, 3)})"

    def over(self, below: "Rgba") -> "Rgba":
        """This colour composited over an opaque ``below``."""
        a = self.a
        return Rgba(self.r * a + below.r * (1 - a), self.g * a + below.g * (1 - a), self.b * a + below.b * (1 - a), 1.0)

    def luminance(self) -> float:
        def lin(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * lin(self.r) + 0.7152 * lin(self.g) + 0.0722 * lin(self.b)


def contrast(a: Rgba, b: Rgba) -> float:
    """WCAG 2 contrast ratio of two opaque colours."""
    la, lb = sorted((a.luminance(), b.luminance()), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(a: Rgba, pa: float, b: Rgba) -> Rgba:
    """CSS ``color-mix(in srgb, a pa, b)``: premultiplied, as browsers compute it."""
    pb = 1 - pa
    alpha = a.a * pa + b.a * pb
    if alpha == 0:
        return Rgba(0, 0, 0, 0)
    return Rgba((a.r * a.a * pa + b.r * b.a * pb) / alpha, (a.g * a.a * pa + b.g * b.a * pb) / alpha,
                (a.b * a.a * pa + b.b * b.a * pb) / alpha, alpha)


@dataclass(frozen=True)
class Palette:
    dark: bool
    surface: Rgba
    text: Rgba
    text_secondary: Rgba
    text_tertiary: Rgba
    stroke: Rgba
    speakers: tuple[Rgba, ...]
    radius_px: float = FALLBACK_RADIUS_PX
    font: str = FALLBACK_FONT
    source: str = "fallback"
    failed: Optional[Rgba] = None  # --ui-red; None means the nous value for this mode

    @property
    def failed_color(self) -> Rgba:
        """The colour of the "not translated" marker, as the pane marks a failed line (``--ui-red``)."""
        return self.failed if self.failed is not None else _RED[self.dark]

    def speaker(self, number: int) -> Rgba:
        return self.speakers[(number - 1) % len(self.speakers)]

    def to_dict(self) -> dict:
        return asdict(self)


def _nous(dark: bool) -> Palette:
    base = Rgba.hex("#e6edf3" if dark else "#1f2328")  # nous foreground
    accent = Rgba.hex("#4a84fe" if dark else "#0053fd")  # nous primary / midground
    # --ui-stroke-secondary: color-mix(accent 16%, color-mix(base 7%, transparent)) (styles.css:380-384, :231)
    stroke = _mix(accent, 0.16, base.with_alpha(0.07))
    # --ui-blue has no dark override (styles.css:248), so it is #0053fd in both modes, as in the pane.
    speakers = ("#0053fd", "#55a583" if dark else "#1f8a65", "#db704b", "#9e94d5", "#6f9ba6" if dark else "#4c7f8c",
                "#c08532")
    return Palette(dark=dark, surface=Rgba.hex("#161b22" if dark else "#ffffff"), text=base.with_alpha(0.94),
                   text_secondary=base.with_alpha(0.74), text_tertiary=base.with_alpha(0.54), stroke=stroke,
                   speakers=tuple(Rgba.hex(s) for s in speakers))


# --ui-red, light styles.css:243 and dark :613; the pane colours a failed line's caption with it.
_RED = {False: Rgba.hex("#cf2d56"), True: Rgba.hex("#e75e78")}
FALLBACK = {False: _nous(False), True: _nous(True)}


# -- parsing what the pane sends ---------------------------------------------------------

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?"
_RGB = re.compile(rf"^rgba?\(\s*({_NUM})%?[\s,]+({_NUM})%?[\s,]+({_NUM})%?(?:\s*[,/]\s*({_NUM})(%?))?\s*\)$", re.I)
_SRGB = re.compile(rf"^color\(\s*srgb\s+({_NUM})\s+({_NUM})\s+({_NUM})(?:\s*/\s*({_NUM})(%?))?\s*\)$", re.I)
_HEX = re.compile(r"^#([0-9a-f]{6})([0-9a-f]{2})?$", re.I)


def _unit(v: float) -> float:
    return max(0.0, min(1.0, v))


def parse_color(value: Any) -> Optional[Rgba]:
    """A CSS colour as ``getComputedStyle`` writes it: rgb(), rgba(), color(srgb ...) or #rrggbb[aa]."""
    if not isinstance(value, str) or len(value) > 80:
        return None
    text = value.strip()
    m = _HEX.match(text)
    if m:
        alpha = int(m.group(2), 16) / 255 if m.group(2) else 1.0
        return Rgba.hex(m.group(1), alpha)
    m = _RGB.match(text)
    if m:
        r, g, b = (float(m.group(i)) / 255 for i in (1, 2, 3))
        a = 1.0 if m.group(4) is None else float(m.group(4)) / (100 if m.group(5) else 1)
        return Rgba(_unit(r), _unit(g), _unit(b), _unit(a))
    m = _SRGB.match(text)
    if m:
        r, g, b = (float(m.group(i)) for i in (1, 2, 3))
        a = 1.0 if m.group(4) is None else float(m.group(4)) / (100 if m.group(5) else 1)
        return Rgba(_unit(r), _unit(g), _unit(b), _unit(a))
    return None


class ThemeError(ValueError):
    pass


def palette_from_payload(payload: Any) -> Palette:
    """Validate what the pane sent. Raises ThemeError naming the first bad field."""
    if not isinstance(payload, dict):
        raise ThemeError("theme must be an object")
    mode = payload.get("mode")
    if mode not in ("light", "dark"):
        raise ThemeError("mode must be light or dark")
    colors = payload.get("colors")
    if not isinstance(colors, dict):
        raise ThemeError("colors must be an object")
    parsed = {}
    for role in COLOR_TOKENS:
        color = parse_color(colors.get(role))
        if color is None:
            raise ThemeError(f"colors.{role} is not a CSS colour")
        parsed[role] = color
    speakers = colors.get("speakers")
    if not isinstance(speakers, list) or not 1 <= len(speakers) <= 12:
        raise ThemeError("colors.speakers must list 1 to 12 colours")
    speaker_colors = tuple(parse_color(s) for s in speakers)
    if any(c is None for c in speaker_colors):
        raise ThemeError("colors.speakers holds a value that is not a CSS colour")
    radius = payload.get("radius_px", FALLBACK_RADIUS_PX)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 0 <= radius <= 32:
        raise ThemeError("radius_px must be a number from 0 to 32")
    font = payload.get("font", FALLBACK_FONT)
    if not isinstance(font, str) or len(font) > 300:
        raise ThemeError("font must be a short string")
    failed = None
    if colors.get("failed") is not None:  # optional: a pane from before this field sends none
        failed = parse_color(colors.get("failed"))
        if failed is None:
            raise ThemeError("colors.failed is not a CSS colour")
    name = payload.get("name", "")
    return Palette(dark=mode == "dark", speakers=speaker_colors, radius_px=float(radius), font=font,
                   source=f"desktop:{name}"[:80] if isinstance(name, str) else "desktop", failed=failed, **parsed)


def palette_to_json(p: Palette) -> str:
    return json.dumps(p.to_dict(), sort_keys=True)


def palette_from_json(raw: str) -> Optional[Palette]:
    """A stored palette, or None when the file is missing or damaged."""
    try:
        data = json.loads(raw)
        speakers = tuple(Rgba(**c) for c in data.pop("speakers"))
        colors = {k: Rgba(**data.pop(k)) for k in COLOR_TOKENS}
        failed = Rgba(**data["failed"]) if data.get("failed") else None
        return Palette(speakers=speakers, dark=bool(data["dark"]), radius_px=float(data.get("radius_px", FALLBACK_RADIUS_PX)),
                       font=str(data.get("font", FALLBACK_FONT)), source=str(data.get("source", "desktop")),
                       failed=failed, **colors)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def css_vars(p: Palette) -> dict:
    """The palette as the 127.0.0.1 page's CSS variables."""
    out = {"--surface": p.surface.css(), "--text-primary": p.text.css(), "--text-secondary": p.text_secondary.css(),
           "--text-tertiary": p.text_tertiary.css(), "--stroke": p.stroke.css(), "--radius": f"{p.radius_px}px",
           "--font-sans": p.font, "--failed": p.failed_color.css()}
    for i, color in enumerate(p.speakers[:6], start=1):
        out[f"--speaker-{i}"] = color.css()
    return out


def page_tokens(sent: Optional[Palette]) -> dict:
    """What the page needs: the fallback for each system appearance, and what the Desktop pane sent."""
    return {"light": css_vars(FALLBACK[False]), "dark": css_vars(FALLBACK[True]),
            "sent": css_vars(sent) if sent is not None else None,
            "sent_mode": ("dark" if sent.dark else "light") if sent is not None else None}


def first_font(stack: str) -> str:
    """The first family of a CSS font stack, unquoted."""
    for part in stack.split(","):
        name = part.strip().strip("'\"")
        if name:
            return name
    return FALLBACK_FONT


__all__ = ["FALLBACK", "Palette", "Rgba", "ThemeError", "contrast", "palette_from_payload", "parse_color",
           "first_font", "palette_from_json", "palette_to_json"]
