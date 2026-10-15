import asyncio
import os
import time
from dataclasses import replace

from . import VERSION
from .bodies import render
from .store import FlowFilter

MAX_LIST_LIMIT = 500
MAX_WAIT_SECONDS = 300
DEFAULT_BODY_CHARS = 20000


class ToolError(Exception):
    pass


def _int(arguments, name, default=None, low=None, high=None):
    value = arguments.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ToolError("{0} must be a number".format(name))
    try:
        value = int(value)
    except ValueError:
        raise ToolError("{0} must be a number".format(name))
    if low is not None and value < low:
        raise ToolError("{0} must be >= {1}".format(name, low))
    if high is not None and value > high:
        raise ToolError("{0} must be <= {1}".format(name, high))
    return value


def _str(arguments, name):
    value = arguments.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ToolError("{0} must be a string".format(name))
    return value


def build_filter(arguments):
    return FlowFilter(
        host=_str(arguments, "host"),
        path=_str(arguments, "path"),
        method=_str(arguments, "method"),
        status=_int(arguments, "status", low=100, high=599),
        failed_only=bool(arguments.get("failed_only", False)),
        since_seq=_int(arguments, "since_seq", low=0),
        text=_str(arguments, "url_contains"),
    )


FILTER_PROPERTIES = {
    "host": {"type": "string", "description": "Substring of the host, case-insensitive"},
    "path": {"type": "string", "description": "Substring of the path including query string"},
    "method": {"type": "string", "description": "HTTP method, e.g. GET"},
    "status": {"type": "integer", "description": "Exact response status code"},
    "failed_only": {"type": "boolean", "description": "Only flows with a connection error or status >= 400"},
    "since_seq": {"type": "integer", "description": "Only flows with seq greater than this value"},
    "url_contains": {"type": "string", "description": "Substring of the full URL, case-insensitive"},
}


class ProxyService:
    def __init__(self, store, settings, data_dir, cert_dir):
        self.store = store
        self.settings = settings
        self.data_dir = data_dir
        self.cert_dir = cert_dir
        self.started_at = time.time()
        self.proxy_running = False

    def ca_paths(self):
        return {
            "pem": os.path.join(self.cert_dir, "mitmproxy-ca-cert.pem"),
            "cer": os.path.join(self.cert_dir, "mitmproxy-ca-cert.cer"),
        }

    def status(self, arguments=None):
        settings = self.settings
        api = "http://127.0.0.1:{0}".format(settings.api_port)
        return {
            "version": VERSION,
            "proxy": {"host": settings.listen_host, "port": settings.proxy_port, "running": self.proxy_running},
            "api": api,
            "mcp": api + "/mcp",
            "monitor": api + "/",
            "flows": self.store.count(),
            "last_seq": self.store.last_seq(),
            "uptime_seconds": int(time.time() - self.started_at),
            "data_dir": self.data_dir,
            "ca_certificate": {
                "pem_url": api + "/api/ca.pem",
                "cer_url": api + "/api/ca.cer",
                "exists": os.path.exists(self.ca_paths()["pem"]),
            },
        }

    def list_flows(self, arguments):
        limit = _int(arguments, "limit", 50, 1, MAX_LIST_LIMIT)
        return {"flows": self.store.query(build_filter(arguments), limit)}

    def get_flow(self, arguments):
        flow_id = _str(arguments, "id")
        if flow_id is None:
            raise ToolError("id is required")
        max_chars = _int(arguments, "max_body_chars", DEFAULT_BODY_CHARS, 0, 10 * 1024 * 1024)
        record = self.store.get(flow_id)
        if record is None:
            raise ToolError("flow {0} not found".format(flow_id))
        record["request_body"] = render(record.pop("request_body"), record.pop("request_body_size"),
                                        record["request_content_type"], max_chars)
        record["response_body"] = render(record.pop("response_body"), record.pop("response_body_size"),
                                         record["response_content_type"], max_chars)
        return record

    def clear_flows(self, arguments):
        return {"removed": self.store.clear()}

    async def wait_for_flow(self, arguments):
        timeout = _int(arguments, "timeout_seconds", 60, 1, MAX_WAIT_SECONDS)
        flow_filter = build_filter(arguments)
        if flow_filter.since_seq is None:
            flow_filter = replace(flow_filter, since_seq=self.store.last_seq())
        existing = self.store.query(flow_filter, 1)
        if existing:
            return {"flow": existing[0], "timed_out": False}
        loop = asyncio.get_running_loop()
        found = loop.create_future()

        def listener(summary):
            if summary and not found.done() and flow_filter.matches(summary):
                found.set_result(summary)

        self.store.add_listener(listener)
        try:
            flow = await asyncio.wait_for(found, timeout)
            return {"flow": flow, "timed_out": False}
        except asyncio.TimeoutError:
            return {"flow": None, "timed_out": True}
        finally:
            self.store.remove_listener(listener)


TOOLS = [
    {
        "name": "proxy_status",
        "description": "AgentProxy state: version, proxy address and port, number of recorded flows, "
                       "CA certificate download URLs, monitor page URL.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": "status",
    },
    {
        "name": "list_flows",
        "description": "Recorded HTTP(S) flows, newest first, as summaries without bodies. "
                       "Use filters to narrow down; use get_flow for headers and bodies.",
        "inputSchema": {"type": "object", "properties": dict(FILTER_PROPERTIES, limit={
            "type": "integer", "description": "Maximum flows to return, 1..500, default 50"})},
        "handler": "list_flows",
    },
    {
        "name": "get_flow",
        "description": "Full flow by id or seq: request and response headers and bodies. "
                       "JSON bodies are pretty-printed, binary bodies are base64.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string", "description": "Flow id or seq number"},
            "max_body_chars": {"type": "integer", "description": "Cut each body to this many characters, default 20000"},
        }},
        "handler": "get_flow",
    },
    {
        "name": "wait_for_flow",
        "description": "Block until a flow matching the filters completes, then return its summary. "
                       "By default only flows finished after the call are considered. "
                       "Typical use: ask the user to tap something in the app and wait for the request.",
        "inputSchema": {"type": "object", "properties": dict(FILTER_PROPERTIES, timeout_seconds={
            "type": "integer", "description": "1..300, default 60"})},
        "handler": "wait_for_flow",
    },
    {
        "name": "clear_flows",
        "description": "Delete all recorded flows.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": "clear_flows",
    },
]


async def call_tool(service, name, arguments):
    for tool in TOOLS:
        if tool["name"] == name:
            handler = getattr(service, tool["handler"])
            result = handler(arguments or {})
            if asyncio.iscoroutine(result):
                result = await result
            return result
    raise ToolError("unknown tool {0}".format(name))


def public_tools():
    return [{key: value for key, value in tool.items() if key != "handler"} for tool in TOOLS]
