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

    def __call__(self, moment, direction):
        self.calls.append(moment)
        return self.problem


def changer(*rules, closer=None, whole_connection=False):
    flow = SimpleNamespace(metadata={})
    return EventChanger(flow, list(rules), closer or Closer(), whole_connection)


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


def test_refused_stream_does_not_spend_times(rules):
    rules.add({"action": "events", "path": "/feed", "close": "after", "times": 1})
    assert [rule["id"] for rule in rules.plan_events("example.com", "/feed", "GET", "http://example.com/feed",
                                                     count=False)] == ["r1"]
    assert rules.list()[0]["remaining"] == 1


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


def test_event_ended_by_cr_is_delivered_at_once():
    editor = SseEditor(changer(events_rule(replace=[{"find": "x", "with": "y"}])))
    assert edit(editor, b"data: x\r\r") == b"data: y\r\r"
    assert edit(editor, b"data: x\r", b"\r") == b"data: y\r\r"
    assert edit(editor, b"data: x\r\n\r", b"\ndata: x\n\n") == b"data: y\r\n\r\ndata: y\n\n"


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


def test_finished_stream_is_released_after_the_recorder_wraps_the_editor(rules):
    import asyncio
    from mitmproxy.test import tflow, tutils
    from agentproxy.rewriter import Rewriter
    rules.add({"action": "events", "path": "/feed", "replace": [{"find": "a", "with": "b"}]})
    rewriter = Rewriter(rules)
    flow = tflow.tflow(resp=tutils.tresp(headers=[(b"content-type", b"text/event-stream"),
                                                  (b"content-length", b"9")], content=None))
    flow.request.path = "/feed"
    rewriter.responseheaders(flow)
    editor = flow.response.stream
    assert isinstance(editor, SseEditor) and "content-length" not in flow.response.headers
    flow.response.stream = lambda chunk: editor(chunk)
    asyncio.run(rewriter.response(flow))
    assert rewriter.events.live == {}


def test_new_data_keeps_its_leading_spaces_when_the_server_writes_data_without_a_space():
    editor = SseEditor(changer(events_rule(event_data=" a\n  b")))
    assert edit(editor, b"data:x\n\n") == b"data:  a\ndata:   b\n\n"


def test_disabled_or_changed_rule_stops_changing_open_streams():
    events = changer(events_rule("r1", replace=[{"find": "a", "with": "b"}]),
                     events_rule("r2", replace=[{"find": "c", "with": "d"}]))
    streams = EventStreams()
    streams.add("flow-1", events)
    streams.change_rule("r1", dict(events_rule("r1", replace=[{"find": "a", "with": "b"}]), enabled=False))
    streams.change_rule("r2", normalize({"action": "rewrite", "path": "/feed", "status": 201}))
    assert events.handle(INCOMING, "ac") == ("ac", True)


def test_notes_and_counts_are_kept_per_stream():
    events = changer(events_rule("r1", replace=[{"find": "a", "with": "b"}]),
                     events_rule("r2", close="after", event_contains="stop"), whole_connection=True)
    events.handle(INCOMING, "aa")
    events.handle(INCOMING, "aa stop")
    first, second = events.flow.metadata[APPLIED_MARK]
    assert first["changed"] == 2 and first["replaced"] == 4
    assert second["closed"].endswith(eventrules.WHOLE_CONNECTION)
    assert len(events.take_notes()) == 1 and events.take_notes() == []


def test_oversized_event_passes_later_chunks_at_once(monkeypatch):
    monkeypatch.setattr(eventrules, "MAX_BLOCK_BYTES", 16)
    editor = SseEditor(changer(events_rule(replace=[{"find": "a", "with": "b"}])))
    edit(editor, b"data: " + b"a" * 20)
    assert editor(b"partial") == b"partial"


def _rewriter_with(rules, rule):
    from agentproxy.rewriter import Rewriter
    rules.add(rule)
    return Rewriter(rules)


def test_injected_websocket_message_is_not_changed_by_an_events_rule(rules):
    import asyncio
    from mitmproxy.test import tflow
    from mitmproxy.websocket import WebSocketMessage
    from wsproto.frame_protocol import Opcode
    from agentproxy.rules import EVENTS_MARK
    rewriter = _rewriter_with(rules, {"action": "events", "path": "/feed", "replace": [{"find": "a", "with": "b"}],
                                      "close": "before"})
    flow = tflow.twebsocketflow(messages=False)
    flow.request.path = "/feed"
    asyncio.run(rewriter.response(flow))
    flow.websocket.messages.append(WebSocketMessage(Opcode.TEXT, False, b"a", injected=True))
    rewriter.websocket_message(flow)
    message = flow.websocket.messages[-1]
    assert message.text == "a" and not message.dropped and not flow.metadata[EVENTS_MARK].closed


