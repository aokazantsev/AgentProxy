import base64
import gzip
import http.client
import http.server
import random
import socket
import ssl
import threading
import time
import zlib

import pytest

from agentproxy.cutter import BodyCutter, cut_point
from test_engine_e2e import api_get, engine, no_proxy_bypass, self_signed, start_engine, wait_until
from test_logs import engine_log
from test_rules_e2e import add_rule, api_call, h2_through_proxy, last_flow

VIDEO = bytes(range(256)) * 1200
PLAIN = random.Random(7).randbytes(100000)
PACKED = gzip.compress(PLAIN, mtime=0)


def media_body(size):
    return (bytes(range(256)) * (size // 256 + 1))[:size]


class MediaHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(VIDEO)))
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/broken"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO)))
            self.end_headers()
            self.wfile.write(VIDEO[:5000])
            self.close_connection = True
            return
        if self.path.startswith("/nocontent"):
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/eof"):
            if self.path.startswith("/eof10"):
                self.protocol_version = "HTTP/1.0"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if not self.path.startswith("/eof10"):
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(VIDEO[:20000])
            self.close_connection = True
            return
        if self.path.startswith("/gzip"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(PACKED)))
            self.end_headers()
            self.wfile.write(PACKED)
            return
        if self.path.startswith("/events"):
            body = b"data: one\n\n" * 2000
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/chunked"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, 0 if "empty" in self.path else 20000, 500):
                piece = VIDEO[start:start + 500]
                self.wfile.write("{0:x}\r\n".format(len(piece)).encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        start = 0
        if self.headers.get("Range", "").startswith("bytes="):
            start = int(self.headers["Range"][len("bytes="):].split("-")[0])
        body = media_body(int(self.path.split("size=")[1])) if "size=" in self.path else VIDEO
        body = body[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", "bytes {0}-{1}/{2}".format(start, start + len(body) - 1, start + len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def media(tmp_path):
    servers = []

    def start(tls=False):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*self_signed(str(tmp_path)))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_port

    yield start
    for server in servers:
        server.shutdown()


def fetch(engine, port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=30)
    connection.request("GET", "http://127.0.0.1:{0}{1}".format(port, path), headers=headers or {})
    response = connection.getresponse()
    try:
        return response, response.read(), None
    except http.client.IncompleteRead as broken:
        return response, broken.partial, broken
    finally:
        connection.close()


def recorded(engine, rule_id):
    assert wait_until(lambda: api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"], 10)
    return last_flow(engine, rule=rule_id)


def test_cut_breaks_the_body_and_the_retry_resumes_with_range(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=100000, times=1)
    response, body, broken = fetch(engine, port, "/video")
    assert response.status == 200 and broken is not None and body == VIDEO[:100000]
    response, rest, broken = fetch(engine, port, "/video", {"Range": "bytes=100000-"})
    assert response.status == 206 and broken is None and body + rest == VIDEO
    flows = api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"]
    cut = api_get(engine["api_port"], "/api/flows/" + flows[-1]["id"])[1]
    assert cut["rules"] == [{"id": rule_id, "action": "cut", "cut_after_bytes": 100000}]
    assert cut["error"] == "AgentProxy rule {0} cut the response after 100000 bytes of body".format(rule_id)
    assert cut["response_body"]["size"] == len(VIDEO) and len(flows) == 1
    rule = api_get(engine["api_port"], "/api/rules")[1]["rules"][0]
    assert rule["hits"] == 1 and rule["remaining"] == 0
    assert wait_until(lambda: "{0} cut cut_after_bytes=100000".format(rule_id) in engine_log(engine), 10)
    assert "Traceback" not in engine_log(engine)


def test_cut_on_a_slow_answer_delivers_the_head_then_breaks(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_percent=50, download_kbps=2000)
    started = time.monotonic()
    response, body, broken = fetch(engine, port, "/video")
    assert broken is not None and body == VIDEO[:len(VIDEO) // 2]
    assert time.monotonic() - started >= 0.4
    assert recorded(engine, rule_id)["rules"] == [{"id": rule_id, "action": "cut", "limited": "request",
                                                   "cut_after_bytes": len(VIDEO) // 2}]


@pytest.mark.parametrize("point", [1200, 1000])
def test_cut_of_a_chunked_answer_leaves_out_the_last_chunk(engine, media, point):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/chunked", after_bytes=point)
    response, body, broken = fetch(engine, port, "/chunked")
    assert broken is not None and body == VIDEO[:point]
    assert recorded(engine, rule_id)["rules"][0]["cut_after_bytes"] == point


def test_cut_resets_only_its_own_http2_stream(engine, media):
    port = media(tls=True)
    add_rule(engine, action="respond", host="localhost", path="/never", name="h2 to the app, HTTP/1.1 to the server")
    rule_id = add_rule(engine, action="cut", host="localhost", path="/video", after_bytes=30000)
    streams = h2_through_proxy(engine, port, ["/video", "/whole?video"])
    assert streams["/video"]["reset"] and streams["/video"]["status"] == b"200"
    assert streams["/video"]["body"] == VIDEO[:30000]
    assert streams["/whole?video"]["ended"] and streams["/whole?video"]["body"] == VIDEO
    flow = recorded(engine, rule_id)
    assert flow["path"] == "/video" and flow["rules"][0]["cut_after_bytes"] == 30000
    assert "Traceback" not in engine_log(engine)


@pytest.mark.parametrize("rule, path, reason", [
    ({"after_bytes": len(VIDEO)}, "/video", "is not before the end of the body (Content-Length 307200)"),
    ({"after_percent": 50}, "/chunked", "after_percent needs Content-Length"),
    ({"after_bytes": 50000}, "/chunked", "the body has 20000 bytes, not more than 50000"),
    ({"after_bytes": 20000}, "/chunked", "the body has 20000 bytes, not more than 20000"),
])
def test_answers_the_rule_does_not_cut_reach_the_app_whole(engine, media, rule, path, reason):
    port = media()
    rule_id = add_rule(engine, action="cut", path=path, **rule)
    response, body, broken = fetch(engine, port, path)
    assert broken is None and body == (VIDEO if path == "/video" else VIDEO[:20000])
    entry = recorded(engine, rule_id)["rules"][0]
    assert reason in entry["skipped"] and "cut_after_bytes" not in entry


def test_edited_body_is_not_cut(engine, media):
    port = media()
    add_rule(engine, action="rewrite", path="/video", response_headers={"X-Seen": "1"},
             replace=[{"find": "absent", "with": "x"}])
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=1000)
    response, body, broken = fetch(engine, port, "/video")
    assert broken is None and body == VIDEO
    entry = recorded(engine, rule_id)["rules"][1]
    assert entry["skipped"] == "not cut: the body is changed by a rewrite rule and goes to the app whole"


def test_the_earliest_cut_point_wins(engine, media):
    port = media()
    first = add_rule(engine, action="cut", path="/video", after_percent=50)
    second = add_rule(engine, action="cut", path="/video", after_bytes=5000)
    response, body, broken = fetch(engine, port, "/video")
    assert broken is not None and body == VIDEO[:5000]
    entries = recorded(engine, first)["rules"]
    assert entries == [{"id": first, "action": "cut", "skipped": "not cut: rule {0} cuts earlier".format(second)},
                       {"id": second, "action": "cut", "cut_after_bytes": 5000}]


@pytest.mark.parametrize("rule, length, point", [
    ({"after_bytes": 10}, None, 10),
    ({"after_bytes": 10}, 11, 10),
    ({"after_bytes": 11}, 11, None),
    ({"after_percent": 50}, 101, 50),
    ({"after_percent": 1}, 50, 1),
    ({"after_percent": 99}, 1, None),
    ({"after_percent": 50}, None, None),
])
def test_cut_point(rule, length, point):
    assert cut_point(rule, length)[0] == point


class FakeFlow:
    def __init__(self):
        self.metadata = {"agentproxy_rules": [{"id": "r1", "action": "cut"}]}


def test_cutter_passes_bytes_up_to_the_point_then_nothing():
    flow = FakeFlow()
    cutter = BodyCutter(flow, "r1", 5, lambda rule_id: True)
    assert [cutter(b"ab"), cutter(b""), cutter(b"cde"), cutter(b"f"), cutter(b"g")] == [b"ab", [], b"cde", [], []]
    assert cutter.fired and flow.metadata["agentproxy_rules"][0] == {"id": "r1", "action": "cut", "cut_after_bytes": 5}


def test_cutter_notes_a_body_shorter_than_the_point():
    flow = FakeFlow()
    cutter = BodyCutter(flow, "r1", 5, lambda rule_id: True)
    assert [cutter(b"ab"), cutter(b""), cutter(b"cde")] == [b"ab", [], b"cde"]
    cutter.finish()
    assert not cutter.fired
    assert flow.metadata["agentproxy_rules"][0]["skipped"] == "not cut: the body has 5 bytes, not more than 5"


def test_cutter_notes_an_answer_broken_before_the_point():
    flow = FakeFlow()
    cutter = BodyCutter(flow, "r1", 5, lambda rule_id: True)
    assert cutter(b"ab") == b"ab"
    cutter.broken()
    assert flow.metadata["agentproxy_rules"][0]["skipped"] == (
        "not cut: the answer broke after 2 bytes of body, before byte 5")


def test_cutter_keeps_the_used_up_note_when_the_answer_breaks_later():
    flow = FakeFlow()
    cutter = BodyCutter(flow, "r1", 3, lambda rule_id: False)
    assert [cutter(b"ab"), cutter(b"cde")] == [b"ab", b"cde"]
    cutter.broken()
    assert flow.metadata["agentproxy_rules"][0]["skipped"] == (
        "not cut: when the answer passed byte 3, the rule was used up by other answers, disabled, deleted or changed "
        "to another action")


def test_cutter_lets_the_body_through_when_the_rule_is_used_up():
    flow = FakeFlow()
    cutter = BodyCutter(flow, "r1", 3, lambda rule_id: False)
    assert [cutter(b"ab"), cutter(b"cde"), cutter(b"f")] == [b"ab", b"cde", b"f"]
    cutter.finish()
    assert not cutter.fired and flow.metadata["agentproxy_rules"][0]["skipped"].startswith("not cut: when the answer")


def fetch_head(engine, port, path):
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=30)
    connection.request("HEAD", "http://127.0.0.1:{0}{1}".format(port, path))
    response = connection.getresponse()
    response.read()
    connection.close()
    return response


def rule_state(engine, rule_id):
    return next(rule for rule in api_get(engine["api_port"], "/api/rules")[1]["rules"] if rule["id"] == rule_id)


def entries(engine, rule_id, count):
    assert wait_until(lambda: len(api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"]) >= count, 10)
    flows = api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"]
    return [api_get(engine["api_port"], "/api/flows/" + flow["id"])[1] for flow in reversed(flows)]


def test_head_and_a_short_range_probe_keep_the_cut_for_the_real_download(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=1000, times=1)
    assert fetch_head(engine, port, "/video").status == 200
    response, tail, broken = fetch(engine, port, "/video", {"Range": "bytes=306800-"})
    assert response.status == 206 and broken is None and tail == VIDEO[306800:]
    assert rule_state(engine, rule_id)["hits"] == 0
    response, body, broken = fetch(engine, port, "/video")
    assert broken is not None and body == VIDEO[:1000]
    head, probe, cut = entries(engine, rule_id, 3)
    assert head["method"] == "HEAD" and head["rules"][0]["skipped"] == "not cut: response has no body"
    assert probe["rules"][0]["skipped"] == "not cut: byte 1000 is not before the end of the body (Content-Length 400)"
    assert cut["rules"][0]["cut_after_bytes"] == 1000
    assert rule_state(engine, rule_id)["hits"] == 1 and rule_state(engine, rule_id)["remaining"] == 0


def test_a_parallel_answer_passes_whole_when_the_other_one_used_up_the_rule(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=150000, download_kbps=500, times=1)
    results = []
    threads = [threading.Thread(target=lambda: results.append(fetch(engine, port, "/video"))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(len(body) for response, body, broken in results) == [150000, len(VIDEO)]
    assert sorted(broken is None for response, body, broken in results) == [False, True]
    skipped = [flow["rules"][0].get("skipped") for flow in entries(engine, rule_id, 2)]
    assert ("not cut: when the answer passed byte 150000, the rule was used up by other answers, disabled, deleted "
            "or changed to another action") in skipped
    assert rule_state(engine, rule_id)["hits"] == 1


@pytest.mark.parametrize("method, payload", [("PATCH", {"enabled": False}), ("DELETE", None)])
def test_rule_turned_off_before_the_point_does_not_cut(engine, media, method, payload):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=250000, download_kbps=1000)
    results = []
    thread = threading.Thread(target=lambda: results.append(fetch(engine, port, "/video")))
    thread.start()
    time.sleep(0.5)
    assert api_call(engine["api_port"], method, "/api/rules/" + rule_id, payload)[0] == 200
    thread.join(60)
    response, body, broken = results[0]
    assert broken is None and body == VIDEO
    assert recorded(engine, rule_id)["rules"][0]["skipped"].startswith("not cut: when the answer passed byte 250000")


@pytest.mark.parametrize("path", ["/eof10", "/eof11"])
def test_answer_without_length_is_cut_for_an_http11_client(engine, media, path):
    port = media()
    rule_id = add_rule(engine, action="cut", path=path, after_bytes=1000)
    response, body, broken = fetch(engine, port, path)
    assert response.version == 11 and response.getheader("Transfer-Encoding") == "chunked"
    assert broken is not None and body == VIDEO[:1000]
    assert recorded(engine, rule_id)["rules"][0]["cut_after_bytes"] == 1000


@pytest.mark.parametrize("path", ["/eof10", "/eof11"])
def test_answer_without_length_shorter_than_the_point_reaches_the_app_whole_and_ends(engine, media, path):
    port = media()
    rule_id = add_rule(engine, action="cut", path=path, after_bytes=50000)
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=10)
    for _ in range(6):
        connection.request("GET", "http://127.0.0.1:{0}{1}".format(port, path))
        response = connection.getresponse()
        assert response.read() == VIDEO[:20000]
    connection.close()
    assert recorded(engine, rule_id)["rules"][0]["skipped"] == "not cut: the body has 20000 bytes, not more than 50000"


def test_http10_client_gets_an_answer_without_length_whole(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/eof10", after_bytes=1000)
    raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=10)
    raw.sendall("GET http://127.0.0.1:{0}/eof10 HTTP/1.0\r\nHost: 127.0.0.1:{0}\r\n\r\n".format(port).encode())
    answer = b""
    while True:
        data = raw.recv(65536)
        if not data:
            break
        answer += data
    raw.close()
    assert answer.split(b"\r\n\r\n", 1)[1] == VIDEO[:20000]
    assert "client speaks HTTP/1.0" in recorded(engine, rule_id)["rules"][0]["skipped"]


def test_compressed_answer_is_cut_on_the_wire(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/gzip", after_bytes=30000)
    response, body, broken = fetch(engine, port, "/gzip")
    assert broken is not None and body == PACKED[:30000]
    assert recorded(engine, rule_id)["rules"][0]["cut_after_bytes"] == 30000
    flows = api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"]
    flow = api_get(engine["api_port"], "/api/flows/{0}?max_body_chars=1000000".format(flows[0]["id"]))[1]
    assert base64.b64decode(flow["response_body"]["content"]) == zlib.decompressobj(31).decompress(PACKED[:30000])


def test_answer_larger_than_the_history_limit_is_cut_where_asked(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=2500000)
    response, body, broken = fetch(engine, port, "/video?size=3000000")
    assert broken is not None and body == media_body(2500000)
    flow = recorded(engine, rule_id)
    assert flow["rules"][0]["cut_after_bytes"] == 2500000 and flow["response_body"]["truncated"] is True


def test_cut_follows_the_speed_of_another_rule(engine, media):
    port = media()
    slow = add_rule(engine, action="delay", path="/video", download_kbps=2000)
    rule_id = add_rule(engine, action="cut", path="/video", after_percent=50)
    started = time.monotonic()
    response, body, broken = fetch(engine, port, "/video")
    assert broken is not None and body == VIDEO[:len(VIDEO) // 2]
    assert time.monotonic() - started >= 0.4
    assert recorded(engine, rule_id)["rules"] == [{"id": slow, "action": "delay", "limited": "request"},
                                                  {"id": rule_id, "action": "cut", "cut_after_bytes": len(VIDEO) // 2}]


def test_answer_of_a_respond_rule_is_not_cut(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=10)
    answer = add_rule(engine, action="respond", path="/video", body="x" * 100)
    response, body, broken = fetch(engine, port, "/video")
    assert broken is None and body == b"x" * 100
    entry = recorded(engine, rule_id)["rules"][0]
    assert entry["skipped"] == "not cut: the answer comes from rule {0}, not from the server".format(answer)


def test_event_stream_of_the_server_is_not_cut(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/events", after_bytes=10)
    response, body, broken = fetch(engine, port, "/events")
    assert broken is None and body == b"data: one\n\n" * 2000
    entry = recorded(engine, rule_id)["rules"][0]
    assert entry["skipped"] == "not cut: event stream; break it with action events and close"


def test_empty_answer_without_length_is_noted_as_no_body(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/chunked", after_bytes=10)
    response, body, broken = fetch(engine, port, "/chunked?empty")
    assert broken is None and body == b""
    assert recorded(engine, rule_id)["rules"][0]["skipped"] == "not cut: response has no body"


def start_h2_server(tmp_path):
    import h2.config
    import h2.connection
    import h2.events
    import h2.exceptions
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*self_signed(str(tmp_path)))
    context.set_alpn_protocols(["h2"])
    listener = socket.create_server(("127.0.0.1", 0))
    resets = []

    def serve(raw):
        tls = context.wrap_socket(raw, server_side=True)
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        tls.settimeout(0.02)
        pending = {}

        def push():
            for stream_id, pieces in list(pending.items()):
                try:
                    if pieces and len(pieces[0]) <= min(conn.local_flow_control_window(stream_id),
                                                        conn.max_outbound_frame_size):
                        conn.send_data(stream_id, pieces.pop(0), end_stream=not pieces)
                except h2.exceptions.StreamClosedError:
                    pieces.clear()
                if not pieces:
                    del pending[stream_id]

        while True:
            push()
            tls.sendall(conn.data_to_send())
            try:
                data = tls.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    whole = dict(event.headers)[b":path"] == b"/whole"
                    body = VIDEO[:40000] if whole else VIDEO
                    length = [] if whole else [("content-length", str(len(body)))]
                    conn.send_headers(event.stream_id, [(":status", "200"), ("content-type", "video/mp4")] + length)
                    pending[event.stream_id] = [body[:5000], b""] + [body[start:start + 10000]
                                                                     for start in range(5000, len(body), 10000)]
                elif isinstance(event, h2.events.StreamReset):
                    resets.append(event.error_code)
                    pending.pop(event.stream_id, None)

    def accept():
        while True:
            raw, _ = listener.accept()
            threading.Thread(target=serve, args=(raw,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener.getsockname()[1], resets


def test_http2_server_answer_is_cut_with_a_reset_both_ways_and_an_empty_frame_is_not_its_end(engine, tmp_path):
    port, resets = start_h2_server(tmp_path)
    cut = add_rule(engine, action="cut", host="localhost", path="/video", after_bytes=20000)
    whole = add_rule(engine, action="cut", host="localhost", path="/whole", after_bytes=45000)
    streams = h2_through_proxy(engine, port, ["/video", "/whole"])
    assert streams["/video"]["reset"] and streams["/video"]["body"] == VIDEO[:20000]
    assert streams["/whole"]["ended"] and streams["/whole"]["body"] == VIDEO[:40000]
    assert recorded(engine, cut)["rules"] == [{"id": cut, "action": "cut", "cut_after_bytes": 20000}]
    assert recorded(engine, whole)["rules"] == [{"id": whole, "action": "cut",
                                                "skipped": "not cut: the body has 40000 bytes, not more than 45000"}]
    assert wait_until(lambda: resets, 10) == [8]
    assert "Traceback" not in engine_log(engine)


def test_history_keeps_the_part_of_the_body_that_reached_the_app(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=100000)
    response, body, broken = fetch(engine, port, "/video")
    assert broken is not None and body == VIDEO[:100000]
    flows = api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"]
    flow = api_get(engine["api_port"], "/api/flows/{0}?max_body_chars=1000000".format(flows[0]["id"]))[1]
    assert base64.b64decode(flow["response_body"]["content"]) == VIDEO[:100000]


def test_answer_the_server_broke_before_the_point_is_noted_and_keeps_the_rule(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/broken", after_bytes=100000, times=1)
    response, body, broken = fetch(engine, port, "/broken")
    assert broken is not None and body == VIDEO[:5000]
    entry = recorded(engine, rule_id)["rules"][0]
    assert entry["skipped"] == "not cut: the answer broke after 5000 bytes of body, before byte 100000"
    assert api_get(engine["api_port"], "/api/rules")[1]["rules"][0]["remaining"] == 1


def test_no_content_answer_is_not_framed_as_chunked(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/nocontent", after_bytes=10)
    response, body, broken = fetch(engine, port, "/nocontent")
    assert response.status == 204 and body == b"" and broken is None
    assert response.getheader("Transfer-Encoding") is None
    assert recorded(engine, rule_id)["rules"][0]["skipped"] == "not cut: response has no body"


def test_http10_client_gets_an_answer_with_length_cut(engine, media):
    port = media()
    rule_id = add_rule(engine, action="cut", path="/video", after_bytes=1000)
    raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=10)
    raw.sendall("GET http://127.0.0.1:{0}/video HTTP/1.0\r\nHost: 127.0.0.1:{0}\r\n\r\n".format(port).encode())
    answer = b""
    while True:
        data = raw.recv(65536)
        if not data:
            break
        answer += data
    raw.close()
    assert answer.split(b"\r\n\r\n", 1)[1] == VIDEO[:1000]
    assert recorded(engine, rule_id)["rules"][0]["cut_after_bytes"] == 1000
