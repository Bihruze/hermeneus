from hermeneus_engine import contracts as c


def test_engine_audio_format():
    assert (c.ENGINE_SAMPLE_RATE, c.ENGINE_SAMPLE_WIDTH, c.ENGINE_CHANNELS) == (16000, 2, 1)


def test_subtitle_line_round_trip():
    line = c.SubtitleLine(line_id="a", state=c.LineState.TRANSLATED, original="hello",
                          translation="merhaba", speaker_id="speaker_0", target_language="tr")
    d = line.to_dict()
    assert d["state"] == "translated"
    assert d["translation"] == "merhaba"


def test_stt_defaults_follow_fixed_decisions():
    cfg = c.SttConfig()
    assert cfg.model_id == "scribe_v3_live"
    assert cfg.diarize and cfg.multilang_mode and cfg.include_language_detection
