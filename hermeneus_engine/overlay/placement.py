"""Where the overlay sits on each display. No AppKit here.

Positions are stored per display as fractions of that display's visible
frame, so a resolution change keeps the strip in the same place. The file is
``<data dir>/overlay/positions.json``; nothing else is stored in it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WIDTH_PRESETS = {"narrow": 0.45, "medium": 0.6, "wide": 0.8}
DEFAULT_WIDTH = WIDTH_PRESETS["medium"]
DEFAULT_BOTTOM = 0.08  # strip bottom edge, as a fraction of the visible height
MIN_WIDTH_PT = 320.0


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


@dataclass(frozen=True)
class Display:
    key: str  # stable name for the display, see display_key
    frame: Rect  # visible frame (menu bar and Dock removed), AppKit coordinates


@dataclass(frozen=True)
class Spot:
    cx: float = 0.5  # strip centre x, fraction of the visible width
    bottom: float = DEFAULT_BOTTOM  # strip bottom, fraction of the visible height
    width: float = DEFAULT_WIDTH  # strip width, fraction of the visible width


def display_key(number: int, name: str) -> str:
    return f"{number}:{name}"


def frame_for(spot: Spot, display: Display, height: float) -> Rect:
    """The strip's frame on ``display``, kept fully inside its visible frame."""
    f = display.frame
    width = max(min(spot.width * f.w, f.w), min(MIN_WIDTH_PT, f.w))
    x = f.x + spot.cx * f.w - width / 2
    y = f.y + spot.bottom * f.h
    x = min(max(x, f.x), f.x + f.w - width)
    y = min(max(y, f.y), f.y + f.h - height)
    return Rect(x, y, width, height)


def spot_for(frame: Rect, display: Display) -> Spot:
    """The stored form of a strip frame on ``display``."""
    f = display.frame
    cx = (frame.x + frame.w / 2 - f.x) / f.w if f.w else 0.5
    bottom = (frame.y - f.y) / f.h if f.h else DEFAULT_BOTTOM
    width = frame.w / f.w if f.w else DEFAULT_WIDTH
    return Spot(_clamp(cx, 0.0, 1.0), _clamp(bottom, 0.0, 1.0), _clamp(width, 0.1, 1.0))


def display_at(frame: Rect, displays: list[Display]) -> Optional[Display]:
    cx, cy = frame.center
    for display in displays:
        if display.frame.contains(cx, cy):
            return display
    return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Positions:
    """The positions file: ``{"last": <display key>, "displays": {<key>: {cx, bottom, width}}}``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.last = ""
        self.spots: dict[str, Spot] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.last = str(data.get("last") or "")
        for key, raw in (data.get("displays") or {}).items():
            if isinstance(raw, dict):
                try:
                    self.spots[str(key)] = Spot(_clamp(float(raw.get("cx", 0.5)), 0, 1),
                                                _clamp(float(raw.get("bottom", DEFAULT_BOTTOM)), 0, 1),
                                                _clamp(float(raw.get("width", DEFAULT_WIDTH)), 0.1, 1))
                except (TypeError, ValueError):
                    continue

    def spot(self, key: str) -> Spot:
        return self.spots.get(key, Spot())

    def choose(self, displays: list[Display], fallback: Display) -> Display:
        """The display the strip last sat on when it is attached, else ``fallback``."""
        for display in displays:
            if display.key == self.last:
                return display
        return fallback

    def remember(self, key: str, spot: Spot) -> None:
        self.last = key
        self.spots[key] = spot
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"last": self.last,
                "displays": {k: {"cx": round(s.cx, 4), "bottom": round(s.bottom, 4), "width": round(s.width, 4)}
                             for k, s in self.spots.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
