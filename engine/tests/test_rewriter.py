import asyncio

import pytest
from mitmproxy.test import tflow

from agentproxy.guard import OWN_PORT_MARK
from agentproxy.recorder import Recorder, build_record
from agentproxy.rewriter import Rewriter, guess_content_type
from agentproxy.rules import NO_REQUEST_STREAM_MARK, NO_RESPONSE_STREAM_MARK, RuleSet


def by_id(rules, rule_id):
    return next(rule for rule in rules.list() if rule["id"] == rule_id)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def rules(tmp_path):
    return RuleSet(str(tmp_path))


def through(rewriter, flow, response=None):
    run(rewriter.requestheaders(flow))
    run(rewriter.request(flow))
    if response is not None and flow.response is None:
        flow.response = response
    if flow.response is not None:
        rewriter.responseheaders(flow)
        run(rewriter.response(flow))
    return flow


def server_response(content=b'{"ok": true}', **headers):
    from mitmproxy import http
    return http.Response.make(200, content, dict({"Content-Type": "application/json"}, **headers))


def test_respond_builds_response_and_turns_streaming_off(rules):
    rules.add({"action": "respond", "path": "/path", "status": 201, "body": "ok",
               "headers": {"content-type": "text/html", "X-A": "1"}, "content_type": "text/csv"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.status_code == 201 and flow.response.content == b"ok"
    assert flow.response.headers.get_all("content-type") == ["text/csv"]
    assert flow.response.headers["X-A"] == "1"
    assert flow.metadata[NO_REQUEST_STREAM_MARK] and flow.metadata[NO_RESPONSE_STREAM_MARK]


def test_recorder_does_not_stream_what_rules_need_whole(rules):
    rules.add({"action": "respond", "path": "/path", "body": "x"})
    flow = tflow.tflow()
    flow.request.headers["content-length"] = "5000"
    recorder = Recorder(store=None, body_limit=10)
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    recorder.requestheaders(flow)
    assert not flow.request.stream
    run(rewriter.request(flow))
    rewriter.responseheaders(flow)
    recorder.responseheaders(flow)
    assert not flow.response.stream
    assert build_record(flow, 10).rules == [{"id": "r1", "action": "respond"}]


def test_own_port_flows_are_left_alone(rules):
    rules.add({"action": "drop", "path": "/"})
    flow = tflow.tflow()
    flow.metadata[OWN_PORT_MARK] = True
    through(Rewriter(rules), flow)
    assert flow.error is None and by_id(rules, "r1")["hits"] == 0


def test_drop_kills_the_flow(rules):
    rules.add({"action": "drop", "path": "/path"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.error is not None and flow.error.msg == "Connection killed." and flow.response is None


@pytest.mark.parametrize("method, status, headers, reason", [
    ("HEAD", 200, {}, "no body"),
    ("GET", 304, {}, "no body"),
    ("GET", 200, {"Content-Type": "text/event-stream"}, "event stream"),
    ("GET", 200, {"Content-Length": str(65 * 1024 * 1024)}, "larger than 64 MB"),
])
def test_body_rewrite_skips_what_cannot_be_buffered(rules, method, status, headers, reason):
    rules.add({"action": "rewrite", "path": "/path", "response_body": "new"})
    flow = tflow.tflow()
    flow.request.method = method
    response = server_response()
    response.status_code = status
    response.headers.update(headers)
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = response
    rewriter.responseheaders(flow)
    assert reason in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert NO_RESPONSE_STREAM_MARK not in flow.metadata
    run(rewriter.response(flow))
    assert flow.response.content != b"new"


def test_body_rewrite_skips_streamed_and_undecodable_bodies(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "ok", "with": "no"}]})
    rewriter = Rewriter(rules)
    streamed = tflow.tflow()
    run(rewriter.requestheaders(streamed))
    streamed.response = server_response()
    rewriter.responseheaders(streamed)
    streamed.response.stream = True
    run(rewriter.response(streamed))
    assert "not recorded" in streamed.metadata["agentproxy_rules"][0]["skipped"]
    broken = tflow.tflow()
    run(rewriter.requestheaders(broken))
    broken.response = server_response(**{"Content-Encoding": "gzip"})
    broken.response.raw_content = b"\x00\x01 not gzip"
    rewriter.responseheaders(broken)
    run(rewriter.response(broken))
    assert "could not be unpacked" in broken.metadata["agentproxy_rules"][0]["skipped"]
    assert broken.response.raw_content == b"\x00\x01 not gzip"


def test_body_rewrite_keeps_compression_and_length(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "true", "with": "false"}]})
    response = server_response()
    response.encode("gzip")
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert flow.response.headers["content-encoding"] == "gzip"
    assert flow.response.content == b'{"ok": false}'
    assert flow.response.headers["content-length"] == str(len(flow.response.raw_content))
    assert flow.metadata["agentproxy_rules"][0]["replaced"] == 1


def test_rewrite_status_sets_reason_and_headers(rules):
    rules.add({"action": "rewrite", "path": "/path", "status": 503,
               "response_headers": {"Content-Type": None, "X-New": "1"}})
    flow = through(Rewriter(rules), tflow.tflow(), server_response())
    assert flow.response.status_code == 503 and flow.response.reason == "Service Unavailable"
    assert "content-type" not in flow.response.headers and flow.response.headers["X-New"] == "1"


