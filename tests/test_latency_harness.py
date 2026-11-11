import hashlib
import json
import wave

import pytest

from hermeneus_engine import contracts as c
from hermeneus_engine import metrics as m
from tests import fakes

H = fakes.load_harness()
TOL_MS = 5.0
CHUNK_MS = 20


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    path = tmp_path_factory.mktemp("corpus") / "bursts.wav"
    man = H.gen_corpus(path, seed=11, duration_s=5.0, burst_ms=(300, 500), gap_ms=(600, 800),
                       freq_hz=(300, 900), grid_ms=CHUNK_MS)
    return path, man


def _per_segment(result, a, b):
    out = []
    for seg in result["segments"]:
        st = seg["stages_ns"]
        if a in st and b in st:
            out.append((st[b] - st[a]) / 1e6)
    return out


def test_gen_corpus_is_deterministic_and_on_grid(tmp_path):
    a = H.gen_corpus(tmp_path / "a.wav", seed=5, duration_s=4, burst_ms=(200, 400), gap_ms=(200, 400),
                     freq_hz=(200, 800))
    b = H.gen_corpus(tmp_path / "b.wav", seed=5, duration_s=4, burst_ms=(200, 400), gap_ms=(200, 400),
                     freq_hz=(200, 800))
    assert a["pcm_sha256"] == b["pcm_sha256"]
    assert hashlib.sha256((tmp_path / "a.wav").read_bytes()).hexdigest() == \
        hashlib.sha256((tmp_path / "b.wav").read_bytes()).hexdigest()
    assert json.loads((tmp_path / "a.json").read_text())["segments"] == a["segments"]
    for s in a["segments"]:
        assert round(s["start_s"] * 1000) % 20 == 0 and round(s["end_s"] * 1000) % 20 == 0
    assert a["segments"][-1]["end_s"] <= a["duration_s"] - 0.2  # trailing silence closes the last burst
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (16000, 2, 1)


def test_wav_in_other_format_is_refused(tmp_path):
    p = tmp_path / "cd.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(bytes(400))
    with pytest.raises(SystemExit, match="afconvert"):
        H.read_engine_wav(p)


@pytest.fixture(scope="module")
def dense_corpus(tmp_path_factory):
    """About 10 bursts in 8 s, enough segments for a median. Gaps exceed commit plus diarized delay."""
    path = tmp_path_factory.mktemp("dense") / "dense.wav"
    man = H.gen_corpus(path, seed=23, duration_s=8.0, burst_ms=(200, 300), gap_ms=(440, 520),
                       freq_hz=(300, 900), grid_ms=CHUNK_MS)
    return path, man


# Wall-clock bound. Timers, chunk deliveries and event loop wakeups can be late under load but
# never early, so every segment must reach at least the nominal delay less LOWER_SLACK_MS (the
# first nonzero sample of a burst sits up to 0.125 ms after the grid line). Lateness is scheduler
# noise: the median over all segments must stay within TOL_MS above nominal. With 4 cores busy,
# 20 runs in a row stayed inside this bound (W11 report, finding F22).
LOWER_SLACK_MS = 0.5


