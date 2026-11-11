"""What the overlay shows: an event reducer and the line layout. No AppKit here.

``OverlayModel`` folds the session's events (docs/events.md) into the few
recent lines an overlay needs. ``compose`` turns them into styled runs: the
translation where there is one, the live original where there is not yet,
so a partial appears at once and its translation later takes the same place.
``wrap`` breaks the runs into at most ``max_lines`` lines of a given width and
keeps the newest ones, so a long passage rolls upwards like film subtitles.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

KEEP_LINES = 8
# A finished line stays up this long after the last change, then the strip clears.
HOLD_S = 7.0

TRANSLATED = "translated"
PENDING = "pending"
FAILED = "failed"
LABEL = "label"

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

    @property
    def empty(self) -> bool:
        return not self.lines and not self.notice


@dataclass
class _Line:
    line_id: str
    state: str
    original: str
    translation: str
    speaker_id: str
    changed_at: float


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
                      str(data.get("translation") or ""), speaker, now)
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

    def recent(self, now: float, hold_s: float = HOLD_S) -> list[_Line]:
        """Lines with text, oldest first; none once the newest has been quiet for ``hold_s``."""
        if not self.lines or now - self.changed_at > hold_s:
            return []
        return [line for line in self.lines.values() if line.original.strip() or line.translation.strip()]


def speaker_label(number: int) -> str:
    return f"Speaker {number}"


def segment(line: _Line) -> Run:
    if line.state == "translated" and line.translation.strip():
        return Run(line.translation.strip(), TRANSLATED)
    if line.state in ("translated", "failed"):
        return Run(line.original.strip(), FAILED)
    return Run(line.original.strip(), PENDING)


def compose(model: OverlayModel, now: float, *, show_speaker: bool, show_original: bool,
            notice_waiting: str = "Waiting for speech", notice_reconnecting: str = "Reconnecting") -> tuple[list[Run], str, str]:
    """(runs for the main block, original of the newest line, notice) for this moment."""
    lines = model.recent(now)
    if not lines:
        if model.state == "reconnecting":
            return [], "", model.message or notice_reconnecting
        if model.running and not model.seen_line:
            return [], "", notice_waiting
        return [], "", ""
    runs: list[Run] = []
    previous_speaker: Optional[str] = None
    for line in lines:
        seg = segment(line)
        if not seg.text:
            continue
        number = model.speakers.get(line.speaker_id) if line.speaker_id else None
        if show_speaker and number is not None and line.speaker_id != previous_speaker:
            runs.append(Run(speaker_label(number), LABEL, number))
        previous_speaker = line.speaker_id
        runs.append(Run(seg.text, seg.kind, number))
    newest = lines[-1]
    original = newest.original.strip() if show_original and segment(newest).kind == TRANSLATED else ""
    return runs, original, ""


# -- layout -----------------------------------------------------------------------

Measure = Callable[[str, Run], float]
_TOKEN = re.compile(r"\S+\s*")


def _tokens(runs: list[Run]) -> list[tuple[str, Run]]:
    out: list[tuple[str, Run]] = []
    for index, run in enumerate(runs):
        words = _TOKEN.findall(run.text)
        for i, word in enumerate(words):
            if i == len(words) - 1 and index < len(runs) - 1 and not word.endswith(" "):
                word += " "
            out.append((word, run))
    return out


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


def _merge(tokens: list[tuple[str, Run]]) -> tuple[Run, ...]:
    merged: list[Run] = []
    for text, run in tokens:
        if merged and merged[-1].kind == run.kind and merged[-1].speaker == run.speaker:
            merged[-1] = Run(merged[-1].text + text, run.kind, run.speaker)
        else:
            merged.append(Run(text, run.kind, run.speaker))
    if merged:
        last = merged[-1]
        merged[-1] = Run(last.text.rstrip(), last.kind, last.speaker)
    return tuple(r for r in merged if r.text)


def wrap(runs: list[Run], width: float, measure: Measure, max_lines: int) -> tuple[tuple[Run, ...], ...]:
    """Greedy word wrap of ``runs`` into lines no wider than ``width``; the last ``max_lines`` lines."""
    if width <= 0 or max_lines <= 0:
        return ()
    lines: list[list[tuple[str, Run]]] = [[]]
    used = 0.0
    for word, run in _tokens(runs):
        trimmed = word.rstrip()
        need = measure(trimmed, run)
        if lines[-1] and used + need > width:
            lines.append([])
            used = 0.0
        if need > width:
            for piece in _split_long(trimmed, run, width, measure):
                if lines[-1]:
                    lines.append([])
                lines[-1].append((piece, run))
                used = measure(piece, run)
            if word != trimmed:
                lines[-1][-1] = (lines[-1][-1][0] + word[len(trimmed):], run)
                used += measure(word[len(trimmed):], run)
            continue
        lines[-1].append((word, run))
        used += measure(word, run)
    kept = [line for line in lines if line][-max_lines:]
    return tuple(_merge(line) for line in kept)


def fit_tail(text: str, width: float, measure: Measure, run: Run) -> str:
    """``text`` on one line; when too wide, an ellipsis and as much of its end as fits."""
    text = " ".join(text.split())
    if not text or measure(text, run) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # smallest cut whose tail fits
        mid = (lo + hi) // 2
        if measure("…" + text[mid:].lstrip(), run) <= width:
            hi = mid
        else:
            lo = mid + 1
    tail = text[lo:]
    if lo > 0 and " " in tail and not text[lo - 1].isspace():
        tail = tail.split(" ", 1)[1]
    return "…" + tail.lstrip()


def _segments(runs: list[Run]) -> list[list[Run]]:
    """Runs grouped per line: a speaker label stays with the text after it."""
    groups: list[list[Run]] = []
    pending_label: list[Run] = []
    for run in runs:
        if run.kind == LABEL:
            pending_label = [run]
            continue
        groups.append(pending_label + [run])
        pending_label = []
    return groups


def _drop_whole_segments(runs: list[Run], width: float, measure: Measure, max_lines: int,
                         show_speaker: bool) -> list[Run]:
    """Older lines leave whole, oldest first, until the rest fits in ``max_lines``.

    Only the newest line is ever cut mid-sentence, when it alone is longer
    than ``max_lines``; that is the roll ``wrap`` does.
    """
    groups = _segments(runs)
    while len(groups) > 1 and len(wrap([r for g in groups for r in g], width, measure, 99)) > max_lines:
        groups.pop(0)
        first = groups[0]
        if show_speaker and first[0].kind != LABEL and first[0].speaker is not None:
            groups[0] = [Run(speaker_label(first[0].speaker), LABEL, first[0].speaker)] + first
    return [r for g in groups for r in g]


def build_view(model: OverlayModel, now: float, *, width: float, measure: Measure, max_lines: int,
               show_speaker: bool, show_original: bool, original_measure: Optional[Measure] = None) -> View:
    runs, original, notice = compose(model, now, show_speaker=show_speaker, show_original=show_original)
    runs = _drop_whole_segments(runs, width, measure, max_lines, show_speaker)
    lines = wrap(runs, width, measure, max_lines) if runs else ()
    if show_speaker and lines and lines[0][0].kind != LABEL and lines[0][0].speaker is not None:
        # The speaker's label rolled off the top: name the speaker again at the start of what shows.
        # Words leave from the front until the label and the rest fit in max_lines.
        number = lines[0][0].speaker
        label = Run(speaker_label(number), LABEL, number)
        rest = [run for line in lines for run in line]
        while rest:
            lines = wrap([label] + rest, width, measure, max_lines + 1)
            if len(lines) <= max_lines:
                break
            _, _, tail = rest[0].text.lstrip().partition(" ")
            rest = ([Run(tail, rest[0].kind, rest[0].speaker)] if tail.strip() else []) + rest[1:]
        lines = lines[-max_lines:]
    if original:
        original = fit_tail(original, width, original_measure or measure, Run(original, PENDING))
    return View(lines, original, notice)
