"""What the overlay shows: an event reducer and the line layout. No AppKit here.

``OverlayModel`` folds the session's events (docs/events.md) into the few
recent lines an overlay needs. ``compose`` turns them into styled runs: the
translation where there is one, the live original where there is not yet,
so a partial appears at once and its translation later takes the same place.
``wrap`` breaks the runs into at most ``max_lines`` lines of a given width and
keeps the newest ones, so a long passage rolls upwards like film subtitles.
"""

from __future__ import annotations

import math
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
MARK = "mark"  # "Not translated" before a failed line, in --ui-red as the pane marks it


@dataclass(frozen=True)
class Strings:
    """The words the overlay draws. The Desktop pane sends its own localised copy (usePluginI18n) with
    the theme; these English defaults are the pane's EN strings, for an overlay no pane has reached."""

    speaker: str = "Speaker {n}"
    not_translated: str = "Not translated"
    waiting: str = "Waiting for speech"
    reconnecting: str = "Reconnecting"

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


def speaker_label(number: int, strings: "Strings" = Strings()) -> str:
    return strings.label(number)


def segment(line: _Line) -> Run:
    """What a line shows. One rule for the strip and the 127.0.0.1 page: a "translated" line with an
    empty translation is shown as failed, its original with the "not translated" marker."""
    if line.state == "translated" and line.translation.strip():
        return Run(line.translation.strip(), TRANSLATED)
    if line.state in ("translated", "failed"):
        return Run(line.original.strip(), FAILED)
    return Run(line.original.strip(), PENDING)


def compose(model: OverlayModel, now: float, *, show_speaker: bool, show_original: bool,
            strings: Strings = Strings()) -> tuple[list[Run], str, str]:
    """(runs for the main block, original of the newest line, notice) for this moment."""
    lines = model.recent(now)
    if not lines:
        if model.state == "reconnecting":
            return [], "", model.message or strings.reconnecting
        if model.running and not model.seen_line:
            return [], "", strings.waiting
        return [], "", ""
    runs: list[Run] = []
    previous_speaker: Optional[str] = None
    for line in lines:
        seg = segment(line)
        if not seg.text:
            continue
        number = model.speakers.get(line.speaker_id) if line.speaker_id else None
        if show_speaker and number is not None and line.speaker_id != previous_speaker:
            runs.append(Run(strings.label(number), LABEL, number))
        previous_speaker = line.speaker_id
        if seg.kind == FAILED:
            runs.append(Run(strings.not_translated, MARK, number))
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
        # A speaker label or the "not translated" marker never breaks across rows.
        words = [run.text.strip() + " "] if run.kind in (LABEL, MARK) and run.text.strip() else _TOKEN.findall(run.text)
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
    tokens = _tokens(runs)
    tags_fit = True
    for index, (word, run) in enumerate(tokens):
        trimmed = word.rstrip()
        need = measure(trimmed, run)
        # A speaker label and a marker stay together, on the row of the first word they introduce.
        # When the three do not fit on one row, the label and marker keep a row and the text breaks.
        tag = run.kind in (LABEL, MARK)
        after_tag = index > 0 and tokens[index - 1][1].kind in (LABEL, MARK)
        breaking = need
        if tag and not after_tag:
            group, j = 0.0, index
            while j < len(tokens) and tokens[j][1].kind in (LABEL, MARK):
                group += measure(tokens[j][0], tokens[j][1])
                j += 1
            tags_only = group - (measure(tokens[j - 1][0], tokens[j - 1][1]) - measure(tokens[j - 1][0].rstrip(), tokens[j - 1][1]))
            with_word = group + (measure(tokens[j][0].rstrip(), tokens[j][1]) if j < len(tokens) else 0.0)
            breaking = with_word if with_word <= width else (tags_only if tags_only <= width else need)
            tags_fit = tags_only <= width
        elif tag and after_tag and tags_fit:
            breaking = 0.0  # placed with the label before it
        if lines[-1] and used + breaking > width:
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


def _turns(runs: list[Run]) -> list[list[Run]]:
    """Runs grouped per speaker turn. Each turn starts a row of its own, as film subtitles do."""
    turns: list[list[Run]] = []
    for run in runs:
        previous = turns[-1][-1] if turns else None
        new_turn = (previous is None or run.kind == LABEL
                    or (previous.kind not in (LABEL, MARK) and previous.speaker != run.speaker))
        if new_turn:
            turns.append([])
        turns[-1].append(run)
    return turns


def _segments(runs: list[Run]) -> list[list[Run]]:
    """Runs grouped per line: a speaker label stays with the text after it."""
    groups: list[list[Run]] = []
    pending: list[Run] = []
    for run in runs:
        if run.kind in (LABEL, MARK):
            pending.append(run)
            continue
        groups.append(pending + [run])
        pending = []
    return groups


def _rows(turn: list[Run], width: float, measure: Measure) -> int:
    return len(wrap(turn, width, measure, 99))


def _label_for(turn: list[Run], number: int) -> Run:
    """The turn's own label run (in whatever language compose wrote it), else the English default."""
    for run in turn:
        if run.kind == LABEL:
            return run
    return Run(speaker_label(number), LABEL, number)