def test_rewrites_before_respond_apply_to_the_mock(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_headers": {"X-Trace": "1"},
               "replace": [{"find": "a", "with": "b"}]})
    rules.add({"action": "respond", "path": "/path", "body": "aaa"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.content == b"bbb" and flow.response.headers["X-Trace"] == "1"


@pytest.mark.parametrize("content, path, expected", [
    (b'{"a": 1}', "mock.txt", "application/json; charset=utf-8"),
    (b"<html></html>", "page.html", "text/html; charset=utf-8"),
    (b"plain", None, "text/plain; charset=utf-8"),
    (b"\xcf\xf0\xe8\xe2\xe5\xf2", None, "application/octet-stream"),
    (b"\xcf\xf0\xe8\xe2\xe5\xf2", "mock.txt", "text/plain"),
    (b"", None, None),
])
def test_guess_content_type(content, path, expected):
    assert guess_content_type(content, path) == expected


def test_response_body_replaces_whole_body_and_replace_applies_after(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_body": "aXa",
               "replace": [{"find": "X", "with": "YY"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b'{"long": "original body"}'))
    assert flow.response.content == b"aYYa"
    assert flow.response.headers["content-length"] == "4"
    assert flow.metadata["agentproxy_rules"][0]["replaced"] == 1


def test_body_without_length_is_buffered_and_edited(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "ok", "with": "fine"}]})
    flow = tflow.tflow()
    recorder = Recorder(store=None, body_limit=10)
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response()
    del flow.response.headers["content-length"]
    flow.response.headers["transfer-encoding"] = "chunked"
    rewriter.responseheaders(flow)
    recorder.responseheaders(flow)
    assert not flow.response.stream
    run(rewriter.response(flow))
    assert flow.response.raw_content == b'{"fine": true}'
    assert "content-length" not in flow.response.headers
    assert flow.metadata["agentproxy_rules"][0]["replaced"] == 1


def test_unpacked_size_is_limited(rules, monkeypatch):
    import gzip
    monkeypatch.setattr("agentproxy.rewriter.MAX_FILE_BYTES", 1000)
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    packed = gzip.compress(b"a" * 100000)
    response = server_response()
    response.headers["content-encoding"] = "gzip"
    response.raw_content = packed
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert "unpacked" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == packed


@pytest.mark.parametrize("content_type, body, expected", [
    ("text/plain", "Ошибка сервера".encode("utf-8"), "Успех сервера".encode("utf-8")),
    ("text/plain; charset=windows-1251", "Ошибка".encode("cp1251"), "Успех".encode("cp1251")),
])
def test_replace_uses_charset_or_utf8(rules, content_type, body, expected):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "Ошибка", "with": "Успех"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(body, **{"Content-Type": content_type}))
    assert flow.response.raw_content == expected


def test_binary_body_is_not_treated_as_text(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(b"\xff\xfe\x00a", **{"Content-Type": "application/octet-stream"}))
    assert "not utf-8 text" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == b"\xff\xfe\x00a"


@pytest.mark.parametrize("framing", [{"content-length": "9"}, {"transfer-encoding": "chunked"},
                                     {"content-length": str(65 * 1024 * 1024)}])
def test_rewrite_to_status_without_body_drops_body_and_framing(rules, framing):
    rules.add({"action": "rewrite", "path": "/path", "status": 204})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response(b"something")
    del flow.response.headers["content-length"]
    flow.response.headers.update(framing)
    rewriter.responseheaders(flow)
    assert flow.response.status_code == 204
    assert "content-length" not in flow.response.headers and "transfer-encoding" not in flow.response.headers
    editor = flow.response.stream
    assert editor(b"some") == [] and editor(b"x" * 100) == [] and editor(b"") == []
    run(rewriter.response(flow))
    assert flow.response.raw_content == b""


@pytest.mark.parametrize("length", [None, "5000"])
def test_rewrite_from_not_modified_gives_length(rules, length):
    from mitmproxy import http
    rules.add({"action": "rewrite", "path": "/path", "status": 200})
    response = http.Response.make(304, b"")
    del response.headers["content-length"]
    if length:
        response.headers["content-length"] = length
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert flow.response.status_code == 200 and flow.response.headers["content-length"] == "0"


def test_empty_response_body_rule_sets_zero_length(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_body": ""})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"abc"))
    assert flow.response.raw_content == b"" and flow.response.headers["content-length"] == "0"


def test_respond_keeps_content_type_given_in_headers(rules):
    rules.add({"action": "respond", "path": "/path", "body": "<a/>",
               "headers": {"Content-Type": "application/xml; charset=windows-1251"}})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.headers.get_all("content-type") == ["application/xml; charset=windows-1251"]


def test_respond_to_head_keeps_the_length_of_get(rules):
    rules.add({"action": "respond", "path": "/path", "body": "abcd"})
    flow = tflow.tflow()
    flow.request.method = "HEAD"
    through(Rewriter(rules), flow)
    assert flow.response.raw_content == b"" and flow.response.headers["content-length"] == "4"


def test_respond_refuses_uploads_it_cannot_buffer(rules):
    rules.add({"action": "respond", "path": "/path", "body": "ok"})
    flow = tflow.tflow()
    flow.request.headers["content-length"] = str(65 * 1024 * 1024)
    run(Rewriter(rules).requestheaders(flow))
    assert flow.error is not None
    assert "cannot buffer" in flow.metadata["agentproxy_rules"][0]["error"]


@pytest.mark.parametrize("content_type", ["application/json; charset=punycode", "text/plain; charset=idna",
                                          "text/plain; charset=nonsense"])
def test_only_text_charsets_are_decoded(rules, content_type):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"x-aaaa", **{"Content-Type": content_type}))
    assert "not" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == b"x-aaaa"


