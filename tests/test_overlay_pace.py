"""The display queue and roll-up rows (overlay/pace.py) and the overlay app that uses them. No AppKit."""

import pytest

from pipeline_fakes import remove_tmpdir, short_tmpdir

from hermeneus_engine import overlay_settings as ovs
from hermeneus_engine.overlay import pace, theme
from hermeneus_engine.overlay.app import HISTORY_IDLE_S, HISTORY_ROWS, OverlayApp
from hermeneus_engine.overlay.model import FAILED, LABEL, MARK, TRANSLATED, OverlayModel, Run, Strings
from hermeneus_engine.overlay.placement import Display, Positions, Rect
from hermeneus_engine.overlay_tokens import FALLBACK


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def line(seq, lid, state, original, translation="", speaker="s1:speaker_0", target="tr"):
    return {"type": "line", "seq": seq, "line": {"line_id": lid, "state": state, "original": original,
                                                 "translation": translation, "speaker_id": speaker,
                                                 "target_language": target}}


def chars(text, run):
    return float(len(text))


TR = lambda unit: pace.reading_cps("tr")  # noqa: E731


def queue_with(*translations, pace_mode=pace.PACE_READ, at=0.0, limit=pace.CHUNK_CHARS):
    model, q = OverlayModel(), pace.PaceQueue(pace=pace_mode, limit=limit)
    for i, text in enumerate(translations):
        model.apply(line(i + 1, f"l{i}", "translated", f"orig {i}", text), at)
    q.sync(model, at)
    return model, q


def run_until(q, model, end, step=0.01, start=0.0):
    now = start
    while now <= end:
        q.sync(model, now)
        q.advance(now, TR)
        now = round(now + step, 4)


# -- reading speed and units --------------------------------------------------------

def test_reading_speed_follows_the_target_language_and_the_override():
    assert pace.reading_cps("tr") == 17.0 and pace.reading_cps("en") == 20.0
    assert pace.reading_cps("en-US") == 20.0 and pace.reading_cps("pt_BR") == pace.DEFAULT_CPS
    assert pace.reading_cps("ja") < pace.reading_cps("ko") < pace.DEFAULT_CPS
    assert pace.reading_cps("tr", 25) == 25.0 and pace.reading_cps("tr", 99) == 30.0 and pace.reading_cps("tr", 1) == 5.0
    assert pace.reading_time("Evet.", 17) == pace.MIN_S  # 5 characters: the minimum duration
    assert pace.reading_time("x" * 34, 17) == pytest.approx(2.0)


SAMPLES = [
    "Sağlık yetkililerine göre virüs haziran sonuna kadar ülkenin sekiz iline yayılmıştı.",
    "Başkentteki hastaneler şu anda ağır baskı altında ve bazı servisler yeni hastalara kapatıldı.",
    "The virus had spread to eight of the country's provinces by the end of June, health officials said.",
    "Kısa.",
    "Afyonkarahisarlılaştıramadıklarımızdanmışsınızcasına bir kelime var burada.",
]


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("limit", [20, 42, 84])
def test_units_keep_every_word_and_never_cut_one(text, limit):
    units = pace.split_phrases(text, limit)
    assert " ".join(units) == " ".join(text.split())  # nothing dropped, nothing shortened
    words = set(text.split())
    for unit in units:
        assert all(w in words for w in unit.split())  # whole words only
        assert len(unit) <= limit or len(unit.split()) == 1  # only a single longer word exceeds it


def test_units_break_at_phrase_boundaries_and_leave_no_one_word_tail():
    assert pace.split_phrases(SAMPLES[0], 42) == ["Sağlık yetkililerine göre", "virüs haziran sonuna kadar",
                                                  "ülkenin sekiz iline yayılmıştı."]
    units = pace.split_phrases("Başkentteki hastaneler şu anda ağır baskı altında.", 42)
    assert len(units) == 2 and min(len(u.split()) for u in units) >= 2
    assert pace.split_phrases("He came home, and then he left again for good.", 30)[0] == "He came home,"
    assert pace.break_before("sonuna", "kadar") is False and pace.break_before("kadar", "ülkenin") is True
    assert pace.break_before("home,", "then") is True and pace.break_before("the", "table") is False


# -- the queue: minimum duration, reading speed, order ------------------------------------

