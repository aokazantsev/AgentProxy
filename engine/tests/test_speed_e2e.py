import http.client
import json
import os
import socket
import ssl
import time

import pytest

from conftest import free_port
from test_engine_e2e import (api_get, engine, no_proxy_bypass, proxied_opener, start_engine, start_server,
                             wait_until)
from test_logs import engine_log
from test_rules_e2e import add_rule, api_call, last_flow


def timed(action):
    started = time.monotonic()
    result = action()
    return result, time.monotonic() - started


def test_download_limit_slows_only_the_matching_request_on_a_kept_connection(engine):
    rule_id = add_rule(engine, action="respond", path="/big", body="b" * 25000, download_kbps=200)
    add_rule(engine, action="respond", path="/small", body="s" * 25000)
    target = "http://127.0.0.1:{0}".format(free_port())
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=30)

    def fetch(path):
        connection.request("GET", target + path)
        response = connection.getresponse()
        return response.read()

    body, slow = timed(lambda: fetch("/big"))
    assert body == b"b" * 25000 and slow >= 0.7
    body, fast = timed(lambda: fetch("/small"))
    assert body == b"s" * 25000 and fast < 0.5
    connection.close()
    flow = last_flow(engine, rule=rule_id)
    assert flow["rules"] == [{"id": rule_id, "action": "respond", "limited": "request"}]
    assert wait_until(lambda: "{0} respond limited=request".format(rule_id) in engine_log(engine), 10)


def test_upload_limit_slows_reading_the_request_body(engine):
    target = start_server()
    add_rule(engine, action="delay", path="/upload", upload_kbps=4000)
    url = "http://127.0.0.1:{0}/upload".format(target.server_port)
    payload = b"u" * (2500 * 1000)
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=60)

    def upload():
        connection.request("POST", url, body=payload, headers={"Content-Type": "application/octet-stream"})
        return connection.getresponse()

    response, elapsed = timed(upload)
    assert response.status == 201 and len(json.loads(response.read())["echo"]) == len(payload)
    assert 2.0 <= elapsed < 9
    connection.close()
    target.shutdown()


SLOW_BODY = "".join("{0:07d}\n".format(number) for number in range(25000)).encode()


def start_slow_answer(engine, close):
    rule_id = add_rule(engine, action="respond", path="/stuck", body=SLOW_BODY.decode(), download_kbps=8)
    target = "http://127.0.0.1:{0}/stuck".format(free_port())
    connection = http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=30)
    connection.request("GET", target, headers={"Connection": "close"} if close else {})
    response = connection.getresponse()
    assert response.read(100) == SLOW_BODY[:100]
    return rule_id, connection, response


@pytest.mark.parametrize("close", [False, True])
def test_deleting_the_rule_lifts_the_limit_at_once(engine, close):
    rule_id, connection, response = start_slow_answer(engine, close)
    assert api_call(engine["api_port"], "DELETE", "/api/rules/" + rule_id)[0] == 200
    rest, elapsed = timed(response.read)
    assert rest == SLOW_BODY[100:] and elapsed < 5
    connection.close()


def test_disabling_the_rule_lifts_the_limit_at_once(engine):
    rule_id, connection, response = start_slow_answer(engine, True)
    assert api_call(engine["api_port"], "PATCH", "/api/rules/" + rule_id, {"enabled": False})[0] == 200
    rest, elapsed = timed(response.read)
    assert rest == SLOW_BODY[100:] and elapsed < 5
    connection.close()


def test_new_speed_applies_to_the_answer_on_the_way(engine):
    rule_id, connection, response = start_slow_answer(engine, False)
    assert api_call(engine["api_port"], "PATCH", "/api/rules/" + rule_id, {"download_kbps": 800})[0] == 200
    rest, elapsed = timed(response.read)
    assert rest == SLOW_BODY[100:] and 1.2 <= elapsed < 6
    connection.close()


