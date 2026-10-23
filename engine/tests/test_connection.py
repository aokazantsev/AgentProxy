import asyncio
import time
from types import SimpleNamespace

import pytest
from mitmproxy import http

from agentproxy import connection
from agentproxy.device import DeviceControl, DeviceError
from agentproxy.recorder import TLS_CLIENT_ERROR, connect_failure, tls_failure_record
from agentproxy.rules import RuleSet
from agentproxy.service import ProxyService, ToolError, call_tool
from agentproxy.settings import Settings

from conftest import make_record
from test_device import FakeAdb

UNTRUSTED = TLS_CLIENT_ERROR.format("The client does not trust the proxy's certificate for pinned.example.com:443 "
                                    "(sslv3 alert bad certificate)")
OLD_TLS = TLS_CLIENT_ERROR.format("Client and mitmproxy cannot agree on a TLS version to use.")


def missing_adb(path):
    raise DeviceError("adb not found: install platform-tools", "adb not found")


@pytest.fixture
def service(store, settings, tmp_path):
    proxy = ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))
    proxy.proxy_running = True
    proxy.device = DeviceControl(lambda: Settings(proxy_port=18890), lambda: proxy.proxy_running, runner=FakeAdb(),
                                 finder=missing_adb)
    return proxy


def routed(service):
    adb = FakeAdb()
    adb.proxy["emu-1"] = "127.0.0.1:18890"
    adb.reverse["emu-1"] = [18890]
    service.device = DeviceControl(lambda: Settings(proxy_port=18890), lambda: service.proxy_running, runner=adb,
                                   finder=lambda path: "adb")
    return adb


def add(store, index, seconds_ago, **fields):
    store.insert(make_record(index, started=time.time() - seconds_ago, **fields))


def check(service, **arguments):
    return asyncio.run(service.check_connection(arguments))


def test_traffic_flows_reports_counts_hosts_and_run_start(service, store):
    add(store, 1, 3000)
    add(store, 2, 200)
    add(store, 3, 100, host="cdn.example.com", scheme="http")
    add(store, 4, 10, error="Client disconnected.")
    answer = check(service, device=False)
    assert answer["verdict"] == "traffic_flows"
    traffic = answer["traffic"]
    assert (traffic["count"], traffic["https"], traffic["http"], traffic["failed"], traffic["https_hosts"]) == (3, 2, 1, 1, 1)
    assert traffic["top_hosts"] == [{"host": "api.example.com", "count": 2}, {"host": "cdn.example.com", "count": 1}]
    assert traffic["flowing_since"] == connection.local_time(store.get("flow-2")["started"])
    assert traffic["last_record_seconds_ago"] <= 11
    assert answer["failures"] == []
    assert answer["device"] == {"checked": False}
    assert traffic["first"] == connection.local_time(store.get("flow-2")["started"])
    assert traffic["last"] == connection.local_time(store.get("flow-4")["started"])
    assert answer["hints"][-1].startswith("traffic goes through the proxy since")


def test_untrusted_certificate_without_requests_is_tls_refused(service, store):
    add(store, 1, 30, method="CONNECT", host="pinned.example.com", error=UNTRUSTED)
    add(store, 2, 20, method="CONNECT", host="api.example.com", error=UNTRUSTED)
    answer = check(service, device=False)
    assert answer["verdict"] == "tls_refused"
    failure = answer["failures"][0]
    assert failure["kind"] == "tls_untrusted" and failure["count"] == 2
    assert failure["sample"].startswith("TLS handshake with the client failed")
    assert any("http://mitm.it" in hint for hint in answer["hints"])


def test_untrusted_hosts_next_to_decrypted_traffic_point_to_pinning(service, store):
    add(store, 1, 30)
    add(store, 2, 20, method="CONNECT", host="pinned.example.com", error=UNTRUSTED)
    answer = check(service, device=False)
    assert answer["verdict"] == "traffic_flows"
    assert any("(pinned.example.com), 1 decrypted" in hint and "pins" in hint for hint in answer["hints"])


