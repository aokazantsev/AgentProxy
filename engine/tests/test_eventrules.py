from types import SimpleNamespace

import pytest

from agentproxy import eventrules
from agentproxy.eventrules import INCOMING, OUTGOING, EventChanger, EventStreams, SseEditor
from agentproxy.rules import APPLIED_MARK, RuleError, RuleSet, normalize


@pytest.fixture
def rules(tmp_path):
    return RuleSet(str(tmp_path))


def events_rule(rule_id="r1", **fields):
    rule = normalize(dict({"action": "events", "path": "/feed"}, **fields))
    rule["id"] = rule_id
    return rule


class Closer:
    def __init__(self, problem=None):
        self.problem = problem
        self.calls = []

    def __call__(self, moment):
        self.calls.append(moment)
        return self.problem


def changer(*rules, closer=None):
    flow = SimpleNamespace(metadata={})
    return EventChanger(flow, list(rules), closer or Closer())


def edit(editor, *chunks):
    out = b""
    for chunk in chunks:
        result = editor(chunk)
        out += result if isinstance(result, bytes) else b"".join(result)
    return out


@pytest.mark.parametrize("fields, message", [
    ({}, "needs at least one of replace, event_data, close"),
    ({"close": "after", "delay_ms": 100}, "delay_ms is not used with action events"),
    ({"close": "after", "download_kbps": 100}, "download_kbps is not used with action events"),
    ({"close": "later"}, "close must be one of before, after"),
    ({"close": "after", "direction": "both"}, "direction must be one of incoming, outgoing"),
    ({"event_data": "x", "replace": [{"find": "a", "with": "b"}]}, "either event_data or replace"),
    ({"event_contains": "", "close": "after"}, "event_contains must be a non-empty string"),
    ({"close": "after", "status": 500}, "status is used only with action respond or rewrite, not events"),
])
def test_events_rule_is_checked(fields, message):
    with pytest.raises(RuleError, match=message):
        normalize(dict({"action": "events", "host": "example.com"}, **fields))


def test_events_rule_keeps_its_fields_and_allows_empty_data():
    rule = normalize({"action": "events", "host": "example.com", "event_data": "", "close": "before",
                      "direction": "outgoing", "event_contains": "stop", "times": 1})
    assert rule == {"action": "events", "host": "example.com", "enabled": True, "times": 1, "event_contains": "stop",
                    "direction": "outgoing", "event_data": "", "close": "before"}


def test_events_rules_are_planned_for_streams_only(rules):
    rules.add({"action": "events", "path": "/feed", "close": "after", "times": 1})
    rules.add({"action": "rewrite", "path": "/feed", "response_headers": {"X-A": "1"}})
    assert [rule["action"] for rule in rules.plan("example.com", "/feed", "GET", "http://example.com/feed")] == [
        "rewrite"]
    assert rules.list()[0]["hits"] == 0
    assert [rule["id"] for rule in rules.plan_events("example.com", "/feed", "GET", "http://example.com/feed")] == [
        "r1"]
    assert rules.plan_events("example.com", "/feed", "GET", "http://example.com/feed") == []
    assert rules.list()[0]["remaining"] == 0


def test_closing_rule_with_host_asks_for_http1(rules):
    rules.add({"action": "events", "host": "rt.example.com", "replace": [{"find": "a", "with": "b"}]})
    assert not rules.needs_http1("rt.example.com")
    rules.add({"action": "events", "host": "rt.example.com", "close": "after"})
    assert rules.needs_http1("rt.example.com")


def test_data_is_replaced_and_other_fields_stay():
    editor = SseEditor(changer(events_rule(replace=[{"find": "old", "with": "new"}])))
    stream = b"event: tick\r\nid: 7\r\ndata: old\r\ndata:old too\r\nretry: 5\r\n\r\n"
    assert edit(editor, stream) == b"event: tick\r\nid: 7\r\ndata: new\r\ndata: new too\r\nretry: 5\r\n\r\n"


def test_unchanged_events_keep_their_bytes():
    editor = SseEditor(changer(events_rule(replace=[{"find": "zzz", "with": "y"}])))
    stream = b"data:a\n\n: keepalive\n\ndata: b\r\rdata: c\n\n"
    assert edit(editor, stream) == stream


