import asyncio
import json
import logging
import os
import sys

import pytest

from agentproxy import device, settings as settings_module
from agentproxy.device import DeviceControl, DeviceError, find_adb, parse_devices
from agentproxy.rules import RuleSet
from agentproxy.service import ProxyService, ToolError, call_tool, public_tools
from agentproxy.settings import Settings


class FakeAdb:
    def __init__(self, devices=None, crlf=False):
        self.devices = devices if devices is not None else {"emu-1": "device"}
        self.crlf = crlf
        self.proxy = {}
        self.reverse = {}
        self.calls = []
        self.fail = {}

    def __call__(self, command):
        code, text = self.answer(command)
        return code, text.replace("\n", "\r\n") if self.crlf else text

    def answer(self, command):
        self.calls.append(command)
        args = command[1:]
        serial = None
        if args[:1] == ["-s"]:
            serial, args = args[1], args[2:]
        joined = " ".join(args)
        for key, message in self.fail.items():
            if joined.startswith(key):
                return 1, message
        if args == ["devices"]:
            lines = "".join("{0}\t{1}\n".format(name, state) for name, state in self.devices.items())
            return 0, "* daemon started successfully\nList of devices attached\n" + lines
        if args[:4] == ["shell", "settings", "get", "global"]:
            return 0, self.proxy.get(serial, "null") + "\n"
        if args[:4] == ["shell", "settings", "put", "global"]:
            self.proxy[serial] = args[5]
            return 0, ""
        if args[:2] == ["reverse", "--list"]:
            prefix = "UsbFfs" if self.crlf else "host-19"
            return 0, "".join("{0} tcp:{1} tcp:{1}\n".format(prefix, port) for port in self.reverse.get(serial, []))
        if args[:2] == ["reverse", "--remove"]:
            port = int(args[2].split(":")[1])
            if port not in self.reverse.get(serial, []):
                return 1, "adb: error: listener 'tcp:{0}' not found".format(port)
            self.reverse[serial].remove(port)
            return 0, ""
        if args[0] == "reverse":
            self.reverse.setdefault(serial, []).append(int(args[1].split(":")[1]))
            return 0, "8890\n"
        return 1, "unknown"


def control(adb, port=8890, listening=True):
    return DeviceControl(lambda: Settings(proxy_port=port), lambda: listening, runner=adb, finder=lambda path: "adb")


def test_enable_sets_reverse_and_proxy_and_reports_state():
    adb = FakeAdb()
    answer = control(adb).apply("enable", None)
    assert adb.proxy["emu-1"] == "127.0.0.1:8890"
    assert adb.reverse["emu-1"] == [8890]
    assert answer["serial"] == "emu-1"
    assert answer["previous_proxy"] is None
    assert answer["device_proxy"] == "127.0.0.1:8890"
    assert answer["reverse_active"] is True and answer["ready"] is True and answer["changed"] is True


def test_enable_uses_configured_port_and_returns_previous_proxy():
    adb = FakeAdb()
    adb.proxy["emu-1"] = "10.0.0.5:3128"
    answer = control(adb, port=9001).apply("enable", None)
    assert adb.proxy["emu-1"] == "127.0.0.1:9001"
    assert adb.reverse["emu-1"] == [9001]
    assert answer["previous_proxy"] == "10.0.0.5:3128"


def test_enable_twice_changes_nothing_second_time():
    adb = FakeAdb()
    tool = control(adb)
    tool.apply("enable", None)
    assert tool.apply("enable", None)["changed"] is False


def test_disable_clears_proxy_and_reverse():
    adb = FakeAdb()
    tool = control(adb)
    tool.apply("enable", None)
    answer = tool.apply("disable", None)
    assert adb.proxy["emu-1"] == ":0"
    assert adb.reverse["emu-1"] == []
    assert answer["previous_proxy"] == "127.0.0.1:8890"
    assert answer["device_proxy"] is None and answer["reverse_active"] is False and answer["hint"] is None


def test_disable_without_reverse_is_fine():
    answer = control(FakeAdb()).apply("disable", None)
    assert answer["device_proxy"] is None