def test_a_short_line_stays_for_the_minimum_duration():
    model, q = queue_with("Evet.", "Hayır.")
    q.advance(0.0, TR)
    assert [u.text for u in q.shown()] == ["Evet."]
    q.advance(pace.MIN_S - 0.01, TR)
    assert len(q.shown()) == 1  # "Evet." needs 0.3 s at 17 cps but stays the minimum 1 s
    q.advance(pace.MIN_S, TR)
    assert [u.text for u in q.shown()] == ["Evet.", "Hayır."]


def test_each_unit_gets_its_reading_time_at_the_languages_speed():
    first = "Bu cümle tam otuz dört karakterdir."  # 35 characters
    model, q = queue_with(first, "Sonraki.")
    q.advance(0.0, TR)
    assert q.free_at == pytest.approx(len(first) / 17)
    q.advance(len(first) / 17 - 0.01, TR)
    assert len(q.shown()) == 1
    q.advance(len(first) / 17, TR)
    assert len(q.shown()) == 2
    # English reads at 20 cps: the same text is free sooner.
    model, q = OverlayModel(), pace.PaceQueue()
    model.apply(line(1, "a", "translated", "x", first, target="en"), 0)
    q.sync(model, 0)
    q.advance(0, lambda u: pace.reading_cps(u.language))
    assert q.free_at == pytest.approx(len(first) / 20)


def test_read_everything_never_shortens_reading_time_however_far_behind():
    texts = [f"Cümle numarası {i} burada bitiyor ve devam ediyor." for i in range(12)]
    model, q = queue_with(*texts)
    run_until(q, model, 60)
    shown = q.shown()
    assert len(shown) == len(q.units)
    for a, b in zip(shown, shown[1:]):
        assert b.shown_at - a.shown_at >= pace.reading_time(a.text, 17) - 1e-6
    assert shown[-1].shown_at > 20  # far behind: 12 sentences arrived at once


def test_follow_live_catches_up_but_keeps_the_minimum_and_half_the_reading_time():
    texts = [f"Cümle numarası {i} burada bitiyor ve devam ediyor." for i in range(12)]
    model, q = queue_with(*texts, pace_mode=pace.PACE_LIVE)
    run_until(q, model, 60)
    shown = q.shown()
    assert len(shown) == len(q.units)  # catching up drops nothing
    gaps = [b.shown_at - a.shown_at for a, b in zip(shown, shown[1:])]
    for unit, gap in zip(shown, gaps):
        nominal = pace.reading_time(unit.text, 17)
        assert gap >= max(pace.MIN_S, nominal / pace.LIVE_MAX_SPEEDUP) - 0.011
    read_model, read_q = queue_with(*texts)
    run_until(read_q, read_model, 60)
    assert shown[-1].shown_at < read_q.shown()[-1].shown_at * 0.8  # the minimum duration bounds the speed-up


def test_follow_live_keeps_full_reading_time_while_it_keeps_up():
    model, q = queue_with("Kısa bir cümle.", pace_mode=pace.PACE_LIVE)
    q.advance(0, TR)
    assert q.shown()[0].dwell == pytest.approx(pace.reading_time("Kısa bir cümle.", 17))


def test_lines_enter_in_speech_order_and_a_slow_translation_does_not_block_for_ever():
    model, q = OverlayModel(), pace.PaceQueue()
    model.apply(line(1, "a", "committed", "First."), 0)
    model.apply(line(2, "b", "translated", "Second.", "İkinci."), 0.5)
    q.sync(model, 0.5)
    assert q.units == []  # "İkinci." waits for the line spoken before it
    model.apply(line(3, "a", "translated", "First.", "Birinci."), 1.0)
    q.sync(model, 1.0)
    assert [u.text for u in q.units] == ["Birinci.", "İkinci."]
    model, q = OverlayModel(), pace.PaceQueue()
    model.apply(line(1, "a", "committed", "First."), 0)
    model.apply(line(2, "b", "translated", "Second.", "İkinci."), 0.5)
    q.sync(model, pace.ORDER_WAIT_S + 0.1)
    assert [u.text for u in q.units] == ["İkinci."]
    model.apply(line(3, "a", "translated", "First.", "Birinci."), 9)
    q.sync(model, 9)
    assert [u.text for u in q.units] == ["İkinci.", "Birinci."]  # late, but still shown


