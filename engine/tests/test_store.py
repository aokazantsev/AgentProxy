from agentproxy.store import FlowFilter, FlowStore

from conftest import make_record


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


def test_clear_and_count(store):
    store.insert(make_record(1))
    store.insert(make_record(2))
    assert store.count() == 2
    assert store.clear() == 2
    assert store.count() == 0
    assert store.last_seq() == 0


def test_reopen_keeps_data(tmp_path):
    path = str(tmp_path / "persist.db")
    first = FlowStore(path, 1000)
    first.insert(make_record(1))
    first.close()
    second = FlowStore(path, 1000)
    assert second.get("flow-1") is not None
    second.close()