def test_disable_fails_loudly_on_other_reverse_error():
    adb = FakeAdb()
    adb.fail["reverse --remove"] = "error: closed"
    with pytest.raises(DeviceError, match="reverse --remove failed"):
        control(adb).apply("disable", None)


def test_status_changes_nothing():
    adb = FakeAdb()
    answer = control(adb).apply("status", None)
    assert answer["ready"] is False and answer["changed"] is False
    assert "run enable" in answer["hint"]
    assert adb.proxy == {} and adb.reverse == {}


def test_status_hint_for_foreign_proxy_and_missing_reverse():
    adb = FakeAdb()
    adb.proxy["emu-1"] = "10.0.0.5:3128"
    assert "another proxy" in control(adb).apply("status", None)["hint"]
    adb.proxy["emu-1"] = "127.0.0.1:8890"
    assert "adb reverse is missing" in control(adb).apply("status", None)["hint"]


def test_failed_proxy_write_rolls_back_reverse():
    adb = FakeAdb()
    adb.fail["shell settings put"] = "Permission denied"
    with pytest.raises(DeviceError, match="Permission denied"):
        control(adb).apply("enable", None)
    assert adb.reverse["emu-1"] == []


def test_failed_proxy_write_keeps_reverse_that_was_there_before():
    adb = FakeAdb()
    adb.reverse["emu-1"] = [8890]
    adb.fail["shell settings put"] = "Permission denied"
    with pytest.raises(DeviceError):
        control(adb).apply("enable", None)
    assert adb.reverse["emu-1"] == [8890, 8890]


def test_enable_is_refused_while_the_proxy_does_not_listen():
    adb = FakeAdb()
    with pytest.raises(DeviceError, match="not listening on port 8890.*proxy_status"):
        control(adb, listening=False).apply("enable", None)
    assert adb.proxy == {} and adb.reverse == {}


def test_status_while_the_proxy_does_not_listen_is_not_ready():
    adb = FakeAdb()
    control(adb).apply("enable", None)
    answer = control(adb, listening=False).apply("status", None)
    assert answer["proxy_listening"] is False and answer["ready"] is False
    assert "not listening" in answer["hint"]
    assert control(adb, listening=False).apply("disable", None)["device_proxy"] is None


def test_service_passes_the_listening_state(service):
    adb = FakeAdb()
    service.device.runner = adb
    service.device.finder = lambda path: "adb"
    service.proxy_running = False
    with pytest.raises(ToolError, match="not listening"):
        asyncio.run(call_tool(service, "device_proxy", {"action": "enable"}))
    service.proxy_running = True
    assert asyncio.run(call_tool(service, "device_proxy", {"action": "enable"}))["ready"] is True


def test_no_device_names_next_step():
    with pytest.raises(DeviceError, match="no device: connect"):
        control(FakeAdb({})).apply("status", None)


def test_several_devices_need_serial_and_serial_selects():
    adb = FakeAdb({"a1": "device", "b2": "device"})
    with pytest.raises(DeviceError, match="pass serial"):
        control(adb).apply("enable", None)
    control(adb).apply("enable", "b2")
    assert adb.proxy == {"b2": "127.0.0.1:8890"}
    assert all(call[1:3] == ["-s", "b2"] for call in adb.calls[2:])


def test_unknown_serial_lists_connected():
    with pytest.raises(DeviceError, match=r"not connected: pass one of the connected devices \(emu-1\)"):
        control(FakeAdb()).apply("status", "zzz")


def test_unauthorized_and_offline_devices_are_named():
    with pytest.raises(DeviceError, match="accept the debugging prompt"):
        control(FakeAdb({"a1": "unauthorized"})).apply("status", None)
    with pytest.raises(DeviceError, match="offline"):
        control(FakeAdb({"a1": "offline"})).apply("status", "a1")


def test_bad_serial_and_action_are_refused_before_adb():
    adb = FakeAdb()
    with pytest.raises(DeviceError):
        control(adb).apply("status", "-s evil")
    with pytest.raises(DeviceError):
        control(adb).apply("reboot", None)
    assert adb.calls == []


def test_parse_devices_skips_daemon_noise():
    text = ("* daemon not running\n* daemon started successfully\nList of devices attached\n"
            "R58\tdevice\n192.168.1.5:5555\toffline\n\n")
    assert parse_devices(text) == [("R58", "device"), ("192.168.1.5:5555", "offline")]


