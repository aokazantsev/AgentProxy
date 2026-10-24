import http.server
import socket
import threading
import time

from test_engine_e2e import (CRLF, HEADERS_END, api_get, engine, no_proxy_bypass, start_engine,
                             start_streaming_server, start_websocket_echo, wait_until)
from test_engine_e2e import StreamingHandler
from test_rules_e2e import add_rule, api_call
from test_streams_e2e import client_frame, connect_websocket, events_of, read_frame, stream_flow


class Feed(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release = None

    def do_GET(self):
        if self.path.startswith("/sized"):
            body = b"data: one\n\ndata: two\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for number in range(1, 6):
                self._chunk("event: tick\r\nid: {0}\r\ndata: tick {0}\r\ndata: end\r\n\r\n".format(number).encode())
                time.sleep(0.05)
            self._chunk(b"")
        except OSError:
            pass

    def _chunk(self, data):
        self.wfile.write("{0:x}\r\n".format(len(data)).encode() + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def start_feed():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def fetch(engine, port, path="/feed"):
    sock = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    lines = ["GET http://127.0.0.1:{0}{1} HTTP/1.1".format(port, path), "Host: 127.0.0.1:{0}".format(port),
             "Accept: text/event-stream", "Connection: close"]
    sock.sendall(CRLF.join(line.encode() for line in lines) + HEADERS_END)
    data = b""
    try:
        while True:
            part = sock.recv(65536)
            if not part:
                break
            data += part
    except ConnectionError:
        pass
    finally:
        sock.close()
    return data


def finished_flow(engine, kind, **query):
    return wait_until(lambda: (stream_flow(engine, kind, **query) or {}).get("open") is False and
                      stream_flow(engine, kind, **query), 10)


def rule_entry(flow, rule_id):
    return next(entry for entry in flow["rules"] if entry["id"] == rule_id)


def test_sse_event_data_is_replaced_in_matching_events_only(engine):
    feed = start_feed()
    try:
        rule = add_rule(engine, action="events", path="/feed", event_contains="tick 2",
                        replace=[{"find": "tick", "with": "tock"}])
        body = fetch(engine, feed.server_port)
        assert b"data: tick 1\r\n" in body and b"data: tock 2\r\ndata: end\r\n\r\n" in body
        assert b"event: tick\r\nid: 2\r\ndata: tock 2" in body and b"data: tick 3" in body
        assert body.endswith(b"0\r\n\r\n")
        flow = finished_flow(engine, "sse")
        assert [event["data"] for event in events_of(engine, flow)] == [
            "tick 1\nend", "tock 2\nend", "tick 3\nend", "tick 4\nend", "tick 5\nend"]
        assert rule_entry(flow, rule) == {"id": rule, "action": "events", "changed": 1, "replaced": 1}
    finally:
        feed.shutdown()


def test_sse_event_data_is_set_whole(engine):
    feed = start_feed()
    try:
        add_rule(engine, action="events", path="/feed", event_contains="tick 1", event_data='{"a":1}\n{"b":2}')
        body = fetch(engine, feed.server_port)
        assert b"id: 1\r\ndata: {\"a\":1}\r\ndata: {\"b\":2}\r\n\r\n" in body
        events = events_of(engine, finished_flow(engine, "sse"))
        assert events[0]["data"] == '{"a":1}\n{"b":2}' and events[0]["name"] == "tick" and events[0]["id"] == "1"
    finally:
        feed.shutdown()


def test_sse_close_after_delivers_the_event_and_breaks_the_stream(engine):
    feed = start_feed()
    try:
        rule = add_rule(engine, action="events", path="/feed", event_contains="tick 2", close="after")
        body = fetch(engine, feed.server_port)
        assert b"data: tick 2" in body and b"tick 3" not in body and not body.endswith(b"0\r\n\r\n")
        flow = finished_flow(engine, "sse")
        events = events_of(engine, flow)
        assert [event["data"] for event in events[:2]] == ["tick 1\nend", "tick 2\nend"]
        assert events[2]["type"] == "note" and "rule {0} closed the stream after incoming event 2".format(
            rule) in events[2]["note"]
        assert len(events) == 3
        assert rule_entry(flow, rule)["closed"] == "after incoming event 2"
    finally:
        feed.shutdown()


def test_sse_close_before_with_times_breaks_one_stream_and_lets_the_reconnect_through(engine):
    feed = start_feed()
    try:
        rule = add_rule(engine, action="events", path="/feed", event_contains="tick 2", close="before", times=1)
        first = fetch(engine, feed.server_port)
        assert b"data: tick 1" in first and b"tick 2" not in first
        second = fetch(engine, feed.server_port)
        assert b"data: tick 5" in second and second.endswith(b"0\r\n\r\n")
        rules = api_get(engine["api_port"], "/api/rules")[1]["rules"]
        assert rules[0]["id"] == rule and rules[0]["hits"] == 1 and rules[0]["remaining"] == 0
    finally:
        feed.shutdown()


def test_plain_requests_do_not_use_up_times_of_an_events_rule(engine):
    target = start_streaming_server()
    try:
        add_rule(engine, action="events", host="127.0.0.1", close="before", times=1)
        assert fetch(engine, target.server_port, "/plain").startswith(b"HTTP/1.1 200")
        rules = api_get(engine["api_port"], "/api/rules")[1]["rules"]
        assert rules[0]["hits"] == 0
    finally:
        target.shutdown()


def test_compressed_sse_stream_is_passed_unchanged_with_a_reason(engine):
    target = start_streaming_server()
    try:
        rule = add_rule(engine, action="events", path="/gzip-events", replace=[{"find": "packed", "with": "x"}],
                        times=1)
        fetch(engine, target.server_port, "/gzip-events")
        flow = finished_flow(engine, "sse")
        assert "compressed (gzip)" in rule_entry(flow, rule)["skipped"]
        assert [event["data"] for event in events_of(engine, flow)] == ["packed"]
        assert api_get(engine["api_port"], "/api/rules")[1]["rules"][0]["remaining"] == 1
    finally:
        target.shutdown()


def test_sse_with_content_length_is_sent_chunked_and_a_close_is_a_break(engine):
    feed = start_feed()
    try:
        rule = add_rule(engine, action="events", path="/sized", replace=[{"find": "one", "with": "longer one"}])
        head, _, body = fetch(engine, feed.server_port, "/sized").partition(HEADERS_END)
        assert b"content-length" not in head.lower() and b"transfer-encoding: chunked" in head.lower()
        assert b"data: longer one\n\n" in body and body.endswith(b"0\r\n\r\n")
        api_call(engine["api_port"], "PATCH", "/api/rules/" + rule, {"close": "after", "event_contains": "two"})
        body = fetch(engine, feed.server_port, "/sized").partition(HEADERS_END)[2]
        assert b"data: two" in body and not body.endswith(b"0\r\n\r\n")
    finally:
        feed.shutdown()


def test_deleting_the_rule_stops_changing_an_open_stream(engine):
    target = start_streaming_server()
    try:
        rule = add_rule(engine, action="events", path="/events", replace=[{"find": "s", "with": "S"}])
        sock = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
        lines = ["GET http://127.0.0.1:{0}/events HTTP/1.1".format(target.server_port),
                 "Host: 127.0.0.1:{0}".format(target.server_port), "Connection: close"]
        sock.sendall(CRLF.join(line.encode() for line in lines) + HEADERS_END)
        data = b""
        while b"fir" not in data:
            data += sock.recv(4096)
        assert b"data: firSt" in data
        assert api_call(engine["api_port"], "DELETE", "/api/rules/" + rule)[0] == 200
        StreamingHandler.release.set()
        while True:
            part = sock.recv(4096)
            if not part:
                break
            data += part
        sock.close()
        assert b"data: second" in data
    finally:
        target.shutdown()


def test_websocket_messages_are_replaced_in_each_direction(engine):
    target = start_websocket_echo()
    try:
        add_rule(engine, action="events", path="/ws", replace=[{"find": "ping", "with": "pong"}])
        add_rule(engine, action="events", path="/ws", direction="outgoing", event_contains="up",
                 event_data="changed by the proxy")
        sock = connect_websocket(engine, target)
        sock.sendall(client_frame(1, b"ping"))
        assert read_frame(sock) == (1, b"pong")
        sock.sendall(client_frame(1, b"up"))
        assert read_frame(sock) == (1, b"changed by the proxy")
        sock.sendall(client_frame(2, b"ping"))
        assert read_frame(sock) == (2, b"ping")
        sock.close()
        flow = finished_flow(engine, "websocket")
        events = events_of(engine, flow)
        assert [(event["direction"], event["data"]) for event in events[:4]] == [
            ("outgoing", "ping"), ("incoming", "pong"), ("outgoing", "changed by the proxy"),
            ("incoming", "changed by the proxy")]
        assert [entry.get("changed") for entry in flow["rules"]] == [1, 1]
        assert "binary" in flow["rules"][0]["skipped"]
    finally:
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_websocket_close_before_drops_the_message_and_breaks_the_connection(engine):
    target = start_websocket_echo()
    try:
        rule = add_rule(engine, action="events", path="/ws", direction="outgoing", event_contains="stop",
                        close="before")
        sock = connect_websocket(engine, target)
        sock.sendall(client_frame(1, b"go"))
        assert read_frame(sock) == (1, b"go")
        sock.sendall(client_frame(1, b"stop"))
        try:
            closed = read_frame(sock) is None
        except (EOFError, ConnectionError):
            closed = True
        assert closed
        sock.close()
        flow = finished_flow(engine, "websocket")
        events = events_of(engine, flow)
        assert events[2]["data"] == "stop" and events[2]["note"] == "dropped"
        assert events[3]["type"] == "note" and "closed the stream before outgoing event 2" in events[3]["note"]
        assert all(event["data"] != "stop" or event["direction"] == "outgoing" for event in events)
        assert rule_entry(flow, rule)["closed"] == "before outgoing event 2"
    finally:
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_websocket_close_after_delivers_the_message_first(engine):
    target = start_websocket_echo()
    try:
        add_rule(engine, action="events", path="/ws", event_contains="last", close="after")
        sock = connect_websocket(engine, target)
        sock.sendall(client_frame(1, b"last"))
        assert read_frame(sock) == (1, b"last")
        try:
            closed = read_frame(sock) is None
        except (EOFError, ConnectionError):
            closed = True
        assert closed
    finally:
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_websocket_close_after_an_outgoing_message_breaks_at_once(engine):
    target = start_websocket_echo()
    try:
        add_rule(engine, action="events", path="/ws", direction="outgoing", event_contains="stop", close="after")
        sock = connect_websocket(engine, target)
        started = time.monotonic()
        sock.sendall(client_frame(1, b"stop"))
        try:
            while read_frame(sock) is not None:
                pass
        except (EOFError, ConnectionError):
            pass
        assert time.monotonic() - started < 0.8
        sock.close()
        events = events_of(engine, finished_flow(engine, "websocket"))
        assert events[0]["direction"] == "outgoing" and events[0]["data"] == "stop" and "note" not in events[0]
    finally:
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_closing_rule_host_is_offered_http1(engine):
    from test_speed_e2e import negotiated_protocol
    add_rule(engine, action="respond", host="close.example.test", body="{}")
    add_rule(engine, action="events", host="close.example.test", close="after")
    add_rule(engine, action="respond", host="open.example.test", body="{}")
    assert negotiated_protocol(engine, "close.example.test") == "http/1.1"
    assert negotiated_protocol(engine, "open.example.test") == "h2"


def test_outgoing_events_rule_spends_times_on_a_websocket_but_not_on_an_sse_stream(engine):
    feed = start_feed()
    target = start_websocket_echo()
    try:
        rule = add_rule(engine, action="events", host="127.0.0.1", direction="outgoing", event_contains="stop",
                        close="after", times=1)
        body = fetch(engine, feed.server_port)
        assert b"data: tick 5" in body and body.endswith(b"0\r\n\r\n")
        sse = finished_flow(engine, "sse")
        assert all(entry["id"] != rule for entry in sse.get("rules") or [])
        rules = api_get(engine["api_port"], "/api/rules")[1]["rules"]
        assert rules[0]["hits"] == 0 and rules[0]["remaining"] == 1
        sock = connect_websocket(engine, target)
        sock.sendall(client_frame(1, b"stop"))
        try:
            closed = read_frame(sock) is None
        except (EOFError, ConnectionError):
            closed = True
        assert closed
        sock.close()
        rules = api_get(engine["api_port"], "/api/rules")[1]["rules"]
        assert rules[0]["hits"] == 1 and rules[0]["remaining"] == 0
    finally:
        feed.shutdown()
        target["loop"].call_soon_threadsafe(target["loop"].stop)
