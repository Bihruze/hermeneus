"""Hermes Desktop theme values for the native overlay.

The overlay window lives outside the Desktop renderer, so it cannot read CSS
variables. These are the values of the Desktop tokens, copied from Hermes
v0.23.1 (paths relative to ``apps/desktop/src`` in the hermes-agent checkout):

- Surfaces and text come from the default skin ``nous``
  (``themes/presets.ts:462`` names it; the colours are in
  ``apps/shared/src/theme-presets.ts:125-170``): ``popover`` and
  ``popoverForeground`` for light and dark.
- Secondary and tertiary text are the foreground at 74% and 54%
  (``styles.css:365-366``, ``--ui-text-secondary`` and ``--ui-text-tertiary``).
- Speaker colours are ``--ui-blue``, ``--ui-green``, ``--ui-orange``,
  ``--ui-purple``, ``--ui-cyan`` and ``--ui-yellow`` (``styles.css:243-249``,
  dark overrides ``styles.css:613-615``), in the order the Desktop pane uses
  (``desktop/plugin.js`` ``SPEAKER_TOKENS``). Dark blue is the skin's dark
  primary ``#4a84fe`` (``theme-presets.ts:163``). A line whose translation
  failed shows its original in secondary text.
- The radius is ``--radius-lg`` = ``--radius-scalar`` 0.2 x 0.75rem
  (``styles.css:158`` and ``styles.css:501``), 2.4 pt at 16 pt per rem.
- The hairline is ``--stroke-nous``, currentColor at 3% (``styles.css:194``),
  and the shadow follows ``--shadow-nous`` (``styles.css:187-191``).
- The font is the first face of ``--dt-font-sans`` that macOS has
  (``styles.css:476-478``): ``-apple-system``, which is the system font.

A user who picks another skin in Desktop still sees these default values in
the native overlay.
"""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class Palette:
    surface: Rgba
    text: Rgba
    text_secondary: Rgba
    text_tertiary: Rgba
    stroke: Rgba
    speakers: tuple[Rgba, ...]

    def speaker(self, number: int) -> Rgba:
        return self.speakers[(number - 1) % len(self.speakers)]


def _palette(surface: str, text: str, speakers: tuple[str, ...]) -> Palette:
    fg = Rgba.hex(text)
    return Palette(surface=Rgba.hex(surface), text=fg.with_alpha(0.94), text_secondary=fg.with_alpha(0.74),
                   text_tertiary=fg.with_alpha(0.54), stroke=fg.with_alpha(0.03 * 4),
                   speakers=tuple(Rgba.hex(s) for s in speakers))


# --ui-text-primary is the base at 94% (styles.css:364). The hairline is
# --stroke-nous (3%) drawn four times as strong, because the overlay sits on
# video instead of on the Desktop chrome; Seher decides this in review.
LIGHT = _palette("#ffffff", "#1f2328",
                 ("#0053fd", "#1f8a65", "#db704b", "#9e94d5", "#4c7f8c", "#c08532"))
DARK = _palette("#161b22", "#e6edf3",
                ("#4a84fe", "#55a583", "#db704b", "#9e94d5", "#6f9ba6", "#c08532"))

RADIUS_PT = 0.2 * 0.75 * 16
FONT_FAMILY = "-apple-system"
# Text at regular weight; speaker labels at 500, the weight the pane gives its
# status label (desktop/plugin.js, S.statusState).
WEIGHT_TEXT = 0.0  # NSFontWeightRegular
WEIGHT_LABEL = 0.23  # NSFontWeightMedium


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT
