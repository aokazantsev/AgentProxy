import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from agentproxy import device
from agentproxy.device import DeviceControl, DeviceError
from agentproxy.deviceports import FILE_NAME, MAX_DEVICES, DeviceListeners, DevicePortError, DevicePorts
from agentproxy.flowlog import describe_rule
from agentproxy.guard import OwnPortsGuard
from agentproxy.recorder import build_record
from agentproxy.rules import RuleError, RuleSet, normalize
from agentproxy.service import ProxyService, ToolError, call_tool
from agentproxy.settings import Settings
from agentproxy.store import FlowFilter, FlowStore

from conftest import make_record
from test_device import FakeAdb

XIAOMI = "iz6pfqea6xjbhahi"
SAMSUNG = "R58X707W8GR"


def ports_in(directory, busy=(), proxy_port=8890, api_port=8891):
    return DevicePorts(str(directory), proxy_port, api_port, lambda port: port not in busy)


class InstantLoop:
    def call_soon_threadsafe(self, callback, *args):
        callback(*args)


class Listeners(DeviceListeners):
    def __init__(self, ports, listening=None, paused=False):
        self.applied = []
        self.listening = listening
        DeviceListeners.__init__(self, ports, self.applied.append, self._listening, lambda: paused, InstantLoop(),
                                 wait=0.2)

    def _listening(self, port):
        return self.listening is None or port in self.listening


def test_ports_are_assigned_once_per_serial_and_kept_in_the_data_folder(tmp_path):
    ports = ports_in(tmp_path)
    assert ports.assign(XIAOMI) == 8892
    assert ports.assign(SAMSUNG) == 8893
    assert ports.assign(XIAOMI) == 8892
    assert ports.device_of(8893) == SAMSUNG and ports.device_of(8890) is None
    reloaded = ports_in(tmp_path)
    assert reloaded.mapping() == {XIAOMI: 8892, SAMSUNG: 8893}
    assert json.load(open(os.path.join(str(tmp_path), FILE_NAME))) == {"ports": {XIAOMI: 8892, SAMSUNG: 8893}}


def test_assignment_skips_busy_and_own_ports(tmp_path):
    ports = DevicePorts(str(tmp_path), 9000, 9002, lambda port: port != 9001)
    assert ports.assign(XIAOMI) == 9003


def test_broken_or_foreign_entries_are_dropped(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"ports": {XIAOMI: 8891, "-bad": 8900, SAMSUNG: 8900, "x": True,
                                                            "y": 8900}}))
    assert ports_in(tmp_path).mapping() == {SAMSUNG: 8900}
    (tmp_path / FILE_NAME).write_text("{")
    assert ports_in(tmp_path).mapping() == {}


def test_busy_port_at_start_moves_the_device(tmp_path):
    ports_in(tmp_path).assign(XIAOMI)
    moved = ports_in(tmp_path, busy=(8892,))
    assert moved.check_free() == [(8892, 8893)]
    assert moved.port_of(XIAOMI) == 8893


def test_device_count_and_serial_form_are_limited(tmp_path):
    ports = ports_in(tmp_path)
    for index in range(MAX_DEVICES):
        ports.assign("dev{0}".format(index))
    with pytest.raises(DevicePortError):
        ports.assign("one-more")
    with pytest.raises(DevicePortError):
        ports.assign("-s")


def test_client_is_mapped_by_its_local_port(tmp_path):
    ports = ports_in(tmp_path)
    ports.assign(XIAOMI)
    assert ports.device_of_client(SimpleNamespace(sockname=("127.0.0.1", 8892))) == XIAOMI
    assert ports.device_of_client(SimpleNamespace(sockname=("127.0.0.1", 8890))) is None
    assert ports.device_of_client(SimpleNamespace(sockname=None)) is None
    assert ports.device_of_client(None) is None


def test_listeners_open_a_new_port_and_wait_for_it(tmp_path):
    listeners = Listeners(ports_in(tmp_path))
    assert listeners.ensure(XIAOMI) == 8892
    assert listeners.ensure(SAMSUNG) == 8893
    assert listeners.applied == [[8892], [8892, 8893]]
    assert listeners.ensure(XIAOMI) == 8892
    assert len(listeners.applied) == 2


def test_listeners_move_a_device_off_a_port_taken_by_another_program(tmp_path):
    busy = set()
    ports = DevicePorts(str(tmp_path), 8890, 8891, lambda port: port not in busy)
    ports.assign(XIAOMI)
    busy.add(8892)
    listeners = Listeners(ports, listening={8893})
    assert listeners.ensure(XIAOMI) == 8893
    assert listeners.applied[-1] == [8893]


