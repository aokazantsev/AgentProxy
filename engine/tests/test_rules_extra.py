import json

import pytest
from mitmproxy.test import tflow

from agentproxy.rewriter import Rewriter
from agentproxy.rules import APPLIED_MARK, RuleSet

from test_rewriter import through


@pytest.fixture
def rules(tmp_path):
    return RuleSet(str(tmp_path))


def test_unreadable_body_file_answers_plain_text_without_the_rule_headers(rules, tmp_path):
    mock = tmp_path / "mock.json"
    mock.write_text("{}")
    rules.add({"action": "respond", "path": "/path", "body_file": str(mock), "content_type": "application/json",
               "headers": {"Content-Encoding": "gzip", "X-Mock": "1"}})
    mock.unlink()
    flow = through(Rewriter(rules), tflow.tflow())
    assert flow.response.status_code == 502
    assert flow.response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "content-encoding" not in flow.response.headers and "X-Mock" not in flow.response.headers
    assert flow.response.raw_content.startswith(b"AgentProxy rule r1: body_file unreadable")


def test_rule_with_empty_id_in_file_is_skipped(tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps({"next_id": 2, "rules": [
        {"id": "", "action": "drop", "host": "a.example"},
        {"id": "r1", "action": "drop", "host": "b.example"}]}), encoding="utf-8")
    rule_set = RuleSet(str(tmp_path))
    assert [rule["id"] for rule in rule_set.list()] == ["r1"]
    assert "rule #1: rule without id" in rule_set.counts()["problem"]


def test_host_condition_ignores_the_case_of_the_request_host():
    from agentproxy.rules import matches
    assert matches({"host": "api.example.com"}, "API.Example.COM", "/", "GET", "https://API.Example.COM/")


def test_uppercase_content_encoding_is_edited_and_packed_again(rules):
    import gzip
    from test_rewriter import server_response
    rules.add({"action": "rewrite", "path": "/path", "replace": [{"find": "a", "with": "b"}]})
    response = server_response(**{"Content-Type": "text/plain", "Content-Encoding": "GZIP"})
    response.raw_content = gzip.compress(b"aaa")
    flow = through(Rewriter(rules), tflow.tflow(), response)
    assert gzip.decompress(flow.response.raw_content) == b"bbb"
    assert "skipped" not in flow.metadata["agentproxy_rules"][0]


@pytest.mark.parametrize("field", ["delay_ms", "times"])
def test_integer_field_with_words_is_a_rule_error(field):
    from agentproxy.rules import RuleError, normalize
    with pytest.raises(RuleError, match="must be an integer"):
        normalize({"action": "delay", "path": "/", "delay_ms": 10, field: "500ms"})


def test_text_fields_and_replace_list_are_limited():
    from agentproxy.rules import MAX_REPLACEMENTS, RuleError, normalize
    with pytest.raises(RuleError, match="longer than 2000"):
        normalize({"action": "drop", "host": "a" * 2001})
    with pytest.raises(RuleError, match="name"):
        normalize({"action": "drop", "host": "a", "name": 5})
    with pytest.raises(RuleError, match="name is longer than 200"):
        normalize({"action": "drop", "host": "a", "name": "n" * 201})
    with pytest.raises(RuleError, match="at most"):
        normalize({"action": "rewrite", "host": "a",
                   "replace": [{"find": "x", "with": "y"}] * (MAX_REPLACEMENTS + 1)})


@pytest.mark.parametrize("next_id", [True, -3, "7"])
def test_odd_next_id_in_file_numbers_rules_from_one(tmp_path, next_id):
    (tmp_path / "rules.json").write_text(json.dumps({"next_id": next_id, "rules": []}), encoding="utf-8")
    assert RuleSet(str(tmp_path)).add({"action": "drop", "host": "a"})["id"] == "r1"


@pytest.mark.parametrize("rule", [{"status": 500}, {"response_headers": {"X-Debug": "1"}}])
@pytest.mark.parametrize("headers, limit", [({"Content-Type": "text/event-stream"}, 1024),
                                            ({"Content-Length": "5000"}, 10)])
def test_rewrite_without_body_changes_keeps_the_response_streaming(rules, store, rule, headers, limit):
    from agentproxy.recorder import Recorder
    from agentproxy.rules import NO_REQUEST_STREAM_MARK, NO_RESPONSE_STREAM_MARK
    from agentproxy.sse import SseTap
    from agentproxy.store import FlowFilter
    from agentproxy.streamtap import StreamTap
    from test_rewriter import run, server_response
    rules.add(dict({"action": "rewrite", "path": "/path"}, **rule))
    flow = tflow.tflow()
    recorder = Recorder(store=store, body_limit=limit)
    rewriter = Rewriter(rules)
    run(rewriter.requestheaders(flow))
    recorder.requestheaders(flow)
    run(rewriter.request(flow))
    response = server_response()
    response.headers.update(headers)
    flow.response = response
    rewriter.responseheaders(flow)
    run(recorder.responseheaders(flow))
    event_stream = "Content-Type" in headers
    assert isinstance(flow.response.stream, SseTap if event_stream else StreamTap)
    if event_stream:
        opened = store.query(FlowFilter(kind="sse"), 1)[0]
        assert opened["open"] is True and [entry["id"] for entry in opened["rules"]] == ["r1"]
    assert NO_RESPONSE_STREAM_MARK not in flow.metadata and NO_REQUEST_STREAM_MARK not in flow.metadata
    assert [entry["id"] for entry in flow.metadata[APPLIED_MARK]] == ["r1"]


@pytest.mark.parametrize("rule", [{"action": "delay", "delay_ms": 10}, {"action": "rewrite", "status": 500},
                                  {"action": "rewrite", "response_headers": {"X-Debug": "1"}}])
def test_rules_without_body_changes_keep_a_large_upload_streaming(rules, rule):
    from agentproxy.recorder import Recorder
    from agentproxy.rules import NO_REQUEST_STREAM_MARK
    from agentproxy.streamtap import StreamTap
    from test_rewriter import run
    rules.add(dict({"path": "/path"}, **rule))
    flow = tflow.tflow()
    flow.request.headers["content-length"] = "5000"
    run(Rewriter(rules).requestheaders(flow))
    Recorder(store=None, body_limit=10).requestheaders(flow)
    assert isinstance(flow.request.stream, StreamTap)
    assert NO_REQUEST_STREAM_MARK not in flow.metadata and flow.error is None
    assert [entry["id"] for entry in flow.metadata[APPLIED_MARK]] == ["r1"]


def test_delayed_drop_of_a_small_upload_records_its_body_and_rule(rules, monkeypatch):
    from agentproxy.recorder import build_record
    from test_rewriter import fake_sleep_log
    slept = fake_sleep_log(monkeypatch)
    rules.add({"action": "drop", "path": "/path", "delay_ms": 10})
    flow = tflow.tflow()
    flow.request.content = b"hello"
    flow.request.headers["content-length"] = "5"
    through(Rewriter(rules), flow)
    assert slept == [0.01] and flow.error.msg == "Connection killed."
    record = build_record(flow, 1024)
    assert record.request_body == b"hello"
    assert record.rules == [{"id": "r1", "action": "drop"}]
