import json
import logging

from . import APP_NAME, VERSION
from .service import UNTRUSTED_NOTE, ToolError, call_tool, public_tools

log = logging.getLogger("agentproxy.mcp")

SUPPORTED_PROTOCOLS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
STOPPING_MESSAGE = "engine is stopping"

INSTRUCTIONS = (
    "AgentProxy is a local HTTP(S) debugging proxy (Charles replacement) controlled by you. "
    "Devices and browsers send traffic through it; every finished request is recorded, SSE and WebSocket streams when they open. "
    "Start with proxy_status. Use list_flows/get_flow to inspect traffic and wait_for_flow to wait for a "
    "request the user is about to trigger. SSE and WebSocket streams are flows with events: list_events, "
    "wait_for_event and send_event (any WebSocket; SSE only for streams an sse rule serves). add_rule answers or "
    "changes requests (map local, rewrite, error, delay, drop, an SSE stream of its own); list_rules shows the rules, "
    "delete them when done. "
    "When something does not go as planned, read_log first: it shows what AgentProxy itself did. "
    "device_proxy routes an Android device through AgentProxy over adb (and takes it off); check_connection tells "
    "whether traffic arrives and what to fix when it does not. The agentproxy skill "
    "explains device setup (CA certificate) and troubleshooting. " + UNTRUSTED_NOTE
)


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def stopping_error(body):
    try:
        message = json.loads(body)
    except ValueError:
        message = None
    request_id = message.get("id") if isinstance(message, dict) else None
    return _error(request_id, INTERNAL_ERROR, STOPPING_MESSAGE)


def _result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def negotiate(requested):
    if requested in SUPPORTED_PROTOCOLS:
        return requested
    return LATEST_PROTOCOL


async def handle_message(service, message):
    try:
        return await _dispatch(service, message)
    except Exception:
        log.exception("MCP message failed")
        request_id = message.get("id") if isinstance(message, dict) else None
        return _error(request_id, INTERNAL_ERROR, "internal error: read_log (source engine) has the details")


async def _dispatch(service, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "invalid request")
    method = message["method"]
    request_id = message.get("id")
    params = message.get("params") or {}
    if "id" not in message:
        return None
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "params must be an object")
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
        service.count_tool_call(name)
        try:
            result = await call_tool(service, name, arguments)
        except ToolError as error:
            return _result(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
        except Exception as error:
            log.exception("tool %s failed", name)
            return _result(request_id, {"content": [{"type": "text", "text": "internal error: {0}; read_log (source engine) has the details, quote them to the user".format(error)}],
                                        "isError": True})
        text = json.dumps(result, ensure_ascii=False, indent=1)
        return _result(request_id, {"content": [{"type": "text", "text": text}], "structuredContent": result,
                                    "isError": False})
    return _error(request_id, METHOD_NOT_FOUND, "method {0} not found".format(method))


async def handle_body(service, body):
    try:
        payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _error(None, PARSE_ERROR, "parse error")
    if isinstance(payload, list):
        if not payload:
            return _error(None, INVALID_REQUEST, "empty batch")
        responses = [await handle_message(service, item) for item in payload]
        responses = [item for item in responses if item is not None]
        return responses or None
    return await handle_message(service, payload)
