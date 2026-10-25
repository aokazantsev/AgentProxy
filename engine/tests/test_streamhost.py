import asyncio
import types

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

from agentproxy import streamhost
from agentproxy.rewriter import Rewriter
from agentproxy.rules import RuleError, RuleSet, normalize
from agentproxy.service import ProxyService, ToolError
from agentproxy.streamhost import STREAM_HEADER, StreamHost, StreamRefused, build_frame


class FakeStreams:
    def __init__(self):
        self.released = []
        self.reserved = []

    def reserve(self, flow_id, rule_id, headers, body):
        self.reserved.append(flow_id)
        return types.SimpleNamespace(token="one-time-key", port=4321)

    def release(self, flow_id):
        self.released.append(flow_id)


def rerouted_flow(tmp_path, method="GET", rules_before=(), between=None):
    rules = RuleSet(str(tmp_path))
    for rule in rules_before:
        rules.add(rule)
    rule_id = rules.add({"action": "sse", "host": "address"})["id"]
    rewriter = Rewriter(rules, FakeStreams())
    flow = tflow.tflow()
    flow.request.method = method
    flow.request.headers["Host"] = "ADDRESS:22"
    asyncio.run(rewriter.requestheaders(flow))
    if between is not None:
        between(rules, rule_id)
    asyncio.run(rewriter.request(flow))
    return rewriter, flow


def test_head_under_an_sse_rule_gets_stream_headers_without_a_stream(tmp_path):
    rewriter, flow = rerouted_flow(tmp_path, method="HEAD")
    assert flow.response.status_code == 200 and flow.response.headers["Content-Type"] == "text/event-stream"
    assert rewriter.streams.reserved == [] and flow.request.host == "address"


def test_rule_deleted_or_disabled_while_the_request_waited_gives_no_stream(tmp_path):
    for change in (lambda rules, rule_id: rules.delete(rule_id),
                   lambda rules, rule_id: rules.update(rule_id, {"enabled": False})):
        folder = tmp_path / str(id(change))
        folder.mkdir()
        rewriter, flow = rerouted_flow(folder, between=change)
        assert flow.response.status_code == 503 and rewriter.streams.reserved == []
        assert "while the request waited" in flow.metadata["agentproxy_rules"][-1]["error"]


def test_rewrite_before_an_sse_rule_leaves_the_stream_response_alone(tmp_path):
    rewriter, flow = rerouted_flow(tmp_path, rules_before=[
        {"action": "rewrite", "host": "address", "status": 500, "response_headers": {"Content-Type": "text/plain"}}])
    flow.response = http.Response.make(200, b"", {"Content-Type": "text/event-stream"})
    rewriter.responseheaders(flow)
    assert flow.response.status_code == 200 and flow.response.headers["Content-Type"] == "text/event-stream"
    assert "sse rule" in flow.metadata["agentproxy_rules"][0]["skipped"]


@pytest.mark.parametrize("arguments, text", [
    ({"event": "update"}, "without data"),
    ({"id": "7"}, "without data"),
    ({"data": "\ud800"}, "valid Unicode"),
    ({"data": "x", "retry": 86400001}, "retry"),
])
def test_sse_events_the_app_would_not_get_are_refused(arguments, text):
    service = ProxyService.__new__(ProxyService)
    service.streams = types.SimpleNamespace(find=lambda seq: None)
    with pytest.raises(ToolError) as refusal:
        service._send_sse({"seq": 7, "kind": "sse", "open": True, "rules": []}, arguments)
    assert text in str(refusal.value)


def test_rule_stream_request_goes_to_the_listener_and_comes_back_whole_for_the_history(tmp_path):
    rewriter, flow = rerouted_flow(tmp_path)
    request = flow.request
    assert (request.scheme, request.host, request.port) == ("http", "127.0.0.1", 4321)
    assert request.headers[STREAM_HEADER] == "one-time-key"
    flow.response = http.Response.make(200, b"", {"Content-Type": "text/event-stream"})
    rewriter.responseheaders(flow)
    assert (request.scheme, request.host, request.port) == ("http", "address", 22)
    assert request.headers["Host"] == "ADDRESS:22" and STREAM_HEADER not in request.headers


def test_failed_rule_stream_request_comes_back_whole_and_frees_its_slot(tmp_path):
    rewriter, flow = rerouted_flow(tmp_path)
    rewriter.error(flow)
    assert (flow.request.host, flow.request.port, flow.request.headers["Host"]) == ("address", 22, "ADDRESS:22")
    assert STREAM_HEADER not in flow.request.headers
    assert rewriter.streams.released == [flow.id]