def test_partial_lines_never_enter_and_the_source_text_is_not_queued():
    model, q = OverlayModel(), pace.PaceQueue()
    model.apply(line(1, "p", "partial", "The virus had spread"), 0)
    model.apply(line(2, "a", "translated", "The outbreak began.", "Salgın başladı."), 0)
    q.sync(model, 0)
    assert [u.text for u in q.units] == ["Salgın başladı."]
    model.apply(line(3, "f", "failed", "Fast talk"), 0)
    q.sync(model, 0)
    assert (q.units[-1].kind, q.units[-1].text) == (FAILED, "")  # the marker alone, not the English
    model2, q2 = OverlayModel(), pace.PaceQueue()
    model2.apply(line(1, "f", "failed", "Fast talk"), 0)
    q2.sync(model2, 0, show_original=True)
    assert (q2.units[-1].kind, q2.units[-1].text) == (FAILED, "Fast talk")
    model2.apply(line(2, "e", "translated", "Dropped", ""), 0)  # a translated line without text
    q2.sync(model2, 0, show_original=True)
    assert q2.units[-1].kind == FAILED


# -- rows: roll-up, phrase breaks, nothing lost ---------------------------------------------

def rows_of(units, model, width, show_speaker=False, strings=Strings()):
    return pace.all_rows(units, model, width=width, measure=chars, show_speaker=show_speaker, strings=strings)


def test_rows_roll_up_and_rows_already_up_never_move():
    texts = [f"Cümle {i} burada bitiyor ve sonra devam ediyor." for i in range(6)]
    model, q = queue_with(*texts)
    run_until(q, model, 60)
    shown = q.shown()
    previous = []
    for k in range(1, len(shown) + 1):
        rows = rows_of(shown[:k], model, 40)
        assert rows[:max(0, len(previous) - 1)] == previous[:max(0, len(previous) - 1)]  # only the bottom row grows
        previous = rows
    window = pace.window(previous, 2)
    assert len(window) == 2
    assert "".join(r.text for r in window[-1]).endswith("devam ediyor.")


def test_a_unit_enters_whole_on_a_fresh_row_when_it_does_not_fit_the_bottom_row():
    model, q = queue_with("aaa bbb ccc.", "ddd eee fff ggg.")
    run_until(q, model, 10)
    rows = rows_of(q.shown(), model, 20)
    assert ["".join(r.text for r in row.runs) for row in rows] == ["aaa bbb ccc.", "ddd eee fff ggg."]


def test_no_text_is_dropped_and_no_word_is_cut_in_rows():
    texts = SAMPLES[:4] * 3
    model, q = queue_with(*texts, limit=30)
    run_until(q, model, 200)
    rows = rows_of(q.shown(), model, 34)
    words = " ".join(" ".join(r.text for r in row.runs) for row in rows).split()
    assert words == " ".join(texts).split()
    assert all(sum(chars(r.text, r) for r in row.runs) + len(row.runs) - 1 <= 34 for row in rows)


def test_a_row_breaks_at_a_phrase_boundary_when_the_row_stays_full_enough():
    pairs = [(Run("one two three four, five six seven", TRANSLATED), True, 0)]
    rows = pace.wrap_rows(pairs, 24, chars)
    assert ["".join(r.text for r in row.runs) for row in rows] == ["one two three four,", "five six seven"]


def test_only_a_word_wider_than_the_strip_is_split_and_it_carries_a_hyphen():
    pairs = [(Run("ab abcdefghijklmnop cd", TRANSLATED), True, 0)]
    rows = pace.wrap_rows(pairs, 8, chars)
    texts = ["".join(r.text for r in row.runs) for row in rows]
    assert texts[0] == "ab" and texts[1].endswith("-") and texts[-1].endswith("cd")
    assert "".join(t.rstrip("-") for t in texts[1:-1]) + texts[-1].split()[0] == "abcdefghijklmnop"


def test_the_top_row_says_when_it_continues_a_sentence():
    model, q = queue_with("Bir iki üç dört beş altı yedi sekiz dokuz on.", limit=60)
    run_until(q, model, 10)
    rows = rows_of(q.shown(), model, 12)
    assert not rows[0].continues and rows[1].continues
    top = pace.window(rows, 2)[0]
    assert top[0].text.startswith(pace.ELLIPSIS)
    assert not pace.window(rows, len(rows))[0][0].text.startswith(pace.ELLIPSIS)  # a real start has none


