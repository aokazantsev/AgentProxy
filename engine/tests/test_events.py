import asyncio

import pytest

from agentproxy import store as store_module
from agentproxy.rules import RuleSet
from agentproxy.service import ProxyService, ToolError, call_tool, public_tools
from agentproxy.store import ENGINE_STOPPED, EventFilter, FlowFilter, FlowStore

from conftest import make_record


@pytest.fixture
def service(store, settings, tmp_path):
    return ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))


def run(coroutine):
    return asyncio.run(coroutine)


def stream(store, index=1, kind="sse", **overrides):
    return store.insert(make_record(index, kind=kind, open=True, response_body=b"", response_body_size=0,
                                    duration_ms=None, **overrides))


def test_plain_flows_carry_no_stream_fields(store):
    seq = store.insert(make_record(1))
    assert "kind" not in store.summary(seq) and "open" not in store.get("flow-1") and "event_count" not in store.query(FlowFilter(), 5)[0]


def test_stream_flow_is_listed_open_and_counts_its_events(store):
    seq = stream(store)
    assert store.summary(seq)["open"] is True and store.summary(seq)["kind"] == "sse"
    store.add_event(seq, "incoming", "event", b"a")
    store.add_event(seq, "incoming", "event", b"b")
    assert store.summary(seq)["event_count"] == 2
    assert [event["n"] for event in store.events(seq, EventFilter(), 10)] == [1, 2]


def test_finish_updates_the_same_row_and_keeps_events(store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"a")
    done = make_record(1, kind="sse", open=False, status=200, duration_ms=1234, error="Client disconnected.")
    assert store.finish(done) == seq
    summary = store.summary(seq)
    assert summary["open"] is False and summary["duration_ms"] == 1234 and summary["error"] == "Client disconnected."
    assert summary["event_count"] == 1 and store.last_seq() == seq


def test_filters_by_kind_and_open(store):
    plain = store.insert(make_record(1))
    live = stream(store, 2, "websocket")
    gone = stream(store, 3, "sse")
    store.finish(make_record(3, kind="sse", open=False))
    assert [row["seq"] for row in store.query(FlowFilter(kind="websocket"), 10)] == [live]
    assert [row["seq"] for row in store.query(FlowFilter(kind="http"), 10)] == [plain]
    assert [row["seq"] for row in store.query(FlowFilter(open_only=True), 10)] == [live]
    assert FlowFilter(kind="sse").matches(store.summary(gone)) and not FlowFilter(kind="sse").matches(store.summary(plain))
    assert FlowFilter(kind="http").matches(store.summary(plain))


