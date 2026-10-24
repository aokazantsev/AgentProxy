import asyncio
import json
import os
import sys

import pytest

from agentproxy import connection, settings as settings_module
from agentproxy.charles import CharlesYield, charles_running, process_names
from agentproxy.claude import hooks
from agentproxy.device import DeviceControl, DeviceError
from agentproxy.rules import RuleSet
from agentproxy.service import ProxyService, ToolError
from agentproxy.settings import Settings

from test_connection import check
from test_device import FakeAdb

ADDRESS = "127.0.0.1:18890"


@pytest.fixture
def service(store, settings, tmp_path):
    proxy = ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))
    proxy.proxy_running = True
    return proxy


def attach(service, adb):
    service.device = DeviceControl(lambda: Settings(proxy_port=18890), lambda: service.proxy_running, runner=adb,
                                   finder=lambda path: "adb")
    return adb


class Switch:
    def __init__(self):
        self.listening = True
        self.calls = []

    def set(self, on):
        self.calls.append(on)
        self.listening = on

    def is_listening(self):
        return self.listening


def watcher(service, switch, running=False):
    state = {"running": running}
    yielder = CharlesYield(service, switch.set, switch.is_listening, detect=lambda: state["running"], interval=0.01,
                           resume_wait=0.2)
    return yielder, state


def test_process_list_has_this_process():
    names = process_names()
    assert os.path.basename(sys.executable).lower() in names
    assert not charles_running()


def test_charles_is_found_by_name_in_any_case():
    assert charles_running(lambda: {"explorer.exe", "charles.exe"})
    assert not charles_running(lambda: {"charlesproxy.exe", "python.exe"})


def test_setting_is_off_by_default_and_read_from_file(tmp_path):
    assert Settings().yield_to_charles is False
    (tmp_path / "settings.json").write_text(json.dumps({"yield_to_charles": True}))
    current, problems = settings_module.load(str(tmp_path))
    assert current.yield_to_charles is True and problems == []


def test_pause_stops_listening_and_takes_only_own_device_proxy_off(service):
    adb = attach(service, FakeAdb({"emu-1": "device", "emu-2": "device", "emu-3": "offline"}))
    adb.proxy.update({"emu-1": ADDRESS, "emu-2": "192.168.1.49:8888", "emu-3": ADDRESS})
    adb.reverse["emu-1"] = [18890]
    switch = Switch()
    yielder, _ = watcher(service, switch)
    asyncio.run(yielder.pause())
    assert switch.calls == [False]
    assert service.paused_for == "charles" and service.proxy_running is False
    assert adb.proxy["emu-1"] == ":0" and adb.reverse["emu-1"] == []
    assert adb.proxy["emu-2"] == "192.168.1.49:8888"
    assert adb.proxy["emu-3"] == ADDRESS
    assert yielder.released == ["emu-1"]


def test_resume_listens_again_and_returns_released_device_only_if_still_off(service):
    adb = attach(service, FakeAdb({"emu-1": "device", "emu-2": "device"}))
    adb.proxy.update({"emu-1": ADDRESS, "emu-2": ADDRESS})
    switch = Switch()
    yielder, _ = watcher(service, switch)
    asyncio.run(yielder.pause())
    adb.proxy["emu-2"] = "192.168.1.49:8888"
    asyncio.run(yielder.resume())
    assert switch.calls == [False, True]
    assert service.paused_for is None and service.proxy_running is True
    assert adb.proxy["emu-1"] == ADDRESS and adb.reverse["emu-1"] == [18890]
    assert adb.proxy["emu-2"] == "192.168.1.49:8888"
    assert yielder.released == []


def test_resume_without_listening_leaves_devices_off(service):
    adb = attach(service, FakeAdb())
    adb.proxy["emu-1"] = ADDRESS
    switch = Switch()
    yielder = CharlesYield(service, lambda on: switch.calls.append(on), lambda: False, detect=lambda: False,
                           resume_wait=0.1)
    asyncio.run(yielder.pause())
    asyncio.run(yielder.resume())
    assert adb.proxy["emu-1"] == ":0"
    assert service.paused_for is None and yielder.released == []