def test_rule_speed_is_shared_by_parallel_connections(engine):
    add_rule(engine, action="respond", path="/shared", body="s" * 25000, download_kbps=400)
    target = "http://127.0.0.1:{0}/shared".format(free_port())
    connections = [http.client.HTTPConnection("127.0.0.1", engine["proxy_port"], timeout=30) for _ in range(2)]
    started = time.monotonic()
    for connection in connections:
        connection.request("GET", target)
    responses = [connection.getresponse() for connection in connections]
    bodies = [response.read() for response in responses]
    assert bodies == [b"s" * 25000] * 2 and time.monotonic() - started >= 0.8
    for connection in connections:
        connection.close()


def negotiated_protocol(engine, host):
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=15)
    raw.sendall("CONNECT {0}:443 HTTP/1.1\r\nHost: {0}:443\r\n\r\n".format(host).encode())
    assert b" 200 " in raw.recv(4096)
    context = ssl.create_default_context(cafile=cafile)
    context.set_alpn_protocols(["h2", "http/1.1"])
    secure = context.wrap_socket(raw, server_hostname=host)
    protocol = secure.selected_alpn_protocol()
    secure.close()
    return protocol


def test_limited_host_is_offered_http1(engine):
    add_rule(engine, action="respond", host="slow.example.test", body="{}", download_kbps=500)
    add_rule(engine, action="respond", host="fast.example.test", body="{}")
    assert negotiated_protocol(engine, "slow.example.test") == "http/1.1"
    assert negotiated_protocol(engine, "fast.example.test") == "h2"
    answer = api_get(engine["api_port"], "/api/rules")[1]
    assert answer["rules"][0]["download_kbps"] == 500


def h2_get(secure, conn, stream_id, path, host):
    conn.send_headers(stream_id, [(":method", "GET"), (":path", path), (":scheme", "https"), (":authority", host)],
                      end_stream=True)
    secure.sendall(conn.data_to_send())


def h2_read(secure, conn, stream_id):
    import h2.events
    body = b""
    started = time.monotonic()
    while True:
        data = secure.recv(65535)
        assert data
        for event in conn.receive_data(data):
            if isinstance(event, h2.events.DataReceived):
                body += event.data
                conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded) and event.stream_id == stream_id:
                secure.sendall(conn.data_to_send())
                return body, time.monotonic() - started
        secure.sendall(conn.data_to_send())


def test_http2_connection_is_slowed_as_a_whole_without_host(engine):
    import h2.config
    import h2.connection
    host = "h2.example.test"
    slow_id = add_rule(engine, action="delay", url_regex="/slow", download_kbps=200)
    add_rule(engine, action="respond", host=host, body="z" * 25000)
    cafile = os.path.join(engine["data_dir"], "certs", "mitmproxy-ca-cert.pem")
    raw = socket.create_connection(("127.0.0.1", engine["proxy_port"]), timeout=30)
    raw.sendall("CONNECT {0}:443 HTTP/1.1\r\nHost: {0}:443\r\n\r\n".format(host).encode())
    assert b" 200 " in raw.recv(4096)
    context = ssl.create_default_context(cafile=cafile)
    context.set_alpn_protocols(["h2", "http/1.1"])
    secure = context.wrap_socket(raw, server_hostname=host)
    assert secure.selected_alpn_protocol() == "h2"
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    secure.sendall(conn.data_to_send())
    timings = []
    for stream_id, path in ((1, "/fast"), (3, "/slow"), (5, "/fast")):
        h2_get(secure, conn, stream_id, path, host)
        body, elapsed = h2_read(secure, conn, stream_id)
        assert body == b"z" * 25000
        timings.append(elapsed)
    secure.close()
    assert timings[0] < 0.5 and timings[1] >= 0.7 and timings[2] >= 0.7
    flow = last_flow(engine, rule=slow_id)
    assert flow["rules"][0] == {"id": slow_id, "action": "delay", "limited": "connection"}