def _fit_turn(turn: list[Run], width: float, measure: Measure, max_lines: int,
              show_speaker: bool) -> tuple[tuple[Run, ...], ...]:
    """One speaker turn in at most ``max_lines`` rows. Older lines of the turn leave whole; then the
    newest rolls by words, so the rows hold the newest words. Whatever start is shown carries its
    header again: the speaker label and, for a failed line, the "not translated" marker."""
    groups = _segments(turn)
    flat = lambda gs: [r for g in gs for r in g]  # noqa: E731
    while len(groups) > 1 and _rows(flat(groups), width, measure) > max_lines:
        groups.pop(0)
    speaker = next((r.speaker for r in turn if r.kind not in (LABEL, MARK)), None)
    label = _label_for(turn, speaker) if show_speaker and speaker is not None else None
    if label is not None and groups and groups[0][0].kind != LABEL:
        groups[0] = [label] + groups[0]
    if _rows(flat(groups), width, measure) <= max_lines:
        return wrap(flat(groups), width, measure, max_lines)
    # Roll by words. Each word knows its line's tags; the first word shown gets the label and its
    # line's marker in front, so a long failed line keeps "not translated" and the text stays on
    # the label's row (A3 final U18, U19).
    units: list[tuple[list[Run], list[Run], str, Run]] = []
    for group in groups:
        tags = [r for r in group if r.kind in (LABEL, MARK)]
        first = True
        for run in group:
            if run.kind in (LABEL, MARK):
                continue
            for word in run.text.split():
                units.append((tags if first else [], tags, word, run))
                first = False
    def build(k: int, use_label: bool, use_marker: bool) -> list[Run]:
        _, seg_tags, word, run = units[k]
        runs = ([label] if use_label and label is not None else [])
        runs += [t for t in seg_tags if t.kind == MARK] if use_marker else []
        runs.append(Run(word, run.kind, run.speaker))
        for own, _, later, later_run in units[k + 1:]:
            runs.extend(t for t in own if t.kind == MARK)
            runs.append(Run(later, later_run.kind, later_run.speaker))
        return runs

    # On a row too narrow for the whole header and a word, the label goes first, then the marker:
    # the newest words always fill the row.
    for use_label, use_marker in ((True, True), (False, True), (False, False)):
        for k in range(len(units)):
            attempt = build(k, use_label, use_marker)
            if _rows(attempt, width, measure) <= max_lines:
                return wrap(attempt, width, measure, max_lines)
    return wrap(build(len(units) - 1, False, False), width, measure, max_lines) if units else ()


def layout(runs: list[Run], width: float, measure: Measure, max_lines: int,
           show_speaker: bool) -> tuple[tuple[Run, ...], ...]:
    """The rows to show: whole older turns leave first, each turn on its own rows, newest at the bottom."""
    turns = _turns(runs)
    while len(turns) > 1 and sum(_rows(t, width, measure) for t in turns) > max_lines:
        turns.pop(0)
    rows: list[tuple[Run, ...]] = []
    for turn in turns:
        rows.extend(_fit_turn(turn, width, measure, max_lines, show_speaker))
    return tuple(rows[-max_lines:])


def build_view(model: OverlayModel, now: float, *, width: float, measure: Measure, max_lines: int,
               show_speaker: bool, show_original: bool, original_measure: Optional[Measure] = None,
               strings: Strings = Strings()) -> View:
    runs, original, notice = compose(model, now, show_speaker=show_speaker, show_original=show_original,
                                     strings=strings)
    lines = layout(runs, width, measure, max_lines, show_speaker) if runs else ()
    if original:
        share = _shown_share(runs[-1], lines)
        if share < 1.0:
            # The translation rolled: show the matching end of the original, cut at its front.
            words = original.split()
            keep = max(1, math.ceil(len(words) * share))
            tail = " ".join(words[-keep:])
            original = fit_tail(("\u2026" if keep < len(words) else "") + tail, width,
                                original_measure or measure, Run(original, PENDING))
        else:
            original = fit_head(original, width, original_measure or measure, Run(original, PENDING))
    return View(lines, original, notice)


def _shown_share(newest: Run, lines: tuple[tuple[Run, ...], ...]) -> float:
    """The share of the newest translation's words still on screen (1.0 when its start is shown)."""
    words = newest.text.split()
    shown = " ".join(r.text for row in lines for r in row if r.kind not in (LABEL, MARK)).split()
    if not words:
        return 1.0
    for k in range(len(words), 0, -1):  # the longest end of the translation that ends what is shown
        if shown[-k:] == words[-k:]:
            return k / len(words)
    return 1.0


def fit_tail(text: str, width: float, measure: Measure, run: Run) -> str:
    """``text`` on one line; when too wide, an ellipsis and as much of its end as fits, from a word start."""
    text = " ".join(text.split())
    if not text or measure(text, run) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # the smallest cut whose tail fits after the ellipsis
        mid = (lo + hi) // 2
        if measure("\u2026" + text[mid:].lstrip(), run) <= width:
            hi = mid
        else:
            lo = mid + 1
    tail = text[lo:]
    if lo > 0 and not text[lo - 1].isspace() and " " in tail:
        tail = tail.split(" ", 1)[1]
    return "\u2026" + tail.lstrip()