def test_missing_adb_does_not_stop_the_pause(service):
    def missing(path):
        raise DeviceError("adb not found", "adb not found")
    service.device = DeviceControl(lambda: Settings(proxy_port=18890), lambda: True, runner=FakeAdb(), finder=missing)
    switch = Switch()
    yielder, _ = watcher(service, switch)
    asyncio.run(yielder.pause())
    asyncio.run(yielder.resume())
    assert switch.calls == [False, True] and service.paused_for is None


def test_watch_loop_follows_charles_and_keeps_state_when_check_fails(service):
    attach(service, FakeAdb({}))
    switch = Switch()
    yielder, state = watcher(service, switch)

    async def scenario():
        task = asyncio.ensure_future(yielder.run())
        await asyncio.sleep(0.05)
        assert service.paused_for is None
        state["running"] = True
        await asyncio.sleep(0.1)
        assert service.paused_for == "charles"

        def broken():
            raise OSError("snapshot failed")
        yielder.detect = broken
        await asyncio.sleep(0.05)
        assert service.paused_for == "charles"
        yielder.detect = lambda: False
        await asyncio.sleep(0.1)
        task.cancel()
    asyncio.run(scenario())
    assert switch.calls == [False, True] and service.paused_for is None


def test_paused_at_start_releases_devices_on_first_run(service):
    adb = attach(service, FakeAdb())
    adb.proxy["emu-1"] = ADDRESS
    service.paused_for = "charles"
    switch = Switch()
    yielder, _ = watcher(service, switch, running=True)

    async def scenario():
        task = asyncio.ensure_future(yielder.run())
        await asyncio.sleep(0.05)
        task.cancel()
    asyncio.run(scenario())
    assert adb.proxy["emu-1"] == ":0" and yielder.released == ["emu-1"]


def test_status_connection_and_device_proxy_report_the_pause(service):
    attach(service, FakeAdb())
    service.paused_for = "charles"
    service.proxy_running = False
    assert service.status()["proxy"]["paused"] == "charles"
    answer = check(service)
    assert answer["verdict"] == "paused_for_charles"
    assert answer["proxy"]["paused"] == "charles"
    assert answer["hints"] == [connection.PAUSED_FOR_CHARLES]
    with pytest.raises(ToolError) as refused:
        asyncio.run(service.device_proxy({"action": "enable"}))
    assert "Charles" in str(refused.value)
    assert asyncio.run(service.device_proxy({"action": "status"}))["serial"] == "emu-1"


def test_session_hook_says_the_proxy_waits_for_charles():
    env = type("Env", (), {"proxy_port": 8890, "api_port": 8891, "install_dir": "x", "data_dir": "y"})()
    text = hooks.describe(env, {"proxy": {"running": False, "paused": "charles"}, "flows": 3})
    assert "пока запущен Charles" in text and "не слушает" not in text


def test_disable_during_pause_is_not_undone_when_charles_closes(service):
    adb = attach(service, FakeAdb())
    adb.proxy["emu-1"] = ADDRESS
    switch = Switch()
    yielder, _ = watcher(service, switch)
    service.charles = yielder
    asyncio.run(yielder.pause())
    asyncio.run(service.device_proxy({"action": "disable"}))
    asyncio.run(yielder.resume())
    assert adb.proxy["emu-1"] == ":0" and yielder.released == []


