import os
import socket
import ssl

import pytest

from test_engine_e2e import (api_get, api_post, engine, no_proxy_bypass, proxied_opener, start_engine,
                             start_streaming_server, wait_until)
from test_rules_e2e import add_rule
from test_streams_e2e import events_of, stream_flow


def send(engine, **payload):
    return api_post(engine["api_port"], "/api/events/send", payload)


def read_event(response):
    lines = []
    while True:
        line = response.readline()
        if not line:
            return lines
        if line == b"\n":
            return lines
        lines.append(line.rstrip(b"\n"))


def open_flow(engine, **query):
    return wait_until(lambda: stream_flow(engine, "sse", open="true", **query), 10)


def test_sse_rule_serves_an_open_stream_and_events_are_sent_into_it(engine):
    rule_id = add_rule(engine, action="sse", host="sse.example.test", path="/events",
                       body="event: hello\ndata: first\n\n")
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    assert response.headers["Content-Type"] == "text/event-stream"
    assert read_event(response) == [b"event: hello", b"data: first"]
    flow = open_flow(engine)
    assert flow["host"] == "sse.example.test" and flow["status"] == 200 and flow["scheme"] == "http"
    full = api_get(engine["api_port"], "/api/flows/" + flow["id"])[1]
    assert full["rules"] == [{"id": rule_id, "action": "sse"}]
    assert not [name for name, value in full["request_headers"] if name.lower() == "x-agentproxy-stream"]
    assert [value for name, value in full["request_headers"] if name.lower() == "host"] == ["sse.example.test"]
    status, answer = send(engine, flow=flow["id"], data="line one\nline two", event="update", id="7", retry=1500)
    assert status == 200 and answer["sent"] is True and answer["events_before"] == 1 and answer["closed"] is False
    assert isinstance(answer["last_seq"], int)
    assert read_event(response) == [b"event: update", b"id: 7", b"retry: 1500", b"data: line one", b"data: line two"]
    events = wait_until(lambda: len(events_of(engine, flow)) == 2 and events_of(engine, flow), 10)
    assert [(e["name"], e["data"], e.get("id")) for e in events] == [
        ("hello", "first", None), ("update", "line one\nline two", "7")]
    assert all(e["direction"] == "incoming" for e in events)
    response.close()
    assert wait_until(lambda: stream_flow(engine, "sse")["open"] is False, 15)
    status, answer = send(engine, flow=flow["id"], data="late")
    assert status == 400 and "closed" in answer["error"]


def test_close_ends_the_stream_and_the_app_reconnects_to_a_new_flow(engine):
    add_rule(engine, action="sse", host="sse.example.test", body="data: hi\n\n")
    first = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    assert read_event(first) == [b"data: hi"]
    flow = open_flow(engine)
    status, answer = send(engine, flow=flow["id"], data="bye", close=True)
    assert status == 200 and answer["closed"] is True
    assert read_event(first) == [b"data: bye"]
    assert first.read() == b""
    assert wait_until(lambda: stream_flow(engine, "sse")["open"] is False, 10)
    assert stream_flow(engine, "sse")["error"] is None
    second = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    assert read_event(second) == [b"data: hi"]
    assert wait_until(lambda: open_flow(engine) and open_flow(engine)["id"] != flow["id"], 10)
    second.close()


def test_close_alone_sends_nothing_more(engine):
    add_rule(engine, action="sse", host="sse.example.test")
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    flow = open_flow(engine)
    status, answer = send(engine, flow=flow["id"], close=True)
    assert status == 200 and answer["sent"] is False and answer["closed"] is True
    assert response.read() == b""
    assert wait_until(lambda: stream_flow(engine, "sse")["open"] is False, 10)


def test_sse_rule_with_times_lets_later_connections_reach_the_server(engine):
    target = start_streaming_server()
    from test_engine_e2e import StreamingHandler
    StreamingHandler.release.set()
    add_rule(engine, action="sse", host="127.0.0.1", path="/events", times=1, body="data: mock\n\n")
    url = "http://127.0.0.1:{0}/events".format(target.server_port)
    first = proxied_opener(engine).open(url, timeout=10)
    assert read_event(first) == [b"data: mock"]
    first.close()
    second = proxied_opener(engine).open(url, timeout=10)
    assert b"data: first" in second.read()
    target.shutdown()