def test_listeners_report_a_port_that_never_opens(tmp_path):
    listeners = Listeners(ports_in(tmp_path), listening=set())
    with pytest.raises(DevicePortError, match="did not start listening on port 8892"):
        listeners.ensure(XIAOMI)


def test_paused_listeners_do_not_wait(tmp_path):
    listeners = Listeners(ports_in(tmp_path), listening=set(), paused=True)
    assert listeners.ensure(XIAOMI) == 8892


def two_devices():
    return FakeAdb({XIAOMI: "device", SAMSUNG: "device"})


def control_with_ports(adb, tmp_path, listening=True):
    return DeviceControl(lambda: Settings(), lambda: listening, runner=adb, finder=lambda path: "adb",
                         ports=Listeners(ports_in(tmp_path)))


def test_each_device_gets_its_own_port_on_the_pc(tmp_path):
    adb = two_devices()
    control = control_with_ports(adb, tmp_path)
    first = control.apply("enable", XIAOMI)
    second = control.apply("enable", SAMSUNG)
    assert (first["pc_port"], second["pc_port"]) == (8892, 8893)
    assert first["ready"] and second["ready"]
    assert adb.proxy == {XIAOMI: "127.0.0.1:8890", SAMSUNG: "127.0.0.1:8890"}
    assert adb.targets == {(XIAOMI, 8890): 8892, (SAMSUNG, 8890): 8893}
    assert control.apply("status", XIAOMI)["ready"] is True
    off = control.apply("disable", XIAOMI)
    assert off["pc_port"] == 8892 and not off["reverse_active"] and off["device_proxy"] is None
    assert control.apply("status", SAMSUNG)["ready"] is True


def test_reverse_left_on_the_shared_port_is_not_ready(tmp_path):
    adb = two_devices()
    control = control_with_ports(adb, tmp_path)
    control.apply("enable", XIAOMI)
    adb.targets[(XIAOMI, 8890)] = 8890
    state = control.apply("status", XIAOMI)
    assert state["ready"] is False and "port 8892" in state["hint"]
    assert control.apply("enable", XIAOMI)["ready"] is True


def test_status_of_a_device_without_a_port_reads_the_shared_reverse(tmp_path):
    adb = two_devices()
    adb.proxy[XIAOMI] = "127.0.0.1:8890"
    adb.reverse[XIAOMI] = [8890]
    state = control_with_ports(adb, tmp_path).apply("status", XIAOMI)
    assert state["pc_port"] == 8890 and state["ready"] is True


def test_restore_after_charles_uses_the_device_port(tmp_path):
    adb = two_devices()
    control = control_with_ports(adb, tmp_path)
    control.apply("enable", XIAOMI)
    control.apply("enable", SAMSUNG)
    assert sorted(control.release()) == sorted([XIAOMI, SAMSUNG])
    assert adb.proxy == {XIAOMI: ":0", SAMSUNG: ":0"}
    assert control.restore([SAMSUNG]) == [SAMSUNG]
    assert adb.proxy[SAMSUNG] == "127.0.0.1:8890" and adb.targets[(SAMSUNG, 8890)] == 8893
    assert adb.proxy[XIAOMI] == ":0"


def test_timeout_advises_reconnecting_one_device(monkeypatch):
    def stuck(*args, **kwargs):
        raise device.subprocess.TimeoutExpired("adb", 15)
    monkeypatch.setattr(device.subprocess, "run", stuck)
    with pytest.raises(DeviceError) as caught:
        device.run_command(["adb", "-s", XIAOMI, "shell", "true"])
    assert "adb -s {0} reconnect".format(XIAOMI) in str(caught.value)
    assert "kill-server would" not in str(caught.value) and "run adb kill-server" not in str(caught.value)
    with pytest.raises(DeviceError) as caught:
        device.run_command(["adb", "devices"])
    assert "adb -s <serial> reconnect" in str(caught.value)


def test_offline_device_gets_the_same_advice():
    adb = FakeAdb({XIAOMI: "offline"})
    control = DeviceControl(lambda: Settings(), lambda: True, runner=adb, finder=lambda path: "adb")
    with pytest.raises(DeviceError, match="adb -s {0} reconnect".format(XIAOMI)):
        control.apply("status", XIAOMI)


