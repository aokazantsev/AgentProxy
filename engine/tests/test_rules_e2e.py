import http.client
import http.server
import json
import os
import time
import urllib.error
import urllib.request

import pytest

from conftest import free_port
from test_engine_e2e import (api_get, api_post, engine, launch, no_proxy_bypass, proxied_opener, start_engine,
                             start_server, wait_until)


def api_call(port, method, path, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    body = payload if isinstance(payload, bytes) else (json.dumps(payload).encode() if payload is not None else None)
    connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, (json.loads(data) if data else None)


def add_rule(engine, **rule):
    status, answer = api_post(engine["api_port"], "/api/rules", rule)
    assert status == 200, answer
    return answer["rule"]["id"]


def last_flow(engine, **query):
    params = "&".join("{0}={1}".format(name, value) for name, value in query.items())
    flows = api_get(engine["api_port"], "/api/flows?" + params)[1]["flows"]
    assert flows, query
    return api_get(engine["api_port"], "/api/flows/" + flows[0]["id"])[1]


def test_map_local_answers_without_server_and_rereads_file(engine, tmp_path):
    mock = tmp_path / "items.json"
    mock.write_text('{"items": []}', encoding="utf-8")
    rule_id = add_rule(engine, action="respond", path="/v1/items", body_file=str(mock))
    url = "http://127.0.0.1:{0}/v1/items?page=1".format(free_port())
    response = proxied_opener(engine).open(url, timeout=15)
    assert response.status == 200 and json.loads(response.read()) == {"items": []}
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    mock.write_text('{"items": [1]}', encoding="utf-8")
    assert json.loads(proxied_opener(engine).open(url, timeout=15).read()) == {"items": [1]}
    flow = last_flow(engine, rule=rule_id)
    assert flow["status"] == 200 and flow["rules"] == [{"id": rule_id, "action": "respond"}]
    assert json.loads(flow["response_body"]["content"]) == {"items": [1]}
    rules = api_get(engine["api_port"], "/api/rules")[1]
    assert rules["rules"][0]["hits"] == 2
    assert api_get(engine["api_port"], "/api/status")[1]["rules"]["active"] == 1


def test_https_request_is_answered_by_rule(engine, tmp_path):
    target = start_server(str(tmp_path))
    add_rule(engine, action="respond", host="localhost", status=503, body="maintenance",
             headers={"Retry-After": "120"})
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    url = "https://localhost:{0}/secure".format(target.server_port)
    with pytest.raises(urllib.error.HTTPError) as error:
        proxied_opener(engine, cafile).open(url, timeout=15)
    assert error.value.code == 503 and error.value.read() == b"maintenance"
    assert error.value.headers["Retry-After"] == "120"
    assert error.value.headers["Content-Type"] == "text/plain; charset=utf-8"
    target.shutdown()


def test_rewrite_changes_request_and_response(engine):
    target = start_server()
    rule_id = add_rule(engine, action="rewrite", path="/echo", status=202,
                       request_headers={"X-Added": "yes", "Accept": None},
                       response_headers={"X-Mocked": "1"},
                       replace=[{"find": "hello", "with": "привет"}, {"find": "absent", "with": "x"}])
    url = "http://127.0.0.1:{0}/echo".format(target.server_port)
    request = urllib.request.Request(url, data=b"hello hello", method="POST", headers={"Accept": "text/plain"})
    response = proxied_opener(engine).open(request, timeout=15)
    assert response.status == 202 and response.headers["X-Mocked"] == "1"
    assert json.loads(response.read().decode("utf-8"))["echo"] == "привет привет"
    flow = last_flow(engine, rule=rule_id)
    sent = dict((name.lower(), value) for name, value in flow["request_headers"])
    assert sent["x-added"] == "yes" and "accept" not in sent
    assert flow["rules"] == [{"id": rule_id, "action": "rewrite", "replaced": 2, "not_found": ["absent"]}]
    assert "привет" in flow["response_body"]["content"]
    target.shutdown()


def test_drop_delay_and_times_scenario(engine):
    target = start_server()
    add_rule(engine, action="drop", path="/pay", times=1, name="first attempt fails")
    add_rule(engine, action="respond", path="/pay", body='{"paid": true}', delay_ms=700)
    url = "http://127.0.0.1:{0}/pay".format(target.server_port)
    with pytest.raises((ConnectionError, http.client.RemoteDisconnected, urllib.error.URLError)) as error:
        proxied_opener(engine).open(url, timeout=15).read()
    assert not isinstance(error.value, urllib.error.HTTPError)
    started = time.monotonic()
    response = proxied_opener(engine).open(url, timeout=15)
    assert time.monotonic() - started >= 0.6
    assert json.loads(response.read()) == {"paid": True}
    dropped = last_flow(engine, rule="r1")
    assert "killed" in dropped["error"] and dropped["status"] is None
    assert dropped["rules"][0]["name"] == "first attempt fails"
    rules = api_get(engine["api_port"], "/api/rules")[1]["rules"]
    assert [rule["active"] for rule in rules] == [False, True]
    target.shutdown()


def test_missing_body_file_answers_502_with_reason(engine, tmp_path):
    mock = tmp_path / "gone.json"
    mock.write_text("{}")
    rule_id = add_rule(engine, action="respond", path="/gone", body_file=str(mock))
    os.remove(str(mock))
    with pytest.raises(urllib.error.HTTPError) as error:
        proxied_opener(engine).open("http://127.0.0.1:{0}/gone".format(free_port()), timeout=15)
    body = error.value.read()
    assert error.value.code == 502 and b"unreadable" in body and b"gone.json" not in body
    rule = api_get(engine["api_port"], "/api/rules")[1]["rules"][0]
    assert "unreadable" in rule["last_error"] and "gone.json" in rule["last_error"]
    assert "unreadable" in last_flow(engine, rule=rule_id)["rules"][0]["error"]


def test_large_map_local_is_sent_whole_and_recorded_from_the_start(start_engine, tmp_path):
    engine = start_engine(max_body_bytes=1024)
    mock = tmp_path / "big.bin"
    mock.write_bytes(os.urandom(5000))
    add_rule(engine, action="respond", path="/big", body_file=str(mock))
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/big".format(free_port()), timeout=15)
    assert response.read() == mock.read_bytes()
    flow = last_flow(engine, path="/big")
    assert flow["response_body"]["size"] == 5000 and flow["response_body"]["truncated"] is True


def test_rules_survive_engine_restart_with_fresh_hits(start_engine):
    first = start_engine()
    add_rule(first, action="respond", path="/kept", body="kept")
    url = "http://127.0.0.1:{0}/kept".format(free_port())
    assert proxied_opener(first).open(url, timeout=15).read() == b"kept"
    first["process"].kill()
    first["process"].wait(10)
    second = launch(first["data_dir"])
    try:
        assert wait_until(lambda: proxy_ready(first), 40)
        rules = api_get(first["api_port"], "/api/rules")[1]["rules"]
        assert [(rule["id"], rule["hits"]) for rule in rules] == [("r1", 0)]
        assert proxied_opener(first).open(url, timeout=15).read() == b"kept"
    finally:
        second.kill()
        second.wait(10)


def proxy_ready(engine):
    try:
        status, body = api_get(engine["api_port"], "/api/status", timeout=2)
        return status == 200 and body["proxy"]["running"]
    except OSError:
        return False


class ChunkedHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in (b'{"state": ', b'"old", ', b'"n": 1}'):
            self.wfile.write("{0:x}\r\n".format(len(piece)).encode() + piece + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args):
        pass


def test_chunked_response_body_is_rewritten(engine):
    import threading
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChunkedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rule_id = add_rule(engine, action="rewrite", path="/chunked", replace=[{"find": "old", "with": "new"}])
    url = "http://127.0.0.1:{0}/chunked".format(server.server_port)
    response = proxied_opener(engine).open(url, timeout=15)
    assert not (response.headers["Transfer-Encoding"] and response.headers["Content-Length"])
    assert json.loads(response.read()) == {"state": "new", "n": 1}
    flow = last_flow(engine, rule=rule_id)
    assert json.loads(flow["response_body"]["content"]) == {"state": "new", "n": 1}
    assert flow["rules"][0]["replaced"] == 1
    server.shutdown()


def test_https_mock_works_when_the_host_is_unreachable(engine):
    add_rule(engine, action="respond", host="localhost", path="/offline", body='{"mock": true}')
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    url = "https://localhost:{0}/offline".format(free_port())
    response = proxied_opener(engine, cafile).open(url, timeout=15)
    assert json.loads(response.read()) == {"mock": True}


def test_rules_api_routes(engine):
    port = engine["api_port"]
    assert api_call(port, "POST", "/api/rules", b"{broken")[0] == 400
    with_mark = b"\xef\xbb\xbf" + json.dumps({"action": "respond", "path": "/bom", "body": "x"}).encode("utf-8")
    assert api_call(port, "POST", "/api/rules", with_mark)[0] == 200
    status, answer = api_call(port, "POST", "/api/rules", {"action": "respond", "body": "x"})
    assert status == 400 and "condition" in answer["error"]
    rule_id = api_call(port, "POST", "/api/rules", {"action": "respond", "path": "/a", "body": "x"})[1]["rule"]["id"]
    status, answer = api_call(port, "PATCH", "/api/rules/" + rule_id, {"enabled": False})
    assert status == 200 and answer["rule"]["enabled"] is False
    assert api_call(port, "PATCH", "/api/rules/r99", {"enabled": False})[0] == 404
    assert api_call(port, "DELETE", "/api/rules/r99")[0] == 404
    assert api_call(port, "DELETE", "/api/rules/" + rule_id)[1]["removed"] == [rule_id]
    api_call(port, "POST", "/api/rules", {"action": "drop", "path": "/b"})
    assert api_call(port, "DELETE", "/api/rules")[1]["counts"]["total"] == 0


def test_mcp_lists_rule_tools(engine):
    status, answer = api_post(engine["api_port"], "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200
    names = [tool["name"] for tool in answer["result"]["tools"]]
    for name in ("add_rule", "list_rules", "update_rule", "delete_rule"):
        assert name in names
    status, answer = api_post(engine["api_port"], "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "add_rule", "arguments": {"action": "respond", "path": "/m", "body": "{}"}}})
    assert answer["result"]["isError"] is False
    assert answer["result"]["structuredContent"]["rule"]["id"] == "r1"


def test_rewrite_before_respond_changes_the_mock(engine):
    add_rule(engine, action="rewrite", path="/chain", status=201, response_headers={"X-Debug": "1"},
             replace=[{"find": "a", "with": "b"}])
    add_rule(engine, action="respond", path="/chain", body="aaa")
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/chain".format(free_port()), timeout=15)
    assert response.status == 201 and response.headers["X-Debug"] == "1" and response.read() == b"bbb"


def test_https_mock_on_one_path_lets_other_paths_reach_the_server(engine, tmp_path):
    target = start_server(str(tmp_path))
    add_rule(engine, action="respond", host="localhost", path="/mocked", body='{"mock": true}')
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    base = "https://localhost:{0}".format(target.server_port)
    opener = proxied_opener(engine, cafile)
    assert json.loads(opener.open(base + "/mocked", timeout=15).read()) == {"mock": True}
    real = opener.open(base + "/real", timeout=15)
    assert real.status == 201 and json.loads(real.read())["path"] == "/real"
    target.shutdown()


def test_chunked_response_rewritten_to_no_content_keeps_the_connection_usable(engine):
    import threading
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChunkedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    add_rule(engine, action="rewrite", path="/empty", status=204)
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=15)
    base = "http://127.0.0.1:{0}".format(server.server_port)
    connection.request("GET", base + "/empty")
    first = connection.getresponse()
    assert first.status == 204 and first.read() == b""
    connection.request("GET", base + "/after")
    second = connection.getresponse()
    assert second.status == 200 and json.loads(second.read()) == {"state": "old", "n": 1}
    connection.close()
    server.shutdown()


def test_text_that_is_not_valid_unicode_is_answered_not_crashed(engine):
    port = engine["api_port"]
    status, answer = api_call(port, "POST", "/api/rules", b'{"action": "drop", "host": "x", "\\ud800": 1}')
    assert status == 400 and "unknown field" in answer["error"]
    status, answer = api_call(port, "DELETE", "/api/rules/%ED%A0%80")
    assert status in (400, 404)


def test_delete_all_refuses_parameters(engine):
    port = engine["api_port"]
    add_rule(engine, action="drop", host="kept.example")
    status, answer = api_call(port, "DELETE", "/api/rules?id=r1")
    assert status == 400 and "/api/rules/<id>" in answer["error"]
    assert api_get(port, "/api/rules")[1]["counts"]["total"] == 1
    status, answer = api_call(port, "DELETE", "/api/rules", {"id": "r1"})
    assert status == 400 and api_get(port, "/api/rules")[1]["counts"]["total"] == 1
    status, answer = api_call(port, "POST", "/api/rules", b"[" * 100000 + b"]" * 100000)
    assert status == 400
    status, answer = api_call(port, "POST", "/mcp", b"[" * 100000 + b"]" * 100000)
    assert answer["error"]["code"] == -32700


def test_https_drop_breaks_the_connection_when_the_host_is_unreachable(engine):
    rule_id = add_rule(engine, action="drop", host="localhost", path="/offline")
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    url = "https://localhost:{0}/offline".format(free_port())
    with pytest.raises((ConnectionError, http.client.RemoteDisconnected, urllib.error.URLError)) as error:
        proxied_opener(engine, cafile).open(url, timeout=15).read()
    assert not isinstance(error.value, urllib.error.HTTPError)
    assert wait_until(lambda: api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"], 10)
    dropped = last_flow(engine, rule=rule_id)
    assert dropped["rules"] == [{"id": rule_id, "action": "drop"}]
    assert "killed" in dropped["error"] and dropped["status"] is None
    failures = api_get(engine["api_port"], "/api/flows?failed_only=true")[1]["flows"]
    assert not [flow for flow in failures if "CONNECT failed" in (flow["error"] or "")]


def test_rule_for_loopback_does_not_open_the_proxy_api(engine):
    add_rule(engine, action="respond", host="127.0.0.1", path="/api/status", body='{"mock": true}')
    url = "http://127.0.0.1:{0}/api/status".format(engine["api_port"])
    with pytest.raises(urllib.error.HTTPError) as failure:
        proxied_opener(engine).open(url, timeout=15)
    assert failure.value.code == 502 and b"mock" not in failure.value.read()
    assert api_get(engine["api_port"], "/api/rules")[1]["rules"][0]["hits"] == 0


def h2_through_proxy(engine, port, paths):
    import socket
    import ssl
    import h2.config
    import h2.connection
    import h2.events
    raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    raw.sendall("CONNECT localhost:{0} HTTP/1.1\r\nHost: localhost:{0}\r\n\r\n".format(port).encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        reply += raw.recv(1024)
    assert b" 200 " in reply.split(b"\r\n")[0]
    context = ssl.create_default_context(cafile=os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem"))
    context.set_alpn_protocols(["h2"])
    tls = context.wrap_socket(raw, server_hostname="localhost")
    assert tls.selected_alpn_protocol() == "h2"
    connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    connection.initiate_connection()
    streams = {}
    for path in paths:
        stream_id = connection.get_next_available_stream_id()
        connection.send_headers(stream_id, [(":method", "GET"), (":path", path), (":scheme", "https"),
                                            (":authority", "localhost:{0}".format(port))], end_stream=True)
        streams[stream_id] = {"path": path, "status": None, "body": b"", "reset": False, "ended": False}
    tls.sendall(connection.data_to_send())
    while not all(stream["reset"] or stream["ended"] for stream in streams.values()):
        data = tls.recv(65536)
        if not data:
            break
        for event in connection.receive_data(data):
            if isinstance(event, h2.events.ResponseReceived):
                streams[event.stream_id]["status"] = dict(event.headers)[b":status"]
            elif isinstance(event, h2.events.DataReceived):
                streams[event.stream_id]["body"] += event.data
                connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                streams[event.stream_id]["ended"] = True
            elif isinstance(event, h2.events.StreamReset):
                streams[event.stream_id]["reset"] = True
        tls.sendall(connection.data_to_send())
    tls.close()
    return dict((stream["path"], stream) for stream in streams.values())


def test_http2_drop_resets_only_its_own_stream(engine, tmp_path):
    target = start_server(str(tmp_path))
    rule_id = add_rule(engine, action="drop", host="localhost", path="/dropped")
    streams = h2_through_proxy(engine, target.server_port, ["/dropped", "/real"])
    assert streams["/dropped"]["reset"] and streams["/dropped"]["status"] is None
    assert streams["/real"]["ended"] and not streams["/real"]["reset"]
    assert streams["/real"]["status"] == b"201" and json.loads(streams["/real"]["body"])["path"] == "/real"
    assert wait_until(lambda: api_get(engine["api_port"], "/api/flows?rule=" + rule_id)[1]["flows"], 10)
    assert last_flow(engine, rule=rule_id)["rules"] == [{"id": rule_id, "action": "drop"}]
    target.shutdown()