@pytest.mark.parametrize("encoding, edited", [("identity", True), ("gzip", False)])
def test_identity_stream_is_edited_and_a_compressed_one_is_not(rules, encoding, edited):
    from mitmproxy.test import tflow, tutils
    rewriter = _rewriter_with(rules, {"action": "events", "path": "/feed", "replace": [{"find": "a", "with": "b"}]})
    flow = tflow.tflow(resp=tutils.tresp(headers=[(b"content-type", b"text/event-stream"),
                                                  (b"content-encoding", encoding.encode())], content=None))
    flow.request.path = "/feed"
    rewriter.responseheaders(flow)
    assert isinstance(flow.response.stream, SseEditor) is edited


def test_stream_of_an_sse_rule_is_not_edited_and_spends_no_times(rules):
    from mitmproxy.test import tflow, tutils
    from agentproxy.rules import PLAN_MARK
    rewriter = _rewriter_with(rules, {"action": "events", "path": "/feed", "replace": [{"find": "a", "with": "b"}],
                                      "times": 1})
    flow = tflow.tflow(resp=tutils.tresp(headers=[(b"content-type", b"text/event-stream")], content=None))
    flow.request.path = "/feed"
    flow.metadata[PLAN_MARK] = [{"id": "r9", "action": "sse"}]
    rewriter._edit_event_stream(flow)
    assert not callable(flow.response.stream)
    assert "sse rule" in next(entry for entry in flow.metadata[APPLIED_MARK] if entry["id"] == "r1")["skipped"]
    assert rules.list()[0]["remaining"] == 1


def test_comment_line_without_an_empty_line_reaches_the_app_at_once():
    editor = SseEditor(changer(events_rule(replace=[{"find": "a", "with": "b"}])))
    assert edit(editor, b"data: a\n\n: ping\n") == b"data: b\n\n: ping\n"
    assert edit(editor, b": ping\r\ndata: a") == b": ping\r\n"
    assert edit(editor, b"\n\n") == b"data: b\n\n"


def test_messages_sent_after_a_close_are_dropped_even_when_injected(rules):
    import asyncio
    from mitmproxy.test import tflow
    from mitmproxy.websocket import WebSocketMessage
    from wsproto.frame_protocol import Opcode
    from agentproxy.rules import EVENTS_MARK
    rewriter = _rewriter_with(rules, {"action": "events", "path": "/feed", "event_contains": "stop",
                                      "close": "before"})
    flow = tflow.twebsocketflow(messages=False)
    flow.request.path = "/feed"
    asyncio.run(rewriter.response(flow))
    flow.metadata[EVENTS_MARK].closer = lambda moment, direction: None
    flow.websocket.messages.append(WebSocketMessage(Opcode.TEXT, False, b"stop"))
    rewriter.websocket_message(flow)
    flow.websocket.messages.append(WebSocketMessage(Opcode.TEXT, True, b"late", injected=True))
    rewriter.websocket_message(flow)
    assert flow.websocket.messages[-1].dropped


def test_many_small_chunks_after_a_long_line_stay_linear():
    import time
    editor = SseEditor(changer(events_rule(replace=[{"find": "a", "with": "b"}])))
    editor(b"data: " + b"x" * 900000 + b"\n")
    started = time.perf_counter()
    for _ in range(5000):
        editor(b"id: 1\n")
    assert time.perf_counter() - started < 2


@pytest.mark.parametrize("client, server, chunked", [
    ("HTTP/1.1", "HTTP/1.1", True),
    ("HTTP/1.1", "HTTP/1.0", False),
    ("HTTP/1.0", "HTTP/1.1", False),
    ("HTTP/2.0", "HTTP/1.1", False),
    ("HTTP/2.0", "HTTP/2.0", False),
])
def test_edited_sse_stream_is_chunked_only_for_an_http11_client_of_an_http11_server(rules, client, server, chunked):
    from mitmproxy.test import tflow, tutils
    rewriter = _rewriter_with(rules, {"action": "events", "path": "/feed", "replace": [{"find": "a", "with": "b"}]})
    flow = tflow.tflow(resp=tutils.tresp(headers=[(b"content-type", b"text/event-stream"),
                                                  (b"content-length", b"9")], content=None))
    flow.request.path = "/feed"
    flow.request.http_version = client
    flow.response.http_version = server
    rewriter.responseheaders(flow)
    assert isinstance(flow.response.stream, SseEditor) and "content-length" not in flow.response.headers
    assert (flow.response.headers.get("transfer-encoding") == "chunked") is chunked