def test_speaker_turns_start_rows_and_carry_a_compact_label_that_follows_relabel():
    model, q = OverlayModel(), pace.PaceQueue()
    model.apply(line(1, "a", "translated", "Hi.", "Merhaba.", "s1:speaker_0"), 0)
    model.apply(line(2, "b", "translated", "How are you?", "Nasılsın?", "s1:speaker_1"), 0)
    model.apply(line(3, "c", "failed", "Fine.", "", "s1:speaker_1"), 0)
    run_until(q, model, 10)
    rows = rows_of(q.shown(), model, 200, show_speaker=True)
    assert [[(r.text, r.kind) for r in row.runs] for row in rows] == [
        [("1", LABEL), ("Merhaba.", TRANSLATED)], [("2", LABEL), ("Nasılsın?", TRANSLATED), ("Not translated", MARK)]]
    model.apply({"type": "relabel", "seq": 4, "from": "s1:speaker_1", "to": "s1:speaker_0"}, 10)
    q.sync(model, 10)
    rows = rows_of(q.shown(), model, 200, show_speaker=True)
    assert len(rows) == 1 and rows[0].runs[0] == Run("1", LABEL, 1)
    tr = Strings(not_translated="Çevrilmedi")
    rows = rows_of(q.shown(), model, 200, show_speaker=False, strings=tr)
    assert rows[0].runs[-1].text == "Çevrilmedi"


# -- the app: target language only, history, back to live -----------------------------------

D1 = Display("1:Built-in", Rect(0, 0, 1440, 900))


class FakePanel:
    def __init__(self):
        self.renders, self.visible, self.frames = [], [], []

    def displays(self):
        return [D1]

    def main_display(self):
        return D1

    def measure(self, text, run, settings):
        return float(len(text) * 10)

    def measure_original(self, text, settings):
        return float(len(text) * 6)

    def strip_height(self, settings, rows=None, original=None):
        return 40.0 * (settings.max_lines if rows is None else rows)

    def text_inset(self):
        return 20.0

    def set_frame(self, frame):
        self.frames.append(frame)

    def render(self, view, settings):
        self.renders.append(view)

    def set_visible(self, visible):
        self.visible.append(visible)


def texts(view):
    return [" ".join(r.text for r in row if r.kind != LABEL) for row in view.lines]


def make_app(data_dir, **settings):
    panel = FakePanel()
    app = OverlayApp(panel, Positions(data_dir / "p.json"), ovs.OverlaySettings(**settings))
    app.place()
    app.on_event({"type": "hello", "seq": 1, "session": {"state": "live"}}, 0)
    return panel, app


def test_the_strip_shows_only_the_translation_and_a_neutral_indicator_while_it_waits(data_dir):
    panel, app = make_app(data_dir)
    app.on_event(line(2, "a", "partial", "The virus had spread to eight"), 0.2)
    view = panel.renders[-1]
    assert view.lines == () and view.pending and not view.empty  # dots, no English
    app.on_event(line(3, "a", "translated", "The virus had spread.", "Virüs yayılmıştı."), 1.0)
    view = panel.renders[-1]
    assert texts(view) == ["Virüs yayılmıştı."] and view.original == "" and not view.pending
    assert all("virus" not in r.text.lower() for row in view.lines for r in row)
    _, app2 = make_app(data_dir, show_original=True)
    app2.on_event(line(2, "a", "translated", "The virus had spread.", "Virüs yayılmıştı."), 1.0)
    assert app2.view.original == "The virus had spread."


def test_the_strip_holds_then_clears_and_starts_afresh(data_dir):
    panel, app = make_app(data_dir)
    app.on_event(line(2, "a", "translated", "Hi.", "Merhaba."), 1.0)
    app.refresh(1.0 + pace.HOLD_S - 0.1)
    assert panel.visible[-1] is True
    app.refresh(1.0 + pace.HOLD_S + 0.2)
    assert panel.visible[-1] is False
    app.on_event(line(3, "b", "translated", "Bye.", "Hoşça kal."), 20)
    assert texts(panel.renders[-1]) == ["Hoşça kal."]  # the old line does not come back above it