def test_json_keeps_its_declared_charset(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "Ошибка", "with": "Успех"}]})
    body = '{"text": "Ошибка"}'.encode("cp1251")
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(body, **{"Content-Type": "application/json; charset=windows-1251"}))
    assert flow.response.raw_content == '{"text": "Успех"}'.encode("cp1251")


def test_quoted_charset_is_understood(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "Ошибка", "with": "Успех"}]})
    body = "Ошибка".encode("cp1251")
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(body, **{"Content-Type": "text/plain; charset='windows-1251'"}))
    assert flow.response.raw_content == "Успех".encode("cp1251")


def test_replace_growing_over_the_limit_is_skipped(rules, monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.MAX_FILE_BYTES", 10)
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "aaaa"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaaaa", **{"Content-Type": "text/plain"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert "after replace" in entry["skipped"] and "replaced" not in entry
    assert flow.response.raw_content == b"aaaaa"


def test_new_body_outside_the_charset_is_skipped(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "\U0001F600"}]})
    body = "a".encode("cp1251")
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(body, **{"Content-Type": "text/plain; charset=windows-1251"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert "cannot be encoded" in entry["skipped"] and "replaced" not in entry
    assert flow.response.raw_content == body


def test_http2_body_without_length_is_edited_and_gets_length(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "ok", "with": "fine"}]})
    flow = tflow.tflow()
    run(Rewriter(rules).requestheaders(flow))
    flow.response = server_response()
    flow.response.http_version = "HTTP/2.0"
    del flow.response.headers["content-length"]
    rewriter = Rewriter(rules)
    rewriter.responseheaders(flow)
    assert not flow.response.stream
    run(rewriter.response(flow))
    assert flow.response.raw_content == b'{"fine": true}'


def test_discarder_swallows_any_amount(rules):
    rules.add({"action": "rewrite", "path": "/path", "status": 304})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response()
    del flow.response.headers["content-length"]
    rewriter.responseheaders(flow)
    editor = flow.response.stream
    assert editor(b"123456789") == [] and editor(b"more") == [] and editor(b"") == []


def test_respond_to_head_sends_no_body(rules):
    rules.add({"action": "respond", "path": "/path", "body": "abc"})
    flow = tflow.tflow()
    flow.request.method = "HEAD"
    through(Rewriter(rules), flow)
    assert flow.response.raw_content == b""


def test_drop_happens_before_the_body_is_read(rules):
    rules.add({"action": "drop", "path": "/path"})
    flow = tflow.tflow()
    run(Rewriter(rules).requestheaders(flow))
    assert flow.error is not None and flow.error.msg == "Connection killed."


def test_delays_of_a_chain_add_up_and_are_capped(rules, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("agentproxy.rewriter.asyncio.sleep", fake_sleep)
    rules.add({"action": "delay", "path": "/path", "delay_ms": 10})
    rules.add({"action": "rewrite", "path": "/path", "delay_ms": 5, "request_headers": {"X-A": "1"}})
    flow = through(Rewriter(rules), tflow.tflow(), server_response())
    assert slept == [0.015] and flow.error is None and flow.response.status_code == 200
    assert flow.request.headers["X-A"] == "1"
    rules.add({"action": "delay", "path": "/path", "delay_ms": 300000})
    del slept[:]
    through(Rewriter(rules), tflow.tflow(), server_response())
    assert sum(slept) == 300.0 and max(slept) == 0.5


def fake_sleep_log(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("agentproxy.rewriter.asyncio.sleep", fake_sleep)
    return slept


@pytest.mark.parametrize("action", [{"action": "delay"}, {"action": "respond", "body": "x"},
                                    {"action": "rewrite", "status": 201}])
def test_delay_waits_after_the_request_body(rules, monkeypatch, action):
    slept = fake_sleep_log(monkeypatch)
    rules.add(dict({"path": "/path", "delay_ms": 10}, **action))
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    assert slept == []
    run(rewriter.request(flow))
    assert slept == [0.01]


def test_drop_of_a_small_upload_waits_for_its_body(rules, monkeypatch):
    slept = fake_sleep_log(monkeypatch)
    rules.add({"action": "drop", "path": "/path", "delay_ms": 10})
    flow = tflow.tflow()
    flow.request.headers["content-length"] = "5"
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    assert slept == [] and flow.error is None
    run(rewriter.request(flow))
    assert slept == [0.01] and flow.error.msg == "Connection killed."


@pytest.mark.parametrize("framing", [{"transfer-encoding": "chunked"}, {"content-length": str(64 * 1024 * 1024 + 1)}])
def test_drop_of_a_large_upload_does_not_wait(rules, monkeypatch, framing):
    slept = fake_sleep_log(monkeypatch)
    rules.add({"action": "drop", "path": "/path", "delay_ms": 10})
    flow = tflow.tflow()
    flow.request.headers.update(framing)
    run(Rewriter(rules).requestheaders(flow))
    assert slept == [] and flow.error.msg == "Connection killed."
    assert "has no length or is larger than 64 MB" in flow.metadata["agentproxy_rules"][0]["skipped"]


def test_drop_without_body_waits_before_reading_anything(rules, monkeypatch):
    slept = fake_sleep_log(monkeypatch)
    rules.add({"action": "drop", "path": "/path", "delay_ms": 10})
    flow = tflow.tflow()
    flow.request.headers.pop("content-length", None)
    run(Rewriter(rules).requestheaders(flow))
    assert slept == [0.01] and flow.error.msg == "Connection killed."


def test_connect_is_answered_locally_only_for_hosts_a_rule_may_answer(rules):
    rewriter = Rewriter(rules)
    plain = tflow.tflow()
    plain.request.method = "CONNECT"
    rewriter.http_connect(plain)
    assert plain.response is None
    rules.add({"action": "rewrite", "host": "address", "status": 500})
    rewriter.http_connect(plain)
    assert plain.response is None
    rules.add({"action": "respond", "host": "ADDRESS", "body": "x"})
    rewriter.http_connect(plain)
    assert plain.response.status_code == 200 and not plain.response.headers
    other = tflow.tflow()
    other.request.host = "elsewhere.example"
    rewriter.http_connect(other)
    assert other.response is None


def test_server_tls_is_skipped_only_for_connections_answered_locally(rules):
    class Data:
        pass

    def hello(client):
        data = Data()
        data.establish_server_tls_first = True
        data.context = Data()
        data.context.client = client
        return data

    rules.add({"action": "respond", "host": "address", "body": "x"})
    rewriter = Rewriter(rules)
    plain = tflow.tflow()
    plain.request.host = "elsewhere.example"
    rewriter.http_connect(plain)
    untouched = hello(plain.client_conn)
    rewriter.tls_clienthello(untouched)
    assert untouched.establish_server_tls_first is True
    mocked = tflow.tflow()
    rewriter.http_connect(mocked)
    lazy = hello(mocked.client_conn)
    rewriter.tls_clienthello(lazy)
    assert lazy.establish_server_tls_first is False


def test_response_streamed_by_mitmproxy_is_skipped(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_body": "x"})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response()
    flow.response.http_version = "HTTP/2.0"
    del flow.response.headers["content-length"]
    rewriter.responseheaders(flow)
    flow.response.stream = True
    run(rewriter.response(flow))
    assert "not recorded" in flow.metadata["agentproxy_rules"][0]["skipped"]


def test_engine_streams_bodies_beyond_the_editing_limit(tmp_path):
    from agentproxy.engine import create_master
    from agentproxy.rules import MAX_FILE_BYTES
    from agentproxy.settings import Settings

    async def build():
        return create_master(Settings(), str(tmp_path)).options.stream_large_bodies

    assert run(build()) == str(MAX_FILE_BYTES)


def test_later_response_body_takes_over_earlier_replace(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    rules.add({"action": "rewrite", "path": "/path", "response_body": "zzz"})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaa", **{"Content-Type": "text/plain"}))
    first, second = flow.metadata["agentproxy_rules"]
    assert flow.response.raw_content == b"zzz"
    assert "replaced" not in first and "r2" in first["skipped"] and "skipped" not in second


def test_respond_clears_last_error_when_the_file_is_back(rules, tmp_path):
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock)})
    mock.unlink()
    through(Rewriter(rules), tflow.tflow())
    assert "last_error" in by_id(rules, "r1")
    mock.write_text("{}")
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.status_code == 200 and "last_error" not in by_id(rules, "r1")


def test_not_modified_with_chunked_framing_gets_plain_length(rules):
    from mitmproxy import http
    rules.add({"action": "rewrite", "path": "/path", "status": 200})
    response = http.Response.make(304, b"")
    del response.headers["content-length"]
    response.headers["transfer-encoding"] = "chunked"
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert "transfer-encoding" not in flow.response.headers and flow.response.headers["content-length"] == "0"


def test_respond_refuses_upload_that_mitmproxy_started_streaming(rules):
    rules.add({"action": "respond", "path": "/path", "body": "ok"})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.request.stream = True
    run(rewriter.request(flow))
    assert flow.response is None and flow.error is not None
    assert "went on to the server" in flow.metadata["agentproxy_rules"][0]["error"]


def test_growth_is_estimated_before_each_replacement(rules, monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.MAX_FILE_BYTES", 10)
    rules.add({"action": "rewrite", "path": "/path",
               "replace": [{"find": "a", "with": "aaaa"}, {"find": "aaaa", "with": "a"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaaaa", **{"Content-Type": "text/plain"}))
    assert "after replace" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == b"aaaaa"


def test_size_is_checked_in_bytes_after_encoding(rules, monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.MAX_FILE_BYTES", 10)
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "Ж"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaaaaa", **{"Content-Type": "text/plain"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert "after replace" in entry["skipped"] and "replaced" not in entry
    assert flow.response.raw_content == b"aaaaaa"


def test_nothing_replaced_keeps_the_original_bytes(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "absent", "with": "x"}]})
    body = b"\xfe\xff" + "hello".encode("utf-16-be")
    flow = through(Rewriter(rules), tflow.tflow(), server_response(body, **{"Content-Type": "text/plain; charset=utf-16"}))
    assert flow.response.raw_content == body
    assert flow.metadata["agentproxy_rules"][0]["replaced"] == 0


def test_x_gzip_body_is_edited(rules):
    import gzip
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    response = server_response(**{"Content-Type": "text/plain"})
    response.headers["content-encoding"] = "x-gzip"
    response.raw_content = gzip.compress(b"aaa")
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert gzip.decompress(flow.response.raw_content) == b"bbb"


@pytest.mark.parametrize("charset", ["punycode", "idna", "unicode-escape", "raw-unicode-escape"])
def test_response_body_is_not_written_in_a_non_text_charset(rules, charset):
    rules.add({"action": "rewrite", "path": "/path", "response_body": "привет"})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(b"old", **{"Content-Type": "text/plain; charset=" + charset}))
    assert "cannot be encoded" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == b"old"


def test_takeover_mark_only_when_the_new_body_applies(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    rules.add({"action": "rewrite", "path": "/path", "response_body": "привет"})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(b"aaa", **{"Content-Type": "text/plain; charset=ascii"}))
    first, second = flow.metadata["agentproxy_rules"]
    assert flow.response.raw_content == b"aaa"
    assert "replaced by" not in first.get("skipped", "") and "cannot be encoded" in second["skipped"]


def test_content_type_is_guessed_off_the_event_loop(rules, monkeypatch):
    calls = []
    real = asyncio.to_thread

    async def recording(function, *args):
        calls.append(function.__name__)
        return await real(function, *args)

    monkeypatch.setattr("agentproxy.rewriter.asyncio.to_thread", recording)
    rules.add({"action": "respond", "path": "/path", "body": "{}"})
    through(Rewriter(rules), tflow.tflow())
    assert "guess_content_type" in calls


def test_large_json_is_recognised_without_parsing(monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.JSON_SNIFF_BYTES", 4)
    assert guess_content_type(b'{"a": 1,}') == "application/json; charset=utf-8"
    assert guess_content_type(b"plain text") == "text/plain; charset=utf-8"
    assert guess_content_type(b"[2026-10-17] log line", "app.txt") == "text/plain; charset=utf-8"


def test_response_body_may_use_any_text_charset(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_body": "テスト"})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(b"old", **{"Content-Type": "text/plain; charset=shift_jis"}))
    assert flow.response.raw_content == "テスト".encode("shift_jis")


@pytest.mark.parametrize("charset, encoded", [("utf-16", "aaa".encode("utf-16-be")),
                                              ("utf-32", "aaa".encode("utf-32-be"))])
def test_utf16_without_byte_order_mark_is_not_guessed(rules, charset, encoded):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(encoded, **{"Content-Type": "text/plain; charset=" + charset}))
    assert "no byte order mark" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == encoded


def test_parallel_edits_are_limited(rules, monkeypatch):
    active = []
    peak = []

    async def counting(function, *args):
        active.append(1)
        peak.append(len(active))
        await asyncio.sleep(0.05)
        active.pop()
        return function(*args)

    monkeypatch.setattr("agentproxy.rewriter.asyncio.to_thread", counting)
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "ok", "with": "fine"}]})

    async def scenario():
        rewriter = Rewriter(rules)
        flows = []
        for _ in range(4):
            flow = tflow.tflow()
            await rewriter.requestheaders(flow)
            flow.response = server_response()
            rewriter.responseheaders(flow)
            flows.append(flow)
        await asyncio.gather(*(rewriter.response(flow) for flow in flows))
        return flows

    flows = run(scenario())
    assert max(peak) == 2
    assert all(flow.response.raw_content == b'{"fine": true}' for flow in flows)


