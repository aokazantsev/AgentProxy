import logging
import os
import time
import types
import urllib.error
import urllib.request

import pytest

from agentproxy import flowlog, logreader
from agentproxy.rules import APPLIED_MARK
from agentproxy.service import ProxyService, ToolError, call_tool, public_tools
from conftest import free_port
from test_engine_e2e import (api_get, certifi_bundle, engine, proxied_opener, start_engine, start_server,
                             wait_until)
from test_rules_e2e import add_rule, api_call

ENGINE_LINES = (
    "2026-10-19 10:00:00  INFO  agentproxy.engine: engine started\n"
    "2026-10-19 10:00:01  WARNING agentproxy.rules: rule r1 skipped\n"
    "2026-10-19 10:00:02  ERROR agentproxy.api: boom\n"
    "Traceback (most recent call last):\n"
    "  File \"x.py\", line 1\n"
    "ValueError: bad\n"
    "2026-10-19 10:00:03  INFO  agentproxy.engine: done\n"
)


def write_engine_log(directory, text=ENGINE_LINES):
    with open(os.path.join(directory, "engine.log"), "w", encoding="utf-8") as stream:
        stream.write(text)


def test_reader_keeps_tracebacks_with_their_record(tmp_path):
    write_engine_log(str(tmp_path))
    answer = logreader.read(str(tmp_path), "engine", 100, "error")
    assert answer["matched"] == 1 and "ValueError: bad" in answer["text"] and "done" not in answer["text"]


def test_reader_levels_contains_and_last_lines(tmp_path):
    write_engine_log(str(tmp_path))
    assert logreader.read(str(tmp_path), "engine", 100, "warning")["matched"] == 2
    assert logreader.read(str(tmp_path), "engine", 100, "info")["matched"] == 4
    assert logreader.read(str(tmp_path), "engine", 100, None, "SKIPPED")["returned"] == 1
    last = logreader.read(str(tmp_path), "engine", 1)
    assert last["returned"] == 1 and last["matched"] == 4 and "done" in last["text"]


def test_reader_joins_rotated_files_oldest_first(tmp_path):
    (tmp_path / "engine.log.1").write_text("2026-10-18 09:00:00  INFO  a: older\n", encoding="utf-8")
    write_engine_log(str(tmp_path))
    answer = logreader.read(str(tmp_path), "engine", 100)
    assert answer["oldest"] == "2026-10-18 09:00:00" and answer["text"].startswith("2026-10-18")
    assert [item["name"] for item in answer["files"]] == ["engine.log.1", "engine.log"]


def test_reader_reads_every_rotation_oldest_first(tmp_path):
    for number, name in enumerate(("engine.log.3", "engine.log.2", "engine.log.1", "engine.log")):
        (tmp_path / name).write_text("2026-10-19 10:00:0{0}  INFO  a: {1}\n".format(number, name), encoding="utf-8")
    answer = logreader.read(str(tmp_path), "engine", 1)
    assert answer["text"].endswith("a: engine.log") and answer["oldest"] == "2026-10-19 10:00:00"
    assert [item["name"] for item in answer["files"]] == ["engine.log.3", "engine.log.2", "engine.log.1", "engine.log"]
    (tmp_path / "log.old.txt").write_text("2026-10-19 09:00:00  older\n", encoding="utf-8")
    (tmp_path / "log.txt").write_text("2026-10-19 11:00:00  newer\n", encoding="utf-8")
    tray = logreader.read(str(tmp_path), "tray", 1)
    assert tray["text"].endswith("newer") and tray["matched"] == 2


def test_reader_since_minutes(tmp_path):
    now = time.time()
    fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3600))
    (tmp_path / "log.txt").write_text("{0}  engine started\n{1}  engine exited\n".format(old, fresh), encoding="utf-8")
    answer = logreader.read(str(tmp_path), "tray", 100, since_minutes=10)
    assert answer["returned"] == 1 and "exited" in answer["text"]