def test_event_split_at_every_byte_gives_the_same_output():
    stream = b"\xef\xbb\xbfdata: one\r\n\r\n: note one\r\ndata: one two\r\n\r\ndata: one\r\rdata: four\n\n"
    whole = edit(SseEditor(changer(events_rule(replace=[{"find": "one", "with": "1"}]))), stream)
    pieces = edit(SseEditor(changer(events_rule(replace=[{"find": "one", "with": "1"}]))),
                  *[stream[index:index + 1] for index in range(len(stream))])
    assert whole == pieces == (b"\xef\xbb\xbfdata: 1\r\n\r\n: note one\r\ndata: 1 two\r\n\r\ndata: 1\r\r"
                               b"data: four\n\n")


def test_comments_are_never_matched_or_changed():
    editor = SseEditor(changer(events_rule(event_contains="ping", close="before")))
    assert edit(editor, b": ping\n\ndata: pong\n\n") == b": ping\n\ndata: pong\n\n"
    assert not editor.changer.closed


def test_new_data_with_line_breaks_becomes_several_data_lines():
    editor = SseEditor(changer(events_rule(event_data="a\r\nb\nc")))
    assert edit(editor, b"id: 1\ndata: x\n\n") == b"id: 1\ndata: a\ndata: b\ndata: c\n\n"


def test_close_before_keeps_the_event_and_everything_after_from_the_app():
    closer = Closer()
    editor = SseEditor(changer(events_rule(event_contains="2", close="before"), closer=closer))
    assert edit(editor, b"data: 1\n\ndata: 2\n\ndata: 3\n\n", b"data: 4\n\n") == b"data: 1\n\n"
    assert closer.calls == ["before"]
    entry = editor.changer.flow.metadata[APPLIED_MARK][0]
    assert entry["closed"] == "before incoming event 2"
    assert editor.changer.take_notes() == ["rule r1 closed the stream before incoming event 2"]


def test_close_after_delivers_the_event():
    editor = SseEditor(changer(events_rule(event_contains="2", close="after")))
    assert edit(editor, b"data: 1\n\ndata: 2\n\ndata: 3\n\n") == b"data: 1\n\ndata: 2\n\n"


def test_failed_close_delivers_the_event_with_an_error():
    editor = SseEditor(changer(events_rule(close="before"), closer=Closer("not TCP")))
    assert edit(editor, b"data: 1\n\n") == b"data: 1\n\n"
    assert editor.changer.flow.metadata[APPLIED_MARK][0]["error"] == "not TCP"


def test_oversized_event_turns_editing_off_for_the_rest(monkeypatch):
    monkeypatch.setattr(eventrules, "MAX_BLOCK_BYTES", 16)
    editor = SseEditor(changer(events_rule(replace=[{"find": "a", "with": "b"}])))
    stream = b"data: " + b"a" * 20 + b"\n\ndata: a\n\n"
    assert edit(editor, stream[:20], stream[20:]) == stream
    assert "larger than" in editor.changer.flow.metadata[APPLIED_MARK][0]["skipped"]


def test_before_wins_over_after_and_directions_are_kept_apart():
    closer = Closer()
    events = changer(events_rule("r1", close="after"), events_rule("r2", close="before"),
                     events_rule("r3", direction="outgoing", event_data="x"), closer=closer)
    assert events.handle(OUTGOING, "hello") == ("x", True)
    assert events.handle(INCOMING, "hello") == ("hello", False)
    assert closer.calls == ["before"]
    assert events.handle(INCOMING, "late") == ("late", False)


def test_binary_messages_are_only_closed():
    events = changer(events_rule("r1", replace=[{"find": "a", "with": "b"}]),
                     events_rule("r2", event_contains="a", close="before"), events_rule("r3", close="after"))
    assert events.handle(INCOMING, None) == (None, True)
    entries = {entry["id"]: entry for entry in events.flow.metadata[APPLIED_MARK]}
    assert "binary" in entries["r1"]["skipped"] and "closed" not in entries["r2"]
    assert entries["r3"]["closed"] == "after incoming event 1"


def test_changed_and_deleted_rules_apply_to_open_streams():
    events = changer(events_rule("r1", replace=[{"find": "a", "with": "b"}]))
    streams = EventStreams()
    streams.add("flow-1", events)
    streams.change_rule("r1", dict(events_rule("r1", replace=[{"find": "a", "with": "c"}]), hits=1))
    assert events.handle(INCOMING, "a") == ("c", True)
    streams.change_rule("r1", None)
    assert events.handle(INCOMING, "a") == ("a", True)
    streams.release("flow-1")
    assert streams.live == {}
