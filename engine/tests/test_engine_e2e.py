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
import urllib.request

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
    env.pop("AGENTPROXY_DATA_DIR", None)
    return subprocess.Popen([sys.executable, "-I", "-m", "agentproxy", "--data-dir", data_dir] + list(extra_args),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


@pytest.fixture
def engine(tmp_path):
    data_dir = str(tmp_path / "engine-data")
    os.makedirs(data_dir)
    ports = {"proxy_port": free_port(), "api_port": free_port(), "verify_upstream": False}
    with open(os.path.join(data_dir, "settings.json"), "w") as stream:
        json.dump(ports, stream)
    process = launch(data_dir)

    def ready():
        try:
            status, body = api_get(ports["api_port"], "/api/status", timeout=2)
            return status == 200 and body["proxy"]["running"]
        except OSError:
            return False

    if not wait_until(ready, ENGINE_START_TIMEOUT):
        process.kill()
        log = open(os.path.join(data_dir, "engine.log"), encoding="utf-8").read()
        pytest.fail("engine did not start:\n" + log)
    yield {"process": process, "data_dir": data_dir, **ports}
    if process.poll() is None:
        process.kill()
        process.wait(10)


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

    def call_wait():
        result["response"] = api_post(port, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "wait_for_flow", "arguments": {"path": "/later", "timeout_seconds": 20}}})

    waiter = threading.Thread(target=call_wait)
    waiter.start()
    time.sleep(1.0)
    proxied_opener(engine).open("http://127.0.0.1:{0}/later".format(target.server_port), timeout=15)
    waiter.join(30)
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


def test_monitor_page_served(engine):
    status, body = api_get(engine["api_port"], "/")
    assert status == 200 and b"AgentProxy" in body


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


def test_prompt_hook_endpoint_answers_json(engine):
    status, body = api_post(engine["api_port"], "/claude/hooks/user-prompt-submit", {"session_id": "e2e"})
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
    api_port = free_port()
    with open(os.path.join(data_dir, "settings.json"), "w") as stream:
        json.dump({"proxy_port": free_port(), "api_port": api_port}, stream)
    process = launch(data_dir, ["--parent-pid", str(parent.pid)])

    def up():
        try:
            return api_get(api_port, "/api/status", timeout=2)[0] == 200
        except OSError:
            return False

    assert wait_until(up, ENGINE_START_TIMEOUT)
    parent.kill()
    assert process.wait(30) == 0