def test_closing_a_rule_leaves_streams_of_other_rules_alone():
    async def scenario():
        host = StreamHost()
        await host.start()
        host.reserve("flow-1", "r1", {}, "")
        host.reserve("flow-2", "r2", {}, "")
        host.close_rules(["r1"])
        assert list(host._by_flow) == ["flow-2"] and len(host._pending) == 1
        host.stop()
    asyncio.run(scenario())


def test_sending_into_a_closing_rule_stream_names_the_rule_not_the_server():
    service = ProxyService.__new__(ProxyService)
    service.streams = types.SimpleNamespace(find=lambda seq: None)
    summary = {"seq": 7, "kind": "sse", "open": True, "rules": [{"id": "r3", "action": "sse"}]}
    with pytest.raises(ToolError) as refusal:
        service._send_sse(summary, {"data": "x"})
    assert "rule r3" in str(refusal.value) and "closing" in str(refusal.value)
    summary["rules"] = [{"id": "r4", "action": "rewrite"}]
    with pytest.raises(ToolError) as refusal:
        service._send_sse(summary, {"data": "x"})
    assert "real server" in str(refusal.value)


def test_frame_has_one_line_per_field_and_ends_with_an_empty_line():
    assert build_frame("a") == b"data: a\n\n"
    assert build_frame("a", "ping", "5", 1000) == b"event: ping\nid: 5\nretry: 1000\ndata: a\n\n"
    assert build_frame(event="x") == b"event: x\n\n"
    assert build_frame(retry=0) == b"retry: 0\n\n"


def test_data_line_breaks_of_every_kind_become_data_lines():
    assert build_frame("a\r\nb\rc\nd") == b"data: a\ndata: b\ndata: c\ndata: d\n\n"
    assert build_frame("a\n") == b"data: a\ndata: \n\n"
    assert build_frame("") == b"data: \n\n"
    assert build_frame("a\u2028b\x0bc") == "data: a\u2028b\x0bc\n\n".encode("utf-8")
    assert build_frame("привет") == "data: привет\n\n".encode("utf-8")


def run(coroutine):
    return asyncio.run(coroutine)


async def connect(port, token, extra=""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = "GET /x HTTP/1.1\r\nHost: x\r\n" + ("{0}: {1}\r\n".format(STREAM_HEADER, token) if token else "") + extra
    writer.write((lines + "\r\n").encode())
    return reader, writer


async def read_head(reader):
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)


def test_listener_serves_only_the_stream_a_token_was_reserved_for():
    async def scenario():
        host = StreamHost()
        assert await host.start()
        stream = host.reserve("flow-1", "r1", {"X-Mock": "1"}, "data: first\n\n")
        reader, writer = await connect(stream.port, stream.token)
        head = await read_head(reader)
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"Content-Type: text/event-stream\r\n" in head and b"Cache-Control: no-cache\r\n" in head
        assert b"X-Mock: 1\r\n" in head and b"Transfer-Encoding: chunked\r\n" in head
        assert await reader.readuntil(b"\r\n") == b"d\r\n"
        assert await reader.readexactly(15) == b"data: first\n\n\r\n"
        await asyncio.sleep(0.05)
        assert host.find("flow-1") is stream and stream.is_open
        stream.send(b"data: x\n\n")
        assert await reader.readuntil(b"\r\n") == b"9\r\n"
        assert await reader.readexactly(11) == b"data: x\n\n\r\n"
        stream.close()
        assert await asyncio.wait_for(reader.read(), 5) == b"0\r\n\r\n"
        await asyncio.sleep(0.05)
        assert host.find("flow-1") is None
        with pytest.raises(StreamRefused):
            stream.send(b"late")
        host.stop()
    run(scenario())


def test_a_token_works_once_and_forged_requests_get_404():
    async def scenario():
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow-1", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        for token in (None, "wrong", stream.token):
            other_reader, other_writer = await connect(stream.port, token)
            assert (await asyncio.wait_for(other_reader.read(), 5)).startswith(b"HTTP/1.1 404")
            other_writer.close()
        assert stream.is_open
        writer.close()
        host.stop()
    run(scenario())


def test_the_app_leaving_closes_the_stream():
    async def scenario():
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow-1", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        await asyncio.sleep(0.05)
        writer.close()
        for _ in range(50):
            if host.find("flow-1") is None:
                break
            await asyncio.sleep(0.05)
        assert host.find("flow-1") is None and stream.closed
        host.stop()
    run(scenario())


def test_a_stream_that_nobody_connected_to_expires_and_release_frees_the_slot(monkeypatch):
    async def scenario():
        host = StreamHost()
        await host.start()
        host.reserve("lost", "r1", {}, "")
        host.release("lost")
        assert host.find("lost") is None and not host._pending
        host.reserve("lost-2", "r1", {}, "")
        monkeypatch.setattr(streamhost, "PENDING_SECONDS", -1)
        host.reserve("next", "r1", {}, "")
        assert list(host._by_flow) == ["next"]
        host.stop()
    run(scenario())