def test_probe_skips_the_port_while_paused_and_drops_a_result_older_than_a_switch(service, monkeypatch):
    from agentproxy import engine
    probes = []

    def listening(host, port):
        probes.append(port)
        service.listen_epoch += 1
        return False

    async def scenario():
        monkeypatch.setattr(engine, "proxy_listening", listening)
        monkeypatch.setattr(engine, "PROBE_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(engine, "STARTUP_PROBE_SECONDS", 0.01)
        service.proxy_running = True
        task = asyncio.ensure_future(engine.probe_proxy(service))
        await asyncio.sleep(0.05)
        assert probes and service.proxy_running is True
        service.paused_for = "charles"
        await asyncio.sleep(0.02)
        count = len(probes)
        await asyncio.sleep(0.05)
        assert len(probes) == count and service.proxy_running is False
        task.cancel()
    asyncio.run(scenario())


def test_resume_skips_a_device_unplugged_during_the_pause(service):
    adb = attach(service, FakeAdb())
    adb.proxy["emu-1"] = ADDRESS
    switch = Switch()
    yielder, _ = watcher(service, switch)
    asyncio.run(yielder.pause())
    del adb.devices["emu-1"]
    adb.calls.clear()
    asyncio.run(yielder.resume())
    assert not any("put" in call for call in adb.calls)
    assert yielder.released == [] and service.proxy_running is True


def test_resume_finishes_when_adb_is_gone_after_the_pause(service):
    adb = attach(service, FakeAdb())
    adb.proxy["emu-1"] = ADDRESS
    switch = Switch()
    yielder, _ = watcher(service, switch)
    asyncio.run(yielder.pause())

    def missing(path):
        raise DeviceError("adb not found", "adb not found")
    service.device.finder = missing
    asyncio.run(yielder.resume())
    assert service.paused_for is None and service.proxy_running is True and yielder.released == []


def test_failed_process_check_warns_once_and_counts_as_no_charles_at_start(service, monkeypatch, caplog):
    from agentproxy import engine

    def broken():
        raise OSError("snapshot failed")
    monkeypatch.setattr(engine, "charles_running", broken)
    assert engine.charles_at_start() is False
    switch = Switch()
    yielder = CharlesYield(service, switch.set, switch.is_listening, detect=broken, interval=0.01)

    async def scenario():
        task = asyncio.ensure_future(yielder.run())
        await asyncio.sleep(0.08)
        task.cancel()
    with caplog.at_level("WARNING", logger="agentproxy.charles"):
        asyncio.run(scenario())
    assert len([record for record in caplog.records if record.name == "agentproxy.charles"]) == 1
    assert switch.calls == []


def test_unexpected_error_in_a_check_keeps_the_watch_alive(service, caplog):
    attach(service, FakeAdb({}))
    switch = Switch()
    yielder, state = watcher(service, switch)
    failures = []

    def flaky():
        if not failures:
            failures.append(1)
            raise RuntimeError("boom")
        return state["running"]
    yielder.detect = flaky

    async def scenario():
        task = asyncio.ensure_future(yielder.run())
        await asyncio.sleep(0.03)
        state["running"] = True
        await asyncio.sleep(0.05)
        task.cancel()
    with caplog.at_level("ERROR", logger="agentproxy.charles"):
        asyncio.run(scenario())
    assert service.paused_for == "charles"
    assert any("check step failed" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("enabled", [True, False])
def test_serve_builds_a_silent_master_when_charles_runs_at_start(tmp_path, monkeypatch, enabled):
    from agentproxy import engine
    seen = {}

    class Stop(Exception):
        pass

    def fake(current, cert_dir, listening=True):
        seen["listening"] = listening
        raise Stop()
    monkeypatch.setattr(engine, "charles_at_start", lambda: True)
    monkeypatch.setattr(engine, "create_master", fake)
    with pytest.raises(Stop):
        asyncio.run(engine.serve(Settings(yield_to_charles=enabled), str(tmp_path), 0))
    assert seen["listening"] is (not enabled)
    assert engine.proxy_options(Settings(), str(tmp_path), listening=False).server is False


def test_pause_and_resume_move_the_listen_epoch(service):
    attach(service, FakeAdb({}))
    switch = Switch()
    yielder, _ = watcher(service, switch)
    start = service.listen_epoch
    asyncio.run(yielder.pause())
    assert service.listen_epoch == start + 1
    asyncio.run(yielder.resume())
    assert service.listen_epoch == start + 3


def test_status_wait_stops_at_once_on_a_paused_engine(monkeypatch, capsys):
    from agentproxy import cli
    calls = []
    monkeypatch.setattr(cli.hooks, "fetch_status", lambda env: calls.append(1) or {"proxy": {"paused": "charles"}})
    monkeypatch.setattr(cli.integration, "status", lambda env: {})
    assert cli.command_status(type("Args", (), {"wait": 5})()) == 0
    assert len(calls) == 1
