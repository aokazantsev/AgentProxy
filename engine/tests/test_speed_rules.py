import asyncio

import pytest
from mitmproxy.test import tflow

from agentproxy.flowlog import describe_rule
from agentproxy.rewriter import NO_LINK, Rewriter
from agentproxy.rules import RuleError, RuleSet, normalize
from agentproxy.speed import SCOPE_CONNECTION, SCOPE_REQUEST
from test_speed import make_link


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def rules(tmp_path):
    return RuleSet(str(tmp_path))


class Limits:
    def __init__(self, link=None):
        self.link = link

    def get(self, client_id):
        return self.link


def test_speed_fields_are_any_action_and_bounded():
    assert normalize({"action": "respond", "path": "/a", "download_kbps": "500"})["download_kbps"] == 500
    assert normalize({"action": "delay", "path": "/a", "upload_kbps": 64})["upload_kbps"] == 64
    for value in (0, 1000001, True, 1.5):
        with pytest.raises(RuleError):
            normalize({"action": "rewrite", "path": "/a", "status": 201, "download_kbps": value})


def test_delay_action_needs_a_wait_or_a_limit():
    with pytest.raises(RuleError) as error:
        normalize({"action": "delay", "path": "/a"})
    assert "download_kbps or upload_kbps" in str(error.value)
    assert normalize({"action": "delay", "path": "/a", "download_kbps": 8})["action"] == "delay"


def test_needs_http1_requires_an_active_host_rule(rules):
    rules.add({"action": "delay", "path": "/img", "download_kbps": 100})
    assert not rules.needs_http1("cdn.example.com")
    off = rules.add({"action": "delay", "host": "cdn.example.com", "download_kbps": 100, "enabled": False})
    assert not rules.needs_http1("cdn.example.com")
    rules.delete(off["id"])
    rules.add({"action": "delay", "host": "CDN.example.com", "download_kbps": 100, "times": 1})
    assert rules.needs_http1("cdn.example.com.")
    rules.plan("cdn.example.com", "/x", "GET", "https://cdn.example.com/x")
    assert not rules.needs_http1("cdn.example.com")
    rules.add({"action": "rewrite", "host": "api.example.com", "status": 201})
    assert not rules.needs_http1("api.example.com")


@pytest.fixture
def service(store, settings, tmp_path):
    from agentproxy.service import ProxyService
    return ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))


def test_speed_hints_depend_on_host(service):
    with_host = service.add_rule({"action": "delay", "host": "cdn.example.com", "download_kbps": 100})["hint"]
    assert "opened before this rule stay HTTP/2" in with_host and "add host" not in with_host
    without_host = service.add_rule({"action": "delay", "path": "/img", "upload_kbps": 100})["hint"]
    assert "add host" in without_host and "opened before this rule" not in without_host
    disabled = service.update_rule({"id": "r2", "enabled": False})
    assert "add host" not in disabled.get("hint", "")
    plain = service.add_rule({"action": "delay", "path": "/x", "delay_ms": 10})
    assert "HTTP/2" not in plain.get("hint", "")


def test_deleting_all_rules_lifts_every_limit(service):
    calls = []

    class Recorder:
        def change_rule(self, rule_id, limits):
            calls.append((rule_id, limits))

    service.speeds = Recorder()
    service.add_rule({"action": "delay", "path": "/", "download_kbps": 400})
    service.add_rule({"action": "delay", "path": "/w", "upload_kbps": 100})
    service.delete_rule({"all": True})
    assert calls == [("r1", None), ("r2", None)]


def test_describe_rule_names_limits():
    text = describe_rule({"action": "delay", "host": "h", "download_kbps": 5, "upload_kbps": 7})
    assert "download_kbps 5" in text and "upload_kbps 7" in text


def limited_flow(http2=False):
    flow = tflow.tflow()
    if http2:
        flow.request.http_version = "HTTP/2.0"
    return flow


