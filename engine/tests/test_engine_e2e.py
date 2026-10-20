import datetime
import http.client
import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib

import pytest

from conftest import free_port

ENGINE_START_TIMEOUT = 40


class EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._reply(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self._reply(self.rfile.read(length))

    def _reply(self, request_body):
        body = json.dumps({"path": self.path, "echo": request_body.decode("utf-8")}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def self_signed(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_path = os.path.join(directory, "server.pem")
    key_path = os.path.join(directory, "server.key")
    with open(cert_path, "wb") as stream:
        stream.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as stream:
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return cert_path, key_path


def start_server(tls_dir=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    if tls_dir:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*self_signed(tls_dir))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def api_get(port, path, headers=None, timeout=10):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, (json.loads(body) if body and body[:1] in b"{[" else body)


def api_post(port, path, payload, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(payload).encode()
    connection.request("POST", path, body=data, headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, (json.loads(body) if body else None)


def wait_until(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    return None


def launch(data_dir, extra_args=()):
    env = dict(os.environ)
    env["AGENTPROXY_DATA_DIR"] = data_dir
    return subprocess.Popen([sys.executable, "-I", "-m", "agentproxy"] + list(extra_args),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def two_ports():
    first = free_port()
    second = free_port()
    while second == first:
        second = free_port()
    return first, second


@pytest.fixture
def start_engine(tmp_path):
    started = []

    def start(**settings):
        data_dir = str(tmp_path / "engine-data-{0}".format(len(started)))
        os.makedirs(data_dir)
        proxy_port, api_port = two_ports()
        values = {"proxy_port": proxy_port, "api_port": api_port, "verify_upstream": False}
        values.update(settings)
        with open(os.path.join(data_dir, "settings.json"), "w") as stream:
            json.dump(values, stream)
        process = launch(data_dir)
        started.append(process)

        def ready():
            try:
                status, body = api_get(values["api_port"], "/api/status", timeout=2)
                return status == 200 and body["proxy"]["running"]
            except OSError:
                return False

        if not wait_until(ready, ENGINE_START_TIMEOUT):
            process.kill()
            log = open(os.path.join(data_dir, "engine.log"), encoding="utf-8").read()
            pytest.fail("engine did not start:\n" + log)
        return dict(values, process=process, data_dir=data_dir)

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
            process.wait(10)


@pytest.fixture
def engine(start_engine):
    return start_engine()


@pytest.fixture(autouse=True)
def no_proxy_bypass(monkeypatch):
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)


def proxied_opener(engine, cafile=None):
    proxy = "http://127.0.0.1:{0}".format(engine["proxy_port"])
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    if cafile:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
    return urllib.request.build_opener(*handlers)


def test_http_flow_is_recorded_and_queryable(engine):
    target = start_server()
    url = "http://127.0.0.1:{0}/v1/echo?x=1".format(target.server_port)
    response = proxied_opener(engine).open(urllib.request.Request(url, data=b'{"hello":"world"}', method="POST"), timeout=15)
    assert response.status == 201
    status, listing = api_get(engine["api_port"], "/api/flows?path=/v1/echo")
    assert status == 200
    flow = listing["flows"][0]
    assert flow["method"] == "POST" and flow["status"] == 201
    status, full = api_get(engine["api_port"], "/api/flows/" + flow["id"])
    assert json.loads(full["request_body"]["content"]) == {"hello": "world"}
    assert json.loads(full["response_body"]["content"])["path"] == "/v1/echo?x=1"
    target.shutdown()


def test_https_is_intercepted_with_generated_ca(engine, tmp_path):
    target = start_server(str(tmp_path))
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    assert os.path.exists(cafile)
    url = "https://localhost:{0}/secure".format(target.server_port)
    response = proxied_opener(engine, cafile).open(url, timeout=15)
    assert response.status == 201
    status, listing = api_get(engine["api_port"], "/api/flows?path=/secure")
    assert listing["flows"][0]["scheme"] == "https"
    status, cert = api_get(engine["api_port"], "/api/ca.pem")
    assert status == 200 and b"BEGIN CERTIFICATE" in cert
    target.shutdown()


def test_mcp_over_http_and_wait(engine):
    port = engine["api_port"]
    status, init = api_post(port, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-06-18"}})
    assert status == 200 and init["result"]["serverInfo"]["name"] == "agentproxy"
    status, _ = api_post(port, "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    target = start_server()
    result = {}
    since = api_get(port, "/api/status")[1]["last_seq"]

    def call_wait():
        result["response"] = api_post(port, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "wait_for_flow", "arguments": {"path": "/later", "since_seq": since, "timeout_seconds": 30}}})

    waiter = threading.Thread(target=call_wait)
    waiter.start()
    proxied_opener(engine).open("http://127.0.0.1:{0}/later".format(target.server_port), timeout=15)
    waiter.join(40)
    assert "response" in result
    payload = result["response"][1]["result"]["structuredContent"]
    assert payload["timed_out"] is False and payload["flow"]["path"] == "/later"
    target.shutdown()


def test_foreign_host_and_origin_are_rejected(engine):
    port = engine["api_port"]
    status, _ = api_get(port, "/api/status", headers={"Host": "evil.example:{0}".format(port)})
    assert status == 403
    status, _ = api_get(port, "/api/status", headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _ = api_get(port, "/api/status", headers={"Origin": "http://127.0.0.1:{0}".format(port)})
    assert status == 200
    status, _ = api_get(port, "/api/status", headers={"X-AgentProxy-Proxied": "1"})
    assert status == 403


def test_monitor_page_served(engine):
    status, body = api_get(engine["api_port"], "/")
    assert status == 200 and b"AgentProxy" in body
    for element in (b'id="fMethod"', b'id="fCode"', b'id="fMode"', b'id="fPinned"', b'id="menu"', b'id="heads"', b'id="rows"', b'id="nomatch"'):
        assert element in body


def test_monitor_pins_are_stored_in_data_directory(engine):
    port = engine["api_port"]
    assert api_get(port, "/api/monitor/pins") == (200, {"pinned_hosts": []})
    assert api_post(port, "/api/monitor/pins", {"host": "api.example.com", "pinned": True}) == (200, {"pinned_hosts": ["api.example.com"]})
    assert api_post(port, "/api/monitor/pins", {"host": "cdn.example.com", "pinned": True}) == (200, {"pinned_hosts": ["api.example.com", "cdn.example.com"]})
    assert api_post(port, "/api/monitor/pins", {"host": "api.example.com", "pinned": False}) == (200, {"pinned_hosts": ["cdn.example.com"]})
    with open(os.path.join(engine["data_dir"], "monitor.json"), encoding="utf-8") as stream:
        assert json.load(stream) == {"pinned_hosts": ["cdn.example.com"]}
    status, body = api_post(port, "/api/monitor/pins", {"host": "", "pinned": True})
    assert status == 400 and "host" in body["error"]
    status, body = api_post(port, "/api/monitor/pins", {"host": "a.example", "pinned": "yes"})
    assert status == 400 and "pinned" in body["error"]
    status, _ = api_post(port, "/api/monitor/pins", {"host": "x.example", "pinned": True}, headers={"Origin": "https://evil.example"})
    assert status == 403
    assert api_get(port, "/api/monitor/pins") == (200, {"pinned_hosts": ["cdn.example.com"]})


def test_monitor_pins_unreadable_file_is_reported(engine):
    os.makedirs(os.path.join(engine["data_dir"], "monitor.json"))
    status, body = api_get(engine["api_port"], "/api/monitor/pins")
    assert status == 500 and "monitor.json" in body["error"]
    status, body = api_post(engine["api_port"], "/api/monitor/pins", {"host": "a.example", "pinned": True})
    assert status == 500 and "monitor.json" in body["error"]


def test_shutdown_requires_token(engine):
    port = engine["api_port"]
    status, _ = api_post(port, "/api/shutdown", {}, headers={"X-AgentProxy-Token": "wrong"})
    assert status == 403
    with open(os.path.join(engine["data_dir"], "engine-runtime.json")) as stream:
        runtime = json.load(stream)
    status, _ = api_post(port, "/api/shutdown", {}, headers={"X-AgentProxy-Token": runtime["token"]})
    assert status == 200
    assert engine["process"].wait(20) == 0
    assert not os.path.exists(os.path.join(engine["data_dir"], "engine-runtime.json"))


def test_waiting_clients_get_error_when_engine_stops(engine):
    port = engine["api_port"]
    answers = {}
    mcp_call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "wait_for_flow", "arguments": {"path": "/never", "timeout_seconds": 60}}}
    waiters = [
        threading.Thread(target=lambda: answers.update(api=api_get(port, "/api/flows/wait?path=/never&timeout_seconds=60", timeout=30))),
        threading.Thread(target=lambda: answers.update(mcp=api_post(port, "/mcp", mcp_call))),
    ]
    for waiter in waiters:
        waiter.start()
    time.sleep(1)
    with open(os.path.join(engine["data_dir"], "engine-runtime.json")) as stream:
        runtime = json.load(stream)
    api_post(port, "/api/shutdown", {}, headers={"X-AgentProxy-Token": runtime["token"]})
    for waiter in waiters:
        waiter.join(30)
    assert answers["api"] == (503, {"error": "engine is stopping"})
    status, body = answers["mcp"]
    assert status == 503 and body["id"] == 7 and body["error"]["message"] == "engine is stopping"
    assert engine["process"].wait(20) == 0


def test_prompt_hook_endpoint_answers_json(engine):
    status, body = api_post(engine["api_port"], "/claude/hooks/user-prompt-submit", {"session_id": "e2e"})
    assert status == 200 and body == {}


def test_prompt_hook_tells_open_session_about_update(engine):
    with open(os.path.join(engine["data_dir"], "claude-sessions.json"), "w", encoding="utf-8") as stream:
        json.dump({"old-session": {"version": "0.0.9", "seen": time.time()}}, stream)
    status, body = api_post(engine["api_port"], "/claude/hooks/user-prompt-submit", {"session_id": "old-session"})
    assert status == 200
    assert "обновлён 0.0.9 →" in body["hookSpecificOutput"]["additionalContext"]
    status, body = api_post(engine["api_port"], "/claude/hooks/user-prompt-submit", {"session_id": "old-session"})
    assert status == 200 and body == {}


def test_busy_port_exits_with_code_3(tmp_path):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    data_dir = str(tmp_path / "busy")
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, "settings.json"), "w") as stream:
        json.dump({"proxy_port": holder.getsockname()[1], "api_port": free_port()}, stream)
    process = launch(data_dir)
    assert process.wait(40) == 3
    assert "PORT BUSY" in open(os.path.join(data_dir, "engine.log"), encoding="utf-8").read()
    holder.close()


def test_engine_stops_when_parent_exits(tmp_path):
    parent = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    data_dir = str(tmp_path / "orphan")
    os.makedirs(data_dir)
    proxy_port, api_port = two_ports()
    with open(os.path.join(data_dir, "settings.json"), "w") as stream:
        json.dump({"proxy_port": proxy_port, "api_port": api_port}, stream)
    process = launch(data_dir, ["--parent-pid", str(parent.pid)])

    def up():
        try:
            return api_get(api_port, "/api/status", timeout=2)[0] == 200
        except OSError:
            return False

    assert wait_until(up, ENGINE_START_TIMEOUT)
    parent.kill()
    assert process.wait(30) == 0


class StreamingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release = None

    def do_GET(self):
        if self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b"data: first\n\n")
            StreamingHandler.release.wait(20)
            self._chunk(b"data: second\n\n")
            self._chunk(b"")
            return
        if self.path.startswith("/chunked"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(b'{"part": 1}\n')
            StreamingHandler.release.wait(20)
            self._chunk(b'{"part": 2}\n')
            self._chunk(b"")
            return
        if self.path.startswith("/packed-"):
            encoding = self.path[len("/packed-"):]
            body = b'{"packed": "' + encoding.encode() + b'"}'
            if encoding == "zstd":
                import zstandard
                wire = zstandard.ZstdCompressor().compress(body)
            else:
                import brotli
                wire = brotli.compress(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", encoding)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(wire[:5])
            self._chunk(wire[5:])
            self._chunk(b"")
            return
        if self.path.startswith("/gzip-events"):
            compressor = zlib.compressobj(wbits=31)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(compressor.compress(b"data: packed\n\n") + compressor.flush(zlib.Z_SYNC_FLUSH))
            self._chunk(compressor.flush())
            self._chunk(b"")
            return
        if self.path.startswith("/bigslow"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "10240")
            self.end_headers()
            self.wfile.write(b"a" * 2048)
            self.wfile.flush()
            StreamingHandler.release.wait(20)
            self.wfile.write(b"b" * 8192)
            return
        body = b"x" * 10240
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, data):
        self.wfile.write("{0:x}\r\n".format(len(data)).encode() + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def start_streaming_server():
    StreamingHandler.release = threading.Event()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_server_sent_events_pass_through_while_open_and_are_recorded(engine):
    target = start_streaming_server()
    response = proxied_opener(engine).open("http://127.0.0.1:{0}/events".format(target.server_port), timeout=10)
    assert response.readline() == b"data: first\n"
    StreamingHandler.release.set()
    rest = response.read()
    assert b"data: second" in rest
    status, listing = api_get(engine["api_port"], "/api/flows?path=/events")
    flow = api_get(engine["api_port"], "/api/flows/" + listing["flows"][0]["id"])[1]
    assert "data: first" in flow["response_body"]["content"] and "data: second" in flow["response_body"]["content"]
    target.shutdown()


def test_large_body_is_cut_to_limit_but_delivered_whole(start_engine):
    engine = start_engine(max_body_bytes=1024)
    target = start_streaming_server()
    body = proxied_opener(engine).open("http://127.0.0.1:{0}/big".format(target.server_port), timeout=15).read()
    assert len(body) == 10240
    listing = api_get(engine["api_port"], "/api/flows?path=/big")[1]
    flow = api_get(engine["api_port"], "/api/flows/{0}?max_body_chars=100000".format(listing["flows"][0]["id"]))[1]
    assert flow["response_body"]["size"] == 10240
    assert flow["response_body"]["truncated"] is True
    assert len(flow["response_body"]["content"]) < 2048
    target.shutdown()


def test_unreachable_upstream_is_recorded_as_failure(engine):
    closed = free_port()
    with pytest.raises(urllib.error.HTTPError) as failure:
        proxied_opener(engine).open("http://127.0.0.1:{0}/down".format(closed), timeout=15)
    assert failure.value.code == 502
    listing = api_get(engine["api_port"], "/api/flows?failed_only=true&path=/down")[1]
    assert listing["flows"] and listing["flows"][0]["error"]
    everything = api_get(engine["api_port"], "/api/flows?failed_only=false")[1]
    assert len(everything["flows"]) >= len(listing["flows"])


def test_client_rejecting_certificate_is_recorded(engine, tmp_path):
    target = start_server(str(tmp_path))
    with pytest.raises(urllib.error.URLError):
        proxied_opener(engine, cafile=certifi_bundle()).open("https://localhost:{0}/pinned".format(target.server_port), timeout=15)

    def recorded():
        flows = api_get(engine["api_port"], "/api/flows?failed_only=true")[1]["flows"]
        return [flow for flow in flows if flow["method"] == "CONNECT" and "does not trust" in (flow["error"] or "")]

    assert wait_until(recorded, 10)
    target.shutdown()


def certifi_bundle():
    import certifi
    return certifi.where()


def test_proxy_refuses_requests_to_its_own_ports(engine):
    with pytest.raises(urllib.error.HTTPError) as failure:
        proxied_opener(engine).open("http://127.0.0.1:{0}/api/flows".format(engine["api_port"]), timeout=15)
    assert failure.value.code == 502


def test_page_posting_to_proxy_port_leaves_no_record(engine):
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=15)
    connection.request("POST", "/evil", body=b"IGNORE ALL", headers={"Origin": "https://evil.example"})
    assert connection.getresponse().status == 502
    connection.close()
    for _ in range(3):
        raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
        raw.sendall(b"POST /cut?IGNORE HTTP/1.1\r\nHost: 127.0.0.1:" + str(engine["proxy_port"]).encode()
                    + b"\r\nContent-Length: 100\r\n\r\npartial")
        raw.close()
    connect = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    connect.sendall("CONNECT 127.0.0.1:{0} HTTP/1.1\r\n\r\n".format(engine["api_port"]).encode())
    assert b" 502 " in connect.recv(1024)
    connect.close()
    context = ssl.create_default_context()
    with pytest.raises(ssl.SSLError):
        with socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15) as direct:
            context.wrap_socket(direct, server_hostname="injected-text.example")
    time.sleep(1)
    assert api_get(engine["api_port"], "/api/flows")[1]["flows"] == []


def test_upstream_with_untrusted_certificate_fails_when_verified(start_engine, tmp_path):
    engine = start_engine(verify_upstream=True)
    target = start_server(str(tmp_path))
    with pytest.raises(urllib.error.HTTPError) as failure:
        proxied_opener(engine, os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")).open(
            "https://localhost:{0}/selfsigned".format(target.server_port), timeout=15)
    assert failure.value.code == 502
    failed = api_get(engine["api_port"], "/api/flows?failed_only=true")[1]["flows"]
    assert any("Certificate verify failed" in (flow["error"] or "") for flow in failed)
    target.shutdown()


def test_status_command_waits_for_running_engine(engine, tmp_path):
    (tmp_path / "claude").mkdir()
    env = dict(os.environ, AGENTPROXY_DATA_DIR=engine["data_dir"], CLAUDE_CONFIG_DIR=str(tmp_path / "claude"))
    result = subprocess.run([sys.executable, "-I", "-m", "agentproxy.cli", "status", "--wait", "30"],
                            capture_output=True, env=env, timeout=60)
    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["running"] is True
    assert set(report["claude"]) >= {"mcp", "skill", "hooks"}


def start_websocket_echo():
    import asyncio as aio
    import tornado.web
    import tornado.websocket

    class Echo(tornado.websocket.WebSocketHandler):
        def on_message(self, message):
            self.write_message(message, binary=isinstance(message, bytes))

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
    return holder


CRLF = b"\r\n"
HEADERS_END = CRLF + CRLF


def masked_frame(payload):
    mask = bytes([1, 2, 3, 4])
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes([0x81, 0x80 | len(payload)]) + mask + body


def test_websocket_passes_through_and_handshake_is_recorded(engine):
    target = start_websocket_echo()
    sock = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    try:
        lines = ["GET http://127.0.0.1:{0}/ws HTTP/1.1".format(target["port"]),
                 "Host: 127.0.0.1:{0}".format(target["port"]), "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"]
        sock.sendall(CRLF.join(line.encode() for line in lines) + HEADERS_END)
        head = b""
        while HEADERS_END not in head:
            head += sock.recv(4096)
        assert head.startswith(b"HTTP/1.1 101")
        rest = head.split(HEADERS_END, 1)[1]
        sock.sendall(masked_frame(b"hello"))
        while len(rest) < 7:
            rest += sock.recv(4096)
        assert rest[:7] == bytes([0x81, 5]) + b"hello"
    finally:
        sock.close()
        target["loop"].call_soon_threadsafe(target["loop"].stop)

    def recorded():
        flows = api_get(engine["api_port"], "/api/flows?path=/ws")[1]["flows"]
        return [flow for flow in flows if flow["status"] == 101]

    assert wait_until(recorded, 10)


def test_streamed_upload_to_own_port_never_reaches_it(engine):
    def chunks():
        yield b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call",'
        yield b' "params": {"name": "clear_flows", "arguments": {}}}'

    request = urllib.request.Request("http://localhost:{0}/mcp".format(engine["api_port"]), data=chunks(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Transfer-Encoding": "chunked"})
    with pytest.raises(urllib.error.HTTPError) as failure:
        proxied_opener(engine).open(request, timeout=15)
    assert failure.value.code == 502


def test_claude_urls_from_integration_hit_engine_routes(engine):
    from agentproxy.claude import integration
    env = integration.Environment(claude_dir="", claude_json="", data_dir=engine["data_dir"], install_dir="",
                                  python_exe="", proxy_port=engine["proxy_port"], api_port=engine["api_port"])
    for url, payload in ((env.mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                         (env.hook_url, {"session_id": "contract"})):
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            assert response.status == 200


def test_fetch_status_ignores_proxy_environment(engine, monkeypatch):
    from agentproxy.claude import hooks, integration
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    env = integration.Environment(claude_dir="", claude_json="", data_dir=engine["data_dir"], install_dir="",
                                  python_exe="", proxy_port=engine["proxy_port"], api_port=engine["api_port"])
    status = hooks.fetch_status(env)
    assert status is not None and status["proxy"]["running"] is True


def read_first_part_before_release(opener, url, size):
    response = opener.open(url, timeout=10)
    first = response.read(size) if size else response.readline()
    StreamingHandler.release.set()
    return first, response.read()


def test_large_request_body_is_streamed_and_cut_to_limit(start_engine):
    engine = start_engine(max_body_bytes=1024)
    target = start_server()
    body = b"y" * 10240
    request = urllib.request.Request("http://127.0.0.1:{0}/upload".format(target.server_port), data=body,
                                     method="POST")
    reply = json.loads(proxied_opener(engine).open(request, timeout=15).read())
    assert reply["echo"] == body.decode()
    listing = api_get(engine["api_port"], "/api/flows?path=/upload")[1]
    flow = api_get(engine["api_port"], "/api/flows/" + listing["flows"][0]["id"])[1]
    assert flow["request_body"]["size"] == 10240
    assert flow["request_body"]["truncated"] is True
    assert flow["request_body"]["content"] == "y" * 1024
    target.shutdown()


def test_large_body_streams_before_upstream_finishes(start_engine):
    engine = start_engine(max_body_bytes=1024)
    target = start_streaming_server()
    first, rest = read_first_part_before_release(
        proxied_opener(engine), "http://127.0.0.1:{0}/bigslow".format(target.server_port), 2048)
    assert first == b"a" * 2048 and len(rest) == 8192
    target.shutdown()


def test_chunked_response_without_length_streams_and_is_recorded(engine):
    target = start_streaming_server()
    first, rest = read_first_part_before_release(
        proxied_opener(engine), "http://127.0.0.1:{0}/chunked".format(target.server_port), 0)
    assert first == b'{"part": 1}\n' and b'"part": 2' in rest
    listing = api_get(engine["api_port"], "/api/flows?path=/chunked")[1]
    flow = api_get(engine["api_port"], "/api/flows/" + listing["flows"][0]["id"])[1]
    assert '"part": 1' in flow["response_body"]["content"] and '"part": 2' in flow["response_body"]["content"]
    target.shutdown()


def test_compressed_stream_is_recorded_decoded(engine):
    target = start_streaming_server()
    request = urllib.request.Request("http://127.0.0.1:{0}/gzip-events".format(target.server_port),
                                     headers={"Accept-Encoding": "gzip"})
    proxied_opener(engine).open(request, timeout=10).read()
    listing = api_get(engine["api_port"], "/api/flows?path=/gzip-events")[1]
    flow = api_get(engine["api_port"], "/api/flows/" + listing["flows"][0]["id"])[1]
    assert flow["response_body"]["content"] == "data: packed\n\n"
    target.shutdown()


@pytest.mark.parametrize("encoding", ["zstd", "br"])
def test_chunked_compressed_response_is_recorded_decoded(engine, encoding):
    target = start_streaming_server()
    path = "/packed-" + encoding
    request = urllib.request.Request("http://127.0.0.1:{0}{1}".format(target.server_port, path),
                                     headers={"Accept-Encoding": encoding})
    proxied_opener(engine).open(request, timeout=10).read()
    listing = api_get(engine["api_port"], "/api/flows?path=" + path)[1]
    flow = api_get(engine["api_port"], "/api/flows/" + listing["flows"][0]["id"])[1]
    assert json.loads(flow["response_body"]["content"]) == {"packed": encoding}
    target.shutdown()


def test_https_to_closed_port_is_recorded_as_connect_failure(engine):
    closed = free_port()
    with pytest.raises(urllib.error.URLError):
        proxied_opener(engine).open("https://127.0.0.1:{0}/".format(closed), timeout=15)

    def recorded():
        flows = api_get(engine["api_port"], "/api/flows?failed_only=true")[1]["flows"]
        return [f for f in flows if f["method"] == "CONNECT" and "CONNECT failed" in (f["error"] or "")]

    assert wait_until(recorded, 10)


@pytest.mark.parametrize("host", ["localhost.", "LOCALHOST", "127.0.0.1"])
def test_guard_blocks_loopback_aliases(engine, host):
    request = urllib.request.Request("http://{0}:{1}/api/flows".format(host, engine["api_port"]),
                                     headers={"Host": "127.0.0.1:{0}".format(engine["api_port"])})
    with pytest.raises(urllib.error.HTTPError) as failure:
        proxied_opener(engine).open(request, timeout=15)
    assert failure.value.code == 502


def test_documented_routes_answer(engine):
    port = engine["api_port"]
    target = start_server()
    proxied_opener(engine).open("http://127.0.0.1:{0}/one".format(target.server_port), timeout=15)
    seq = api_get(port, "/api/flows")[1]["flows"][0]["seq"]
    assert api_get(port, "/api/flows/{0}".format(seq))[0] == 200
    assert api_get(port, "/api/flows/nope")[0] == 404
    assert api_get(port, "/api/flows?limit=0")[0] == 400
    status, waited = api_get(port, "/api/flows/wait?timeout_seconds=1")
    assert status == 200 and waited["timed_out"] is True
    status, cer = api_get(port, "/api/ca.cer")
    assert status == 200 and cer
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("DELETE", "/api/flows")
    response = connection.getresponse()
    assert response.status == 200 and json.loads(response.read())["removed"] >= 1
    connection.close()
    status, call = api_post(port, "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                           "params": {"name": "get_flow", "arguments": {"id": seq}}})
    assert call["result"]["isError"] is True and "not found" in call["result"]["content"][0]["text"]
    target.shutdown()
