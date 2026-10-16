import asyncio
import json

import pytest

from agentproxy import VERSION, mcp
from agentproxy.service import UNTRUSTED_NOTE, ProxyService, ToolError, call_tool, public_tools

from conftest import make_record


@pytest.fixture
def service(store, settings, tmp_path):
    return ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"))


def run(coroutine):
    return asyncio.run(coroutine)


def test_status_reports_ports_and_counts(service, store):
    store.insert(make_record(1))
    status = service.status()
    assert status["version"] == VERSION
    assert status["proxy"]["port"] == 18890
    assert status["mcp"] == "http://127.0.0.1:18891/mcp"
    assert status["flows"] == 1
    assert status["ca_certificate"]["exists"] is False


def test_list_flows_validates_limit(service):
    with pytest.raises(ToolError):
        service.list_flows({"limit": 0})
    with pytest.raises(ToolError):
        service.list_flows({"limit": "lots"})


def test_list_flows_accepts_string_numbers_from_query(service, store):
    store.insert(make_record(1, status=404))
    store.insert(make_record(2))
    flows = service.list_flows({"status": "404", "limit": "5"})["flows"]
    assert [flow["id"] for flow in flows] == ["flow-1"]


def test_get_flow_renders_bodies(service, store):
    store.insert(make_record(1, request_body=b"\x00\x01", request_body_size=2, request_content_type="application/octet-stream"))
    flow = service.get_flow({"id": "flow-1"})
    assert flow["response_body"]["content"] == '{\n  "ok": true\n}'
    assert flow["request_body"]["encoding"] == "base64"


def test_get_flow_unknown_and_missing_id(service):
    with pytest.raises(ToolError, match="not found"):
        service.get_flow({"id": "nope"})
    with pytest.raises(ToolError, match="required"):
        service.get_flow({})


def test_wait_returns_flow_inserted_later(service, store):
    store.insert(make_record(1, path="/target"))

    async def scenario():
        waiter = asyncio.ensure_future(service.wait_for_flow({"path": "/target", "timeout_seconds": 5}))
        await asyncio.sleep(0.05)
        store.insert(make_record(2, path="/other"))
        store.insert(make_record(3, path="/target?x=1"))
        return await waiter

    result = run(scenario())
    assert result["timed_out"] is False
    assert result["flow"]["id"] == "flow-3"


def test_wait_times_out(service):
    result = run(service.wait_for_flow({"timeout_seconds": 1}))
    assert result == {"flow": None, "timed_out": True}


def test_wait_with_since_seq_returns_existing(service, store):
    store.insert(make_record(1))
    result = run(service.wait_for_flow({"since_seq": 0, "timeout_seconds": 1}))
    assert result["flow"]["id"] == "flow-1"


def test_wait_cleans_listener(service, store):
    run(service.wait_for_flow({"timeout_seconds": 1}))
    assert store._listeners == []


def test_unknown_tool(service):
    with pytest.raises(ToolError):
        run(call_tool(service, "drop_database", {}))


def test_public_tools_have_no_handler():
    for tool in public_tools():
        assert "handler" not in tool
        assert tool["inputSchema"]["type"] == "object"


def rpc(service, payload):
    body = json.dumps(payload).encode()
    return run(mcp.handle_body(service, body))


def test_mcp_initialize_negotiates(service):
    response = rpc(service, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["version"] == VERSION
    assert "tools" in result["capabilities"]
    assert result["instructions"]
    unknown = rpc(service, {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert unknown["result"]["protocolVersion"] == mcp.LATEST_PROTOCOL


def test_mcp_notification_has_no_response(service):
    assert rpc(service, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_tools_list_and_call(service, store):
    store.insert(make_record(1))
    tools = rpc(service, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {tool["name"] for tool in tools} >= {"proxy_status", "list_flows", "get_flow", "wait_for_flow", "clear_flows"}
    call = rpc(service, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "list_flows", "arguments": {"limit": 1}}})["result"]
    assert call["isError"] is False
    assert json.loads(call["content"][0]["text"])["flows"][0]["id"] == "flow-1"
    assert call["structuredContent"]["flows"][0]["id"] == "flow-1"


def test_mcp_serves_flow_with_raw_header_bytes(service, store):
    raw = b"\xcf\xf0".decode("utf-8", "surrogateescape")
    store.insert(make_record(1, response_headers=[["content-disposition", "attachment; filename=" + raw]]))
    response = rpc(service, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "get_flow", "arguments": {"id": "flow-1"}}})
    text = json.dumps(response, ensure_ascii=False).encode("utf-8")
    assert response["result"]["isError"] is False and b"filename=\\\\xcf\\\\xf0" in text


def test_mcp_tool_error_is_reported_in_result(service):
    call = rpc(service, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "get_flow", "arguments": {"id": "x"}}})["result"]
    assert call["isError"] is True
    assert "not found" in call["content"][0]["text"]


def test_mcp_protocol_errors(service):
    assert run(mcp.handle_body(service, b"{bad"))["error"]["code"] == mcp.PARSE_ERROR
    assert rpc(service, {"id": 1, "method": "x"})["error"]["code"] == mcp.INVALID_REQUEST
    assert rpc(service, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})["error"]["code"] == mcp.METHOD_NOT_FOUND
    bad_args = rpc(service, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "proxy_status", "arguments": [1]}})
    assert bad_args["error"]["code"] == mcp.INVALID_PARAMS


def test_mcp_batch(service):
    response = rpc(service, [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert response == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("true", True), ("1", True), ("yes", True),
                                          ("false", False), ("0", False), ("no", False), ("", False)])
def test_failed_only_parses_query_strings(service, store, raw, expected):
    store.insert(make_record(1, status=500))
    store.insert(make_record(2, status=200))
    flows = service.list_flows({"failed_only": raw})["flows"]
    assert [flow["id"] for flow in flows] == (["flow-1"] if expected else ["flow-2", "flow-1"])


def test_failed_only_rejects_garbage(service):
    with pytest.raises(ToolError):
        service.list_flows({"failed_only": "maybe"})


def test_mcp_params_must_be_object(service):
    response = rpc(service, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": [1]})
    assert response["error"]["code"] == mcp.INVALID_PARAMS


def test_mcp_empty_batch_is_invalid(service):
    assert rpc(service, [])["error"]["code"] == mcp.INVALID_REQUEST


def test_mcp_unexpected_failure_is_internal_error(service, monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp, "negotiate", broken)
    response = rpc(service, {"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}})
    assert response["error"]["code"] == mcp.INTERNAL_ERROR
    assert response["id"] == 9


def test_instructions_and_flow_tools_warn_about_untrusted_content():
    assert UNTRUSTED_NOTE in mcp.INSTRUCTIONS
    descriptions = {tool["name"]: tool["description"] for tool in public_tools()}
    for name in ("list_flows", "get_flow", "wait_for_flow"):
        assert UNTRUSTED_NOTE in descriptions[name]
