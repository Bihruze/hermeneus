"""When subtitle text goes on screen: a display queue paced at reading speed, and the roll-up rows.

No AppKit here. The rules come from established subtitle practice (sources and
numbers in the README section "Subtitle pace"):

- A sentence enters the queue when its translation is ready; the source text is
  never shown unless ``show_original`` is on. Lines enter in speech order.
- The queue shows one paced unit at a time: a sentence that fits the strip's
  rows (at most ``UNIT_CHARS``, a two-line subtitle), or phrases of a longer
  sentence split after punctuation or before a conjunction, never inside a
  word. A unit enters whole: it starts a new row when it does not fit the rest
  of the bottom row. Each unit stays ``max(MIN_S, characters / cps)`` before
  the next one enters, with ``cps`` the reading speed of the target language
  (Netflix: Turkish 17, English 20).
- Shown text rolls up: it is laid out in rows, new rows enter at the bottom
  and older rows move up and out. Nothing is dropped or shortened; rows that
  left the strip stay in the history the user can scroll back to.
- ``PACE_READ`` ("read everything") always keeps the reading time, so the
  strip may fall behind fast speech. ``PACE_LIVE`` ("follow live") shortens the
  reading time, down to half, while more than ``LIVE_BACKLOG_S`` of text waits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .model import FAILED, LABEL, MARK, TRANSLATED, Measure, OverlayModel, Run, Strings, _split_long

# -- reading speed -----------------------------------------------------------------

# Characters per second for adult programmes. Netflix Timed Text Style Guides: Turkish I.17 (17),
# English (USA) I.14 and II.17 (20), 17 for most other languages, Traditional Chinese 9; ja and ko are
# character-based targets with their own, lower, limits.
READING_CPS = {"tr": 17.0, "en": 20.0, "zh": 9.0, "ja": 4.0, "ko": 12.0}
DEFAULT_CPS = 17.0
CPS_MIN, CPS_MAX = 5, 30

MIN_S = 1.0  # minimum time per unit (Netflix 5/6 s; DCMP 40 frames, 1.33 s at 30 fps; BBC 0.3 s per word)
CHUNK_CHARS = 42  # Netflix characters per line: the unit size when the strip's width is not known
UNIT_CHARS = 84  # at most a two-line subtitle (2 x 42) goes up as one unit

HOLD_S = 7.0  # the strip clears this long after the last unit went up, once none waits (Netflix: 7 s per event)
ORDER_WAIT_S = 6.0  # a later translation waits this long at most for an earlier line still being translated
LIVE_BACKLOG_S = 3.0  # "follow live" speeds up while more reading time than this waits (Ofcom's 3 s)
LIVE_MAX_SPEEDUP = 2.0  # ... but never to less than half the reading time
HISTORY_UNITS = 600  # units kept for the history scroll

PACE_READ = "read"
PACE_LIVE = "live"
PACES = (PACE_READ, PACE_LIVE)


def reading_cps(language: Optional[str], override: int = 0) -> float:
    """Reading speed for a target language (ISO 639-1, any region suffix ignored); ``override`` when set."""
    if override:
        return float(max(CPS_MIN, min(CPS_MAX, override)))
    base = (language or "").strip().lower().replace("_", "-").split("-")[0]
    return READING_CPS.get(base, DEFAULT_CPS)


def reading_time(text: str, cps: float, min_s: float = MIN_S) -> float:
    """Seconds a unit needs: its characters (spaces and punctuation count, as Netflix counts) at ``cps``."""
    return max(min_s, len(" ".join(text.split())) / cps)


# -- phrase boundaries -------------------------------------------------------------

# A row or a unit may end after these (Netflix, BBC 3.4: after punctuation).
_ENDS_PHRASE = re.compile(r"[,;:.!?\u2026\u2014\u2013)\]\"\u201d\u00bb]$")
# A break may come before these words (Netflix: before conjunctions and prepositions). Turkish
# clitics (de, da, ki, mi) follow their word and are never listed.
_BREAK_BEFORE = frozenset("""
and but or nor so yet because although though while whereas which who whom whose where when if unless
until since than that as after before about with without from into for of on in at to by between during
ve ama fakat ancak lakin oysa halbuki veya ya yahut yoksa çünkü zira eğer
et mais ou donc car que qui dont où quand si y pero porque aunque und aber oder weil dass wenn als
e ma perché quando
""".split())
# Turkish postpositions close their phrase, so the break comes after them ("haziran sonuna kadar | ...").
_BREAK_AFTER = frozenset("gibi kadar için sonra önce rağmen karşın göre beri dolayı doğru itibaren".split())


def _bare(word: str) -> str:
    return word.strip("\"'“”«»()").lower()


def break_before(prev_word: str, word: str) -> bool:
    """True when a line may break between ``prev_word`` and ``word`` at a phrase boundary."""
    if not prev_word or not word:
        return False
    if _ENDS_PHRASE.search(prev_word):
        return True
    return _bare(word) in _BREAK_BEFORE or _bare(prev_word) in _BREAK_AFTER


def split_phrases(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """``text`` in units of at most ``limit`` characters, broken only between words.

    A text longer than ``limit`` is cut into the fewest units that fit, of about even length (no
    one-word leftover, Netflix: "Avoid having one short word"), each cut at the phrase boundary
    nearest that length (after punctuation, before a conjunction), otherwise at the word nearest it.
    A single word longer than ``limit`` is a unit of its own, never cut. Joining the units with
    spaces gives the text back word for word.
    """
    words = text.split()
    units: list[str] = []
    total = max(1, -(-len(" ".join(words)) // limit))
    while words:
        rest = " ".join(words)
        if len(rest) <= limit:
            units.append(rest)
            break
        parts = max(total - len(units), -(-len(rest) // limit))
        target = len(rest) / parts
        best, best_score = 1, None
        for k in range(1, len(words)):
            size = len(" ".join(words[:k]))
            if size > limit:
                break
            score = abs(size - target) - (limit * 0.4 if break_before(words[k - 1], words[k]) else 0.0)
            if best_score is None or score < best_score:
                best, best_score = k, score
        units.append(" ".join(words[:best]))
        words = words[best:]
    return units


# -- the queue ---------------------------------------------------------------------


@dataclass
class Unit:
    line_id: str
    index: int  # the unit's place in its line
    text: str  # translation (or, for a failed line with show_original, its original)
    kind: str  # TRANSLATED or FAILED
    speaker_id: str
    ready_at: float  # when the translation arrived (or the line failed)
    first: bool  # the unit starts its sentence
    language: str = ""  # the line's target language, for its reading speed
    original: str = ""  # the heard sentence, for the optional original line
    shown_at: Optional[float] = None
    dwell: float = 0.0  # how long it was given before the next unit could enter


@dataclass
class _Entry:
    signature: tuple
    units: list[Unit]


def _final(line) -> bool:
    return line.state in ("translated", "failed")


def _units_for(line, now: float, show_original: bool, limit: int) -> list[Unit]:
    translation = line.translation.strip()
    if line.state == "translated" and translation:
        parts, kind = split_phrases(translation, limit), TRANSLATED
    elif line.original.strip() or line.state == "failed":
        # Not translated: the marker alone, or with its original when the user asked for originals.
        parts = split_phrases(line.original.strip(), limit) if show_original and line.original.strip() else [""]
        kind = FAILED
    else:
        return []  # Scribe dropped the words: nothing to show
    return [Unit(line.line_id, i, part, kind, line.speaker_id, now, i == 0, line.target, line.original.strip())
            for i, part in enumerate(parts)]


@dataclass
class PaceQueue:
    """Units in speech order with the time each one went on screen."""

    pace: str = PACE_READ
    min_s: float = MIN_S
    limit: int = CHUNK_CHARS
    units: list[Unit] = field(default_factory=list)
    entries: dict[str, _Entry] = field(default_factory=dict)
    free_at: float = 0.0  # when the newest shown unit has had its reading time
    first_seen: dict[str, float] = field(default_factory=dict)
    version: int = 0  # bumps when shown text changes

    def clear(self) -> None:
        self.units.clear()
        self.entries.clear()
        self.first_seen.clear()
        self.free_at = 0.0
        self.version += 1

    # -- input ------------------------------------------------------------------

    def sync(self, model: OverlayModel, now: float, show_original: bool = False) -> None:
        """Take the lines whose translation is ready, in speech order, from the overlay model."""
        blocked = False
        for line in list(model.lines.values()):
            self.first_seen.setdefault(line.line_id, min(now, line.changed_at))
            if line.state == "partial":
                continue  # the words still being spoken; a sentence released from it comes as its own line
            if not _final(line):
                # Still being translated: later lines wait for it, but not for ever.
                if now - self.first_seen[line.line_id] < ORDER_WAIT_S:
                    blocked = True
                continue
            signature = (line.state, line.translation.strip(), line.original.strip() if show_original else "",
                         line.speaker_id)
            entry = self.entries.get(line.line_id)
            if entry is not None:
                if entry.signature != signature:
                    self._replace(entry, line, signature, show_original)
                continue
            if blocked:
                continue
            units = _units_for(line, now, show_original, self.limit)
            self.entries[line.line_id] = _Entry(signature, units)
            self.units.extend(units)
        if len(self.units) > HISTORY_UNITS:
            gone = self.units[: len(self.units) - HISTORY_UNITS]
            del self.units[: len(gone)]
            for line_id in {u.line_id for u in gone}:
                if not any(u.line_id == line_id for u in self.units):
                    self.entries.pop(line_id, None)
        for line_id in [k for k in self.first_seen if k not in model.lines and k not in self.entries]:
            self.first_seen.pop(line_id, None)

    def _replace(self, entry: _Entry, line, signature: tuple, show_original: bool) -> None:
        """A line changed after it was queued (its speaker, rarely its text): keep its place and what was shown."""
        old = entry.units
        ready = old[0].ready_at if old else 0.0
        new = _units_for(line, ready, show_original, self.limit) or []
        for i, unit in enumerate(new):
            if i < len(old) and old[i].shown_at is not None:
                unit.shown_at, unit.dwell = old[i].shown_at, old[i].dwell
        at = next((i for i, u in enumerate(self.units) if u.line_id == line.line_id), len(self.units))
        self.units = [u for u in self.units if u.line_id != line.line_id]
        self.units[at:at] = new
        entry.units, entry.signature = new, signature
        self.version += 1

    # -- time -------------------------------------------------------------------

    def waiting(self) -> list[Unit]:
        return [u for u in self.units if u.shown_at is None]

    def backlog_s(self, cps: float) -> float:
        return sum(reading_time(u.text or "x" * 8, cps, self.min_s) for u in self.waiting())

    def advance(self, now: float, cps: Callable[[Unit], float]) -> bool:
        """Put every unit whose turn has come on screen. True when something new is shown."""
        changed = False
        while True:
            waiting = self.waiting()
            if not waiting:
                break
            unit = waiting[0]
            start = max(unit.ready_at, self.free_at)
            if start > now + 1e-9:
                break
            if now - start > 0.5:
                start = now  # the clock jumped (sleep, a stalled main thread): no burst of stale units
            speed = cps(unit)
            dwell = reading_time(unit.text or "x" * 8, speed, self.min_s)  # a bare marker gets the minimum
            if self.pace == PACE_LIVE:
                backlog = sum(reading_time(u.text or "x" * 8, cps(u), self.min_s) for u in waiting)
                if backlog > LIVE_BACKLOG_S:
                    # Faster, but never below half the reading time nor the minimum duration.
                    dwell = max(self.min_s, dwell / LIVE_MAX_SPEEDUP, dwell * LIVE_BACKLOG_S / backlog)
            unit.shown_at, unit.dwell = start, dwell
            self.free_at = start + dwell
            changed = True
        if changed:
            self.version += 1
        return changed

    def next_due(self) -> Optional[float]:
        waiting = self.waiting()
        return max(waiting[0].ready_at, self.free_at) if waiting else None

    def shown(self) -> list[Unit]:
        return [u for u in self.units if u.shown_at is not None]

    def quiet_since(self) -> Optional[float]:
        """When the newest shown unit's reading time ended; None while units wait."""
        if self.waiting() or not self.shown():
            return None
        return self.free_at