def feed(app, n, start=0.0):
    for i in range(n):
        app.on_event(line(10 + i, f"l{i}", "translated", "x", f"Cümle {i} tamamlandı ve bitti."), start)
    t = start
    while app.queue.waiting() or t < start + 1:
        app.refresh(t)
        t += 0.1
    return t + 0.5


def test_history_scrolls_back_keeps_its_place_and_returns_to_live(data_dir):
    panel, app = make_app(data_dir)
    app.set_width(0.25)  # 360 pt: one sentence per row
    now = feed(app, 12)
    live = panel.renders[-1]
    assert texts(live)[-1] == "Cümle 11 tamamlandı ve bitti." and live.older and not live.history
    app.scroll(1, now)
    view = panel.renders[-1]
    assert view.history and len(view.lines) == HISTORY_ROWS and texts(view)[-1] == "Cümle 11 tamamlandı ve bitti."
    app.scroll(4, now + 0.5)
    view = panel.renders[-1]
    assert view.newer == 4 and texts(view)[-1] == "Cümle 7 tamamlandı ve bitti."
    # A new line arrives while reading back: the rows in view stay put.
    app.on_event(line(50, "z", "translated", "x", "Yeni bir cümle geldi."), now + 1)
    app.refresh(now + 1)
    assert texts(panel.renders[-1])[-1] == "Cümle 7 tamamlandı ve bitti."
    app.back_to_live(now + 2)
    view = panel.renders[-1]
    assert not view.history and texts(view)[-1] == "Yeni bir cümle geldi."
    # Scrolling forward past the newest row is live again, and so is a quiet history.
    app.scroll(2, now + 3)
    app.scroll(-5, now + 3.5)
    assert not panel.renders[-1].history
    app.scroll(3, now + 4)
    app.refresh(now + 4 + HISTORY_IDLE_S - 0.5)
    assert panel.renders[-1].history
    app.refresh(now + 4 + HISTORY_IDLE_S + 0.5)
    assert not panel.renders[-1].history


def test_history_scrolls_no_further_than_the_first_row(data_dir):
    panel, app = make_app(data_dir)
    app.set_width(0.25)
    now = feed(app, 9)
    app.scroll(100, now)
    view = panel.renders[-1]
    assert texts(view)[0] == "Cümle 0 tamamlandı ve bitti." and not view.older


def test_a_reset_empties_the_queue(data_dir):
    panel, app = make_app(data_dir)
    app.on_event(line(2, "a", "translated", "Hi.", "Merhaba."), 1.0)
    app.on_event({"type": "reset"}, 1.5)
    assert app.queue.units == [] and panel.renders[-1].lines == ()


def test_unit_size_follows_the_strip_so_a_unit_always_fits_its_rows(data_dir):
    panel, app = make_app(data_dir, max_lines=2)
    app.set_width(320 / 1440)
    width = app.frame.w - 40
    limit = app._unit_chars(width)
    assert limit <= 2 * width / 10 and limit >= 16
    app.on_event(line(2, "a", "translated", "x", SAMPLES[1]), 1.0)
    for unit in app.queue.units:
        rows = pace.wrap_rows([(Run(unit.text, TRANSLATED), True, 0)], width, lambda t, r: len(t) * 10.0)
        assert len(rows) <= 2


# -- legibility --------------------------------------------------------------------------

def test_under_half_opacity_text_is_light_with_a_dark_edge_in_every_theme():
    light = FALLBACK[False]
    see = theme.see_through(light, 30)
    assert see.dark and see.text.r > 0.8 and see.surface.r < 0.2  # light text, dark edge
    assert theme.see_through(light, 50) is light  # 50% and up: the theme as it is
    dark = FALLBACK[True]
    assert theme.see_through(dark, 10) is dark


def test_defaults_are_target_only_read_everything_and_a_solid_background():
    s = ovs.OverlaySettings()
    assert (s.show_original, s.pace, s.opacity, s.reading_cps) == (False, "read", 90, 0)