def test_lazy_mark_is_dropped_when_the_client_leaves(rules):
    rules.add({"action": "respond", "host": "address", "body": "x"})
    rewriter = Rewriter(rules)
    flow = tflow.tflow()
    rewriter.http_connect(flow)
    assert flow.client_conn.id in rewriter.lazy_clients
    rewriter.client_disconnected(flow.client_conn)
    assert rewriter.lazy_clients == set()


def test_body_file_is_sent_exactly_as_stored(rules, tmp_path):
    import gzip
    mock = tmp_path / "mock.json.gz"
    mock.write_bytes(gzip.compress(b'{"a": 1}'))
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock),
               "headers": {"Content-Encoding": "gzip"}, "content_type": "application/json"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == mock.read_bytes()
    assert flow.response.headers["content-length"] == str(len(mock.read_bytes()))
    assert flow.response.headers["content-encoding"] == "gzip"


@pytest.mark.parametrize("status", [204, 304])
def test_respond_without_body_has_no_length(rules, status):
    rules.add({"action": "respond", "path": "/path", "status": status})
    flow = through(Rewriter(rules), tflow.tflow())
    assert "content-length" not in flow.response.headers


def test_broken_body_file_keeps_its_502_under_a_rewrite(rules, tmp_path):
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    rules.add({"action": "rewrite", "path": "/path", "status": 200, "response_headers": {"X-A": "1"}})
    rules.add({"action": "rewrite", "path": "/path", "response_body": "mocked"})
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock)})
    mock.unlink()
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.status_code == 502 and "X-A" not in flow.response.headers
    assert b"unreadable" in flow.response.raw_content
    assert all("could not read" in entry["skipped"] for entry in flow.metadata["agentproxy_rules"][:2])
    assert str(tmp_path) not in flow.response.raw_content.decode("utf-8")
    assert str(tmp_path) in flow.metadata["agentproxy_rules"][2]["error"]


def test_event_stream_is_recognised_before_headers_change(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_headers": {"Content-Type": "text/plain"},
               "replace": [{"find": "a", "with": "b"}]})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response(**{"Content-Type": "text/event-stream"})
    rewriter.responseheaders(flow)
    assert "event stream" in flow.metadata["agentproxy_rules"][0]["skipped"]


def test_large_text_file_with_json_inside_stays_json(monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.JSON_SNIFF_BYTES", 4)
    assert guess_content_type(b'{"a": 1}', "items.txt") == "application/json; charset=utf-8"
    assert guess_content_type(b"<html></html>", "page.html") == "text/html; charset=utf-8"


@pytest.mark.parametrize("status", [204, 304])
def test_respond_without_body_sends_nothing_even_with_encoding(rules, status):
    rules.add({"action": "respond", "path": "/path", "status": status, "headers": {"Content-Encoding": "gzip"}})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == b"" and "content-length" not in flow.response.headers
    assert flow.response.headers["content-encoding"] == "gzip"


def test_unknown_encoding_of_body_file_is_kept(rules, tmp_path):
    mock = tmp_path / "mock.bin"
    mock.write_bytes(b"\x1f\x8b raw")
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock), "headers": {"Content-Encoding": "compress"}})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.headers["content-encoding"] == "compress"
    assert flow.response.raw_content == b"\x1f\x8b raw"


