"""What the overlay knows: an event reducer and the shapes it draws. No AppKit here.

``OverlayModel`` folds the session's events (docs/events.md) into the recent
lines an overlay needs. When those lines go on screen, and in which rows, is
``pace.py``: a display queue paced at reading speed and roll-up rows.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

KEEP_LINES = 40  # enough for the display queue to keep speech order (overlay/pace.py)

TRANSLATED = "translated"
PENDING = "pending"
FAILED = "failed"
LABEL = "label"
MARK = "mark"  # "Not translated" before a failed line, in --ui-red as the pane marks it


@dataclass(frozen=True)
class Strings:
    """The words the overlay draws. The Desktop pane sends its own localised copy (usePluginI18n) with
    the theme; these English defaults are the pane's EN strings, for an overlay no pane has reached."""

    speaker: str = "Speaker {n}"
    not_translated: str = "Not translated"
    waiting: str = "Waiting for speech"
    reconnecting: str = "Reconnecting"
    back_to_live: str = "Back to live"

    def label(self, number: int) -> str:
        return self.speaker.replace("{n}", str(number))

RUNNING = frozenset({"starting", "live", "reconnecting", "stopping"})


@dataclass(frozen=True)
class Run:
    text: str
    kind: str  # TRANSLATED, PENDING, FAILED or LABEL
    speaker: Optional[int] = None  # 1-based speaker number, for colour


@dataclass(frozen=True)
class View:
    lines: tuple[tuple[Run, ...], ...] = ()
    original: str = ""
    notice: str = ""  # a plain status line when there is nothing to show
    pending: bool = False  # speech heard whose translation is not on screen yet: a small neutral indicator
    history: bool = False  # the user scrolled back; the strip shows "back to live"
    newer: int = 0  # rows below the window (history only)
    older: bool = False  # rows above the window that the user can scroll back to

    @property
    def empty(self) -> bool:
        return not self.lines and not self.notice and not self.pending


@dataclass
class _Line:
    line_id: str
    state: str
    original: str
    translation: str
    speaker_id: str
    changed_at: float
    target: str = ""  # target_language of the line


@dataclass
class OverlayModel:
    keep: int = KEEP_LINES
    lines: "OrderedDict[str, _Line]" = field(default_factory=OrderedDict)
    speakers: dict[str, int] = field(default_factory=dict)
    last_seq: int = 0
    state: str = ""
    message: str = ""
    seen_line: bool = False
    changed_at: float = 0.0

    def apply(self, event: dict, now: float) -> bool:
        """Fold one event in. True when what the overlay shows may have changed."""
        kind = event.get("type")
        seq = event.get("seq")
        if kind == "reset":
            self.lines.clear()
            self.last_seq = 0
            return True
        if kind == "hello":
            body = event.get("session") or {}
            if isinstance(seq, int) and seq < self.last_seq:
                # A different owner process: its counter started again.
                self.lines.clear()
                self.last_seq = 0
            return self._status(body.get("state", ""), body.get("message", "")) or True
        if isinstance(seq, int) and kind in ("line", "relabel"):
            if seq <= self.last_seq:
                return False
            self.last_seq = seq
        if kind == "status":
            return self._status(event.get("state", ""), event.get("message", ""))
        if kind == "relabel":
            return self._relabel(str(event.get("from") or ""), str(event.get("to") or ""))
        if kind == "line":
            return self._line(event.get("line") or {}, now)
        return False

    def _status(self, state: str, message: str) -> bool:
        changed = (state, message) != (self.state, self.message)
        self.state, self.message = state, message
        return changed

    def _relabel(self, old: str, new: str) -> bool:
        if not old or not new or old == new:
            return False
        for line in self.lines.values():
            if line.speaker_id == old:
                line.speaker_id = new
        if new not in self.speakers and old in self.speakers:
            self.speakers[new] = self.speakers[old]
        self.speakers.pop(old, None)
        return True

    def _line(self, data: dict, now: float) -> bool:
        line_id = str(data.get("line_id") or "")
        if not line_id:
            return False
        speaker = str(data.get("speaker_id") or "")
        if speaker and speaker not in self.speakers:
            self.speakers[speaker] = len(self.speakers) + 1
        entry = _Line(line_id, str(data.get("state") or ""), str(data.get("original") or ""),
                      str(data.get("translation") or ""), speaker, now, str(data.get("target_language") or ""))
        old = self.lines.get(line_id)
        if old is not None and (old.state, old.original, old.translation, old.speaker_id) == \
                (entry.state, entry.original, entry.translation, entry.speaker_id):
            return False
        self.lines[line_id] = entry
        if old is None:
            while len(self.lines) > self.keep:
                self.lines.popitem(last=False)
        self.seen_line = True
        self.changed_at = now
        return True

    @property
    def running(self) -> bool:
        return self.state in RUNNING


# -- measuring -------------------------------------------------------------------

Measure = Callable[[str, Run], float]


def _split_long(word: str, run: Run, width: float, measure: Measure) -> list[str]:
    """Break a word wider than the line into pieces that fit."""
    pieces, current = [], ""
    for ch in word:
        if current and measure(current + ch, run) > width:
            pieces.append(current)
            current = ch
        else:
            current += ch
    if current:
        pieces.append(current)
    return pieces


def fit_head(text: str, width: float, measure: Measure, run: Run) -> str:
    """``text`` on one line; when too wide, its start up to a word boundary and an ellipsis.

    The original is cut at its end, never at its front, so the heard sentence
    always reads from its first word.
    """
    text = " ".join(text.split())
    if not text or measure(text, run) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # the longest head that fits with the ellipsis
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "\u2026", run) <= width:
            lo = mid
        else:
            hi = mid - 1
    head = text[:lo]
    if lo < len(text) and not text[lo].isspace() and " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "\u2026"