def test_sse_rule_answers_https_for_an_unreachable_host(engine):
    add_rule(engine, action="sse", host="secure.example.test", body="data: tls\n\n")
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    response = proxied_opener(engine, cafile).open("https://secure.example.test/events", timeout=15)
    assert read_event(response) == [b"data: tls"]
    flow = open_flow(engine)
    assert flow["scheme"] == "https" and flow["host"] == "secure.example.test" and flow["port"] == 443
    status, answer = send(engine, flow=flow["id"], data="over tls")
    assert status == 200
    assert read_event(response) == [b"data: over tls"]
    response.close()


def test_sse_stream_reaches_an_http2_client(engine):
    import h2.config
    import h2.connection
    import h2.events
    add_rule(engine, action="sse", host="secure.example.test", body="data: h2\n\n")
    raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    raw.sendall(b"CONNECT secure.example.test:443 HTTP/1.1\r\nHost: secure.example.test:443\r\n\r\n")
    reply = b""
    while b"\r\n\r\n" not in reply:
        reply += raw.recv(1024)
    context = ssl.create_default_context(cafile=os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem"))
    context.set_alpn_protocols(["h2"])
    tls = context.wrap_socket(raw, server_hostname="secure.example.test")
    assert tls.selected_alpn_protocol() == "h2"
    connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    connection.initiate_connection()
    connection.send_headers(1, [(":method", "GET"), (":path", "/events"), (":scheme", "https"),
                                (":authority", "secure.example.test")], end_stream=True)
    tls.sendall(connection.data_to_send())
    received = {"status": None, "body": b"", "ended": False}

    def pump(until):
        while not until():
            data = tls.recv(65536)
            if not data:
                break
            for event in connection.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    received["status"] = dict(event.headers)[b":status"]
                elif isinstance(event, h2.events.DataReceived):
                    received["body"] += event.data
                    connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    received["ended"] = True
            tls.sendall(connection.data_to_send())

    try:
        pump(lambda: b"data: h2\n\n" in received["body"])
        assert received["status"] == b"200"
        flow = open_flow(engine)
        assert flow["host"] == "secure.example.test"
        assert api_get(engine["api_port"], "/api/flows/" + flow["id"])[1]["http_version"] == "HTTP/2.0"
        status, answer = send(engine, flow=flow["id"], data="second", close=True)
        assert status == 200
        pump(lambda: received["ended"])
        assert received["body"] == b"data: h2\n\ndata: second\n\n"
    finally:
        tls.close()


def test_sending_into_a_stream_of_the_real_server_is_refused_with_the_way_out(engine):
    target = start_streaming_server()
    from test_engine_e2e import StreamingHandler
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/events".format(target.server_port), timeout=10)
    response.readline()
    flow = open_flow(engine)
    status, answer = send(engine, flow=flow["id"], data="x")
    assert status == 400 and "real server" in answer["error"] and "action sse" in answer["error"]
    StreamingHandler.release.set()
    response.read()
    target.shutdown()


@pytest.mark.parametrize("arguments, text", [
    ({"direction": "outgoing", "data": "x"}, "from the server only"),
    ({"data_base64": "AAEC"}, "text"),
    ({}, "pass data"),
    ({"data": "x", "event": "a\nb"}, "single line"),
    ({"data": "x", "id": "a\x00b"}, "single line"),
    ({"data": "x", "retry": -1}, "retry"),
    ({"data": 5}, "must be a string"),
])
def test_bad_sse_events_are_refused_and_nothing_is_sent(engine, arguments, text):
    add_rule(engine, action="sse", host="sse.example.test")
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    flow = open_flow(engine)
    status, answer = send(engine, flow=flow["id"], **arguments)
    assert status == 400 and text in answer["error"]
    assert flow["event_count"] == 0 and events_of(engine, flow) == []
    response.close()


def test_sse_only_fields_are_refused_for_websocket_messages(engine):
    from test_streams_e2e import connect_websocket, start_websocket_echo
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    try:
        flow = wait_until(lambda: stream_flow(engine, "websocket"), 10)
        status, answer = send(engine, flow=flow["id"], data="x", close=True)
        assert status == 400 and "only for SSE" in answer["error"]
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_app_closing_the_stream_finishes_the_flow(engine):
    add_rule(engine, action="sse", host="sse.example.test", body="data: x\n\n")
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    read_event(response)
    flow = open_flow(engine)
    response.close()
    assert wait_until(lambda: stream_flow(engine, "sse")["open"] is False, 15)
    assert events_of(engine, flow)[0]["data"] == "x"


def test_rule_with_delay_waits_before_the_stream_opens(engine):
    import time
    add_rule(engine, action="sse", host="sse.example.test", delay_ms=800, body="data: late\n\n")
    started = time.time()
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    assert read_event(response) == [b"data: late"]
    assert time.time() - started >= 0.7
    response.close()


def test_the_internal_listener_does_not_serve_a_forged_request(engine):
    add_rule(engine, action="sse", host="sse.example.test", body="data: x\n\n")
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    read_event(response)
    with open(os.path.join(engine["data_dir"], "engine.log"), encoding="utf-8") as stream:
        listening = [line for line in stream if "stream listeners on" in line]
    port = int(listening[-1].split(" ports ")[1].split(",")[0])
    response.close()
    for headers in ("", "X-AgentProxy-Stream: not-a-token\r\n"):
        probe = socket.create_connection(("127.0.0.1", port), timeout=5)
        probe.sendall("GET /events HTTP/1.1\r\nHost: x\r\n{0}\r\n".format(headers).encode())
        assert probe.recv(100).startswith(b"HTTP/1.1 404")
        probe.close()


def test_adding_an_sse_rule_says_the_app_must_reconnect_and_how_the_body_should_end(engine):
    status, answer = api_post(engine["api_port"], "/api/rules",
                              {"action": "sse", "host": "sse.example.test", "body": "data: no empty line"})
    assert status == 200 and "reconnect" in answer["hint"] and "empty line" in answer["hint"]
    status, answer = api_post(engine["api_port"], "/api/rules",
                              {"action": "sse", "host": "sse.example.test", "body": "data: ok\n\n"})
    assert status == 200 and "empty line" not in answer["hint"]


def test_sending_into_a_closing_rule_stream_says_so_instead_of_blaming_the_server(engine):
    rule_id = add_rule(engine, action="sse", host="sse.example.test", body="data: x\n\n")
    response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
    read_event(response)
    flow = open_flow(engine)
    status, answer = send(engine, flow=flow["id"], close=True)
    assert status == 200
    status, answer = send(engine, flow=flow["id"], data="late")
    assert status == 400
    assert ("closed" in answer["error"]) or (rule_id in answer["error"] and "closing" in answer["error"])
    assert "real server" not in answer["error"]
    response.close()


def test_deleting_or_disabling_the_rule_closes_its_streams(engine):
    from test_rules_e2e import api_call
    changes = {"disable": {"enabled": False}, "action": {"action": "respond", "body": "x"}}
    for change in ("delete", "disable", "action"):
        rule_id = add_rule(engine, action="sse", host="sse.example.test")
        response = proxied_opener(engine).open("http://sse.example.test/events", timeout=10)
        flow = open_flow(engine)
        if change == "delete":
            status, answer = api_call(engine["api_port"], "DELETE", "/api/rules/" + rule_id)
        else:
            status, answer = api_call(engine["api_port"], "PATCH", "/api/rules/" + rule_id, changes[change])
        assert status == 200, answer
        assert response.read() == b""
        assert wait_until(lambda: stream_flow(engine, "sse")["open"] is False, 10)
        api_call(engine["api_port"], "DELETE", "/api/rules")
