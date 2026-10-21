import json
import socket
import threading
import time
import urllib.request

from test_engine_e2e import (CRLF, HEADERS_END, api_get, engine, no_proxy_bypass, proxied_opener, start_engine,
                             start_streaming_server, start_websocket_echo, wait_until)


def mask(payload):
    key = bytes([1, 2, 3, 4])
    return key + bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))


def client_frame(opcode, payload):
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask(payload)


def read_frame(sock):
    def take(count):
        data = b""
        while len(data) < count:
            part = sock.recv(count - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    head = take(2)
    length = head[1] & 0x7F
    return head[0] & 0x0F, take(length)


def connect_websocket(engine, target, path="/ws"):
    sock = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    lines = ["GET http://127.0.0.1:{0}{1} HTTP/1.1".format(target["port"], path),
             "Host: 127.0.0.1:{0}".format(target["port"]), "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"]
    sock.sendall(CRLF.join(line.encode() for line in lines) + HEADERS_END)
    head = b""
    while HEADERS_END not in head:
        head += sock.recv(4096)
    assert head.startswith(b"HTTP/1.1 101")
    return sock


def stream_flow(engine, kind, **query):
    parameters = "&".join("{0}={1}".format(name, value) for name, value in dict(query, kind=kind).items())
    flows = api_get(engine["api_port"], "/api/flows?" + parameters)[1]["flows"]
    return flows[0] if flows else None


def events_of(engine, flow, **query):
    parameters = "&".join("{0}={1}".format(name, value) for name, value in dict(query, flow=flow["id"]).items())
    status, answer = api_get(engine["api_port"], "/api/events?" + parameters)
    assert status == 200, answer
    return answer["events"]


def test_websocket_messages_are_recorded_while_the_connection_is_open(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    try:
        sock.sendall(client_frame(1, b"hello"))
        assert read_frame(sock) == (1, b"hello")
        sock.sendall(client_frame(2, b"\x00\x01\x02"))
        assert read_frame(sock) == (2, b"\x00\x01\x02")
        flow = wait_until(lambda: (stream_flow(engine, "websocket", open="true") or {}).get("event_count") == 4 and
                          stream_flow(engine, "websocket"), 10)
        assert flow["status"] == 101 and flow["open"] is True and flow["path"] == "/ws"
        events = events_of(engine, flow)
        assert [(e["direction"], e["type"], e["data"]) for e in events[:2]] == [
            ("outgoing", "text", "hello"), ("incoming", "text", "hello")]
        assert events[2]["type"] == "binary" and events[2]["encoding"] == "base64" and events[2]["data"] == "AAEC"
        assert [e["n"] for e in events] == [1, 2, 3, 4]
        assert [e["n"] for e in events_of(engine, flow, since_n=2)] == [3, 4]
        assert [e["n"] for e in events_of(engine, flow, direction="incoming")] == [2, 4]
        assert [e["n"] for e in events_of(engine, flow, tail="true", limit=1)] == [4]
        assert [e["n"] for e in events_of(engine, flow, contains="HELLO")] == [1, 2]
        sock.sendall(client_frame(8, b"\x03\xe8bye"))
        assert wait_until(lambda: not stream_flow(engine, "websocket")["open"], 10)
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)
    closed = events_of(engine, stream_flow(engine, "websocket"))[-1]
    assert (closed["type"], closed["direction"], closed["name"], closed["data"]) == ("close", "outgoing", "1000", "bye")


def test_websocket_dropped_without_close_frame_is_finished(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    sock.sendall(client_frame(1, b"x"))
    assert read_frame(sock) == (1, b"x")
    assert wait_until(lambda: stream_flow(engine, "websocket"), 10)
    sock.close()
    try:
        assert wait_until(lambda: not stream_flow(engine, "websocket")["open"], 10)
    finally:
        target["loop"].call_soon_threadsafe(target["loop"].stop)
    flow = stream_flow(engine, "websocket")
    assert flow["event_count"] >= 2 and flow["duration_ms"] is not None


def test_wait_for_event_returns_a_matching_websocket_message(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    answer = {}

    def wait():
        answer["value"] = api_get(engine["api_port"], "/api/events/wait?kind=websocket&contains=ping-42&"
                                  "direction=incoming&timeout_seconds=20", timeout=30)[1]

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        time.sleep(0.5)
        sock.sendall(client_frame(1, b"other"))
        read_frame(sock)
        sock.sendall(client_frame(1, b"ping-42"))
        read_frame(sock)
        thread.join(25)
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)
    assert answer["value"]["timed_out"] is False
    assert answer["value"]["event"]["data"] == "ping-42" and answer["value"]["event"]["direction"] == "incoming"
    assert answer["value"]["flow"]["kind"] == "websocket"


def test_wait_for_event_on_one_stream_skips_history_unless_asked(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    try:
        sock.sendall(client_frame(1, b"old"))
        read_frame(sock)
        flow = wait_until(lambda: (stream_flow(engine, "websocket") or {}).get("event_count") == 2 and
                          stream_flow(engine, "websocket"), 10)
        status, history = api_get(engine["api_port"], "/api/events/wait?flow={0}&since_n=0&timeout_seconds=5".format(flow["id"]))
        assert history["event"]["data"] == "old"
        status, fresh = api_get(engine["api_port"], "/api/events/wait?flow={0}&timeout_seconds=1".format(flow["id"]))
        assert fresh["timed_out"] is True and fresh["event"] is None
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_wait_for_event_on_a_closed_stream_answers_at_once(engine):
    target = start_streaming_server()
    from test_engine_e2e import StreamingHandler
    StreamingHandler.release.set()
    proxied_opener(engine).open("http://127.0.0.1:{0}/events".format(target.server_port), timeout=10).read()
    flow = wait_until(lambda: (stream_flow(engine, "sse") or {}).get("open") is False and stream_flow(engine, "sse"), 10)
    started = time.time()
    status, answer = api_get(engine["api_port"], "/api/events/wait?flow={0}&timeout_seconds=30".format(flow["id"]))
    assert answer["closed"] is True and answer["event"] is None and time.time() - started < 5
    target.shutdown()


def test_events_of_a_plain_request_are_refused(engine):
    from test_engine_e2e import start_server
    target = start_server()
    proxied_opener(engine).open("http://127.0.0.1:{0}/plain".format(target.server_port), timeout=10).read()
    flow = api_get(engine["api_port"], "/api/flows?path=/plain")[1]["flows"][0]
    assert "kind" not in flow
    status, answer = api_get(engine["api_port"], "/api/events?flow=" + flow["id"])
    assert status == 400 and "plain request" in answer["error"]
    status, answer = api_get(engine["api_port"], "/api/events?flow=nope")
    assert status == 404
    target.shutdown()


def test_streams_are_found_by_kind_and_open(engine):
    target = start_streaming_server()
    from test_engine_e2e import StreamingHandler
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/events".format(target.server_port), timeout=10)
    response.readline()
    assert wait_until(lambda: stream_flow(engine, "sse", open="true"), 10)
    assert stream_flow(engine, "websocket") is None
    StreamingHandler.release.set()
    response.read()
    assert wait_until(lambda: stream_flow(engine, "sse", open="true") is None, 10)
    assert stream_flow(engine, "sse")["open"] is False
    target.shutdown()


def api_send(engine, **payload):
    from test_engine_e2e import api_post
    return api_post(engine["api_port"], "/api/events/send", payload)


def test_message_is_sent_to_the_app_as_if_the_server_wrote_it(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    try:
        sock.sendall(client_frame(1, b"first"))
        assert read_frame(sock) == (1, b"first")
        flow = wait_until(lambda: (stream_flow(engine, "websocket") or {}).get("event_count") == 2 and
                          stream_flow(engine, "websocket"), 10)
        status, answer = api_send(engine, flow=flow["id"], data="from the proxy")
        assert status == 200 and answer["sent"] is True and answer["events_before"] == 2
        assert read_frame(sock) == (1, b"from the proxy")
        status, answer = api_send(engine, flow=flow["id"], data_base64="AAEC")
        assert status == 200 and answer["type"] == "binary"
        assert read_frame(sock) == (2, b"\x00\x01\x02")
        events = wait_until(lambda: len(events_of(engine, flow)) == 4 and events_of(engine, flow), 10)
        assert events[2]["note"] == "injected" and events[2]["direction"] == "incoming" and events[2]["data"] == "from the proxy"
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_message_is_sent_to_the_server_as_if_the_app_wrote_it(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    try:
        sock.sendall(client_frame(1, b"x"))
        read_frame(sock)
        flow = wait_until(lambda: (stream_flow(engine, "websocket") or {}).get("event_count") == 2 and
                          stream_flow(engine, "websocket"), 10)
        status, answer = api_send(engine, flow=flow["id"], direction="outgoing", data="to the server")
        assert status == 200
        assert read_frame(sock) == (1, b"to the server")
        events = wait_until(lambda: len(events_of(engine, flow)) == 4 and events_of(engine, flow), 10)
        assert (events[2]["direction"], events[2]["note"]) == ("outgoing", "injected")
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)


def test_sending_is_refused_for_closed_sse_and_bad_input(engine):
    target = start_websocket_echo()
    sock = connect_websocket(engine, target)
    sock.sendall(client_frame(1, b"x"))
    read_frame(sock)
    flow = wait_until(lambda: stream_flow(engine, "websocket"), 10)
    status, answer = api_send(engine, flow=flow["id"])
    assert status == 400 and "exactly one" in answer["error"]
    status, answer = api_send(engine, flow=flow["id"], data="a", data_base64="AA==")
    assert status == 400
    status, answer = api_send(engine, flow=flow["id"], data_base64="***")
    assert status == 400 and "base64" in answer["error"]
    status, answer = api_send(engine, flow=flow["id"], data="a", direction="up")
    assert status == 400
    sock.close()
    wait_until(lambda: not stream_flow(engine, "websocket")["open"], 10)
    target["loop"].call_soon_threadsafe(target["loop"].stop)
    status, answer = api_send(engine, flow=flow["id"], data="late")
    assert status == 400 and "closed" in answer["error"]


def test_sending_into_an_sse_stream_is_refused(engine):
    from test_engine_e2e import StreamingHandler
    target = start_streaming_server()
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/events".format(target.server_port), timeout=10)
    response.readline()
    flow = wait_until(lambda: stream_flow(engine, "sse", open="true"), 10)
    status, answer = api_send(engine, flow=flow["id"], data="x")
    assert status == 400 and "SSE" in answer["error"]
    StreamingHandler.release.set()
    response.read()
    target.shutdown()


class EdgeStreams(__import__("http.server").server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        bodies = {"/half": b"data: one\n\ndata: half", "/odd": b"data: x\n\n",
                  "/flood": b": tick\n" * 2000 + b"data: last\n\n"}
        body = bodies[self.path]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if self.path == "/odd":
            self.send_header("Content-Encoding", "compress")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write("{0:x}\r\n".format(len(body)).encode() + body + b"\r\n0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def edge_stream(engine, path):
    import http.server
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EdgeStreams)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        proxied_opener(engine).open("http://127.0.0.1:{0}{1}".format(server.server_port, path), timeout=10).read()
        flow = wait_until(lambda: (stream_flow(engine, "sse", path=path) or {}).get("open") is False and
                          stream_flow(engine, "sse", path=path), 10)
        return events_of(engine, flow, limit=500)
    finally:
        server.shutdown()


def test_unfinished_last_sse_event_is_recorded_as_incomplete(engine):
    events = edge_stream(engine, "/half")
    assert [event["data"] for event in events] == ["one", "half"]
    assert events[1]["note"] == "incomplete at the end of the stream"


def test_unknown_sse_encoding_leaves_a_note(engine):
    events = edge_stream(engine, "/odd")
    assert [(event["type"], "compress" in event["note"]) for event in events] == [("note", True)]


def test_event_flood_is_recorded_up_to_the_rate_limit_with_a_note(engine):
    edge_stream(engine, "/flood")
    flow = stream_flow(engine, "sse", path="/flood")
    last = events_of(engine, flow, tail="true", limit=1)[0]
    assert flow["event_count"] == 502
    assert last["type"] == "note" and last["note"].startswith("1501 events not recorded")


def test_sse_closed_by_the_app_is_finished_with_its_events(engine):
    from test_engine_e2e import StreamingHandler
    target = start_streaming_server()
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/events".format(target.server_port), timeout=10)
    assert response.readline() == b"data: first\n"
    flow = wait_until(lambda: (stream_flow(engine, "sse") or {}).get("event_count") and stream_flow(engine, "sse"), 10)
    response.close()
    try:
        assert wait_until(lambda: stream_flow(engine, "sse")["open"] is False, 15)
    finally:
        StreamingHandler.release.set()
        target.shutdown()
    closed = stream_flow(engine, "sse")
    assert closed["error"] and closed["duration_ms"] is not None and closed["status"] == 200
    assert [event["data"] for event in events_of(engine, flow)] == ["first"]


def test_compressed_websocket_messages_are_recorded_as_text_and_sending_works(engine):
    import asyncio as aio
    import zlib
    import tornado.web
    import tornado.websocket

    class Echo(tornado.websocket.WebSocketHandler):
        def get_compression_options(self):
            return {}

        def on_message(self, message):
            self.write_message(message)

    ready = threading.Event()
    holder = {}

    def serve():
        loop = aio.new_event_loop()
        aio.set_event_loop(loop)
        server = tornado.web.Application([(r"/ws", Echo)]).listen(0, "127.0.0.1")
        holder["port"] = next(iter(server._sockets.values())).getsockname()[1]
        holder["loop"] = loop
        ready.set()
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    ready.wait(10)
    sock = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    try:
        lines = ["GET http://127.0.0.1:{0}/ws HTTP/1.1".format(holder["port"]), "Host: 127.0.0.1:{0}".format(holder["port"]),
                 "Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
                 "Sec-WebSocket-Version: 13",
                 "Sec-WebSocket-Extensions: permessage-deflate; client_no_context_takeover; server_no_context_takeover"]
        sock.sendall(CRLF.join(line.encode() for line in lines) + HEADERS_END)
        head = b""
        while HEADERS_END not in head:
            head += sock.recv(4096)
        assert head.startswith(b"HTTP/1.1 101") and b"permessage-deflate" in head.lower()
        packer = zlib.compressobj(wbits=-15)
        payload = packer.compress("привет".encode()) + packer.flush(zlib.Z_SYNC_FLUSH)
        payload = payload[:-4]
        sock.sendall(bytes([0xC1, 0x80 | len(payload)]) + mask(payload))

        unpacker = zlib.decompressobj(wbits=-15)

        def read_text():
            opcode, body = read_frame(sock)
            return unpacker.decompress(body + b"\x00\x00\xff\xff").decode()

        assert read_text() == "привет"
        flow = wait_until(lambda: (stream_flow(engine, "websocket") or {}).get("event_count") == 2 and
                          stream_flow(engine, "websocket"), 10)
        assert [event["data"] for event in events_of(engine, flow)] == ["привет", "привет"]
        status, answer = api_send(engine, flow=flow["id"], data="от прокси")
        assert status == 200 and read_text() == "от прокси"
    finally:
        sock.close()
        holder["loop"].call_soon_threadsafe(holder["loop"].stop)