def test_find_adb_prefers_configured_then_path_then_sdk(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk" / "platform-tools"
    sdk.mkdir(parents=True)
    exe = sdk / ("adb.exe" if os.name == "nt" else "adb")
    exe.write_text("x")
    monkeypatch.setattr(device.shutil, "which", lambda name: None)
    assert find_adb("", {"ANDROID_HOME": str(tmp_path / "sdk")}) == str(exe)
    assert find_adb(str(exe), {}) == str(exe)
    other = tmp_path / "pt"
    other.mkdir()
    on_path = other / "adb"
    on_path.write_text("x")
    monkeypatch.setattr(device.shutil, "which", lambda name: str(on_path))
    assert find_adb("", {"ANDROID_HOME": str(tmp_path / "sdk")}) == str(on_path)


def test_find_adb_missing_explains_next_step(monkeypatch, tmp_path):
    monkeypatch.setattr(device.shutil, "which", lambda name: None)
    with pytest.raises(DeviceError, match="adb not found.*adb_path"):
        find_adb("", {"LOCALAPPDATA": str(tmp_path)})
    with pytest.raises(DeviceError, match="forward slashes.*restart it from the tray"):
        find_adb("", {})
    with pytest.raises(DeviceError, match="adb_path in settings.json points to a missing file"):
        find_adb(str(tmp_path / "nope.exe"), {})


def test_run_command_returns_output_and_code():
    code, text = device.run_command(
        [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"])
    assert code == 3 and "out" in text and "err" in text


def test_run_command_reports_missing_program_and_timeout(tmp_path):
    with pytest.raises(DeviceError, match="could not be started"):
        device.run_command([str(tmp_path / "missing.exe")])
    with pytest.raises(DeviceError, match="did not answer"):
        device.run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)


def test_adb_path_setting_is_a_string():
    parsed, problems = settings_module.parse({"adb_path": "  C:\\tools\\adb.exe "})
    assert parsed.adb_path == "C:\\tools\\adb.exe" and problems == []
    parsed, problems = settings_module.parse({"adb_path": 5})
    assert parsed.adb_path == "" and "adb_path must be a string" in problems[0]


@pytest.fixture
def service(store, settings, tmp_path):
    return ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))


def test_tool_runs_through_service_and_logs_without_secrets(service, caplog):
    service.device = control(FakeAdb(), port=18890)
    with caplog.at_level(logging.INFO):
        answer = asyncio.run(call_tool(service, "device_proxy", {"action": "enable"}))
    assert answer["ready"] is True
    lines = [record.getMessage() for record in caplog.records if record.name == "agentproxy.device"]
    assert lines == ["device proxy enable on emu-1: proxy 127.0.0.1:18890, reverse on"]


def test_tool_errors_are_tool_errors_and_logged(service, caplog):
    service.device = control(FakeAdb({}))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ToolError, match="no device"):
            asyncio.run(call_tool(service, "device_proxy", {"action": "status"}))
        with pytest.raises(ToolError, match="action is required"):
            asyncio.run(call_tool(service, "device_proxy", {}))
    assert any("device_proxy refused: no device" in record.getMessage() for record in caplog.records)


def test_tool_is_listed_with_required_action():
    tool = [item for item in public_tools() if item["name"] == "device_proxy"][0]
    assert tool["inputSchema"]["required"] == ["action"]
    assert set(tool["inputSchema"]["properties"]["action"]["enum"]) == set(device.ACTIONS)


def test_enable_restoring_a_lost_reverse_reports_changed():
    adb = FakeAdb()
    adb.proxy["emu-1"] = "127.0.0.1:8890"
    answer = control(adb).apply("enable", None)
    assert answer["changed"] is True and answer["reverse_active"] is True


def test_disable_removing_a_leftover_reverse_reports_changed():
    adb = FakeAdb()
    adb.proxy["emu-1"] = ":0"
    adb.reverse["emu-1"] = [8890]
    assert control(adb).apply("disable", None)["changed"] is True