def test_background_hosts_refusing_the_certificate_do_not_hide_decrypted_traffic(service, store):
    add(store, 1, 40, host="app.example.com")
    for index, host in enumerate(("play.example.com", "fcm.example.com"), 2):
        add(store, index, 30, method="CONNECT", host=host, error=UNTRUSTED)
    answer = check(service, device=False)
    assert answer["verdict"] == "traffic_flows"
    assert answer["traffic"]["https_hosts"] == 1 and answer["failures"][0]["host_count"] == 2
    assert any(hint.startswith("2 HTTPS hosts refuse") and "), 1 decrypted" in hint and "http://mitm.it" in hint
               for hint in answer["hints"])


def test_connections_the_client_cancelled_are_not_failures(service, store):
    add(store, 1, 30, method="CONNECT", error=TLS_CLIENT_ERROR.format("connection closed early"))
    add(store, 2, 20, method="CONNECT", error="CONNECT failed: 502 Bad Gateway (Cannot connect to "
                                              "www.example.org:443: client disconnected)")
    answer = check(service, device=False)
    assert answer["failures"] == [] and answer["traffic"]["count"] == 0
    assert answer["traffic"]["last_record"] is None


def test_connect_failure_keeps_the_reason_without_the_advice():
    response = http.Response.make(502, "Cannot connect to www.example.org:443: [Errno 11001] getaddrinfo failed "
                                       "If you plan to redirect requests away from this server, consider setting it.")
    assert connect_failure(response) == ("CONNECT failed: 502 Bad Gateway (Cannot connect to www.example.org:443: "
                                         "[Errno 11001] getaddrinfo failed)")
    assert connect_failure(http.Response.make(502, b"")) == "CONNECT failed: 502 Bad Gateway"
    assert connect_failure(None) == "CONNECT failed"


def test_connect_the_client_gave_up_after_a_long_wait_is_a_failure(service, store):
    add(store, 1, 30, method="CONNECT", host="intranet.example.com", duration_ms=10500,
        error="CONNECT failed: 502 Bad Gateway (Cannot connect to intranet.example.com:443: client disconnected)")
    answer = check(service, device=False)
    assert answer["verdict"] == "hosts_unreachable"
    assert answer["failures"][0]["kind"] == "connect_failed"


def test_open_stream_older_than_the_window_is_traffic(service, store):
    add(store, 1, 3600, kind="sse", open=True)
    answer = check(service, device=False, since_minutes=5)
    assert answer["verdict"] == "traffic_flows" and answer["traffic"]["count"] == 1


def test_server_certificate_failures_are_traffic_with_a_failure(service, store):
    routed(service)
    add(store, 1, 20, host="internal.example.com", error="Certificate verify failed: unable to get local issuer")
    answer = check(service)
    assert answer["verdict"] == "traffic_flows"
    assert (answer["traffic"]["count"], answer["traffic"]["failed"]) == (1, 1)
    assert answer["failures"][0]["kind"] == "server_certificate"
    assert answer["traffic"]["flowing_since"] == connection.local_time(store.get("flow-1")["started"])
    assert any("server certificate" in hint for hint in answer["hints"])


def test_dropped_handshakes_do_not_claim_a_missing_certificate(service, store):
    add(store, 1, 30, method="CONNECT", host="a.example.com", error=TLS_CLIENT_ERROR.format(
        "The client disconnected during the handshake. If this happens consistently for a.example.com:443, this "
        "may indicate that the client does not trust the proxy's certificate."))
    add(store, 2, 20, method="CONNECT", host="b.example.com", error=TLS_CLIENT_ERROR.format(
        "Cannot parse ClientHello: 1603"))
    add(store, 3, 10)
    answer = check(service, device=False)
    assert answer["verdict"] == "traffic_flows"
    assert [failure["kind"] for failure in answer["failures"]] == ["tls_dropped"]


def test_routed_device_with_traffic_still_says_how_to_reach_the_app(service, store):
    routed(service)
    add(store, 1, 10, host="play.example.com")
    answer = check(service)
    assert answer["verdict"] == "traffic_flows"
    assert any(hint.startswith("if the hosts of the app under test are not in list_flows: restart the app")
               for hint in answer["hints"])


