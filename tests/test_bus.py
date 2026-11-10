import asyncio
import threading

from hermeneus_engine.bus import SubtitleBus, languages_payload, wire_language
from hermeneus_engine.contracts import LineState, SubtitleLine


def line(line_id, text="hi", state=LineState.PARTIAL, speaker=None):
    return SubtitleLine(line_id=line_id, state=state, original=text, speaker_id=speaker, target_language="tr")


def test_same_line_id_updates_in_place_and_seq_grows():
    bus = SubtitleBus()
    a = bus.publish_line(line("a", "he"))
    b = bus.publish_line(line("a", "hello", LineState.COMMITTED))
    assert b["seq"] == a["seq"] + 1
    assert [l.original for l in bus.lines()] == ["hello"]
    assert b == {"type": "line", "seq": b["seq"], "line": bus.lines()[0].to_dict()}


def test_seq_is_shared_by_all_event_types_and_never_goes_back_across_buses():
    first = SubtitleBus()
    seqs = [first.publish_line(line("a"))["seq"], first.publish_relabel("x", "y")["seq"],
            first.publish_status(state="live")["seq"]]
    second = SubtitleBus()
    seqs.append(second.publish_line(line("b"))["seq"])
    assert seqs == sorted(seqs) and len(set(seqs)) == 4


def test_events_after_without_after_is_a_reset_with_every_line():
    bus = SubtitleBus()
    bus.publish_line(line("a"))
    bus.publish_line(line("b"))
    events, reset = bus.events_after(None)
    assert reset is True
    assert [e["line"]["line_id"] for e in events if e["type"] == "line"] == ["a", "b"]
    assert any(e["type"] == "status" for e in events)


def test_events_after_returns_changes_and_relabels_since_seq():
    bus = SubtitleBus()
    bus.publish_line(line("a", speaker="s1:speaker_1"))
    mark = bus.last_seq
    bus.publish_line(line("b"))
    rel = bus.publish_relabel("s1:speaker_1", "s1:speaker_0")
    events, reset = bus.events_after(mark)
    assert reset is False
    assert [e["type"] for e in events] == ["line", "relabel"]
    assert events[1] == rel
    assert bus.lines()[0].speaker_id == "s1:speaker_0"


def test_after_outside_buffer_resets():
    bus = SubtitleBus(max_lines=2)
    first = bus.publish_line(line("a"))["seq"]
    bus.publish_line(line("b"))
    bus.publish_line(line("c"))  # evicts a
    assert bus.events_after(first - 1)[1] is True
    assert bus.events_after(bus.last_seq + 100)[1] is True  # seq from a restarted owner


def test_hello_carries_latest_seq_and_status():
    bus = SubtitleBus()
    bus.publish_status(state="live", target_language="tr")
    hello = bus.hello()
    assert hello["type"] == "hello" and hello["version"] == 1
    assert hello["seq"] == bus.last_seq == hello["session"]["seq"]
    assert hello["session"]["state"] == "live"


def test_status_lists_languages_with_wire_codes():
    langs = {l["code"]: l for l in languages_payload()}
    assert len(langs) == 100
    assert langs["tr"]["speakable"] is True and langs["tr"]["name"] == "Turkish"
    assert wire_language("eng") == "en" and wire_language("en-US") == "en"
    assert wire_language("zzz-unknown") == "zzz-unknown"


async def test_subscriber_gets_events_published_from_another_thread():
    bus = SubtitleBus()
    sub = bus.subscribe()
    t = threading.Thread(target=bus.publish_line, args=(line("a"),))
    t.start()
    t.join()
    event = await sub.get(timeout=1)
    assert event["line"]["line_id"] == "a"
    bus.close()
    assert await sub.get(timeout=1) is None


async def test_slow_subscriber_is_cut_off_and_flagged():
    bus = SubtitleBus()
    sub = bus.subscribe(maxsize=3)
    for i in range(10):
        bus.publish_line(line(f"l{i}"))
    await asyncio.sleep(0.01)
    got = [e async for e in sub]
    assert sub.overflowed is True
    assert len(got) == 3
    assert bus.subscriber_count == 0