def test_event_filters(store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", "Привет".encode(), name="token")
    store.add_event(seq, "outgoing", "text", b"hello")
    store.add_event(seq, "incoming", "comment", b"ping")
    names = lambda **kwargs: [e["n"] for e in store.events(seq, EventFilter(**kwargs), 10)]
    assert names(direction="outgoing") == [2]
    assert names(type="comment") == [3]
    assert names(name="token") == [1]
    assert names(contains="привет") == [1]
    assert names(since_n=1) == [2, 3]
    assert [e["n"] for e in store.events(seq, EventFilter(), 2, tail=True)] == [2, 3]
    assert [e["n"] for e in store.events(seq, EventFilter(), 2)] == [1, 2]


def test_event_text_filter_survives_invalid_utf8(store):
    seq = stream(store)
    store.add_event(seq, "incoming", "binary", b"\xff\xfe abc")
    assert [e["n"] for e in store.events(seq, EventFilter(contains="ABC"), 5)] == [1]


def test_event_data_is_cut_to_the_limit_with_true_size(store):
    seq = stream(store)
    event = store.add_event(seq, "incoming", "text", b"x" * 100, limit=10)
    assert len(event["data"]) == 10 and event["size"] == 100 and event["truncated"] is True


def test_oldest_events_go_when_a_stream_has_too_many(store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_EVENTS_PER_FLOW", 3)
    monkeypatch.setattr(store_module, "EVENTS_BEFORE_TRIM", 3)
    seq = stream(store)
    for index in range(5):
        store.add_event(seq, "incoming", "event", str(index).encode())
    events = store.events(seq, EventFilter(), 10)
    assert [e["n"] for e in events] == [3, 4, 5] and [e["data"] for e in events] == [b"2", b"3", b"4"]
    assert store.summary(seq)["event_count"] == 5
    expected = 3 * (1 + store_module.EVENT_OVERHEAD_BYTES)
    assert store._db.execute("SELECT events_bytes FROM flows WHERE seq = ?", (seq,)).fetchone()[0] == expected


def closed_stream(target, index, kind="sse"):
    seq = stream(target, index, kind)
    target.finish(make_record(index, kind=kind, open=False))
    return seq


def events_total(target):
    return target._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_clear_keeps_open_streams_and_their_events(store):
    live = stream(store, 1, "websocket")
    store.add_event(live, "incoming", "text", b"kept")
    done = closed_stream(store, 2)
    store.add_event(done, "incoming", "event", b"gone")
    store.insert(make_record(3))
    assert store.clear() == 2
    assert store.summary(live)["open"] is True and store.summary(done) is None
    assert [e["data"] for e in store.events(live, EventFilter(), 5)] == [b"kept"]
    assert store.add_event(live, "incoming", "text", b"after")["n"] == 2
    assert events_total(store) == 2


def test_event_of_a_removed_flow_is_dropped(store):
    seq = closed_stream(store, 1)
    store.clear()
    assert store.add_event(seq, "incoming", "event", b"late") is None
    assert events_total(store) == 0


def test_closed_streams_and_their_events_go_when_history_is_pruned(tmp_path):
    small = FlowStore(str(tmp_path / "small.db"), max_flows=100)
    first = closed_stream(small, 1)
    small.add_event(first, "incoming", "event", b"old")
    for index in range(2, 205):
        small.insert(make_record(index))
    assert small.summary(first) is None
    assert events_total(small) == 0
    small.close()


def test_open_stream_is_not_pushed_out_by_newer_flows(tmp_path):
    small = FlowStore(str(tmp_path / "small.db"), max_flows=100)
    live = stream(small, 1, "websocket")
    for index in range(2, 305):
        small.insert(make_record(index))
    assert small.summary(live)["open"] is True
    assert small.add_event(live, "incoming", "text", b"still here")["n"] == 1
    assert small.count() <= 200
    small.close()


def test_events_of_middle_flows_go_even_when_an_older_stream_stays(tmp_path):
    small = FlowStore(str(tmp_path / "small.db"), max_flows=100)
    live = stream(small, 1, "websocket")
    middle = closed_stream(small, 2)
    small.add_event(middle, "incoming", "event", b"old")
    for index in range(3, 305):
        small.insert(make_record(index))
    assert small.summary(middle) is None and small.summary(live) is not None
    assert events_total(small) == 0
    small.close()


def test_event_bytes_count_toward_the_history_size_limit(tmp_path):
    limited = FlowStore(str(tmp_path / "limited.db"), max_flows=1000, max_bytes=2000)
    first = closed_stream(limited, 1)
    for _ in range(5):
        limited.add_event(first, "incoming", "event", b"x" * 400)
    for index in range(2, 103):
        limited.insert(make_record(index, response_body=b"", response_body_size=0))
    assert limited.summary(first) is None
    assert events_total(limited) == 0
    limited.close()


def test_open_streams_over_half_the_history_lose_old_events_first(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "PRUNE_EVERY_BYTES", 1000)
    limited = FlowStore(str(tmp_path / "limited.db"), max_flows=1000, max_bytes=3000)
    old = closed_stream(limited, 1)
    limited.add_event(old, "incoming", "event", b"y" * 900)
    live = stream(limited, 2, "websocket")
    for _ in range(6):
        limited.add_event(live, "incoming", "binary", b"x" * 400)
    assert limited.summary(old) is not None and limited.summary(live)["event_count"] == 6
    open_bytes = limited._db.execute("SELECT events_bytes FROM flows WHERE seq = ?", (live,)).fetchone()[0]
    assert open_bytes < 6 * 464
    limited.close()


def test_events_alone_start_pruning_of_closed_flows(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "PRUNE_EVERY_BYTES", 1000)
    limited = FlowStore(str(tmp_path / "limited.db"), max_flows=1000, max_bytes=3000)
    old = closed_stream(limited, 1)
    limited.add_event(old, "incoming", "event", b"y" * 2000)
    live = stream(limited, 2, "websocket")
    for _ in range(3):
        limited.add_event(live, "incoming", "binary", b"x" * 400)
    assert limited.summary(old) is None and limited.summary(live)["event_count"] == 3
    limited.close()


def test_open_stream_keeps_only_its_last_64_mb_worth_of_events(store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_STREAM_EVENT_BYTES", 2000)
    seq = stream(store, 1, "websocket")
    for index in range(20):
        store.add_event(seq, "incoming", "binary", bytes([index]) * 300)
    kept = store.events(seq, EventFilter(), 100)
    assert kept[-1]["n"] == 20 and len(kept) < 20
    total = store._db.execute("SELECT events_bytes FROM flows WHERE seq = ?", (seq,)).fetchone()[0]
    assert total <= 2000 and total == sum(len(e["data"]) + store_module.EVENT_OVERHEAD_BYTES for e in kept)


def test_one_event_larger_than_the_stream_limit_is_kept(store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_STREAM_EVENT_BYTES", 100)
    seq = stream(store, 1, "websocket")
    store.add_event(seq, "incoming", "binary", b"z" * 500)
    assert [e["n"] for e in store.events(seq, EventFilter(), 5)] == [1]


def test_events_read_only_the_start_of_large_data(store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"a" * 1000)
    assert store.events(seq, EventFilter(), 1, data_bytes=10)[0]["data"] == b"a" * 10
    assert store.events(seq, EventFilter(contains="aaa"), 1, data_bytes=10)[0]["size"] == 1000


def test_open_streams_are_closed_when_the_store_reopens(tmp_path):
    path = str(tmp_path / "again.db")
    first = FlowStore(path, max_flows=100)
    seq = stream(first)
    first.add_event(seq, "incoming", "event", b"a")
    first.close()
    second = FlowStore(path, max_flows=100)
    summary = second.summary(seq)
    assert summary["open"] is False and summary["error"] == ENGINE_STOPPED
    assert second.events(seq, EventFilter(), 5)[0]["data"] == b"a"
    second.close()


def test_finish_open_closes_every_live_stream(store):
    first = stream(store, 1)
    second = stream(store, 2, "websocket")
    store.finish(make_record(2, kind="websocket", open=False, error="boom"))
    store.finish_open(ENGINE_STOPPED)
    assert store.summary(first)["error"] == ENGINE_STOPPED and store.summary(first)["open"] is False
    assert store.summary(second)["error"] == "boom"


def test_orphan_events_left_by_an_older_version_are_removed_on_open(tmp_path):
    path = str(tmp_path / "orphans.db")
    first = FlowStore(path, max_flows=100)
    seq = stream(first)
    first.add_event(seq, "incoming", "event", b"a")
    first._db.execute("DELETE FROM flows")
    first.close()
    second = FlowStore(path, max_flows=100)
    assert second._db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    second.close()


def test_history_of_an_older_version_gets_the_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript(store_module.SCHEMA.split("CREATE TABLE IF NOT EXISTS events")[0].replace(
        "    kind TEXT NOT NULL DEFAULT 'http',\n    open INTEGER NOT NULL DEFAULT 0,\n    event_count INTEGER NOT NULL DEFAULT 0,\n"
        "    events_bytes INTEGER NOT NULL DEFAULT 0\n", "    placeholder TEXT\n").replace("rules TEXT,\n    placeholder TEXT", "rules TEXT"))
    db.execute("PRAGMA user_version={0}".format(store_module.SCHEMA_VERSION))
    db.execute("INSERT INTO flows (id, started, method, scheme, host, port, path, url, request_headers) "
               "VALUES ('x', 1, 'GET', 'https', 'h', 443, '/', 'https://h/', '[]')")
    db.commit()
    db.close()
    upgraded = FlowStore(path, max_flows=100)
    assert upgraded.count() == 1 and "kind" not in upgraded.query(FlowFilter(), 1)[0]
    seq = upgraded.insert(make_record(5, kind="sse", open=True))
    assert upgraded.add_event(seq, "incoming", "event", b"a")["n"] == 1
    upgraded.close()


def test_list_events_shows_events_with_times_and_binary_as_base64(service, store):
    seq = stream(store, kind="websocket", started=1000.0)
    store.add_event(seq, "outgoing", "text", b"hi", timestamp=1000.5)
    store.add_event(seq, "incoming", "binary", b"\x00\x01", timestamp=1001.0)
    store.add_event(seq, "incoming", "close", b"bye", name="1000", timestamp=1002.0)
    answer = service.list_events({"flow": str(seq)})
    assert answer["flow"] == {"seq": seq, "id": "flow-1", "kind": "websocket", "open": True, "host": "api.example.com",
                              "path": "/v1/items?page=1", "event_count": 3}
    first, second, third = answer["events"]
    assert (first["ms"], first["direction"], first["type"], first["data"], first["size"]) == (500, "outgoing", "text", "hi", 2)
    assert second["encoding"] == "base64" and second["data"] == "AAE="
    assert third["name"] == "1000" and third["data"] == "bye"


def test_list_events_accepts_flow_id_seq_and_integers(service, store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"a")
    assert service.list_events({"flow": "flow-1"})["events"][0]["data"] == "a"
    assert service.list_events({"flow": seq})["events"][0]["data"] == "a"


def test_list_events_cuts_long_data(service, store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"x" * 50, size=50)
    event = service.list_events({"flow": seq, "max_data_chars": 10})["events"][0]
    assert event["data"] == "x" * 10 and event["truncated"] is True and event["size"] == 50


def test_list_events_marks_events_cut_by_the_store(service, store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"y" * 20, size=900, truncated=True)
    assert service.list_events({"flow": seq})["events"][0]["truncated"] is True


def test_list_events_refuses_unknown_and_plain_flows(service, store):
    plain = store.insert(make_record(1))
    with pytest.raises(ToolError, match="plain request"):
        service.list_events({"flow": plain})
    with pytest.raises(ToolError, match="not found"):
        service.list_events({"flow": "nope"})
    with pytest.raises(ToolError, match="flow is required"):
        service.list_events({})
    seq = stream(store, 2)
    with pytest.raises(ToolError, match="direction"):
        service.list_events({"flow": seq, "direction": "sideways"})


def test_list_flows_accepts_kind_and_open(service, store):
    live = stream(store, 1, "websocket")
    store.insert(make_record(2))
    assert [row["seq"] for row in service.list_flows({"kind": "websocket", "open": "true"})["flows"]] == [live]
    with pytest.raises(ToolError, match="kind"):
        service.list_flows({"kind": "grpc"})


def test_get_flow_of_a_stream_points_to_list_events(service, store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"a")
    flow = service.get_flow({"id": seq})
    assert flow["kind"] == "sse" and flow["event_count"] == 1
    assert "list_events" in flow["response_body"]["note"] and "events" not in flow


def test_wait_for_event_returns_an_event_that_arrives(service, store):
    seq = stream(store)

    async def scenario():
        waiter = asyncio.ensure_future(service.wait_for_event({"flow": seq, "contains": "needle", "timeout_seconds": 5}))
        await asyncio.sleep(0.05)
        store.add_event(seq, "incoming", "event", b"hay")
        store.add_event(seq, "incoming", "event", b"a needle here")
        return await waiter

    answer = run(scenario())
    assert answer["timed_out"] is False and answer["event"]["n"] == 2 and answer["flow"]["seq"] == seq


def test_wait_for_event_skips_stored_events_unless_since_n_is_given(service, store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"old")
    assert run(service.wait_for_event({"flow": seq, "since_n": 0}))["event"]["data"] == "old"
    answer = run(service.wait_for_event({"flow": seq, "timeout_seconds": 1}))
    assert answer["timed_out"] is True and answer["event"] is None and answer["flow"]["seq"] == seq


def test_wait_for_event_without_flow_follows_streams_by_filter(service, store):
    async def scenario():
        waiter = asyncio.ensure_future(service.wait_for_event({"kind": "websocket", "direction": "incoming",
                                                               "timeout_seconds": 5}))
        await asyncio.sleep(0.05)
        other = stream(store, 1, "sse")
        store.add_event(other, "incoming", "event", b"not me")
        mine = stream(store, 2, "websocket")
        store.add_event(mine, "outgoing", "text", b"not me either")
        store.add_event(mine, "incoming", "text", b"me")
        return await waiter, mine

    answer, mine = run(scenario())
    assert answer["flow"]["seq"] == mine and answer["event"]["data"] == "me"


def test_wait_for_event_without_flow_refuses_since_n(service):
    with pytest.raises(ToolError, match="since_n needs flow"):
        run(service.wait_for_event({"since_n": 3}))


def test_wait_for_event_answers_when_the_stream_ends(service, store):
    seq = stream(store)

    async def scenario():
        waiter = asyncio.ensure_future(service.wait_for_event({"flow": seq, "timeout_seconds": 20}))
        await asyncio.sleep(0.05)
        store.finish(make_record(1, kind="sse", open=False))
        return await waiter

    answer = run(scenario())
    assert answer["closed"] is True and answer["event"] is None and answer["flow"]["open"] is False


def test_wait_for_event_on_a_stream_that_is_already_over_answers_at_once(service, store):
    seq = stream(store)
    store.finish(make_record(1, kind="sse", open=False))
    answer = run(service.wait_for_event({"flow": seq, "timeout_seconds": 20}))
    assert answer["closed"] is True and answer["timed_out"] is False


def test_wait_for_event_times_out_without_a_stream(service):
    answer = run(service.wait_for_event({"kind": "sse", "timeout_seconds": 1}))
    assert answer == {"flow": None, "event": None, "timed_out": True}


def test_wait_for_event_does_not_leak_listeners(service, store):
    seq = stream(store)
    run(service.wait_for_event({"flow": seq, "timeout_seconds": 1}))
    assert store._event_listeners == []


def test_tools_are_listed_and_described(service):
    names = [tool["name"] for tool in public_tools()]
    assert "list_events" in names and "wait_for_event" in names
    assert run(call_tool(service, "list_events", {"flow": stream(service.store)}))["events"] == []


def test_event_name_and_id_count_toward_stored_bytes(store):
    seq = stream(store)
    store.add_event(seq, "incoming", "event", b"x", name="n" * 100, event_id="i" * 50)
    total = store._db.execute("SELECT events_bytes FROM flows WHERE seq = ?", (seq,)).fetchone()[0]
    assert total == 1 + 100 + 50 + store_module.EVENT_OVERHEAD_BYTES


def test_clear_removes_many_closed_flows_in_chunks(store):
    for index in range(1, 1201):
        seq = store.insert(make_record(index))
        if index % 100 == 0:
            store.insert(make_record(10000 + index, kind="sse", open=True))
            store.finish(make_record(10000 + index, kind="sse", open=False))
            store.add_event(store.find_summary("flow-{0}".format(10000 + index))["seq"], "incoming", "event", b"e")
    total = store.count()
    assert total > 1000 and store.clear() == total
    assert store.count() == 0 and events_total(store) == 0


def test_wait_for_event_matches_name_and_type_of_arriving_events(service, store):
    seq = stream(store)

    async def scenario(arguments):
        waiter = asyncio.ensure_future(service.wait_for_event(dict(arguments, timeout_seconds=5)))
        await asyncio.sleep(0.05)
        store.add_event(seq, "incoming", "comment", b"ping")
        store.add_event(seq, "incoming", "event", b"wrong", name="other")
        store.add_event(seq, "incoming", "event", b"right", name="token")
        return await waiter

    assert run(scenario({"flow": seq, "name": "token"}))["event"]["data"] == "right"
    assert run(scenario({"flow": seq, "type": "event"}))["event"]["data"] == "wrong"
    assert run(scenario({"kind": "sse", "name": "token"}))["event"]["data"] == "right"


class FakeMessage:
    def __init__(self, content, from_client=False):
        self.content = content
        self.from_client = from_client
        self.is_text = True
        self.injected = False
        self.dropped = False
        self.timestamp = 1000.0


def test_rate_limit_resets_every_second_and_counts_what_it_skipped(store, monkeypatch):
    from types import SimpleNamespace
    from agentproxy import recorder as recorder_module
    recorder = recorder_module.Recorder(store, 1024)
    clock = {"now": 1000.0}
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recorder_module, "MAX_EVENTS_PER_SECOND", 3)
    seq = stream(store, 1, "websocket")
    live = recorder_module.LiveStream(seq)
    flow = SimpleNamespace(id="ws-1", websocket=SimpleNamespace(messages=[]))
    recorder.streams[flow.id] = live
    for index in range(5):
        flow.websocket.messages.append(FakeMessage(str(index).encode()))
        recorder.websocket_message(flow)
    clock["now"] += 1.0
    flow.websocket.messages.append(FakeMessage(b"later"))
    recorder.websocket_message(flow)
    events = store.events(seq, EventFilter(), 10)
    assert [(e["type"], e["data"]) for e in events] == [("text", b"0"), ("text", b"1"), ("text", b"2"),
                                                       ("note", b""), ("note", b""), ("text", b"later")]
    assert events[3]["note"].startswith("more than 3 events or 4 MB this second")
    assert events[4]["note"].startswith("2 events not recorded") and flow.websocket.messages == []


def test_websocket_message_is_cut_to_the_event_limit(store):
    from types import SimpleNamespace
    from agentproxy import recorder as recorder_module
    from agentproxy.sse import MAX_EVENT_BYTES
    recorder = recorder_module.Recorder(store, 1024)
    seq = stream(store, 1, "websocket")
    flow = SimpleNamespace(id="ws-2", websocket=SimpleNamespace(messages=[FakeMessage(b"m" * (MAX_EVENT_BYTES + 10))]))
    recorder.streams[flow.id] = recorder_module.LiveStream(seq)
    recorder.websocket_message(flow)
    event = store.events(seq, EventFilter(), 1)[0]
    assert len(event["data"]) == MAX_EVENT_BYTES and event["size"] == MAX_EVENT_BYTES + 10 and event["truncated"]


def test_count_trim_keeps_a_margin_and_runs_once_per_margin(store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_EVENTS_PER_FLOW", 20)
    monkeypatch.setattr(store_module, "EVENTS_BEFORE_TRIM", 22)
    calls = []
    original = store._trim_events
    monkeypatch.setattr(store, "_trim_events", lambda *args: calls.append(args[1]) or original(*args))
    seq = stream(store)
    for index in range(30):
        store.add_event(seq, "incoming", "event", str(index).encode())
    assert calls == [23, 26, 29]
    kept = store.events(seq, EventFilter(), 100)
    assert kept[-1]["n"] == 30 and 20 <= len(kept) <= 22


def test_prune_does_not_spin_when_open_streams_cannot_shrink(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "PRUNE_EVERY_BYTES", 1000)
    limited = FlowStore(str(tmp_path / "limited.db"), max_flows=1000, max_bytes=3000)
    streams = [stream(limited, index, "websocket") for index in range(1, 5)]
    for seq in streams:
        limited.add_event(seq, "incoming", "binary", b"x" * 900)
    assert all(limited.summary(seq)["event_count"] == 1 for seq in streams)
    limited.close()


def test_wait_for_flow_reports_a_stream_when_it_opens(service, store):
    async def scenario():
        waiter = asyncio.ensure_future(service.wait_for_flow({"kind": "sse", "open": True, "timeout_seconds": 5}))
        await asyncio.sleep(0.05)
        store.insert(make_record(1))
        seq = stream(store, 2)
        return await waiter, seq

    answer, seq = run(scenario())
    assert answer["flow"]["seq"] == seq and answer["flow"]["open"] is True


def test_prune_trims_the_largest_open_stream_and_keeps_the_small_one(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "PRUNE_EVERY_BYTES", 1000)
    limited = FlowStore(str(tmp_path / "limited.db"), max_flows=1000, max_bytes=4000)
    closed = closed_stream(limited, 1)
    big = stream(limited, 2, "websocket")
    small = stream(limited, 3, "websocket")
    limited.add_event(small, "incoming", "text", b"s" * 200)
    limited.add_event(small, "incoming", "text", b"s" * 200)
    for _ in range(8):
        limited.add_event(big, "incoming", "binary", b"b" * 400)
    assert len(limited.events(small, EventFilter(), 10)) == 2
    assert len(limited.events(big, EventFilter(), 10)) < 8
    assert limited.summary(closed) is not None
    limited.close()


def test_closed_history_goes_when_open_streams_cannot_shrink(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "PRUNE_EVERY_BYTES", 1000)
    limited = FlowStore(str(tmp_path / "limited.db"), max_flows=1000, max_bytes=3000)
    old = limited.insert(make_record(1, response_body=b"o" * 900, response_body_size=900))
    streams = [stream(limited, index, "websocket") for index in range(2, 5)]
    for seq in streams:
        limited.add_event(seq, "incoming", "binary", b"x" * 900)
    assert limited.summary(old) is None
    assert all(limited.summary(seq)["event_count"] == 1 for seq in streams)
    limited.close()


def test_rate_limit_counts_bytes_too(store, monkeypatch):
    from types import SimpleNamespace
    from agentproxy import recorder as recorder_module
    recorder = recorder_module.Recorder(store, 1024)
    clock = {"now": 1000.0}
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recorder_module, "MAX_BYTES_PER_SECOND", 1000)
    seq = stream(store, 1, "websocket")
    flow = SimpleNamespace(id="ws-3", websocket=SimpleNamespace(messages=[]))
    recorder.streams[flow.id] = recorder_module.LiveStream(seq)

    def send(size):
        flow.websocket.messages.append(FakeMessage(b"b" * size))
        recorder.websocket_message(flow)

    for size in (2000, 600, 600, 10):
        send(size)
    kinds = [(e["type"], e["size"]) for e in store.events(seq, EventFilter(), 10)]
    assert kinds == [("text", 2000), ("note", 0)]
    clock["now"] += 1
    send(400)
    send(600)
    events = store.events(seq, EventFilter(), 10)
    assert [(e["type"], e["size"]) for e in events[2:]] == [("note", 0), ("text", 400), ("text", 600)]
    assert events[2]["note"].startswith("3 events not recorded")


def test_rate_limit_counts_only_the_recorded_part_of_large_messages(store, monkeypatch):
    from types import SimpleNamespace
    from agentproxy import recorder as recorder_module
    from agentproxy.sse import MAX_EVENT_BYTES
    recorder = recorder_module.Recorder(store, 1024)
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(recorder_module, "MAX_BYTES_PER_SECOND", 2 * MAX_EVENT_BYTES)
    seq = stream(store, 1, "websocket")
    flow = SimpleNamespace(id="ws-4", websocket=SimpleNamespace(messages=[]))
    recorder.streams[flow.id] = recorder_module.LiveStream(seq)
    for _ in range(2):
        flow.websocket.messages.append(FakeMessage(b"m" * (MAX_EVENT_BYTES + 10)))
        recorder.websocket_message(flow)
    assert [e["type"] for e in store.events(seq, EventFilter(), 10)] == ["text", "text"]