def test_open_streams_are_limited(monkeypatch):
    async def scenario():
        host = StreamHost()
        await host.start()
        monkeypatch.setattr(streamhost, "MAX_STREAMS", 2)
        host.reserve("one", "r1", {}, "")
        host.reserve("two", "r1", {}, "")
        with pytest.raises(StreamRefused) as refusal:
            host.reserve("three", "r1", {}, "")
        assert "2 streams" in str(refusal.value)
        host.stop()
    run(scenario())


def test_reserving_without_a_listener_is_refused():
    with pytest.raises(StreamRefused) as refusal:
        StreamHost().reserve("flow", "r1", {}, "")
    assert "not running" in str(refusal.value)


def test_close_of_a_stream_the_app_does_not_read_cuts_the_connection(monkeypatch):
    async def scenario():
        monkeypatch.setattr(streamhost, "CLOSE_GRACE_SECONDS", 0.2)
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        await asyncio.sleep(0.05)
        with pytest.raises(StreamRefused):
            for _ in range(200):
                stream.send(b"x" * 1024 * 1024)
                await asyncio.sleep(0.01)
        stream.close()
        for _ in range(50):
            if host.find("flow") is None and "flow" not in host._by_flow:
                break
            await asyncio.sleep(0.05)
        assert "flow" not in host._by_flow
        writer.close()
        host.stop()
    run(scenario())


class RefusingStreams(FakeStreams):
    def reserve(self, flow_id, rule_id, headers, body):
        raise StreamRefused("50 streams of sse rules are open already")


def test_refused_reservation_answers_503_and_marks_the_rule(tmp_path):
    rules = RuleSet(str(tmp_path))
    rule_id = rules.add({"action": "sse", "host": "address"})["id"]
    rewriter = Rewriter(rules, RefusingStreams())
    flow = tflow.tflow()
    asyncio.run(rewriter.requestheaders(flow))
    asyncio.run(rewriter.request(flow))
    assert flow.response.status_code == 503 and flow.request.host == "address"
    assert "open already" in flow.metadata["agentproxy_rules"][-1]["error"]
    assert "open already" in rules.get(rule_id)["last_error"]


def test_sse_rule_refuses_uploads_it_cannot_buffer(tmp_path):
    rules = RuleSet(str(tmp_path))
    rules.add({"action": "sse", "host": "address"})
    rewriter = Rewriter(rules, FakeStreams())
    flow = tflow.tflow()
    flow.request.headers["content-length"] = str(65 * 1024 * 1024)
    asyncio.run(rewriter.requestheaders(flow))
    assert flow.error is not None and rewriter.streams.reserved == []
    assert "cannot buffer" in flow.metadata["agentproxy_rules"][0]["error"]


def test_send_event_finds_the_stream_by_its_own_flow():
    from agentproxy.injector import StreamLookup
    recorder = types.SimpleNamespace(streams={"flow-a": types.SimpleNamespace(seq=1),
                                              "flow-b": types.SimpleNamespace(seq=2)})
    host = types.SimpleNamespace(find=lambda flow_id: "stream of " + flow_id)
    lookup = StreamLookup(host, recorder)
    assert lookup.find(2) == "stream of flow-b" and lookup.find(1) == "stream of flow-a"
    assert lookup.find(3) is None


def test_rule_headers_replace_the_default_stream_headers():
    async def scenario():
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow", "r1", {"cache-control": "max-age=0"}, "")
        reader, writer = await connect(stream.port, stream.token)
        head = (await read_head(reader)).lower()
        assert head.count(b"cache-control:") == 1 and b"cache-control: max-age=0" in head
        writer.close()
        host.stop()
    run(scenario())


def test_stopping_the_host_ends_open_streams():
    async def scenario():
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        await asyncio.sleep(0.05)
        host.stop()
        assert await asyncio.wait_for(reader.read(), 5) == b"0\r\n\r\n"
        writer.close()
    run(scenario())


def test_failed_start_leaves_no_listeners(monkeypatch):
    async def scenario():
        real = asyncio.start_server
        calls = []

        async def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) > 2:
                raise OSError("no ports")
            return await real(*args, **kwargs)

        monkeypatch.setattr(asyncio, "start_server", flaky)
        host = StreamHost()
        assert await host.start() is False
        assert host.ports == [] and all(not server.is_serving() for server in host._servers)
        with pytest.raises(StreamRefused):
            host.reserve("flow", "r1", {}, "")
    run(scenario())