# -- roll-up rows ------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    runs: tuple[Run, ...]
    continues: bool  # the row starts inside a sentence that began on an earlier row


@dataclass
class _Paragraph:
    speaker_id: str
    units: list[Unit]
    label: bool  # the first row carries the speaker label
    starts: list[bool] = field(default_factory=list)  # per unit: it starts a sentence


_SENTENCE_END = re.compile(r"[.!?\u2026:\"\u201d\u00bb)\u3002\uff01\uff1f]$")
_FIRST = re.compile(r"[^\W_]")  # the first letter or digit


def continues_sentence(before: str, text: str) -> bool:
    """True when ``text`` starts inside a sentence (Netflix Turkish I.5 then wants "\u2026" on it).

    Its first letter or digit decides. A lowercase letter means mid-sentence: the session cut one
    sentence into two lines, or the model's line picks up mid-sentence. A capital is a sentence start,
    so "\u2026" never lands on a real one. A digit ("30 yaş ...") or a script without case is mid-sentence
    only when ``before`` (the same speaker's text just before it, "" when there is none) has no
    sentence-final mark."""
    first = _FIRST.search(text.strip())
    if first is None:
        return False
    ch = first.group(0)
    if ch.islower():
        return True
    if ch.isupper():
        return False
    before = before.strip()
    return bool(before) and not _SENTENCE_END.search(before)