def test_reader_truncates_long_entries_and_total(tmp_path):
    lines = "".join("2026-10-19 10:00:{0:02d}  INFO  a: {1}\n".format(index % 60, "x" * 1900) for index in range(60))
    write_engine_log(str(tmp_path), lines + "2026-10-19 11:00:00  INFO  a: " + "y" * 5000 + "\n")
    answer = logreader.read(str(tmp_path), "engine", 500)
    assert len(answer["text"]) <= logreader.TEXT_CHARS + logreader.RECORD_CHARS
    assert answer["returned"] < answer["matched"] and answer["text"].rstrip().endswith("y")
    assert "\n…\n" in answer["text"]


def test_reader_keeps_the_end_of_a_long_traceback(tmp_path):
    frames = "".join("  File \"x{0}.py\", line 1, in f\n    call()\n".format(index) for index in range(200))
    write_engine_log(str(tmp_path), "2026-10-19 10:00:00  CRITICAL agentproxy: CRASH: Traceback\n" + frames
                     + "ValueError: the real reason\n")
    text = logreader.read(str(tmp_path), "engine", 10)["text"]
    assert text.startswith("2026-10-19 10:00:00  CRITICAL") and text.endswith("ValueError: the real reason")
    assert len(text) <= logreader.RECORD_CHARS + 3


def test_reader_without_files(tmp_path):
    answer = logreader.read(str(tmp_path), "tray", 10)
    assert answer["returned"] == 0 and answer["files"] == [] and answer["oldest"] is None


def make_service(tmp_path):
    return ProxyService(None, None, str(tmp_path), str(tmp_path), None)


def test_read_log_tool_validates_arguments(tmp_path):
    import asyncio
    service = make_service(tmp_path)
    write_engine_log(str(tmp_path))
    answer = asyncio.run(call_tool(service, "read_log", {"level": "WARNING", "lines": "1"}))
    assert answer["returned"] == 1 and "ERROR" in answer["text"]
    for arguments in ({"source": "cli"}, {"level": "debug"}, {"source": "tray", "level": "error"}, {"lines": 0},
                      {"lines": 501}, {"since_minutes": -1}):
        with pytest.raises(ToolError):
            asyncio.run(call_tool(service, "read_log", arguments))