def test_routed_device_with_refused_certificate_gets_no_restart_advice(service, store):
    routed(service)
    add(store, 1, 30, host="plain.example.com", scheme="http")
    for index, host in enumerate(("a.example.com", "b.example.com"), 2):
        add(store, index, 20, method="CONNECT", host=host, error=UNTRUSTED)
    answer = check(service)
    assert answer["verdict"] == "tls_refused"
    assert not any("restart the app (many" in hint for hint in answer["hints"])


def test_refused_certificate_without_traffic_gets_no_restart_advice(service, store):
    routed(service)
    add(store, 1, 20, method="CONNECT", host="a.example.com", error=UNTRUSTED)
    answer = check(service)
    assert answer["verdict"] == "tls_refused"
    assert not any("restart the app (many" in hint for hint in answer["hints"])


def test_only_dropped_handshakes_without_traffic_mean_a_refused_certificate(service, store):
    add(store, 1, 30, method="CONNECT", host="a.example.com", error=TLS_CLIENT_ERROR.format(
        "The client disconnected during the handshake."))
    answer = check(service, device=False)
    assert answer["verdict"] == "tls_refused"
    assert any("http://mitm.it" in hint for hint in answer["hints"])


def test_failure_kinds_are_told_apart(service, store):
    add(store, 1, 40, method="CONNECT", host="old.example.com", error=OLD_TLS)
    add(store, 2, 30, method="CONNECT", host="intranet.example.com", error="CONNECT failed: 502 Bad Gateway")
    add(store, 3, 20, host="selfsigned.example.com", error="Certificate verify failed: self-signed certificate")
    answer = check(service, device=False)
    assert [failure["kind"] for failure in answer["failures"]] == ["tls_version", "connect_failed", "server_certificate"]
    assert answer["verdict"] == "traffic_flows"
    assert answer["traffic"]["count"] == 1


def test_only_connect_failures_mean_unreachable_hosts(service, store):
    add(store, 1, 30, method="CONNECT", host="intranet.example.com", error="CONNECT failed: 502 Bad Gateway")
    answer = check(service, device=False)
    assert answer["verdict"] == "hosts_unreachable"
    assert any("unreachable (VPN, DNS, firewall)" in hint for hint in answer["hints"])


def test_window_excludes_older_records_but_reports_the_last_one(service, store):
    add(store, 1, 3600)
    answer = check(service, device=False, since_minutes=5)
    assert answer["verdict"] == "no_traffic"
    assert answer["traffic"]["count"] == 0
    assert answer["traffic"]["flowing_since"] is None
    assert answer["traffic"]["last_record"] is not None
    assert any(hint.startswith("nothing recorded in the last 5 min") for hint in answer["hints"])
    assert check(service, device=False, since_minutes=120)["verdict"] == "traffic_flows"


def test_empty_history_without_device_says_where_to_point_the_client(service):
    answer = check(service, device=False)
    assert answer["verdict"] == "no_traffic"
    assert answer["traffic"]["last_record"] is None
    assert any("127.0.0.1:18890" in hint for hint in answer["hints"])


def test_routed_device_without_traffic_asks_to_restart_the_app(service):
    routed(service)
    answer = check(service)
    assert answer["device"]["checked"] is True and answer["device"]["ready"] is True
    assert "hint" not in answer["device"]
    assert answer["verdict"] == "no_traffic"
    assert any(hint.startswith("the device is routed: restart the app") for hint in answer["hints"])


def test_unrouted_device_is_the_verdict_and_the_hint(service):
    adb = routed(service)
    adb.proxy["emu-1"] = "null"
    answer = check(service)
    assert answer["verdict"] == "device_not_routed"
    assert any("run device_proxy enable" in hint for hint in answer["hints"])


def test_missing_adb_is_reported_without_failing_the_call(service):
    answer = check(service)
    assert answer["device"] == {"checked": False, "problem": "adb not found: install platform-tools"}
    assert answer["hints"][0] == "device not checked: adb not found: install platform-tools"


def test_missing_adb_is_quiet_while_traffic_flows(service, store):
    add(store, 1, 10)
    answer = check(service)
    assert not any(hint.startswith("device not checked") for hint in answer["hints"])