def test_a_marker_for_a_whole_line_keeps_its_row():
    # Before the fix the next line's words followed "Not translated" on its row and read as untranslated.
    pairs = [(Run("Not translated", MARK), True, 0), (Run("Sonraki cümle.", TRANSLATED), True, 1)]
    rows = pace.wrap_rows(pairs, 200, chars)
    assert [[r.kind for r in row.runs] for row in rows] == [[MARK], [TRANSLATED]]


def test_a_unit_over_two_rows_is_split_evenly_not_with_one_word_left():
    pairs = [(Run("Başkentteki hastaneler şu", TRANSLATED), True, 0)]
    rows = pace.wrap_rows(pairs, 22, chars)
    assert ["".join(r.text for r in row.runs) for row in rows] == ["Başkentteki", "hastaneler şu"]


_FUZZ_WORDS = ("salgın mayıs ayında başladı ve virüs ülkenin sekiz bölgesine yayıldı ama yetkililer durumun kontrol "
               "altında olduğunu söyledi çünkü aşılama kampanyası hızla ilerliyor için kadar göre sonra, büyük. "
               "küçük; bakanlık").split()


@pytest.mark.parametrize("width", [24, 32, 42, 60])
def test_a_new_unit_never_moves_words_already_on_screen(width):
    # A7-1: the balancing step moved the last words of a row already on screen down when the next unit
    # arrived (a news row ending "... için bir son haber var." became "... için bir" + "son haber
    # var. ..."). Every row of the view before a unit arrived must be unchanged after it, the bottom one
    # only growing.
    import random
    limit = int(max(16, min(pace.UNIT_CHARS, 0.85 * 2 * width)))
    model = OverlayModel()
    model.speakers = {"a": 1}
    for trial in range(150):
        rng = random.Random(trial * 7 + width)
        units = []
        for li in range(rng.randint(2, 7)):
            text = " ".join(rng.choice(_FUZZ_WORDS) for _ in range(rng.randint(2, 30))).capitalize() + "."
            units += [pace.Unit(f"l{li}", i, part, TRANSLATED, "a", 0.0, i == 0, "tr")
                      for i, part in enumerate(pace.split_phrases(text, limit))]
        before = None
        for n in range(1, len(units) + 1):
            rows = rows_of(units[:n], model, width, show_speaker=True)
            if before is not None:
                assert rows[:len(before) - 1] == before[:-1], (trial, n)
                old = " ".join(r.text for r in before[-1].runs).split()
                assert " ".join(r.text for r in rows[len(before) - 1].runs).split()[:len(old)] == old, (trial, n)
            before = rows


def test_a_sentence_split_across_two_lines_takes_an_ellipsis_on_its_top_row():
    # A7-5: the session cut one sentence into two lines; the second line's top row read
    # "telefonla Ankara'dan ..." with no sign that it carries on.
    model, q = queue_with("Bakan bu sabah", "telefonla Ankara'dan konuştu.", "Yeni bir cümle başlıyor.")
    run_until(q, model, 30)
    rows = rows_of(q.shown(), model, 40)
    by_start = {"".join(r.text for r in row.runs).split()[0]: row for row in rows}
    for k in range(1, len(rows) + 1):
        top = pace.window(rows[:k], 1)[0]
        first = top[0].text
        word = first.lstrip(pace.ELLIPSIS).split()[0]
        assert first.startswith(pace.ELLIPSIS) == by_start[word].continues
    narrow = rows_of(q.shown(), model, 16)  # each line starts its own rows at this width
    tops = [pace.window(narrow[:k], 1)[0][0].text for k in range(1, len(narrow) + 1)]
    assert pace.ELLIPSIS + "telefonla" in [t.split()[0] for t in tops]
    assert "Yeni" in [t.split()[0] for t in tops]  # a real sentence start never takes one
    assert "Bakan" in [t.split()[0] for t in tops]
    assert pace.continues_sentence("Bakan bu sabah", "telefonla")
    assert pace.continues_sentence("bildirdi.", "onunla hükümet kurma")  # real news: a lowercase start after a full stop
    assert not pace.continues_sentence("ama", "Mira Tal")  # a capital may start a new sentence: no ellipsis
    assert not pace.continues_sentence("bitti.", "30 yaş altı")  # a digit start: punctuation decides
    assert pace.continues_sentence("yaklaşık", "30 yaş altı")
    assert pace.continues_sentence("他说", "我们") and not pace.continues_sentence("他说。", "我们")  # no case: punctuation
