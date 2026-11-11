from hermeneus_engine.sentences import SentenceSplitter, after_released, split_all, split_sentences

MS = 1_000_000


def test_a_sentence_counts_only_when_more_words_follow():
    assert split_sentences("Good evening and welcome to the news.") == ([], "Good evening and welcome to the news.")
    assert split_sentences("Good evening and welcome to the news. Our top") == (
        ["Good evening and welcome to the news."], "Our top")


def test_abbreviations_initials_and_short_sentences_do_not_split():
    assert split_sentences("Dr. Smith met J. Doe today at noon. Then they")[0] == [
        "Dr. Smith met J. Doe today at noon."]
    assert split_sentences("Yes. No. The minister said no. More")[0] == ["Yes. No. The minister said no."]
    assert split_sentences("Le ministre a parlé ce soir ! Puis il")[0] == ["Le ministre a parlé ce soir !"]


def test_split_all_keeps_the_tail():
    assert split_all("One two three four. Five six seven eight") == ["One two three four.", "Five six seven eight"]
    assert split_all("One two three four. Five") == ["One two three four. Five"]


def test_released_once_after_stable_time_and_partials():
    s = SentenceSplitter(stable_ms=300, stable_partials=2)
    t = 0
    assert s.on_partial("Rain flooded several roads today. Officials", t) == []
    assert s.on_partial("Rain flooded several roads today. Officials say", t + 100 * MS) == []
    out = s.on_partial("Rain flooded several roads today. Officials say the", t + 350 * MS)
    assert out == ["Rain flooded several roads today."]
    assert s.remainder("Rain flooded several roads today. Officials say the") == "Officials say the"
    assert s.on_partial("Rain flooded several roads today. Officials say the water", t + 900 * MS) == []


def test_a_changing_sentence_restarts_its_clock():
    s = SentenceSplitter(stable_ms=300, stable_partials=1)
    s.on_partial("The minister presented the budget. And", 0)
    assert s.on_partial("The minister presented a budget. And", 400 * MS) == []
    assert s.on_partial("The minister presented a budget. And the", 800 * MS) == ["The minister presented a budget."]


def test_commit_reconciles_without_losing_or_repeating_words():
    s = SentenceSplitter(stable_ms=0, stable_partials=1)
    s.on_partial("Heavy rain flooded the roads. Schools", 0)
    assert s.on_partial("Heavy rain flooded the roads. Schools closed", 1) == ["Heavy rain flooded the roads."]
    # Scribe revised a released word at commit time ("the" to "several").
    final = "Heavy rain flooded several roads. Schools closed for a second day. Officials expect more rain"
    assert s.on_commit(final) == ["Schools closed for a second day.", "Officials expect more rain"]
    assert s.released == []


def test_after_released_handles_a_restarted_partial():
    released = ["heavy", "rain", "flooded"]
    assert after_released(released, "Heavy rain flooded the roads") == "the roads"
    assert after_released(released, "Something else entirely here now") == "here now"


def test_disabled_splitter_keeps_whole_segments():
    s = SentenceSplitter(stable_ms=-1)
    assert s.on_partial("One two three four. Five six seven", 10 ** 12) == []
    assert s.on_commit("One two three four. Five six seven eight.") == ["One two three four. Five six seven eight."]