def test_tool_refusal_is_logged(tmp_path, caplog):
    import asyncio
    service = make_service(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agentproxy.service"):
        with pytest.raises(ToolError):
            asyncio.run(call_tool(service, "read_log", {"source": "cli"}))
    assert any("read_log refused: source must be one of" in record.getMessage() for record in caplog.records)


def test_read_log_is_a_public_tool():
    assert "read_log" in [tool["name"] for tool in public_tools()]


def test_throttle_logs_first_then_every_nth():
    throttle = flowlog.Throttle(first=3, every=5)
    logged = [number for number in (throttle.count("k") for _ in range(12)) if number]
    assert logged == [1, 2, 3, 5, 10]
    assert throttle.count("other") == 1


def test_throttle_forgets_when_keys_overflow():
    throttle = flowlog.Throttle(first=1, every=100)
    for index in range(flowlog.MAX_KEYS):
        throttle.count(index)
    assert throttle.count("new") == 1 and len(throttle.counts) == 1


def test_clean_squeezes_and_cuts():
    assert flowlog.clean("a\n  b\tc", 10) == "a b c"
    assert flowlog.clean("x" * 20, 5) == "xxxxx…"


def fake_flow(entries=None, status=200, error=None):
    return types.SimpleNamespace(
        request=types.SimpleNamespace(method="GET", pretty_host="api.example.com"),
        response=types.SimpleNamespace(status_code=status) if status else None,
        error=types.SimpleNamespace(msg=error) if error else None,
        metadata={APPLIED_MARK: entries} if entries else {})


def logged(caplog, *calls):
    flow_log = flowlog.FlowLog()
    with caplog.at_level(logging.INFO, logger="agentproxy.flows"):
        for flow, seq in calls:
            flow_log.saved(flow, seq)
    return [(record.levelname, record.getMessage()) for record in caplog.records]


def test_clean_rule_outcome_is_info(caplog):
    assert logged(caplog, (fake_flow([{"id": "r2", "action": "respond"}]), 5)) == [
        ("INFO", "rules #5 GET api.example.com -> 200: r2 respond")]


def test_skipped_and_error_are_warnings_with_reasons(caplog):
    entries = [{"id": "r1", "action": "rewrite", "skipped": "body not changed: response has no body"},
               {"id": "r3", "action": "respond", "error": "body_file x unreadable"}]
    assert logged(caplog, (fake_flow(entries, 502), 6)) == [
        ("WARNING", "rules #6 GET api.example.com -> 502: r1 rewrite skipped=body not changed: response has no body; "
                    "r3 respond error=body_file x unreadable")]


def test_successful_flow_without_rules_is_silent(caplog):
    assert logged(caplog, (fake_flow(), 7)) == []


def test_failed_flow_without_rules_names_the_reason(caplog):
    assert logged(caplog, (fake_flow(status=None, error="server closed connection"), 8)) == [
        ("INFO", "flow #8 GET api.example.com -> no response (server closed connection)")]


def test_flow_log_thins_out_repeats_per_kind(caplog):
    same = [(fake_flow([{"id": "r1", "action": "respond"}]), seq) for seq in range(1, 61)]
    other = [(fake_flow([{"id": "r1", "action": "rewrite", "skipped": "body not changed: event stream"}]), 99)]
    lines = logged(caplog, *(same + other))
    assert len(lines) == flowlog.FIRST_LOGGED + 1 + 1
    assert lines[-2][1].endswith("[occurrence 50 of this kind]") and lines[-1][0] == "WARNING"


class TlsRecord:
    host = "api.example.com"
    error = "TLS handshake with the client failed: does not trust"


def test_tls_failure_is_logged_with_its_seq(caplog):
    with caplog.at_level(logging.WARNING, logger="agentproxy.flows"):
        flowlog.FlowLog().tls_failed(TlsRecord(), 7)
        flowlog.FlowLog().tls_failed(TlsRecord(), None)
    assert [record.getMessage() for record in caplog.records] == [
        "tls #7 CONNECT api.example.com: TLS handshake with the client failed: does not trust"]


def test_describe_rule_has_host_but_no_path_or_regex():
    text = flowlog.describe_rule({"action": "respond", "host": "api.example.com", "path": "/secret?t=1",
                                  "url_regex": "token", "enabled": False, "method": "GET"})
    assert text == "respond, host api.example.com, method GET, disabled"


def engine_log(engine):
    with open(os.path.join(engine["data_dir"], "engine.log"), encoding="utf-8") as stream:
        return stream.read()


def read_log_api(engine, query=""):
    return api_get(engine["api_port"], "/api/log" + query)


def test_engine_journals_rule_outcomes_without_path(engine):
    target = start_server()
    rule_id = add_rule(engine, action="rewrite", path="/secret-path", status=202,
                       replace=[{"find": "hello", "with": "x"}, {"find": "absent", "with": "y"}])
    url = "http://127.0.0.1:{0}/secret-path?token=abc123".format(target.server_port)
    request = urllib.request.Request(url, data=b"hello", method="POST")
    proxied_opener(engine).open(request, timeout=15).read()
    wait_until(lambda: "rules #" in engine_log(engine), 10)
    answer = read_log_api(engine, "?level=warning&contains=rules%20%23")[1]
    assert answer["returned"] == 1
    line = answer["text"]
    seq = api_get(engine["api_port"], "/api/flows?path=/secret-path")[1]["flows"][0]["seq"]
    assert "WARNING" in line and "rules #{0} POST 127.0.0.1 -> 202".format(seq) in line
    assert "{0} rewrite".format(rule_id) in line
    assert "not_found=1" in line and "replaced=1" in line
    for secret in ("secret-path", "abc123", "hello"):
        assert secret not in engine_log(engine)
    assert "rule {0} added: rewrite".format(rule_id) in engine_log(engine)
    target.shutdown()


def test_engine_journals_drop_and_failed_flow(engine):
    target = start_server()
    rule_id = add_rule(engine, action="drop", path="/dropped")
    with pytest.raises(Exception):
        proxied_opener(engine).open("http://127.0.0.1:{0}/dropped".format(target.server_port), timeout=15)
    dead_port = free_port()
    with pytest.raises(Exception):
        proxied_opener(engine).open("http://127.0.0.1:{0}/".format(dead_port), timeout=15)
    wait_until(lambda: "flow #" in engine_log(engine), 10)
    text = read_log_api(engine, "?since_minutes=5")[1]["text"]
    dropped = api_get(engine["api_port"], "/api/flows?path=/dropped")[1]["flows"][0]["seq"]
    failed = api_get(engine["api_port"], "/api/flows?url_contains={0}".format(dead_port))[1]["flows"][0]["seq"]
    assert "rules #{0} GET 127.0.0.1 -> no response (".format(dropped) in text and "{0} drop".format(rule_id) in text
    assert "flow #{0} GET 127.0.0.1 -> ".format(failed) in text
    target.shutdown()


def test_engine_journals_tls_refusal_with_its_seq(engine, tmp_path):
    target = start_server(str(tmp_path))
    with pytest.raises(urllib.error.URLError):
        proxied_opener(engine, cafile=certifi_bundle()).open("https://localhost:{0}/x".format(target.server_port),
                                                             timeout=15)

    def refused():
        flows = api_get(engine["api_port"], "/api/flows?failed_only=true&method=CONNECT")[1]["flows"]
        return [flow for flow in flows if "does not trust" in (flow["error"] or "")]

    seq = wait_until(refused, 10)[0]["seq"]
    text = read_log_api(engine, "?level=warning&contains=tls%20%23")[1]["text"]
    assert "tls #{0} CONNECT localhost: ".format(seq) in text and "does not trust" in text
    target.shutdown()


def test_engine_journals_refusals_and_rule_changes(engine):
    status, answer = api_call(engine["api_port"], "POST", "/api/rules", {"action": "respond"})
    assert status == 400
    rule_id = add_rule(engine, action="respond", host="example.com", body="x")
    api_call(engine["api_port"], "PATCH", "/api/rules/" + rule_id, {"enabled": False})
    api_call(engine["api_port"], "DELETE", "/api/rules/" + rule_id)
    api_call(engine["api_port"], "DELETE", "/api/rules")
    text = read_log_api(engine, "?contains=rule&lines=50")[1]["text"]
    assert "add_rule refused: a rule needs at least one condition" in text
    assert "rule {0} added: respond, host example.com".format(rule_id) in text
    assert "rule {0} updated (enabled): respond, host example.com, disabled".format(rule_id) in text
    assert "rules deleted: {0}".format(rule_id) in text and "rules deleted: none" in text
    rule_id = add_rule(engine, action="respond", host="example.org", body="x")
    api_call(engine["api_port"], "PATCH", "/api/rules/" + rule_id, {"reset_hits": True})
    assert "rule {0} updated (reset_hits)".format(rule_id) in read_log_api(engine, "?contains=reset_hits")[1]["text"]


def test_log_endpoint_rejects_bad_arguments(engine):
    assert read_log_api(engine, "?source=cli")[0] == 400
    assert read_log_api(engine, "?source=tray&level=error")[0] == 400
    status, answer = read_log_api(engine, "?source=tray")
    assert status == 200 and answer["returned"] == 0


def test_events_rule_outcome_is_in_the_rules_line():
    text = flowlog._entry_text({"id": "r1", "action": "events", "changed": 2, "replaced": 3,
                                "closed": "after incoming event 2"})
    assert text == "r1 events changed=2 replaced=3 closed=after incoming event 2"


def test_describe_rule_names_the_cut_point():
    assert flowlog.describe_rule({"action": "cut", "path": "/v", "after_bytes": 1000}) == "cut, after_bytes 1000"
    assert flowlog.describe_rule({"action": "cut", "path": "/v", "after_percent": 40,
                                  "download_kbps": 500}) == "cut, download_kbps 500, after_percent 40"