PARAGRAPH_SENTENCES = 4  # a speaker's turn starts a new row after this many sentences (rows stay put)


def paragraphs(units: list[Unit], show_speaker: bool) -> list[_Paragraph]:
    """Shown units grouped into paragraphs: each speaker turn starts on a new row, as BBC live
    subtitles and Netflix (one speaker per line) do, and so does every ``PARAGRAPH_SENTENCES``-th
    sentence of a long turn, so a row never reflows once it is up."""
    out: list[_Paragraph] = []
    previous: Optional[Unit] = None
    for unit in units:
        last = out[-1] if out else None
        # A line's first unit starts a sentence unless it starts inside one (continues_sentence).
        same = previous is not None and previous.speaker_id == unit.speaker_id and previous.kind == TRANSLATED
        starts = unit.first and not (unit.kind == TRANSLATED and
                                     continues_sentence(previous.text if same else "", unit.text))
        sentences = sum(1 for s in last.starts if s) if last else 0
        if last is None or unit.speaker_id != last.speaker_id or (starts and sentences >= PARAGRAPH_SENTENCES):
            turn = last is None or unit.speaker_id != last.speaker_id
            out.append(_Paragraph(unit.speaker_id, [unit], label=show_speaker and turn and bool(unit.speaker_id),
                                  starts=[starts]))
        else:
            last.units.append(unit)
            last.starts.append(starts)
        previous = unit
    return out