def test_file_types_do_not_depend_on_the_registry():
    assert guess_content_type(b"a,b", "rates.csv") == "text/csv; charset=utf-8"
    assert guess_content_type("Привет".encode("utf-8"), "note.txt") == "text/plain; charset=utf-8"


def test_body_file_reads_are_limited(rules, tmp_path, monkeypatch):
    active = []
    peak = []

    async def counting(function, *args):
        active.append(1)
        peak.append(len(active))
        await asyncio.sleep(0.05)
        active.pop()
        return function(*args)

    monkeypatch.setattr("agentproxy.rewriter.asyncio.to_thread", counting)
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock), "content_type": "application/json"})

    async def scenario():
        rewriter = Rewriter(rules)
        flows = [tflow.tflow() for _ in range(4)]
        for flow in flows:
            await rewriter.requestheaders(flow)
        await asyncio.gather(*(rewriter.request(flow) for flow in flows))
        return flows

    flows = run(scenario())
    assert max(peak) == 2 and all(flow.response.status_code == 200 for flow in flows)


def test_empty_utf16_body_is_not_reported_as_missing_mark(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"", **{"Content-Type": "text/plain; charset=utf-16"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert "skipped" not in entry and entry["replaced"] == 0


def packed(data, encoding):
    from mitmproxy.net import encoding as content_codings
    return content_codings.custom_encode[encoding](data)


def unpacked(data, encoding):
    from mitmproxy.net import encoding as content_codings
    return content_codings.decode(data, {"x-gzip": "gzip"}.get(encoding, encoding))


@pytest.mark.parametrize("encoding", ["gzip", "br", "zstd", "deflate", "identity"])
@pytest.mark.parametrize("change", [{"replace": [{"find": "a", "with": "b"}]}, {"response_body": "bbb"}])
def test_edited_body_is_packed_in_its_encoding(rules, encoding, change):
    rules.add(dict({"action": "rewrite", "path": "/path"}, **change))
    response = server_response(**{"Content-Type": "text/plain", "Content-Encoding": encoding})
    response.raw_content = b"aaa" if encoding == "identity" else packed(b"aaa", encoding)
    flow = through(Rewriter(rules), tflow.tflow(), response)
    content = flow.response.raw_content
    assert (content if encoding == "identity" else unpacked(content, encoding)) == b"bbb"
    assert flow.response.headers["content-length"] == str(len(content))


@pytest.mark.parametrize("encoding", ["compress", "gzip, gzip"])
@pytest.mark.parametrize("change", [{"replace": [{"find": "a", "with": "b"}]}, {"response_body": "bbb"}])
def test_body_in_unknown_encoding_is_left_alone(rules, encoding, change):
    rules.add(dict({"action": "rewrite", "path": "/path"}, **change))
    response = server_response(**{"Content-Type": "text/plain"})
    response.headers["content-encoding"] = encoding
    response.raw_content = b"\x1f\x8b packed"
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert flow.response.raw_content == b"\x1f\x8b packed"
    assert "skipped" in flow.metadata["agentproxy_rules"][0]


def test_connect_to_own_port_is_not_answered(rules):
    rules.add({"action": "respond", "host": "address", "body": "x"})
    flow = tflow.tflow()
    flow.request.method = "CONNECT"
    flow.metadata[OWN_PORT_MARK] = True
    rewriter = Rewriter(rules)
    rewriter.http_connect(flow)
    assert flow.response is None and flow.client_conn.id not in rewriter.lazy_clients


def test_head_rewritten_to_no_content_has_no_length(rules):
    rules.add({"action": "rewrite", "path": "/path", "status": 204})
    flow = tflow.tflow()
    flow.request.method = "HEAD"
    response = server_response(b"")
    response.headers["content-length"] = "6"
    flow = through(Rewriter(rules), flow, response)
    assert flow.response.status_code == 204 and "content-length" not in flow.response.headers


def test_odd_charset_name_skips_the_edit(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(b"aaa", **{"Content-Type": "text/plain; charset=utf\x00-8"}))
    assert flow.response.raw_content == b"aaa" and "skipped" in flow.metadata["agentproxy_rules"][0]


def test_respond_body_uses_the_declared_charset(rules):
    rules.add({"action": "respond", "path": "/path", "body": "привет",
               "content_type": "text/plain; charset=windows-1251"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == "привет".encode("cp1251")
    assert flow.response.headers["content-length"] == str(len("привет"))


def test_respond_body_outside_its_charset_goes_as_utf8(rules):
    rules.add({"action": "respond", "path": "/path", "body": "привет", "content_type": "text/plain; charset=ascii"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == "привет".encode("utf-8")
    assert "sent as utf-8" in flow.metadata["agentproxy_rules"][0]["error"]


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", "br"])
def test_respond_body_is_packed_by_its_encoding(rules, encoding):
    rules.add({"action": "respond", "path": "/path", "body": "hello", "headers": {"Content-Encoding": encoding}})
    flow = through(Rewriter(rules), tflow.tflow())
    assert unpacked(flow.response.raw_content, encoding) == b"hello"
    assert flow.response.headers["content-encoding"] == encoding
    assert flow.response.headers["content-length"] == str(len(flow.response.raw_content))


@pytest.mark.parametrize("encoding", ["utf-8", "gzip, br", "compress"])
def test_respond_body_in_unknown_encoding_goes_unpacked(rules, encoding):
    rules.add({"action": "respond", "path": "/path", "body": "hello", "headers": {"Content-Encoding": encoding}})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == b"hello" and "content-encoding" not in flow.response.headers
    assert "unsupported content-encoding" in flow.metadata["agentproxy_rules"][0]["error"]


def test_delay_ends_when_the_client_leaves(rules, monkeypatch):
    from mitmproxy.connection import ConnectionState
    slept = []
    flow = tflow.tflow()

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) == 3:
            flow.client_conn.state = ConnectionState.CLOSED

    monkeypatch.setattr("agentproxy.rewriter.asyncio.sleep", fake_sleep)
    rules.add({"action": "respond", "path": "/path", "body": "x", "delay_ms": 60000})
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    run(rewriter.request(flow))
    assert slept == [0.5, 0.5, 0.5]


def test_respond_body_in_a_charset_without_encoder_goes_as_utf8(rules):
    rules.add({"action": "respond", "path": "/path", "body": "x", "content_type": "text/plain; charset=undefined"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == b"x" and "sent as utf-8" in flow.metadata["agentproxy_rules"][0]["error"]


def test_empty_respond_body_needs_no_charset(rules):
    rules.add({"action": "respond", "path": "/path", "body": "", "content_type": "text/plain; charset=nonexistent"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == b"" and "error" not in flow.metadata["agentproxy_rules"][0]


def test_body_file_type_from_headers_is_not_guessed(rules, tmp_path, monkeypatch):
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    guessed = []
    monkeypatch.setattr("agentproxy.rewriter.guess_content_type", lambda *args: guessed.append(args))
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock), "headers": {"Content-Type": "text/plain"}})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.headers["content-type"] == "text/plain" and guessed == []


def test_json_with_byte_order_mark_is_recognised():
    assert guess_content_type(b'\xef\xbb\xbf{"a": 1}', "mock.txt") == "application/json; charset=utf-8"


def test_streamed_upload_under_respond_says_it_reached_the_server(rules):
    rules.add({"action": "respond", "path": "/path", "body": "x"})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.request.stream = True
    run(rewriter.request(flow))
    assert "went on to the server" in flow.metadata["agentproxy_rules"][0]["error"]


def test_chain_of_two_replace_rules_applies_both(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "c", "with": "d"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"ac", **{"Content-Type": "text/plain"}))
    assert flow.response.raw_content == b"bd"


def test_http2_without_length_gets_length_even_when_nothing_replaced(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "absent", "with": "x"}]})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response()
    flow.response.http_version = "HTTP/2.0"
    del flow.response.headers["content-length"]
    rewriter.responseheaders(flow)
    run(rewriter.response(flow))
    assert flow.response.headers["content-length"] == str(len(flow.response.raw_content))


def test_drop_with_delay_keeps_large_upload_from_streaming(rules):
    rules.add({"action": "drop", "path": "/path", "delay_ms": 10})
    flow = tflow.tflow()
    flow.request.headers["content-length"] = str(10 * 1024 * 1024)
    run(Rewriter(rules).requestheaders(flow))
    Recorder(store=None, body_limit=2 * 1024 * 1024).requestheaders(flow)
    assert flow.metadata[NO_REQUEST_STREAM_MARK] and not flow.request.stream


def test_body_file_grown_past_limit_is_502(rules, tmp_path, monkeypatch):
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock)})
    monkeypatch.setattr("agentproxy.rewriter.MAX_FILE_BYTES", 10)
    mock.write_text("[" + "1," * 20 + "1]")
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.status_code == 502 and "larger than" in flow.metadata["agentproxy_rules"][0]["error"]