def test_rule_device_scope_limits_matching(tmp_path):
    rules = RuleSet(str(tmp_path))
    rules.add({"action": "respond", "host": "api.example.com", "device": XIAOMI})
    rules.add({"action": "delay", "host": "api.example.com", "delay_ms": 10})
    plan = rules.plan("api.example.com", "/", "GET", "https://api.example.com/", XIAOMI)
    assert [rule["action"] for rule in plan] == ["respond"]
    plan = rules.plan("api.example.com", "/", "GET", "https://api.example.com/", SAMSUNG)
    assert [rule["action"] for rule in plan] == ["delay"]
    assert [rule["action"] for rule in rules.plan("api.example.com", "/", "GET", "https://api.example.com/")] == [
        "delay"]
    assert rules.may_answer("api.example.com", XIAOMI) is True
    assert rules.may_answer("api.example.com", SAMSUNG) is False


def test_speed_and_event_rules_respect_the_device(tmp_path):
    rules = RuleSet(str(tmp_path))
    rules.add({"action": "delay", "host": "cdn.example.com", "download_kbps": 100, "device": SAMSUNG})
    rules.add({"action": "events", "host": "chat.example.com", "close": "before", "device": SAMSUNG})
    assert rules.needs_http1("cdn.example.com", SAMSUNG) is True
    assert rules.needs_http1("cdn.example.com", XIAOMI) is False
    assert rules.plan_events("chat.example.com", "/", "GET", "https://chat.example.com/", device=XIAOMI) == []
    assert len(rules.plan_events("chat.example.com", "/", "GET", "https://chat.example.com/", device=SAMSUNG)) == 1


def test_clear_with_device_keeps_rules_of_others(tmp_path):
    rules = RuleSet(str(tmp_path))
    first = rules.add({"action": "drop", "host": "a", "device": XIAOMI})["id"]
    second = rules.add({"action": "drop", "host": "b", "device": SAMSUNG})["id"]
    common = rules.add({"action": "drop", "host": "c"})["id"]
    assert rules.clear(XIAOMI) == [first]
    assert [rule["id"] for rule in RuleSet(str(tmp_path)).list()] == [second, common]
    assert rules.clear() == [second, common]


def test_device_field_is_checked_and_scopes_shadowing(tmp_path):
    with pytest.raises(RuleError, match="adb serial"):
        normalize({"action": "drop", "host": "a", "device": "bad serial"})
    with pytest.raises(RuleError, match="at least one condition"):
        normalize({"action": "drop", "device": XIAOMI})
    rules = RuleSet(str(tmp_path))
    scoped = rules.add({"action": "respond", "host": "a", "device": XIAOMI})["id"]
    other = rules.add({"action": "respond", "host": "a", "device": SAMSUNG})["id"]
    everyone = rules.add({"action": "respond", "host": "a"})["id"]
    assert rules.shadowed_by(other) is None
    assert rules.shadowed_by(everyone) is None
    assert rules.shadowing(scoped) == []
    assert describe_rule(rules.get(scoped)) == "respond, host a, one device"


def test_store_keeps_device_filters_and_clears_by_it(store):
    store.insert(make_record(1, device=XIAOMI))
    store.insert(make_record(2, device=SAMSUNG))
    store.insert(make_record(3))
    assert [flow["seq"] for flow in store.query(FlowFilter(device=XIAOMI), 10)] == [1]
    assert [flow["device"] for flow in store.query(FlowFilter(), 10)] == [None, SAMSUNG, XIAOMI]
    assert FlowFilter(device=SAMSUNG).matches(store.summary(2)) and not FlowFilter(device=SAMSUNG).matches(
        store.summary(1))
    assert len(store.activity(10, SAMSUNG)) == 1 and len(store.activity(10)) == 3
    assert store.get("2")["device"] == SAMSUNG
    assert store.clear(XIAOMI) == 1
    assert store.count() == 2


def test_history_without_device_column_gets_it(tmp_path):
    path = str(tmp_path / "old.db")
    store = FlowStore(path, 100)
    store.insert(make_record(1))
    store.close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE flows DROP COLUMN device")
    db.commit()
    db.close()
    reopened = FlowStore(path, 100)
    assert reopened.summary(1)["device"] is None
    reopened.insert(make_record(2, device=XIAOMI))
    assert reopened.summary(2)["device"] == XIAOMI
    reopened.close()


def test_record_takes_the_device_given(tmp_path):
    from mitmproxy.test import tflow
    flow = tflow.tflow(resp=True)
    assert build_record(flow, 1024, device=XIAOMI).device == XIAOMI
    assert build_record(flow, 1024).device is None


def test_guard_covers_device_ports(tmp_path):
    ports = ports_in(tmp_path)
    guard = OwnPortsGuard(8890, 8891, ports.ports)
    assert guard.ports == {8890, 8891}
    ports.assign(XIAOMI)
    assert guard.ports == {8890, 8891, 8892}


@pytest.fixture
def device_service(store, tmp_path):
    return ProxyService(store, Settings(), str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)),
                        ports_in(tmp_path))