def paragraph_runs(par: _Paragraph, number: Optional[int], strings: Strings) -> list[tuple[Run, bool, int]]:
    """(run, starts a sentence, unit number in the paragraph) for one paragraph."""
    runs: list[tuple[Run, bool, int]] = []
    if par.label and number is not None:
        runs.append((Run(str(number), LABEL, number), True, 0))
    previous: Optional[Unit] = None
    for k, unit in enumerate(par.units):
        new_line = previous is None or unit.line_id != previous.line_id
        if unit.kind == FAILED and new_line:
            runs.append((Run(strings.not_translated, MARK, number), True, k))
        if unit.text:
            starts = par.starts[k] if k < len(par.starts) else unit.first
            runs.append((Run(unit.text, unit.kind, number), starts, k))
        previous = unit
    return runs


def wrap_rows(pairs: list[tuple[Run, bool, int]], width: float, measure: Measure) -> list[Row]:
    """Rows no wider than ``width``. A row breaks at the last phrase boundary that keeps it at
    least 60% full, otherwise at the last word that fits; never inside a word, except a single
    word wider than the whole strip. A label and a marker stay with the first word after them.

    A break only moves words of the unit being added: words already on screen never jump to the
    next row when a later unit arrives, so the rows of a prefix are the rows of the whole."""
    if width <= 0:
        return []
    # tokens: (word with its trailing space, run, starts a sentence, tag, unit)
    tokens: list[tuple[str, Run, bool, bool, int]] = []
    for run, starts, unit in pairs:
        if run.kind in (LABEL, MARK):
            tokens.append((run.text.strip() + " ", run, starts, True, unit))
            continue
        words = run.text.split()
        for i, word in enumerate(words):
            tokens.append((word + " ", run, starts and i == 0, False, unit))
    rows: list[list[tuple[str, Run, bool, bool, int]]] = [[]]

    def row_width(row) -> float:
        if not row:
            return 0.0
        return sum(measure(t[0], t[1]) for t in row[:-1]) + measure(row[-1][0].rstrip(), row[-1][1])

    unit_width: dict[int, float] = {}
    for unit_id in {t[4] for t in tokens}:
        unit_width[unit_id] = row_width([t for t in tokens if t[4] == unit_id])

    for index, token in enumerate(tokens):
        word, run, _starts, tag, unit = token
        row = rows[-1]
        if row and row[-1][3] and row[-1][4] != unit and row[-1][1].kind == MARK:
            # A "not translated" marker that stands for a whole line keeps its row: the next line's
            # words must not read as the untranslated one.
            rows.append([])
            row = rows[-1]
        if row and row_width(row + [token]) > width:
            # Where to break. A unit that does not fit the rest of this row starts a new one, so it
            # enters whole and is read in one piece; within a unit longer than a row, the last phrase
            # boundary that keeps this row at least 60% full; else here.
            cut = len(row)
            start = next((k for k, t in enumerate(row) if t[4] == unit), None)
            if start:
                cut = start
            for k in range(len(row) - 1, 0, -1):
                if cut != len(row):
                    break
                before, after = row[k - 1], row[k]
                if after[4] != unit:
                    break  # words of an earlier unit stay where they are
                if before[3] or after[3]:
                    continue  # a tag stays with the word after it
                if break_before(before[0].strip(), after[0].strip()) and row_width(row[:k]) >= 0.6 * width:
                    cut = k
                    break
            if cut == len(row) and start == 0:
                # A plain break inside the unit. When the rest of the unit fits one row, even the two
                # rows out, so no row holds one short word (Netflix: "Avoid having one short word").
                # Only when this row holds nothing but this unit's words: a row that also holds an
                # earlier unit is on screen already and never changes (A7-1).
                tail = [t for t in tokens[index:] if t[4] == unit]
                if row_width(tail) <= width:
                    best, best_gap = cut, None
                    for k in range(1, len(row)):
                        if row[k - 1][3] or row[k][3] or row_width(row[k:] + tail) > width:
                            continue
                        gap = abs(row_width(row[:k]) - row_width(row[k:] + tail))
                        if best_gap is None or gap < best_gap:
                            best, best_gap = k, gap
                    if best_gap is not None and best_gap < abs(row_width(row) - row_width(tail)):
                        cut = best
            while cut > 0 and row[cut - 1][3] and row[cut - 1][4] == unit:
                cut -= 1  # never leave a label or a marker alone at the end of a row
            if cut == 0:
                cut = len(row)
            rows[-1], carry = row[:cut], row[cut:]
            rows.append(carry)
            row = rows[-1]
            if row and row_width(row + [token]) > width:
                rows.append([])
                row = rows[-1]
        if not tag and measure(word.rstrip(), run) > width:
            # A word wider than the whole strip: the only case that splits a word, with a hyphen.
            pieces = _split_long(word.rstrip(), run, width - measure("-", run), measure)
            for i, piece in enumerate(pieces):
                if row:
                    rows.append([])
                    row = rows[-1]
                last = i == len(pieces) - 1
                row.append((piece + (" " if last else "-"), run, token[2] and i == 0, False, unit))
            continue
        row.append(token)
    out: list[Row] = []
    for row in rows:
        if not row:
            continue
        merged: list[Run] = []
        for word, run, _s, _t, _u in row:
            if merged and merged[-1].kind == run.kind and merged[-1].speaker == run.speaker and run.kind not in (LABEL, MARK):
                merged[-1] = Run(merged[-1].text + word, run.kind, run.speaker)
            else:
                merged.append(Run(word, run.kind, run.speaker))
        merged = [Run(r.text.rstrip() if r.kind not in (LABEL, MARK) else r.text.strip(), r.kind, r.speaker)
                  for r in merged]
        first_text = next((t for t in row if not t[3]), None)
        continues = first_text is not None and not first_text[2]
        out.append(Row(tuple(r for r in merged if r.text), continues))
    return out


