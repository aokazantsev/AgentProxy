import json
import logging

from . import APP_NAME, VERSION
from .service import ToolError, call_tool, public_tools

log = logging.getLogger("agentproxy.mcp")

SUPPORTED_PROTOCOLS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "AgentProxy is a local HTTP(S) debugging proxy (Charles replacement) controlled by you. "
    "Devices and browsers send traffic through it; every finished request is recorded. "
    "Start with proxy_status. Use list_flows/get_flow to inspect traffic and wait_for_flow to wait for a "
    "request the user is about to trigger. The agentproxy skill explains device setup (adb reverse, "
    "device proxy, CA certificate) and troubleshooting."
)


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def negotiate(requested):
    if requested in SUPPORTED_PROTOCOLS:
        return requested
    return LATEST_PROTOCOL


async def handle_message(service, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "invalid request")
    method = message["method"]
    request_id = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message
    if is_notification:
        return None
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": negotiate(params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": APP_NAME.lower(), "title": APP_NAME, "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": public_tools()})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, INVALID_PARAMS, "arguments must be an object")
        try:
            result = await call_tool(service, name, arguments)
        except ToolError as error:
            return _result(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
        except Exception as error:
            log.exception("tool %s failed", name)
            return _result(request_id, {"content": [{"type": "text", "text": "internal error: {0}".format(error)}],
                                        "isError": True})
        text = json.dumps(result, ensure_ascii=False, indent=1)
        return _result(request_id, {"content": [{"type": "text", "text": text}], "structuredContent": result,
                                    "isError": False})
    return _error(request_id, METHOD_NOT_FOUND, "method {0} not found".format(method))


async def handle_body(service, body):
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError):
        return _error(None, PARSE_ERROR, "parse error")
    if isinstance(payload, list):
        responses = [await handle_message(service, item) for item in payload]
        responses = [item for item in responses if item is not None]
        return responses or None
    return await handle_message(service, payload)
