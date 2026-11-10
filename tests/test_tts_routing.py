import json
from pathlib import Path

import pytest

from hermeneus_engine.contracts import ENGINE_SAMPLE_RATE, FeedbackRiskError
from hermeneus_engine.tts import routing as r
from hermeneus_engine.tts.coreaudio_devices import CoreAudioDevice

FIX = Path(__file__).parent / "fixtures"


def _sp(name):
    return json.loads((FIX / name).read_text())


def _ca(name):
    return [CoreAudioDevice(object_id=d["object_id"], name=d["name"], uid=d["uid"], transport=d["transport"],
                            sub_device_uids=tuple(d["sub_device_uids"]))
            for d in json.loads((FIX / name).read_text())]


@pytest.fixture
def plain_inv():
    return r.build_inventory(_sp("sp_audio_no_aggregate.json"), None, None)


@pytest.fixture
def multi_inv():
    return r.build_inventory(_sp("sp_audio_multi_output.json"), _ca("coreaudio_multi_output.json"), None)


def test_inventory_reads_system_profiler_layout(plain_inv):
    spk = plain_inv.find("Test Built-in Speakers")
    assert spk.transport == "builtin" and spk.output_channels == 2 and spk.is_default_output
    assert plain_inv.find("BlackHole 2ch").transport == "virtual"
    assert plain_inv.find("blackhole 2ch").name == "BlackHole 2ch"
    assert plain_inv.find("BlackHole") is None  # no substring matching


def test_inventory_maps_aggregate_members(multi_inv):
    mo = multi_inv.find("Multi-Output Device")
    assert mo.is_aggregate
    assert mo.members == ("Test Built-in Speakers", "BlackHole 2ch")


def test_same_device_is_risky(plain_inv):
    v = r.check_feedback_risk("BlackHole 2ch", "BlackHole 2ch", inventory=plain_inv)
    assert v.risky and v.code == r.CODE_SAME_DEVICE
    assert "captured" in v.explanation


def test_speakers_are_safe_with_blackhole_capture(plain_inv):
    v = r.check_feedback_risk("Test Built-in Speakers", "BlackHole 2ch", inventory=plain_inv)
    assert not v.risky and v.code == r.CODE_OK and v.warnings == ()


def test_other_virtual_device_is_safe_but_warns(plain_inv):
    v = r.check_feedback_risk("VB-Cable", "BlackHole 2ch", inventory=plain_inv)
    assert not v.risky
    assert any("virtual" in w for w in v.warnings)


def test_multi_output_containing_capture_is_risky(multi_inv):
    v = r.check_feedback_risk("Multi-Output Device", "BlackHole 2ch", inventory=multi_inv)
    assert v.risky and v.code == r.CODE_OUTPUT_AGGREGATE_HAS_CAPTURE
    assert "Multi-Output Device" in v.explanation and "BlackHole 2ch" in v.explanation


def test_aggregate_without_capture_is_safe(multi_inv):
    v = r.check_feedback_risk("Speakers And Headset", "BlackHole 2ch", inventory=multi_inv)
    assert not v.risky


def test_capture_aggregate_containing_output_is_risky(multi_inv):
    v = r.check_feedback_risk("Test USB Headset", "Capture Aggregate", inventory=multi_inv)
    assert v.risky and v.code == r.CODE_CAPTURE_AGGREGATE_HAS_OUTPUT


def test_unknown_members_fail_safe():
    inv = r.build_inventory(_sp("sp_audio_multi_output.json"), None, None)  # CoreAudio unreadable
    v = r.check_feedback_risk("Multi-Output Device", "BlackHole 2ch", inventory=inv)
    assert v.risky and v.code == r.CODE_AGGREGATE_MEMBERS_UNKNOWN


def test_unknown_output_fails_safe(plain_inv):
    v = r.check_feedback_risk("No Such Device", "BlackHole 2ch", inventory=plain_inv)
    assert v.risky and v.code == r.CODE_OUTPUT_UNKNOWN


def test_process_tap_backend(multi_inv):
    ok = r.check_feedback_risk("Multi-Output Device", None, capture_backend=r.BACKEND_PROCESS_TAP,
                               own_process_excluded=True, inventory=multi_inv)
    assert not ok.risky
    bad = r.check_feedback_risk("Test Built-in Speakers", None, capture_backend=r.BACKEND_PROCESS_TAP,
                                own_process_excluded=False, inventory=multi_inv)
    assert bad.risky and bad.code == r.CODE_OWN_PROCESS_CAPTURED


def test_ensure_route_safe_raises_unless_gate_mode(plain_inv):
    v = r.check_feedback_risk("BlackHole 2ch", "BlackHole 2ch", inventory=plain_inv)
    with pytest.raises(FeedbackRiskError):
        r.ensure_route_safe(v)
    r.ensure_route_safe(v, gate_mode=True)


def test_safe_output_devices(multi_inv, plain_inv):
    names = [d.device_id for d in r.safe_output_devices("BlackHole 2ch", inventory=multi_inv)]
    assert "Multi-Output Device" not in names and "BlackHole 2ch" not in names
    assert "Test Built-in Speakers" in names and "Speakers And Headset" in names
    plain = r.safe_output_devices("BlackHole 2ch", inventory=plain_inv)
    assert plain[0].device_id == "Test Built-in Speakers"  # default output first
    assert all(d.device_id not in ("VB-Cable", "BlackHole 16ch") for d in plain)
    with_virtual = r.safe_output_devices("BlackHole 2ch", inventory=plain_inv, include_virtual=True)
    assert any(d.label == "VB-Cable (virtual)" for d in with_virtual)


# ---------------------------------------------------------------- gate

def _chunk(ms):
    return b"\x01\x00" * int(ENGINE_SAMPLE_RATE * ms / 1000)


def test_gate_zeroes_overlap_and_tail_only():
    gate = r.CaptureGate(tail_s=0.25, lead_s=0.02)
    got = []
    sink = gate.wrap(lambda pcm, ts: got.append((ts, pcm)))
    ms = 1_000_000
    gate.mark_audible(1000 * ms, 1100 * ms)
    gate.mark_audible(1100 * ms, 1500 * ms)  # contiguous blocks merge into one span
    sink(_chunk(100), 800 * ms)    # ends 900 ms, before lead window
    sink(_chunk(100), 1200 * ms)   # during speech
    sink(_chunk(100), 1700 * ms)   # within the 250 ms tail
    sink(_chunk(100), 1800 * ms)   # after the tail
    zeroed = [not any(p) for _, p in got]
    assert zeroed == [False, True, True, False]
    assert [len(p) for _, p in got] == [3200] * 4
    assert gate.gated_chunks == 2