@dataclass
class RowCache:
    """Rows per paragraph. Only the newest paragraph grows, so the others are measured once."""

    key: tuple = ()
    rows: dict = field(default_factory=dict)

    def get(self, key: tuple, compute: Callable[[], list[Row]]) -> list[Row]:
        found = self.rows.get(key)
        if found is None:
            found = compute()
            if len(self.rows) > 400:
                self.rows.clear()
            self.rows[key] = found
        return found


def all_rows(units: list[Unit], model: OverlayModel, *, width: float, measure: Measure, show_speaker: bool,
             strings: Strings, cache: Optional[RowCache] = None) -> list[Row]:
    rows: list[Row] = []
    for par in paragraphs(units, show_speaker):
        number = model.speakers.get(par.speaker_id) if par.speaker_id else None
        pairs = paragraph_runs(par, number, strings)
        key = (width, tuple((r.text, r.kind, r.speaker, s, u) for r, s, u in pairs))
        compute = lambda pairs=pairs: wrap_rows(pairs, width, measure)  # noqa: E731
        rows.extend(cache.get(key, compute) if cache is not None else compute())
    return rows


ELLIPSIS = "…"


def window(rows: list[Row], count: int, offset: int = 0) -> tuple[tuple[Run, ...], ...]:
    """``count`` rows ending ``offset`` rows above the newest. The top row gets an ellipsis when it
    starts inside a sentence (Netflix: "an ellipsis without a space ... starting mid-sentence")."""
    if count <= 0 or not rows:
        return ()
    end = max(0, len(rows) - max(0, offset))
    shown = rows[max(0, end - count):end]
    out: list[tuple[Run, ...]] = []
    for i, row in enumerate(shown):
        runs = row.runs
        if i == 0 and row.continues and runs:
            at = next((j for j, r in enumerate(runs) if r.kind not in (LABEL, MARK)), None)
            if at is not None:
                r = runs[at]
                runs = runs[:at] + (Run(ELLIPSIS + r.text, r.kind, r.speaker),) + runs[at + 1:]
        out.append(runs)
    return tuple(out)
