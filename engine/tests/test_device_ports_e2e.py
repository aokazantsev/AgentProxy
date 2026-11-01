import json
import os
import sys
import urllib.request

import pytest

from conftest import free_port
from test_engine_e2e import ENGINE_START_TIMEOUT, api_get, api_post, launch, start_server, two_ports, wait_until

FIRST = "devA-1"
SECOND = "devB.2"
FAKE_ADB = r'''
import json
import os
import sys

state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adb-state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"proxy": {}, "reverse": {}}
args = sys.argv[1:]
serial = None
if args[:1] == ["-s"]:
    serial, args = args[1], args[2:]
if args == ["devices"]:
    print("List of devices attached")
    for name in ("devA-1", "devB.2"):
        print(name + "\tdevice")
elif args[:4] == ["shell", "settings", "get", "global"]:
    print(state["proxy"].get(serial, "null"))
elif args[:4] == ["shell", "settings", "put", "global"]:
    state["proxy"][serial] = args[5]
elif args[:2] == ["reverse", "--list"]:
    for local, remote in state["reverse"].get(serial, {}).items():
        print("host-1 " + local + " " + remote)
elif args[:2] == ["reverse", "--remove"]:
    if args[2] not in state["reverse"].get(serial, {}):
        print("adb: error: listener '" + args[2] + "' not found")
        sys.exit(1)
    del state["reverse"][serial][args[2]]
elif args[:1] == ["reverse"]:
    state["reverse"].setdefault(serial, {})[args[1]] = args[2]
else:
    sys.exit(1)
json.dump(state, open(state_path, "w"))
'''


@pytest.fixture
def device_engine(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "fakeadb.py").write_text(FAKE_ADB, encoding="utf-8")
    adb = tools / "adb.cmd"
    adb.write_text('@"{0}" -I "%~dp0fakeadb.py" %*\r\n'.format(sys.executable), encoding="ascii")
    proxy_port, api_port = two_ports()
    second_port = free_port()
    while second_port in (proxy_port, api_port):
        second_port = free_port()
    settings = {"proxy_port": proxy_port, "api_port": api_port, "verify_upstream": False,
                "adb_path": str(adb).replace("\\", "/")}
    with open(os.path.join(data_dir, "settings.json"), "w") as stream:
        json.dump(settings, stream)
    with open(os.path.join(data_dir, "device-ports.json"), "w") as stream:
        json.dump({"ports": {SECOND: second_port}}, stream)
    process = launch(data_dir)

    def ready():
        try:
            status, body = api_get(api_port, "/api/status", timeout=2)
            return status == 200 and body["proxy"]["running"]
        except OSError:
            return False

    if not wait_until(ready, ENGINE_START_TIMEOUT):
        process.kill()
        pytest.fail("engine did not start:\n" + open(os.path.join(data_dir, "engine.log"), encoding="utf-8").read())
    yield dict(settings, process=process, data_dir=data_dir, second_port=second_port)
    if process.poll() is None:
        process.kill()
        process.wait(10)


def fetch(port, url):
    proxy = "http://127.0.0.1:{0}".format(port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy}))
    response = opener.open(url, timeout=15)
    return response.status, response.read()


def flows(engine, query=""):
    return api_get(engine["api_port"], "/api/flows" + query)[1]["flows"]


def test_two_devices_on_their_ports_are_kept_apart(device_engine):
    engine = device_engine
    status = api_get(engine["api_port"], "/api/status")[1]
    assert status["proxy"]["device_ports"] == {SECOND: engine["second_port"]}
    code, enabled = api_post(engine["api_port"], "/api/device", {"action": "enable", "serial": FIRST})
    assert code == 200, enabled
    first_port = enabled["pc_port"]
    assert enabled["ready"] is True and first_port not in (engine["proxy_port"], engine["api_port"],
                                                            engine["second_port"])
    code, second = api_post(engine["api_port"], "/api/device", {"action": "enable", "serial": SECOND})
    assert code == 200 and second["pc_port"] == engine["second_port"] and second["ready"] is True

    target = start_server()
    url = "http://127.0.0.1:{0}/v1/items".format(target.server_port)
    code, rule = api_post(engine["api_port"], "/api/rules", {"action": "respond", "path": "/v1/items",
                                                              "body": "mock of A", "device": FIRST})
    assert code == 200, rule
    assert fetch(first_port, url) == (200, b"mock of A")
    second_status, second_body = fetch(engine["second_port"], url)
    assert second_body != b"mock of A"
    pc_status, pc_body = fetch(engine["proxy_port"], url)
    assert pc_body != b"mock of A"

    first_flows = flows(engine, "?device=" + FIRST)
    assert [flow["device"] for flow in first_flows] == [FIRST]
    assert first_flows[0]["rules"] == [{"id": rule["rule"]["id"], "action": "respond"}]
    second_flows = flows(engine, "?device=" + SECOND)
    assert [flow["device"] for flow in second_flows] == [SECOND] and not second_flows[0]["rules"]
    assert sorted(flow["device"] or "" for flow in flows(engine)) == sorted(["", FIRST, SECOND])

    code, cleared = api_post(engine["api_port"], "/api/device", {"action": "disable", "serial": FIRST})
    assert code == 200 and cleared["device_proxy"] is None and cleared["reverse_active"] is False
    assert json.load(open(os.path.join(engine["data_dir"], "device-ports.json")))["ports"] == {
        SECOND: engine["second_port"], FIRST: first_port}
