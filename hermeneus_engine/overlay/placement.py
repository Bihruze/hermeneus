"""Where the overlay sits on each display. No AppKit here.

Positions are stored per display as fractions of that display's visible
frame, so a resolution change keeps the strip in the same place. A display
also keeps the rows the user dragged the strip to (``Spot.rows``; None follows
the ``overlay_max_lines`` setting). The file is ``<data dir>/overlay/positions.json``.
It also holds the rows hand-back to the Desktop pane (``Positions.wanted``, see
``OverlayApp.resized``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from ..overlay_settings import MAX_LINES_MAX, MAX_LINES_MIN

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
    rows: Optional[int] = None  # rows the user dragged the strip to on this display; None: the setting


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


def clamp_rows(rows) -> int:
    return int(_clamp(int(rows), MAX_LINES_MIN, MAX_LINES_MAX))


def resize_frame(start: Rect, edge: str, dx: float, dy: float, bounds: Rect, min_h: float, max_h: float) -> Rect:
    """The strip while one of its edges is dragged by ``(dx, dy)`` from ``start``.

    ``edge`` is "right" (width), "top" (height) or "corner" (the top right corner: both). The bottom edge
    stays where it is, so the strip grows upward as it does with its rows, and the left edge stays unless the
    strip would leave ``bounds`` (the display's visible frame): then it grows leftward or downward instead.
    Width is ``MIN_WIDTH_PT`` to the whole visible width; height is ``min_h`` to ``max_h`` and never taller
    than ``bounds``."""
    w, h = start.w, start.h
    if edge in ("right", "corner"):
        w = _clamp(start.w + dx, min(MIN_WIDTH_PT, bounds.w), bounds.w)
    if edge in ("top", "corner"):
        h = _clamp(start.h + dy, min(min_h, bounds.h), min(max_h, bounds.h))
    x = _clamp(start.x, bounds.x, bounds.x + bounds.w - w)
    y = _clamp(start.y, bounds.y, bounds.y + bounds.h - h)
    return Rect(x, y, w, h)


def snap_rows(height: float, height_for: Callable[[int], float]) -> int:
    """The whole number of rows (1 to 6) whose strip height is nearest ``height``, so text is never cut."""
    rows = range(MAX_LINES_MIN, MAX_LINES_MAX + 1)
    return min(rows, key=lambda n: (abs(height_for(n) - height), n))


def spot_for(frame: Rect, display: Display, rows: Optional[int] = None) -> Spot:
    """The stored form of a strip frame on ``display``."""
    f = display.frame
    cx = (frame.x + frame.w / 2 - f.x) / f.w if f.w else 0.5
    bottom = (frame.y - f.y) / f.h if f.h else DEFAULT_BOTTOM
    width = frame.w / f.w if f.w else DEFAULT_WIDTH
    return Spot(_clamp(cx, 0.0, 1.0), _clamp(bottom, 0.0, 1.0), _clamp(width, 0.1, 1.0),
                None if rows is None else clamp_rows(rows))


def display_at(frame: Rect, displays: list[Display]) -> Optional[Display]:
    cx, cy = frame.center
    for display in displays:
        if display.frame.contains(cx, cy):
            return display
    return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Positions:
    """The positions file: ``{"last": <display key>, "displays": {<key>: {cx, bottom, width, rows?}},
    "lines": {"seen": <int>, "wanted": <int>}}``.

    ``lines.seen`` is the last ``overlay_max_lines`` value the overlay was handed, so a change made while it was
    closed is recognised. ``lines.wanted`` is the rows the user last dragged the strip to, until that value comes
    back as the setting: the Desktop pane reads it (``GET /overlay`` answers ``resized``) and saves it the way its
    own look controls do."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.last = ""
        self.spots: dict[str, Spot] = {}
        self.seen: Optional[int] = None
        self.wanted: Optional[int] = None
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
                    rows = raw.get("rows")
                    self.spots[str(key)] = Spot(_clamp(float(raw.get("cx", 0.5)), 0, 1),
                                                _clamp(float(raw.get("bottom", DEFAULT_BOTTOM)), 0, 1),
                                                _clamp(float(raw.get("width", DEFAULT_WIDTH)), 0.1, 1),
                                                None if rows is None or isinstance(rows, bool) else clamp_rows(rows))
                except (TypeError, ValueError):
                    continue
        lines = data.get("lines")
        if isinstance(lines, dict):
            self.seen, self.wanted = _rows_or_none(lines.get("seen")), _rows_or_none(lines.get("wanted"))

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

    def set_rows(self, key: str, rows: Optional[int]) -> None:
        """Rows for one display, keeping its place."""
        self.spots[key] = replace(self.spot(key), rows=None if rows is None else clamp_rows(rows))
        self.save()

    def clear_rows(self) -> None:
        """Every display follows the setting again (a rows change from the pane or the config)."""
        if any(s.rows is not None for s in self.spots.values()):
            self.spots = {k: replace(s, rows=None) for k, s in self.spots.items()}
            self.save()

    def set_lines(self, *, seen: Optional[int] = None, wanted: Optional[int] = None, clear_wanted: bool = False) -> None:
        if seen is not None:
            self.seen = clamp_rows(seen)
        if wanted is not None:
            self.wanted = clamp_rows(wanted)
        if clear_wanted:
            self.wanted = None
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        displays = {}
        for k, s in self.spots.items():
            entry = {"cx": round(s.cx, 4), "bottom": round(s.bottom, 4), "width": round(s.width, 4)}
            if s.rows is not None:
                entry["rows"] = s.rows
            displays[k] = entry
        body: dict = {"last": self.last, "displays": displays}
        lines = {k: v for k, v in (("seen", self.seen), ("wanted", self.wanted)) if v is not None}
        if lines:
            body["lines"] = lines
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def _rows_or_none(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return clamp_rows(float(value))
    except (TypeError, ValueError):
        return None


def wanted_rows(path: Path, setting: Optional[int] = None) -> Optional[int]:
    """The rows the user dragged the overlay to and the setting does not hold yet, or None. Read by the plugin's
    process (``OverlayService.status``) with the current ``overlay_max_lines`` as ``setting``.

    A drag is handed back only while the setting is still the value the overlay last saw (``lines.seen``): once
    the user picked rows in the pane or in Settings, that pick wins and a stale drag is never offered again. When
    the setting already holds the dragged rows, the hand-back is done here too (``wanted`` cleared, ``seen`` set),
    so it ends even while the overlay is closed."""
    p = Positions(path)
    if p.wanted is None or setting is None:
        return p.wanted if setting is None else None
    if p.wanted == setting:
        p.set_lines(seen=setting, clear_wanted=True)
        return None
    return p.wanted if p.seen == setting else None
