import json

import pytest

from agentproxy import store as store_module
from agentproxy.store import FlowFilter, FlowStore

from conftest import make_record


def test_text_with_raw_bytes_is_stored(store):
    raw = b"/search?q=\xcf\xf0".decode("utf-8", "surrogateescape")
    store.insert(make_record(1, path=raw, url="http://h" + raw, response_content_type=raw))
    flow = store.get("flow-1")
    assert flow["path"] == "/search?q=\\xcf\\xf0" and flow["response_content_type"] == flow["path"]


def test_header_with_raw_bytes_can_be_served(store):
    raw = b"\xcf\xf0".decode("utf-8", "surrogateescape")
    store.insert(make_record(1, request_headers=[["x-" + raw, raw]], response_headers=[["content-disposition", raw]]))
    flow = store.get("flow-1")
    json.dumps(flow["request_headers"] + flow["response_headers"], ensure_ascii=False).encode("utf-8")
    assert flow["request_headers"] == [["x-\\xcf\\xf0", "\\xcf\\xf0"]]
    assert flow["response_headers"] == [["content-disposition", "\\xcf\\xf0"]]


def test_search_ignores_case_of_cyrillic(store):
    store.insert(make_record(1, host="Пример.рф", url="http://Пример.рф/Поиск"))
    assert len(store.query(FlowFilter(host="пример"), 10)) == 1
    assert len(store.query(FlowFilter(text="ПОИСК"), 10)) == 1


def test_insert_and_get_round_trip(store):
    seq = store.insert(make_record(1, request_body=b"hello", request_body_size=5))
    flow = store.get("flow-1")
    assert flow["seq"] == seq
    assert flow["request_headers"] == [["accept", "application/json"]]
    assert flow["request_body"] == b"hello"
    assert flow["response_body"] == b'{"ok":true}'
    assert store.get(str(seq))["id"] == "flow-1"


def test_get_unknown_returns_none(store):
    assert store.get("missing") is None


def test_query_newest_first_with_limit(store):
    for index in range(1, 6):
        store.insert(make_record(index))
    flows = store.query(FlowFilter(), 3)
    assert [flow["id"] for flow in flows] == ["flow-5", "flow-4", "flow-3"]


def test_query_filters(store):
    store.insert(make_record(1, host="api.example.com", method="GET", status=200))
    store.insert(make_record(2, host="cdn.example.org", method="POST", status=500, path="/upload", url="https://cdn.example.org/upload"))
    store.insert(make_record(3, host="API.Example.com", method="GET", status=404, error=None))
    assert [f["id"] for f in store.query(FlowFilter(host="api.example"), 10)] == ["flow-3", "flow-1"]
    assert [f["id"] for f in store.query(FlowFilter(method="post"), 10)] == ["flow-2"]
    assert [f["id"] for f in store.query(FlowFilter(status=404), 10)] == ["flow-3"]
    assert [f["id"] for f in store.query(FlowFilter(failed_only=True), 10)] == ["flow-3", "flow-2"]
    assert [f["id"] for f in store.query(FlowFilter(path="/upload"), 10)] == ["flow-2"]
    assert [f["id"] for f in store.query(FlowFilter(text="CDN.EXAMPLE"), 10)] == ["flow-2"]
    assert [f["id"] for f in store.query(FlowFilter(since_seq=2), 10)] == ["flow-3"]


def test_filter_treats_percent_literally(store):
    store.insert(make_record(1, host="a.example.com"))
    assert store.query(FlowFilter(host="%"), 10) == []


def test_filter_matches_agrees_with_query(store):
    store.insert(make_record(1, host="api.example.com", status=502))
    summary = store.query(FlowFilter(), 1)[0]
    assert FlowFilter(host="API", failed_only=True).matches(summary)
    assert not FlowFilter(host="cdn").matches(summary)
    assert not FlowFilter(since_seq=summary["seq"]).matches(summary)


def test_listener_receives_summary(store):
    received = []
    store.add_listener(received.append)
    store.insert(make_record(1))
    store.remove_listener(received.append)
    store.insert(make_record(2))
    assert [item["id"] for item in received] == ["flow-1"]


def test_prune_keeps_max_flows(tmp_path):
    small = FlowStore(str(tmp_path / "small.db"), max_flows=100)
    for index in range(1, 251):
        small.insert(make_record(index))
    assert small.count() <= 200
    assert small.get("flow-250") is not None
    assert small.get("flow-1") is None
    small.close()


@pytest.mark.parametrize("field,value", [
    ("response_body", b"x" * 64 * 1024),
    ("request_body", b"x" * 64 * 1024),
    ("path", "/" + "p" * 64 * 1024),
    ("response_headers", [["x-big", "h" * 64 * 1024]]),
    ("url", "https://api.example.com/" + "u" * 64 * 1024),
    ("request_headers", [["cookie", "c" * 64 * 1024]]),
], ids=["response_body", "request_body", "path", "response_headers", "url", "request_headers"])
def test_history_stays_under_byte_budget_on_insert(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(store_module, "PRUNE_EVERY_BYTES", 256 * 1024)
    small = FlowStore(str(tmp_path / "bytes.db"), max_flows=5000, max_bytes=1024 * 1024)
    for index in range(1, 301):
        small.insert(make_record(index, **{field: value}))
        assert small._stored_bytes() <= 1024 * 1024 + 256 * 1024 + 2 * len(str(value))
    assert small.get("flow-300") is not None
    assert small.get("flow-1") is None
    small.close()


def test_clear_truncates_the_journal(tmp_path):
    path = tmp_path / "space.db"
    big = FlowStore(str(path), max_flows=5000)
    for index in range(1, 41):
        big.insert(make_record(index, response_body=b"y" * 256 * 1024))
    big.clear()
    assert (tmp_path / "space.db-wal").stat().st_size == 0
    size = path.stat().st_size
    for index in range(41, 81):
        big.insert(make_record(index, response_body=b"y" * 256 * 1024))
    assert path.stat().st_size <= size * 1.2
    big.close()


def test_insert_after_close_is_ignored(tmp_path):
    closed = FlowStore(str(tmp_path / "closed.db"), max_flows=100)
    closed.close()
    assert closed.insert(make_record(1)) is None


def test_clear_and_count(store):
    store.insert(make_record(1))
    store.insert(make_record(2))
    assert store.count() == 2
    assert store.clear() == 2
    assert store.count() == 0
    assert store.last_seq() == 0


@pytest.mark.parametrize("version", [1, 99])
def test_database_of_other_schema_starts_empty(tmp_path, version):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE flows (seq INTEGER PRIMARY KEY, id TEXT)")
    old.execute("INSERT INTO flows (id) VALUES ('legacy')")
    old.execute("PRAGMA user_version={0}".format(version))
    old.commit()
    old.close()
    fresh = FlowStore(path, 1000)
    assert fresh.count() == 0
    fresh.insert(make_record(1, response_body=b"z" * 1000))
    assert fresh._stored_bytes() >= 1000
    assert "stored_bytes" not in fresh.get("flow-1")
    fresh.close()


def test_reopen_keeps_data(tmp_path):
    path = str(tmp_path / "persist.db")
    first = FlowStore(path, 1000)
    first.insert(make_record(1))
    first.close()
    second = FlowStore(path, 1000)
    assert second.get("flow-1") is not None
    second.close()