def _assert_wall_clock(name, got, nominal):
    assert got, name
    assert min(got) >= nominal - LOWER_SLACK_MS, (name, nominal, got)
    med = sorted(got)[len(got) // 2]
    assert med - nominal < TOL_MS, (name, nominal, med, got)


async def test_full_pipeline_measures_injected_delays(dense_corpus):
    wav, man = dense_corpus
    commit_ms, partial_ms, diar_ms = 300, 40, 30
    tr_ms, first_ms, play_ms = 150, 80, 50
    stt = fakes.stt_factory(partial_ms=partial_ms, commit_ms={c.CommitStrategy.VAD: commit_ms}, diarized_ms=diar_ms)
    translator = fakes.FakeTranslator(tr_ms)
    speaker = fakes.FakeSpeaker(first_ms, play_ms)
    cfg = H.RunConfig(label="t", wav=wav, manifest=wav.with_suffix(".json"), stt=stt,
                      translator=lambda s: translator, speaker=lambda s: speaker,
                      chunk_ms=CHUNK_MS, drain_s=3.0, sample_interval_s=0.5)
    r = await H.run_pipeline(cfg)
    n = len(man["segments"])
    assert n >= 8
    assert r["stt_events"]["committed"] == n
    assert r["latency"]["segments"] == n and r["latency"]["unattributed"] == 0
    assert all(seg["attribution"] == "diarized" for seg in r["segments"])
    segs = sorted(r["segments"], key=lambda sg: sg["key"])
    st = [sg["stages_ns"] for sg in segs]

    # Part 1, arithmetic. The harness against what the fakes observed. None of these depend on
    # scheduling, so they hold under any load.
    fake = stt.instances[0]
    partials = [ev for ev in fake.emitted if isinstance(ev, c.PartialTranscript)]
    commits = [ev for ev in fake.emitted if isinstance(ev, c.CommittedTranscript)]
    assert [s_[m.Stage.PARTIAL] for s_ in st] == [ev.received_ns for ev in partials]
    assert [s_[m.Stage.COMMIT] for s_ in st] == [ev.received_ns for ev in commits]
    ae = r["attribution_error"]  # capture and speech_end against the manifest, virtual clock
    assert ae["start_ms"]["count"] == n and ae["abs_start_ms_max"] < 1.0 and ae["abs_end_ms_max"] < 1.0
    calls = {cl["text"]: cl for cl in translator.calls}
    spans = []
    for sg in segs:
        cl = calls[sg["text"]]
        s_ = sg["stages_ns"]
        # The harness span encloses the translator call and differs from it only by the
        # synchronous code around the await.
        assert s_[m.Stage.TRANSLATE_START] <= cl["call_ns"] and cl["resumed_ns"] <= s_[m.Stage.TRANSLATED]
        spans.append(((s_[m.Stage.TRANSLATED] - s_[m.Stage.TRANSLATE_START]) - (cl["resumed_ns"] - cl["call_ns"])) / 1e6)
    assert sorted(spans)[n // 2] < 1.0, spans
    assert len(speaker.calls) == n
    for s_, sp in zip(st, speaker.calls):
        assert s_[m.Stage.TRANSLATED] <= sp["start_ns"]
        assert s_[m.Stage.TTS_FIRST_AUDIO] == sp["first_ns"] and s_[m.Stage.PLAYBACK_START] == sp["play_ns"]
    assert r["end_stage"] == m.Stage.PLAYBACK_START
    assert r["latency"]["intervals"]["end_to_end"]["count"] == n

    # Part 2, wall clock. The measured intervals against the injected nominal delays. The fake
    # sees a burst start when its first chunk arrives, one chunk after the burst starts, and a
    # burst end when the next silent chunk arrives, one chunk after it ends.
    bursts = [(sg["end_s"] - sg["start_s"]) * 1000 for sg in man["segments"]]
    nominal = {
        (m.Stage.CAPTURE, m.Stage.PARTIAL): CHUNK_MS + partial_ms,
        (m.Stage.SPEECH_END, m.Stage.COMMIT): CHUNK_MS + commit_ms,
        (m.Stage.COMMIT, m.Stage.TRANSLATED): tr_ms,
        (m.Stage.TRANSLATED, m.Stage.TTS_FIRST_AUDIO): first_ms,
        (m.Stage.TTS_FIRST_AUDIO, m.Stage.PLAYBACK_START): play_ms,
    }
    for (a, b), want in nominal.items():
        got = [(s_[b] - s_[a]) / 1e6 for s_ in st]
        _assert_wall_clock(f"{a}->{b}", got, want)
    _assert_wall_clock("capture->commit excess",
                       [(s_[m.Stage.COMMIT] - s_[m.Stage.CAPTURE]) / 1e6 - d for s_, d in zip(st, bursts)],
                       CHUNK_MS + commit_ms)
    _assert_wall_clock("end to end excess",
                       [(s_[m.Stage.PLAYBACK_START] - s_[m.Stage.CAPTURE]) / 1e6 - d for s_, d in zip(st, bursts)],
                       CHUNK_MS + commit_ms + tr_ms + first_ms + play_ms)
    pacing = r["source"]
    assert pacing["chunks"] == round(man["duration_s"] * 1000 / CHUNK_MS)
    assert pacing["lateness_ms"]["p50"] < 1.0
    assert r["capture"]["gaps"] == 0 and r["stability"]["dropped_chunks"] == 0
    assert r["stability"]["resources"]["count"] >= 5
    assert r["stability"]["resources"]["rss_self_mb"]["max"] > 1


async def test_partial_words_used_when_no_diarized(corpus):
    wav, _ = corpus
    stt = fakes.stt_factory(emit_diarized=False)
    r = await H.run_pipeline(H.RunConfig(label="p", wav=wav, manifest=wav.with_suffix(".json"), stt=stt,
                                         chunk_ms=CHUNK_MS, drain_s=2.0))
    assert r["latency"]["unattributed"] == 0
    assert {seg["attribution"] for seg in r["segments"]} == {"partial"}
    assert r["attribution_error"]["abs_start_ms_max"] < 1.0
    assert r["end_stage"] == m.Stage.COMMIT


async def test_no_timed_words_counts_unattributed(corpus):
    wav, _ = corpus
    stt = fakes.stt_factory(emit_diarized=False, with_partial_words=False)
    r = await H.run_pipeline(H.RunConfig(label="u", wav=wav, stt=stt, chunk_ms=CHUNK_MS, drain_s=2.0))
    lat = r["latency"]
    assert lat["segments"] > 0 and lat["unattributed"] == lat["segments"]
    assert lat["intervals"]["capture_to_committed"]["count"] == 0


async def test_reconnect_restarts_stream_clock(corpus):
    wav, _ = corpus
    stt = fakes.stt_factory(reconnect_after_bursts=1)
    r = await H.run_pipeline(H.RunConfig(label="rc", wav=wav, manifest=wav.with_suffix(".json"), stt=stt,
                                         chunk_ms=CHUNK_MS, drain_s=2.0))
    assert r["stability"]["reconnects"] == 1
    assert r["stream_sessions"] == 2
    assert r["attribution_error"]["abs_start_ms_max"] < 1.0


async def test_translation_failure_is_counted(corpus):
    wav, _ = corpus
    r = await H.run_pipeline(H.RunConfig(
        label="f", wav=wav, stt=fakes.stt_factory(), chunk_ms=CHUNK_MS, drain_s=2.0,
        translator=lambda s: fakes.FakeTranslator(20, fail_on="tone 1")))
    assert r["stt_events"]["translation_failures"] == 1
    failed = [s for s in r["segments"] if "translation_error" in s]
    assert len(failed) == 1 and m.Stage.TRANSLATED not in failed[0]["stages_ns"]
    assert r["latency"]["intervals"]["end_to_end"]["count"] == r["latency"]["segments"] - 1


async def test_live_capture_counts_gaps_and_samples():
    backend = fakes.FakeCapture(chunk_ms=10, skip=(50,))
    cfg = H.RunConfig(label="cap", capture=lambda: backend, stt=fakes.stt_factory(), duration_s=2.0,
                      drain_s=1.0, sample_interval_s=0.5)
    r = await H.run_pipeline(cfg)
    assert r["source"]["backend"] == "fake-capture"
    assert r["capture"]["gaps"] == 1
    assert abs(r["capture"]["gap_ms_max"] - 10.0) < 0.01
    assert r["stability"]["dropped_chunks"] == 1
    assert backend.stop_calls >= 1
    assert r["stt_events"]["committed"] >= 2
    res = r["stability"]["resources"]
    assert res["count"] >= 3 and res["cpu_tree_pct"]["count"] >= 2


def test_cli_matrix_compares_commit_strategies(tmp_path, corpus):
    wav, _ = corpus
    out = tmp_path / "out"
    rc = H.main(["run", "--wav", str(wav), "--stt", "tests.fakes:fake_stt",
                 "--commit-strategy", "vad,turn_prediction", "--translator", "tests.fakes:fake_translator",
                 "--translate", "on,off", "--chunk-ms", str(CHUNK_MS), "--drain", "2", "--label", "cmp",
                 "--out", str(out)])
    assert rc == 0
    data = json.loads((out / "summary.json").read_text())
    labels = [r["config"]["label"] for r in data["runs"]]
    assert labels == ["cmp-vad-tr-notts", "cmp-vad-notr-notts", "cmp-turn_prediction-tr-notts",
                      "cmp-turn_prediction-notr-notts"]
    se = {r["config"]["label"]: r["latency"]["intervals"]["speech_end_to_committed"]["p50"] for r in data["runs"]}
    assert abs(se["cmp-vad-tr-notts"] - (CHUNK_MS + 300)) < TOL_MS
    assert abs(se["cmp-turn_prediction-tr-notts"] - (CHUNK_MS + 120)) < TOL_MS
    md = (out / "summary.md").read_text()
    assert "## Comparison (ms)" in md and "cmp-turn_prediction-notr-notts" in md
    assert "\u2014" not in md  # house rule: no em dashes


def test_cli_help_and_g1_guard(capsys):
    with pytest.raises(SystemExit) as e:
        H.main(["run", "--help"])
    assert e.value.code == 0
    assert "factory(config: SttConfig, on_event, secrets)" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="G1"):
        H.main(["run", "--wav", "x.wav", "--duration", "60", "--gate", "G1"])


def test_parse_ps_time():
    assert H.parse_ps_time("0:01.50") == 1.5
    assert H.parse_ps_time("2:03:04.25") == 7384.25
    assert H.parse_ps_time("1-00:00:01.00") == 86401.0


def test_bench_reports_costs():
    b = H.bench(n=2000, threads=2)
    assert b["recorder_mark_ns"] > 0 and b["frame_accounting_call_ns"] > 0


def test_sampler_libproc_and_ps_agree_and_see_children():
    import subprocess
    import sys

    s = H.ResourceSampler()
    child = subprocess.Popen(["sleep", "3"])
    try:
        ps = s._ps_tree()
        assert ps[3] >= 2 and ps[0] > 1000
        if sys.platform == "darwin":
            assert s.method == "libproc"
            lp = s._libproc_tree()
            assert lp[3] == ps[3]
            assert abs(lp[0] - ps[0]) < 0.2 * ps[0]  # RSS in kB, both read within milliseconds
    finally:
        child.kill()
        child.wait()