def test_rule_changed_while_the_request_waited_serves_its_new_body(tmp_path):
    rules = RuleSet(str(tmp_path))
    rule_id = rules.add({"action": "sse", "host": "address", "body": "data: old\n\n"})["id"]
    streams = FakeStreams()
    streams.reserve = lambda flow_id, rule_id, headers, body: (
        streams.reserved.append(body) or types.SimpleNamespace(token="key", port=4321))
    rewriter = Rewriter(rules, streams)
    flow = tflow.tflow()
    asyncio.run(rewriter.requestheaders(flow))
    rules.update(rule_id, {"body": "data: new\n\n"})
    asyncio.run(rewriter.request(flow))
    assert streams.reserved == ["data: new\n\n"]


def test_a_stream_whose_connection_is_closing_takes_no_events():
    async def scenario():
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        await asyncio.sleep(0.05)
        stream.writer.transport.close()
        assert not stream.is_open and host.find("flow") is None
        with pytest.raises(StreamRefused):
            stream.send(b"data: x\n\n")
        writer.close()
        host.stop()
    run(scenario())


def test_head_under_a_deleted_rule_gets_no_stream_headers(tmp_path):
    rewriter, flow = rerouted_flow(tmp_path, method="HEAD", between=lambda rules, rule_id: rules.delete(rule_id))
    assert flow.response.status_code == 503


def test_head_keeps_one_header_of_each_name(tmp_path):
    rules = RuleSet(str(tmp_path))
    rules.add({"action": "sse", "host": "address", "headers": {"cache-control": "max-age=0"}})
    rewriter = Rewriter(rules, FakeStreams())
    flow = tflow.tflow()
    flow.request.method = "HEAD"
    asyncio.run(rewriter.requestheaders(flow))
    asyncio.run(rewriter.request(flow))
    assert flow.response.headers.get_all("Cache-Control") == ["max-age=0"]


@pytest.mark.parametrize("arguments", [{"close": False}, {"event": ""}, {"id": ""}])
def test_websocket_sending_ignores_empty_sse_fields(arguments):
    service = ProxyService.__new__(ProxyService)
    service.injector = None
    service.store = None
    summary = {"seq": 3, "kind": "websocket", "open": True}
    service._flow_ref = lambda value: summary
    with pytest.raises(ToolError) as refusal:
        asyncio.run(service.send_event(dict(arguments, flow="3", data="x")))
    assert "only for SSE" not in str(refusal.value)


def test_a_reader_that_does_not_read_is_cut_off(monkeypatch):
    async def scenario():
        host = StreamHost()
        await host.start()
        monkeypatch.setattr(streamhost, "MAX_BUFFERED_BYTES", 1)
        stream = host.reserve("flow", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        await asyncio.sleep(0.05)
        stream.send(b"x" * 4096)
        with pytest.raises(StreamRefused) as refusal:
            for _ in range(2000):
                stream.send(b"x" * 1024 * 1024)
        assert "does not read" in str(refusal.value)
        writer.close()
        host.stop()
    run(scenario())


def test_sse_rule_fields_and_rules_it_refuses():
    rule = normalize({"action": "sse", "host": "a.test", "headers": {"X-A": "1"}, "body": "data: 1\n\n", "times": 2})
    assert rule["action"] == "sse" and rule["headers"] == {"X-A": "1"} and rule["times"] == 2
    for field, value in (("status", 200), ("body_file", "C:/x"), ("content_type", "text/plain"),
                         ("response_body", "x"), ("replace", [{"find": "a", "with": "b"}])):
        with pytest.raises(RuleError):
            normalize({"action": "sse", "host": "a.test", field: value})
    for header in ("Content-Type", "content-encoding"):
        with pytest.raises(RuleError) as refusal:
            normalize({"action": "sse", "host": "a.test", "headers": {header: "x"}})
        assert "text/event-stream" in str(refusal.value)


def test_streams_are_spread_over_ports_so_one_client_connection_never_needs_more_than_five_per_port():
    async def scenario():
        host = StreamHost()
        await host.start()
        assert len(host.ports) == streamhost.MAX_STREAMS // streamhost.STREAMS_PER_PORT
        streams = [host.reserve("flow-{0}".format(number), "r1", {}, "") for number in range(streamhost.MAX_STREAMS)]
        per_port = {}
        for stream in streams:
            per_port[stream.port] = per_port.get(stream.port, 0) + 1
        assert set(per_port.values()) == {streamhost.STREAMS_PER_PORT}
        host.stop()
    run(scenario())


def test_an_idle_stream_gets_keepalive_comments(monkeypatch):
    async def scenario():
        monkeypatch.setattr(streamhost, "KEEPALIVE_SECONDS", 0.2)
        host = StreamHost()
        await host.start()
        stream = host.reserve("flow", "r1", {}, "")
        reader, writer = await connect(stream.port, stream.token)
        await read_head(reader)
        assert await asyncio.wait_for(reader.readexactly(18), 5) == b"d\r\n: keepalive\n\n\r\n"
        writer.close()
        host.stop()
    run(scenario())
