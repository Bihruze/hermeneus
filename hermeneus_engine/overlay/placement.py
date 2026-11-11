"""Where the overlay sits on each display. No AppKit here.

Positions are stored per display as fractions of that display's visible
frame, so a resolution change keeps the strip in the same place. A display
also keeps the rows the user dragged the strip to (``Spot.rows``; None follows
the ``overlay_max_lines`` setting). The file is ``<data dir>/overlay/positions.json``.
It also holds the rows hand-back to the Desktop pane (``Positions.wanted``, see
``OverlayApp.resized``).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
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
    own look controls do.

    The process serving ``GET /overlay`` may settle ``lines`` and clear ``rows`` on disk while the overlay runs
    (``wanted_rows``). So a save merges three ways, under the lock: for ``seen``, ``wanted`` and each display's
    ``rows``, a value the overlay has not changed since it last read or wrote the file takes what is on disk; a
    value it changed is its own. The overlay never writes a settled hand-back back from an older copy."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.last = ""
        self.spots: dict[str, Spot] = {}
        self.seen: Optional[int] = None
        self.wanted: Optional[int] = None
        self._load()

    def _load(self) -> None:
        data = _read(self.path)
        if data:
            self.last = str(data.get("last") or "")
            self.spots = _spots(data)
            self.seen, self.wanted = _lines(data)
        self._base = self._shared()

    def _shared(self) -> tuple:
        """The values another process may change: ``seen``, ``wanted`` and the rows per display."""
        return self.seen, self.wanted, {k: s.rows for k, s in self.spots.items()}

    def _merge(self, data: dict) -> None:
        if not data:
            return
        seen, wanted, rows = self._base
        disk_seen, disk_wanted = _lines(data)
        disk_spots = _spots(data)
        self.seen = _take(self.seen, seen, disk_seen)
        self.wanted = _take(self.wanted, wanted, disk_wanted)
        for key, spot in self.spots.items():
            if key in disk_spots and key in rows:
                merged = _take(spot.rows, rows[key], disk_spots[key].rows)
                if merged != spot.rows:
                    self.spots[key] = replace(spot, rows=merged)

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

    def save(self) -> bool:
        """Write the file (the overlay process owns it), merged with what another process settled on disk. False
        when it could not be written, or the lock was not free in time: the overlay keeps drawing with what it
        has in memory and the next save writes it."""
        try:
            with locked(self.path):
                self._merge(_read(self.path))
                _write(self.path, self._body())
        except OSError:
            return False
        self._base = self._shared()
        return True

    def _body(self) -> dict:
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
        return body


def _take(ours, base, theirs):
    """Three-way merge of one value: the other process's change wins only where this one changed nothing."""
    return theirs if ours == base and theirs != base else ours


def _spots(data: dict) -> dict[str, Spot]:
    spots = {}
    for key, raw in (data.get("displays") or {}).items():
        if isinstance(raw, dict):
            try:
                rows = raw.get("rows")
                spots[str(key)] = Spot(_clamp(float(raw.get("cx", 0.5)), 0, 1),
                                       _clamp(float(raw.get("bottom", DEFAULT_BOTTOM)), 0, 1),
                                       _clamp(float(raw.get("width", DEFAULT_WIDTH)), 0.1, 1),
                                       None if rows is None or isinstance(rows, bool) else clamp_rows(rows))
            except (TypeError, ValueError):
                continue
    return spots


def lock_file(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + ".lock")


LOCK_WAIT_S = 0.05  # the longest a writer waits for the lock; the overlay's main thread never blocks longer


@contextlib.contextmanager
def locked(path: Path, wait: float = LOCK_WAIT_S):
    """An exclusive ``flock`` on ``positions.json.lock`` next to ``path``, held by every writer of the positions
    file (the overlay process and the process serving ``GET /overlay``) around its read-modify-write. Tries
    without blocking, with short retries for at most ``wait`` seconds, then raises ``TimeoutError`` (an
    ``OSError``): the caller skips that write instead of freezing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{lock_file(path)} is held by another process") from None
                time.sleep(0.002)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _write(path: Path, body: dict) -> None:
    """Atomic replace through a temp file of this writer's own (``.positions-*.tmp`` in the same folder)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(body, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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

    A drag is handed back only while the setting is still the value the overlay last saw (``lines.seen``). When
    the setting already holds the dragged rows, the hand-back is done. When the setting differs from both, the
    user picked rows in the pane or in Settings after the drag: that pick wins, as the overlay itself would
    apply it (``OverlayApp._config_arrived``): the drag is dropped and every display follows the setting. Either
    way it is settled here, under the lock, on a freshly read file, changing only ``lines`` and the displays'
    ``rows``, so it ends even while the overlay is closed and a pick back to the old value never revives it."""
    path = Path(path)
    if setting is None:
        return Positions(path).wanted
    if _lines(_read(path))[1] is None:  # nothing pending: no lock, no write (the pane polls every second)
        return None
    with contextlib.suppress(OSError), locked(path):
        data = _read(path)
        seen, wanted = _lines(data)
        if wanted is None:
            return None
        if wanted != setting and seen == setting:
            return wanted
        if wanted != setting:
            for entry in (data.get("displays") or {}).values():
                if isinstance(entry, dict):
                    entry.pop("rows", None)
        data["lines"] = {"seen": clamp_rows(setting)}
        _write(path, data)
    return None


def _lines(data: dict) -> tuple[Optional[int], Optional[int]]:
    lines = data.get("lines")
    if not isinstance(lines, dict):
        return None, None
    return _rows_or_none(lines.get("seen")), _rows_or_none(lines.get("wanted"))