def test_real_adb_output_with_crlf_is_parsed():
    adb = FakeAdb(crlf=True)
    adb.proxy["emu-1"] = ":0"
    adb.reverse["emu-1"] = [8890]
    answer = control(adb).apply("status", None)
    assert answer["previous_proxy"] is None and answer["device_proxy"] is None
    assert answer["reverse_active"] is True and "proxy is off" in answer["hint"]
    answer = control(adb).apply("enable", None)
    assert answer["ready"] is True and answer["changed"] is True
    assert parse_devices("List of devices attached\r\nR58X\tdevice\r\n\r\n") == [("R58X", "device")]


@pytest.mark.parametrize("listed, active", [
    ("UsbFfs tcp:8890 tcp:8890\n", True),
    ("UsbFfs tcp:18890 tcp:18890\n", False),
    ("UsbFfs tcp:8890 tcp:88900\n", False),
    ("UsbFfs tcp:8890 tcp:9000\n", False),
])
def test_reverse_list_matches_only_the_exact_port_pair(listed, active):
    def runner(command):
        if command[1:] == ["devices"]:
            return 0, "emu-1\tdevice\n"
        if command[-2:] == ["reverse", "--list"]:
            return 0, listed
        return 0, ":0\n"
    assert control(runner).apply("status", None)["reverse_active"] is active


def test_failed_reverse_stops_before_the_proxy_is_written():
    adb = FakeAdb()
    adb.fail["reverse tcp:"] = "error: cannot bind listener"
    with pytest.raises(DeviceError, match="cannot bind listener"):
        control(adb).apply("enable", None)
    assert not [call for call in adb.calls if "put" in call]


def test_status_is_not_logged_and_disable_line_has_its_format(caplog):
    tool = control(FakeAdb())
    with caplog.at_level(logging.INFO):
        tool.apply("status", None)
        assert not [record for record in caplog.records if record.name == "agentproxy.device"]
        tool.apply("enable", None)
        tool.apply("disable", None)
    lines = [record.getMessage() for record in caplog.records if record.name == "agentproxy.device"]
    assert lines[-1] == "device proxy disable on emu-1: proxy off, reverse off"


def api_calls(service, adb, requests):
    import tornado.httpclient
    from dataclasses import replace
    from agentproxy.api import make_app
    from conftest import free_port
    port = free_port()
    service.settings = replace(service.settings, api_port=port)
    service.proxy_running = True
    service.device.runner = adb
    service.device.finder = lambda path: "adb"

    async def scenario():
        server = make_app({"service": service}).listen(port, address="127.0.0.1")
        client = tornado.httpclient.AsyncHTTPClient()
        answers = []
        try:
            for method, path, body in requests:
                response = await client.fetch("http://127.0.0.1:{0}{1}".format(port, path), method=method,
                                              body=body, raise_error=False)
                answers.append((response.code, json.loads(response.body)))
            return answers
        finally:
            server.stop()

    return asyncio.run(scenario())


def test_device_api_get_is_status_even_with_action_in_query(service):
    adb = FakeAdb()
    [(code, body)] = api_calls(service, adb, [("GET", "/api/device?action=enable", None)])
    assert code == 200 and body["action"] == "status"
    assert adb.proxy == {} and adb.reverse == {}


def test_device_api_post_enables_with_serial_and_refuses_bad_bodies(service):
    adb = FakeAdb({"a1": "device", "b2": "device"})
    answers = api_calls(service, adb, [
        ("POST", "/api/device", '{"action": "enable", "serial": "b2"}'),
        ("POST", "/api/device", '{"action": "enable"}'),
        ("POST", "/api/device", "not json"),
        ("POST", "/api/device", '{"action": "reboot"}'),
        ("POST", "/api/device", ""),
    ])
    assert answers[0][0] == 200 and answers[0][1]["ready"] is True
    assert adb.proxy == {"b2": "127.0.0.1:18890"}
    assert answers[1][0] == 400 and "pass serial" in answers[1][1]["error"]
    assert answers[2][0] == 400 and "not valid JSON" in answers[2][1]["error"]
    assert answers[3][0] == 400 and "action must be one of" in answers[3][1]["error"]
    assert answers[4][0] == 400 and "action is required" in answers[4][1]["error"]