def test_matching_request_limits_its_connection_with_the_lowest_values(rules):
    rules.add({"action": "delay", "path": "/path", "download_kbps": 300, "upload_kbps": 50})
    rules.add({"action": "rewrite", "path": "/path", "status": 202, "download_kbps": 100})
    link = make_link()
    flow = limited_flow()
    run(Rewriter(rules, speeds=Limits(link)).requestheaders(flow))
    assert link.writer.pace.rate == 12500 and link.reader.pace.rate == 6250 and link.scope == SCOPE_REQUEST
    assert [entry["limited"] for entry in flow.metadata["agentproxy_rules"]] == ["request", "request"]
    run(Rewriter(rules, speeds=Limits(link)).request(flow))
    assert link.reader.pace is None and link.writer.pace.rate == 12500


def test_next_http1_request_without_limit_lifts_it(rules):
    rules.add({"action": "delay", "path": "/slow", "download_kbps": 100})
    link = make_link()
    rewriter = Rewriter(rules, speeds=Limits(link))
    slow = limited_flow()
    slow.request.path = "/slow"
    run(rewriter.requestheaders(slow))
    assert link.scope == SCOPE_REQUEST
    run(rewriter.requestheaders(limited_flow()))
    assert link.scope is None and link.writer.pace is None


def test_http2_keeps_the_whole_connection_limited(rules):
    rules.add({"action": "delay", "path": "/slow", "download_kbps": 100})
    link = make_link()
    rewriter = Rewriter(rules, speeds=Limits(link))
    slow = limited_flow(http2=True)
    slow.request.path = "/slow"
    run(rewriter.requestheaders(slow))
    assert slow.metadata["agentproxy_rules"][0]["limited"] == SCOPE_CONNECTION
    run(rewriter.requestheaders(limited_flow(http2=True)))
    assert link.scope == SCOPE_CONNECTION and link.writer.pace.rate == 12500


def test_http2_connection_keeps_a_rule_that_used_its_times(rules):
    rules.add({"action": "delay", "path": "/slow", "download_kbps": 100, "times": 1})
    link = make_link()
    rewriter = Rewriter(rules, speeds=Limits(link))
    slow = limited_flow(http2=True)
    slow.request.path = "/slow"
    run(rewriter.requestheaders(slow))
    again = limited_flow(http2=True)
    again.request.path = "/slow"
    run(rewriter.requestheaders(again))
    assert "agentproxy_rules" not in again.metadata
    assert link.scope == SCOPE_CONNECTION and link.writer.pace.rate == 12500


def test_limit_without_tcp_link_is_skipped(rules):
    rules.add({"action": "delay", "path": "/path", "download_kbps": 100})
    flow = limited_flow()
    run(Rewriter(rules, speeds=Limits(None)).requestheaders(flow))
    assert flow.metadata["agentproxy_rules"][0]["skipped"] == NO_LINK


class Hello:
    def __init__(self, sni, offers):
        from mitmproxy.connection import Client
        client = Client(peername=("127.0.0.1", 5000), sockname=("127.0.0.1", 8890), timestamp_start=0)
        client.sni = sni
        client.alpn_offers = offers
        self.context = type("Context", (), {"client": client})()
        self.establish_server_tls_first = True


class Start:
    def __init__(self, context, settings):
        self.context = context
        self.ssl_conn = type("Conn", (), {"get_app_data": lambda conn: settings})()


@pytest.mark.parametrize("sni, offers, downgraded", [
    ("img.example.com", (b"h2", b"http/1.1"), True),
    ("api.example.com", (b"h2", b"http/1.1"), False),
    ("img.example.com", (b"h2",), False),
    (None, (b"h2", b"http/1.1"), False),
])
def test_limited_host_is_offered_http1_only(rules, sni, offers, downgraded):
    rules.add({"action": "delay", "host": "img.example.com", "download_kbps": 100})
    rewriter = Rewriter(rules, speeds=Limits())
    hello = Hello(sni, offers)
    rewriter.tls_clienthello(hello)
    settings = {"http2": True}
    rewriter.tls_start_client(Start(hello.context, settings))
    assert (hello.context.client.alpn_offers == (b"http/1.1",)) == downgraded
    assert settings["http2"] is (not downgraded)
    assert hello.establish_server_tls_first and not rewriter.http1_clients


def test_no_downgrade_without_speed_limits(rules):
    rules.add({"action": "delay", "host": "img.example.com", "download_kbps": 100})
    hello = Hello("img.example.com", (b"h2", b"http/1.1"))
    Rewriter(rules).tls_clienthello(hello)
    assert hello.context.client.alpn_offers == (b"h2", b"http/1.1")