def test_comment_after_a_data_line_waits_for_the_end_of_its_event():
    editor = SseEditor(changer(events_rule(replace=[{"find": "x", "with": "y"}])))
    assert edit(editor, b": a\ndata: x\n: b\n") == b": a\n"
    assert edit(editor, b"\n") == b"data: y\n: b\n\n"


def test_comment_after_a_close_does_not_reach_the_app():
    editor = SseEditor(changer(events_rule(event_contains="2", close="before")))
    assert edit(editor, b"data: 1\n\ndata: 2\n\n: ping\n") == b"data: 1\n\n"
    editor = SseEditor(changer(events_rule(event_contains="2", close="after")))
    assert edit(editor, b"data: 2\n\n: ping\n") == b"data: 2\n\n"


def test_new_data_lines_take_the_line_ending_of_the_data_line_not_of_the_first_line():
    editor = SseEditor(changer(events_rule(replace=[{"find": "x", "with": "y"}])))
    assert edit(editor, b"id: 1\r\ndata: x\n\n") == b"id: 1\r\ndata: y\n\n"


def test_comments_then_a_split_event_keep_line_tracking():
    editor = SseEditor(changer(events_rule(replace=[{"find": "x", "with": "y"}])))
    assert edit(editor, b": a\n: b\nda", b"ta: x\n", b": c\n", b"\n") == b": a\n: b\ndata: y\n: c\n\n"


def test_send_event_is_refused_after_an_events_rule_closed_the_connection():
    from agentproxy.injector import InjectError, WebSocketInjector
    from agentproxy.rules import EVENTS_MARK
    events = changer(events_rule(close="before"))
    events.handle(INCOMING, "x")
    flow = SimpleNamespace(live=True, websocket=object(), metadata={EVENTS_MARK: events})
    injector = WebSocketInjector(master=None, recorder=SimpleNamespace(live_websocket=lambda seq: flow))
    with pytest.raises(InjectError, match="events rule closed"):
        injector.send(1, True, b"late", True)


def test_outgoing_rule_does_not_take_sse_streams(rules):
    rules.add({"action": "events", "host": "example.com", "direction": "outgoing", "close": "after", "times": 1})
    assert rules.plan_events("example.com", "/feed", "GET", "http://example.com/feed", websocket=False) == []
    assert rules.list()[0]["remaining"] == 1
    assert [rule["id"] for rule in rules.plan_events("example.com", "/ws", "GET", "http://example.com/ws")] == ["r1"]


def test_dropped_event_is_not_counted_as_changed():
    events = changer(events_rule("r1", replace=[{"find": "a", "with": "b"}]),
                     events_rule("r2", event_contains="stop", close="before"))
    assert events.handle(INCOMING, "a stop") == ("b stop", False)
    entries = {entry["id"]: entry for entry in events.flow.metadata[APPLIED_MARK]}
    assert "changed" not in entries["r1"] and "replaced" not in entries["r1"]


def test_mixed_line_endings_keep_events_apart():
    editor = SseEditor(changer(events_rule(replace=[{"find": "x", "with": "z"}])))
    assert edit(editor, b"data: x\rdata: y\n\ndata: next\n\n") == b"data: z\rdata: y\r\n\ndata: nezt\n\n"


def test_empty_chunk_after_an_oversized_event_is_not_an_end(monkeypatch):
    monkeypatch.setattr(eventrules, "MAX_BLOCK_BYTES", 16)
    editor = SseEditor(changer(events_rule(replace=[{"find": "a", "with": "b"}])))
    edit(editor, b"data: " + b"a" * 20)
    assert editor(b"") == []


@pytest.mark.parametrize("status, encoding, edited", [
    (200, "none", True), (401, "", False), (500, "", False), (200, "x" * 40, False)])
def test_only_successful_identity_streams_are_edited(rules, status, encoding, edited):
    from mitmproxy.test import tflow, tutils
    rewriter = _rewriter_with(rules, {"action": "events", "path": "/feed", "replace": [{"find": "a", "with": "b"}]})
    headers = [(b"content-type", b"text/event-stream")] + ([(b"content-encoding", encoding.encode())] if encoding else [])
    flow = tflow.tflow(resp=tutils.tresp(status_code=status, headers=headers, content=None))
    flow.request.path = "/feed"
    rewriter.responseheaders(flow)
    assert isinstance(flow.response.stream, SseEditor) is edited
    skipped = [entry.get("skipped", "") for entry in flow.metadata.get(APPLIED_MARK, [])]
    assert all("x" * 40 not in text for text in skipped)
