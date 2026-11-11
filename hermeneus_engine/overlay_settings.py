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

TEXT_SIZE_MIN, TEXT_SIZE_MAX = 14, 64
MAX_LINES_MIN, MAX_LINES_MAX = 1, 3
OPACITY_MIN, OPACITY_MAX = 0, 100

DEFAULTS: dict[str, Any] = {
    KEY_AUTO: False,
    KEY_TEXT_SIZE: 26,
    KEY_MAX_LINES: 2,
    KEY_SHOW_ORIGINAL: True,
    KEY_OPACITY: 80,
    KEY_SHOW_SPEAKER: True,
}


@dataclass(frozen=True)
class OverlaySettings:
    auto: bool = DEFAULTS[KEY_AUTO]
    text_size: int = DEFAULTS[KEY_TEXT_SIZE]
    max_lines: int = DEFAULTS[KEY_MAX_LINES]
    show_original: bool = DEFAULTS[KEY_SHOW_ORIGINAL]
    opacity: int = DEFAULTS[KEY_OPACITY]
    show_speaker: bool = DEFAULTS[KEY_SHOW_SPEAKER]

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
    )