def tool(service, name, arguments):
    return asyncio.run(call_tool(service, name, arguments))


def test_tools_filter_and_clear_by_device(device_service, store):
    store.insert(make_record(1, device=XIAOMI))
    store.insert(make_record(2, device=SAMSUNG))
    store.insert(make_record(3))
    assert [flow["seq"] for flow in tool(device_service, "list_flows", {"device": XIAOMI})["flows"]] == [1]
    with pytest.raises(ToolError, match="adb serial"):
        tool(device_service, "list_flows", {"device": "-x"})
    assert tool(device_service, "clear_flows", {"device": SAMSUNG}) == {"removed": 1}
    assert [flow["seq"] for flow in tool(device_service, "list_flows", {})["flows"]] == [3, 1]


def test_wait_for_flow_ignores_other_devices(device_service, store):
    async def scenario():
        waiting = asyncio.ensure_future(call_tool(device_service, "wait_for_flow", {"device": SAMSUNG,
                                                                                    "timeout_seconds": 5}))
        await asyncio.sleep(0.05)
        store.insert(make_record(1, device=XIAOMI))
        store.insert(make_record(2))
        store.insert(make_record(3, device=SAMSUNG))
        return await waiting
    assert asyncio.run(scenario())["flow"]["seq"] == 3


def test_stream_of_another_device_is_refused(device_service, store):
    store.insert(make_record(1, device=XIAOMI, kind="sse", open=True))
    assert tool(device_service, "list_events", {"flow": "1", "device": XIAOMI})["flow"]["seq"] == 1
    with pytest.raises(ToolError, match="not a flow of device"):
        tool(device_service, "list_events", {"flow": "1", "device": SAMSUNG})
    with pytest.raises(ToolError, match="not a flow of device"):
        tool(device_service, "wait_for_event", {"flow": "1", "device": SAMSUNG, "timeout_seconds": 1})


def test_delete_rule_all_with_device(device_service):
    tool(device_service, "add_rule", {"action": "drop", "host": "a", "device": XIAOMI})
    tool(device_service, "add_rule", {"action": "drop", "host": "b"})
    with pytest.raises(ToolError, match="device goes with all=true"):
        tool(device_service, "delete_rule", {"id": "r1", "device": XIAOMI})
    assert tool(device_service, "delete_rule", {"all": True, "device": XIAOMI})["removed"] == ["r1"]
    assert [rule["id"] for rule in tool(device_service, "list_rules", {})["rules"]] == ["r2"]


def test_rule_answers_count_history_of_its_device_and_hint(device_service, store):
    store.insert(make_record(1, device=XIAOMI))
    store.insert(make_record(2, device=SAMSUNG))
    answer = tool(device_service, "add_rule", {"action": "drop", "host": "api.example.com", "device": XIAOMI})
    assert answer["history_matches"] == 1
    assert "has no port of its own yet" in answer["hint"]
    device_service.ports.assign(XIAOMI)
    device_service.ports.assign(SAMSUNG)
    assert "has no port" not in tool(device_service, "update_rule", {"id": "r1", "name": "x"}).get("hint", "")
    answer = tool(device_service, "add_rule", {"action": "delay", "host": "api.example.com", "delay_ms": 5})
    assert answer["history_matches"] == 2 and "set device" in answer["hint"]


def test_moving_a_rule_to_another_device_drops_it_from_open_streams(device_service):
    changes = []
    device_service.events = SimpleNamespace(change_rule=lambda rule_id, rule: changes.append(rule))
    device_service.speeds = SimpleNamespace(change_rule=lambda rule_id, limits: changes.append(limits))
    tool(device_service, "add_rule", {"action": "events", "host": "a", "close": "before", "device": XIAOMI})
    tool(device_service, "update_rule", {"id": "r1", "device": SAMSUNG})
    assert changes == [None, None]
    tool(device_service, "update_rule", {"id": "r1", "name": "same device"})
    assert changes[-1]["name"] == "same device"


def test_status_lists_device_ports(device_service):
    device_service.ports.assign(XIAOMI)
    assert tool(device_service, "proxy_status", {})["proxy"]["device_ports"] == {XIAOMI: 8892}


def test_check_connection_counts_only_the_device(device_service, store):
    import time
    now = time.time()
    store.insert(make_record(1, device=XIAOMI, started=now))
    store.insert(make_record(2, device=SAMSUNG, started=now, host="samsung.example.com"))
    answer = tool(device_service, "check_connection", {"serial": SAMSUNG, "device": False})
    hosts = json.dumps(answer["traffic"])
    assert "samsung.example.com" in hosts and "api.example.com" not in hosts