def test_empty_packed_body_counts_as_empty(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    response = server_response(b"", **{"Content-Type": "text/plain"})
    response.headers["content-encoding"] = "gzip"
    response.raw_content = b""
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert flow.metadata["agentproxy_rules"][0]["replaced"] == 0


def test_broken_gzip_names_the_failing_module(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    response = server_response(**{"Content-Type": "text/plain"})
    response.headers["content-encoding"] = "gzip"
    response.raw_content = b"\x1f\x8b broken"
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert "could not be unpacked (zlib.error)" in flow.metadata["agentproxy_rules"][0]["skipped"]


def test_large_json_with_byte_order_mark_is_recognised(monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.JSON_SNIFF_BYTES", 4)
    assert guess_content_type(b'\xef\xbb\xbf[1, 2, 3]', "mock.txt") == "application/json; charset=utf-8"


def test_refused_upload_with_length_did_not_reach_the_server(rules):
    rules.add({"action": "respond", "path": "/path", "body": "x"})
    flow = tflow.tflow()
    flow.request.headers["content-length"] = str(64 * 1024 * 1024 + 1)
    flow.request.stream = True
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    assert "cannot buffer" in flow.metadata["agentproxy_rules"][0]["error"]


def test_host_condition_leaves_other_hosts_alone(rules):
    rules.add({"action": "respond", "host": "api.example.com", "body": "mock"})
    flow = tflow.tflow()
    flow.request.host = "cdn.example.com"
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    run(rewriter.request(flow))
    assert flow.response is None and by_id(rules, "r1")["hits"] == 0


def test_respond_body_is_not_written_in_a_slow_charset(rules):
    rules.add({"action": "respond", "path": "/path", "body": "привет", "content_type": "text/plain; charset=punycode"})
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.raw_content == "привет".encode("utf-8")
    assert "sent as utf-8" in flow.metadata["agentproxy_rules"][0]["error"]


def test_drop_of_a_chunked_upload_without_length_does_not_wait(rules, monkeypatch):
    slept = fake_sleep_log(monkeypatch)
    rules.add({"action": "drop", "path": "/path", "delay_ms": 10})
    flow = tflow.tflow()
    flow.request.headers.pop("content-length", None)
    flow.request.headers["transfer-encoding"] = "chunked"
    run(Rewriter(rules).requestheaders(flow))
    assert slept == [] and flow.error.msg == "Connection killed."
    assert "has no length" in flow.metadata["agentproxy_rules"][0]["skipped"]


def test_rule_turning_response_into_event_stream_leaves_body_alone(rules):
    rules.add({"action": "rewrite", "path": "/path", "response_headers": {"Content-Type": "text/event-stream"}})
    rules.add({"action": "rewrite", "path": "/path", "response_body": "new"})
    flow = tflow.tflow()
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    flow.response = server_response()
    rewriter.responseheaders(flow)
    run(rewriter.response(flow))
    assert flow.response.content == b'{"ok": true}'
    assert "event stream" in flow.metadata["agentproxy_rules"][1]["skipped"]


def test_large_file_type_comes_from_its_extension(monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.JSON_SNIFF_BYTES", 4)
    assert guess_content_type(b"[1, 2, 3]", "mock.json") == "application/json"


def test_large_json_shaped_file_that_is_not_utf8_is_binary(monkeypatch):
    monkeypatch.setattr("agentproxy.rewriter.JSON_SNIFF_BYTES", 4)
    assert guess_content_type(b"[\xff\xfe\xfd]", "mock.bin.unknownext") == "application/octet-stream"


def test_respond_buffers_upload_of_exactly_64_mb(rules):
    rules.add({"action": "respond", "path": "/path", "body": "ok"})
    flow = tflow.tflow()
    flow.request.headers["content-length"] = str(64 * 1024 * 1024)
    run(Rewriter(rules).requestheaders(flow))
    assert flow.error is None and "error" not in flow.metadata["agentproxy_rules"][0]


def test_replacements_that_found_nothing_are_named(rules):
    rules.add({"action": "rewrite", "path": "/path",
               "replace": [{"find": "a", "with": "b"}, {"find": "absent", "with": "x"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaa", **{"Content-Type": "text/plain"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert entry["replaced"] == 3 and entry["not_found"] == ["absent"]
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "b", "with": "c"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaa", **{"Content-Type": "text/plain"}))
    assert "not_found" not in flow.metadata["agentproxy_rules"][1]


def test_long_find_that_was_not_found_is_named_by_its_start(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "x" * 500, "with": "y"}]})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"aaa", **{"Content-Type": "text/plain"}))
    assert flow.metadata["agentproxy_rules"][0]["not_found"] == ["x" * 100]


def test_skipped_edit_names_no_missing_replacements(rules):
    rules.add({"action": "rewrite", "path": "/path",
               "replace": [{"find": "absent", "with": "x"}, {"find": "a", "with": "я"}]})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(b"abc", **{"Content-Type": "text/plain; charset=ascii"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert flow.response.content == b"abc" and "skipped" in entry
    assert "replaced" not in entry and "not_found" not in entry


def test_replacement_overridden_by_response_body_names_no_missing_ones(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "absent", "with": "x"}]})
    rules.add({"action": "rewrite", "path": "/path", "response_body": "new"})
    flow = through(Rewriter(rules), tflow.tflow(), server_response(b"abc", **{"Content-Type": "text/plain"}))
    entry = flow.metadata["agentproxy_rules"][0]
    assert flow.response.content == b"new" and "replaced by response_body of rule r2" in entry["skipped"]
    assert "not_found" not in entry and "replaced" not in entry


def test_unsupported_charset_is_named_when_skipping(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response("こんにちは".encode("shift_jis"), **{"Content-Type": "text/plain; charset=Shift_JIS"}))
    assert "not supported for editing" in flow.metadata["agentproxy_rules"][0]["skipped"]


def test_file_with_nul_bytes_is_binary():
    assert guess_content_type(b"\x00\x01\x02", "blob") == "application/octet-stream"


def test_uppercase_utf16_charset_without_byte_order_mark_is_not_guessed(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    encoded = "aaa".encode("utf-16-be")
    flow = through(Rewriter(rules), tflow.tflow(),
                   server_response(encoded, **{"Content-Type": "text/plain; charset=UTF-16"}))
    assert "no byte order mark" in flow.metadata["agentproxy_rules"][0]["skipped"]
    assert flow.response.raw_content == encoded


def test_request_only_rewrite_is_not_marked_when_body_file_fails(rules, tmp_path):
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    rules.add({"action": "rewrite", "path": "/path", "request_headers": {"X-Q": "1"}})
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock)})
    mock.unlink()
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.status_code == 502 and "skipped" not in flow.metadata["agentproxy_rules"][0]


def test_edited_body_that_broke_on_the_way_is_marked_skipped(rules):
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "ok", "with": "no"}]})
    rewriter = Rewriter(rules)
    flow = tflow.tflow()
    run(rewriter.requestheaders(flow))
    flow.response = server_response()
    rewriter.responseheaders(flow)
    rewriter.error(flow)
    assert "broke before its end" in flow.metadata["agentproxy_rules"][0]["skipped"]