def test_proxy_not_listening_comes_first(service, store):
    routed(service)
    service.proxy_running = False
    add(store, 1, 10)
    answer = check(service)
    assert answer["verdict"] == "proxy_not_listening"
    assert answer["proxy"] == {"port": 18890, "listening": False}
    assert any("PORT BUSY" in hint for hint in answer["hints"])
    assert any("device_proxy disable" in hint for hint in answer["hints"])


def test_since_minutes_is_validated(service):
    with pytest.raises(ToolError):
        check(service, since_minutes=0)
    with pytest.raises(ToolError):
        check(service, since_minutes=24 * 60 + 1)


def test_device_flag_accepts_query_strings(service):
    routed(service)
    assert asyncio.run(call_tool(service, "check_connection", {"device": "false"}))["device"] == {"checked": False}


def test_flowing_since_stops_at_a_long_pause():
    assert connection.flowing_since([1000.0, 990.0, 900.0, 600.0]) == 900.0
    assert connection.flowing_since([]) is None


def test_failure_reports_the_latest_error(service, store):
    add(store, 1, 60, method="CONNECT", host="a.example.com", error="CONNECT failed: 502 Bad Gateway (old)")
    add(store, 2, 10, method="CONNECT", host="a.example.com", error="CONNECT failed: 502 Bad Gateway (new)")
    failure = check(service, device=False)["failures"][0]
    assert failure["sample"].endswith("(new)")
    assert failure["last"] == connection.local_time(store.get("flow-2")["started"])


def test_serial_picks_one_of_several_devices(service):
    adb = routed(service)
    adb.devices["phone-2"] = "device"
    assert check(service)["device"]["problem"].startswith("several devices")
    answer = check(service, serial="emu-1")
    assert answer["device"]["checked"] is True and answer["device"]["serial"] == "emu-1"


def test_old_tls_without_traffic_is_tls_refused(service, store):
    add(store, 1, 20, method="CONNECT", host="old.example.com", error=OLD_TLS)
    answer = check(service, device=False)
    assert answer["verdict"] == "tls_refused"
    assert any("cannot agree on a TLS version" in hint for hint in answer["hints"])


def test_hints_leave_out_what_does_not_apply(service, store):
    add(store, 1, 30, method="CONNECT", host="intranet.example.com", error="CONNECT failed: 502 Bad Gateway")
    assert not any(hint.startswith("nothing recorded") for hint in check(service, device=False)["hints"])
    adb = routed(service)
    adb.proxy["emu-1"] = "null"
    assert not any(hint.startswith("point the client") for hint in check(service)["hints"])
    service.proxy_running = False
    assert not any("device_proxy disable" in hint for hint in check(service)["hints"])


def test_loud_device_problem_shows_while_traffic_flows(service, store):
    adb = routed(service)
    adb.devices["phone-2"] = "device"
    add(store, 1, 10)
    assert any(hint.startswith("device not checked: several devices") for hint in check(service)["hints"])


@pytest.mark.parametrize("error", [
    "CONNECT failed: 502 Bad Gateway (Cannot connect to a.example.com:443: client disconnected)",
    TLS_CLIENT_ERROR.format("connection closed early")])
@pytest.mark.parametrize("duration,kind", [(None, None), (2999, None), (3000, "connect_failed")])
def test_client_gave_up_threshold(service, store, error, duration, kind):
    add(store, 1, 20, method="CONNECT", duration_ms=duration, error=error)
    kinds = [failure["kind"] for failure in check(service, device=False)["failures"]]
    assert kinds == ([kind] if kind else [])


def test_run_gap_boundary():
    assert connection.flowing_since([1000.0, 880.0]) == 880.0
    assert connection.flowing_since([1000.0, 879.0]) == 1000.0


class BrokenText:
    status_code = 502
    reason = "Bad Gateway"

    def get_text(self, strict=True):
        raise ValueError("broken")


def test_connect_failure_detail_is_one_short_line():
    detail = connect_failure(http.Response.make(502, "line one\nline two " + "x" * 1000))
    assert "\n" not in detail and "line one line two" in detail
    assert len(detail) == len("CONNECT failed: 502 Bad Gateway ()") + 300
    assert connect_failure(BrokenText()) == "CONNECT failed: 502 Bad Gateway"


