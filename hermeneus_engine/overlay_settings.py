"""Shared settings for the subtitle overlays.

The native overlay window, the 127.0.0.1 overlay page and the Desktop pane
read the same plugin settings (``plugins.entries.hermeneus.settings``) under
the keys below. plugin.yaml declares each key in ``config_schema``; this
module is the one place that knows their defaults and limits.

``overlay`` opens the native overlay with every session. The other keys shape
what any overlay shows.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

KEY_AUTO = "overlay"
KEY_TEXT_SIZE = "overlay_text_size"
KEY_MAX_LINES = "overlay_max_lines"
KEY_SHOW_ORIGINAL = "overlay_show_original"
KEY_OPACITY = "overlay_opacity"
KEY_SHOW_SPEAKER = "overlay_show_speaker"
KEY_STYLE = "overlay_style"
KEY_PACE = "overlay_pace"
KEY_READING_CPS = "overlay_reading_cps"

TEXT_SIZE_MIN, TEXT_SIZE_MAX = 14, 64
MAX_LINES_MIN, MAX_LINES_MAX = 1, 6  # 6: the rows the strip can be dragged to (and the history view)
OPACITY_MIN, OPACITY_MAX = 0, 100
# "banner" (the default, owner decision W28) is overlay_tokens.BANNER; "theme" follows Hermes Desktop
# (overlay_tokens.FALLBACK or what the pane sent).
STYLES = ("theme", "banner")
# "read": every line keeps its reading time, the strip may fall behind fast speech. "live": while more than 3 s
# of reading waits, lines go up faster (down to half the reading time); nothing is dropped (overlay/pace.py).
PACES = ("read", "live")
READING_CPS_MIN, READING_CPS_MAX = 0, 30  # 0: the target language's reading speed (pace.READING_CPS)

DEFAULTS: dict[str, Any] = {
    KEY_AUTO: True,
    KEY_TEXT_SIZE: 26,
    KEY_MAX_LINES: 2,
    KEY_SHOW_ORIGINAL: False,
    KEY_OPACITY: 90,
    KEY_SHOW_SPEAKER: True,
    KEY_STYLE: "banner",
    KEY_PACE: "live",
    KEY_READING_CPS: 0,
}


@dataclass(frozen=True)
class OverlaySettings:
    auto: bool = DEFAULTS[KEY_AUTO]
    text_size: int = DEFAULTS[KEY_TEXT_SIZE]
    max_lines: int = DEFAULTS[KEY_MAX_LINES]
    show_original: bool = DEFAULTS[KEY_SHOW_ORIGINAL]
    opacity: int = DEFAULTS[KEY_OPACITY]
    show_speaker: bool = DEFAULTS[KEY_SHOW_SPEAKER]
    style: str = DEFAULTS[KEY_STYLE]
    pace: str = DEFAULTS[KEY_PACE]
    reading_cps: int = DEFAULTS[KEY_READING_CPS]

    @property
    def background_alpha(self) -> float:
        return self.opacity / 100.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "OverlaySettings":
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        return from_settings({_FIELD_TO_KEY[k]: v for k, v in data.items() if k in _FIELD_TO_KEY})


_FIELD_TO_KEY = {
    "auto": KEY_AUTO,
    "text_size": KEY_TEXT_SIZE,
    "max_lines": KEY_MAX_LINES,
    "show_original": KEY_SHOW_ORIGINAL,
    "opacity": KEY_OPACITY,
    "show_speaker": KEY_SHOW_SPEAKER,
    "style": KEY_STYLE,
    "pace": KEY_PACE,
    "reading_cps": KEY_READING_CPS,
}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    if isinstance(value, int):
        return value != 0
    return default


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _choice(value: Any, default: str, choices: tuple[str, ...]) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


def from_settings(settings: Mapping[str, Any]) -> OverlaySettings:
    """Overlay settings from the plugin settings mapping, every value checked and clamped."""
    get = settings.get
    return OverlaySettings(
        auto=_bool(get(KEY_AUTO), DEFAULTS[KEY_AUTO]),
        text_size=_int(get(KEY_TEXT_SIZE), DEFAULTS[KEY_TEXT_SIZE], TEXT_SIZE_MIN, TEXT_SIZE_MAX),
        max_lines=_int(get(KEY_MAX_LINES), DEFAULTS[KEY_MAX_LINES], MAX_LINES_MIN, MAX_LINES_MAX),
        show_original=_bool(get(KEY_SHOW_ORIGINAL), DEFAULTS[KEY_SHOW_ORIGINAL]),
        opacity=_int(get(KEY_OPACITY), DEFAULTS[KEY_OPACITY], OPACITY_MIN, OPACITY_MAX),
        show_speaker=_bool(get(KEY_SHOW_SPEAKER), DEFAULTS[KEY_SHOW_SPEAKER]),
        style=_choice(get(KEY_STYLE), DEFAULTS[KEY_STYLE], STYLES),
        pace=_choice(get(KEY_PACE), DEFAULTS[KEY_PACE], PACES),
        reading_cps=_reading_cps(get(KEY_READING_CPS)),
    )


def _reading_cps(value: Any) -> int:
    """0 (by language) or a speed from 5 to 30 characters per second."""
    number = _int(value, DEFAULTS[KEY_READING_CPS], READING_CPS_MIN, READING_CPS_MAX)
    return 0 if number == 0 else max(5, number)