def test_tls_closed_early_after_a_long_wait_is_an_unreachable_host(service, store):
    add(store, 1, 20, method="CONNECT", host="blackhole.example.com", duration_ms=10000,
        error=TLS_CLIENT_ERROR.format("connection closed early"))
    answer = check(service, device=False)
    assert answer["verdict"] == "hosts_unreachable"
    assert answer["failures"][0]["kind"] == "connect_failed"


def test_flowing_since_counts_requests_only(service, store):
    add(store, 1, 100, method="CONNECT", error=UNTRUSTED)
    add(store, 2, 30)
    add(store, 3, 5, method="CONNECT", error=TLS_CLIENT_ERROR.format("connection closed early"))
    traffic = check(service, device=False)["traffic"]
    assert traffic["flowing_since"] == connection.local_time(store.get("flow-2")["started"])
    assert traffic["last_record"] == connection.local_time(store.get("flow-2")["started"])


def test_failures_alone_have_no_flowing_since(service, store):
    add(store, 1, 20, method="CONNECT", error=UNTRUSTED)
    assert check(service, device=False)["traffic"]["flowing_since"] is None


def tls_refusal(started):
    client = SimpleNamespace(sni="a.example.com", timestamp_start=started, peername=("127.0.0.1", 1))
    context = SimpleNamespace(client=client, server=SimpleNamespace(address=("a.example.com", 443)))
    return tls_failure_record(SimpleNamespace(context=context, conn=SimpleNamespace(error="connection closed early")))


def test_tls_refusal_record_measures_the_wait_in_milliseconds():
    record = tls_refusal(time.time() - 5)
    assert 4900 <= record.duration_ms <= 6000
    assert connection.classify(vars(record)) == "connect_failed"
    assert tls_refusal(None).duration_ms is None


def test_no_device_is_quiet_while_traffic_flows(service, store):
    adb = routed(service)
    adb.devices.clear()
    add(store, 1, 10)
    assert not any(hint.startswith("device not checked") for hint in check(service)["hints"])


def test_activity_takes_the_newest_records(store):
    for index in range(1, 4):
        store.insert(make_record(index))
    assert [row["started"] for row in store.activity(1)] == [store.get("flow-3")["started"]]


def test_failures_alone_prove_the_client_reached_the_proxy(service, store):
    add(store, 1, 20, method="CONNECT", host="a.example.com", error=UNTRUSTED)
    hints = check(service)["hints"]
    assert not any(hint.startswith("device not checked") or hint.startswith("point the client") for hint in hints)


def test_dropped_handshakes_next_to_plain_http_are_tls_refused(service, store):
    add(store, 1, 30, scheme="http", host="plain.example.com")
    add(store, 2, 20, method="CONNECT", host="a.example.com", error=TLS_CLIENT_ERROR.format(
        "The client disconnected during the handshake."))
    assert check(service, device=False)["verdict"] == "tls_refused"


def test_host_decrypted_after_its_last_refusal_is_fixed(service, store):
    add(store, 1, 200, method="CONNECT", host="api.example.com", error=UNTRUSTED)
    add(store, 2, 100, method="CONNECT", host="play.example.com", error=UNTRUSTED)
    add(store, 3, 30, host="api.example.com")
    answer = check(service, device=False)
    failure = answer["failures"][0]
    assert [entry["host"] for entry in failure["hosts"]] == ["play.example.com"]
    assert failure["count"] == 1 and failure["host_count"] == 1
    assert failure["hosts"][0]["last"] == connection.local_time(store.get("flow-2")["started"])
    assert answer["verdict"] == "traffic_flows"


def test_refused_certificate_hint_puts_restarting_the_app_first(service, store):
    add(store, 1, 20, method="CONNECT", host="play.example.com", error=UNTRUSTED)
    hint = next(hint for hint in check(service, device=False)["hints"] if "none is decrypted" in hint)
    assert hint.index("restart it first") < hint.index("http://mitm.it")
