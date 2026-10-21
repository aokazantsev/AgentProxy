import asyncio
import hmac
import json
import logging
import os

import tornado.log
import tornado.web

from . import mcp
from .claude import hooks as claude_hooks
from .guard import PROXIED_HEADER
from .monitorpins import PinError, PinStorageError
from .service import NotFound, ToolError, call_tool

log = logging.getLogger("agentproxy.api")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TOKEN_HEADER = "X-AgentProxy-Token"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


def allowed_hosts(port):
    return {"{0}:{1}".format(host, port) for host in LOCAL_HOSTS}


def allowed_origins(port):
    return {"http://{0}:{1}".format(host, port) for host in LOCAL_HOSTS}


def log_request(handler):
    status = handler.get_status()
    if status < 400 or status == 503:
        write = tornado.log.access_log.info
    elif status < 500:
        write = tornado.log.access_log.warning
    else:
        write = tornado.log.access_log.error
    request = handler.request
    write("%d %s %s (%s) %.2fms", status, request.method, request.path, request.remote_ip,
          1000.0 * request.request_time())


class BaseHandler(tornado.web.RequestHandler):
    def initialize(self, context):
        self.context = context

    @property
    def service(self):
        return self.context["service"]

    def prepare(self):
        port = self.context["service"].settings.api_port
        if self.request.host not in allowed_hosts(port):
            self._deny("host not allowed")
            return
        origin = self.request.headers.get("Origin")
        if origin and origin not in allowed_origins(port):
            self._deny("origin not allowed")
            return
        if PROXIED_HEADER in self.request.headers:
            self._deny("requests through the proxy are not allowed")

    def _deny(self, reason):
        log.warning("rejected %s %s: %s", self.request.method, self.request.path, reason)
        self.set_status(403)
        self.finish({"error": reason})

    def write_json(self, data, status=200):
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        self.finish(json.dumps(data, ensure_ascii=False).encode("utf-8", "backslashreplace"))

    def json_body(self):
        if not self.request.body:
            return {}
        try:
            data = json.loads(self.request.body.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ToolError("body is not valid JSON")
        if not isinstance(data, dict):
            raise ToolError("body must be a JSON object")
        return data

    def query_arguments(self):
        return {name: self.get_query_argument(name) for name in self.request.query_arguments}

    async def run_tool(self, name, arguments):
        try:
            self.write_json(await call_tool(self.service, name, arguments))
        except asyncio.CancelledError:
            self.write_json({"error": mcp.STOPPING_MESSAGE}, 503)
        except ToolError as error:
            self.write_json({"error": str(error)}, 404 if isinstance(error, NotFound) else 400)

    async def run_body_tool(self, name, **extra):
        try:
            arguments = dict(self.json_body(), **extra)
        except ToolError as error:
            self.write_json({"error": str(error)}, 400)
            return
        await self.run_tool(name, arguments)

    def write_error(self, status_code, **kwargs):
        self.write_json({"error": self._reason}, status_code)


class StatusHandler(BaseHandler):
    async def get(self):
        await self.run_tool("proxy_status", {})


class FlowsHandler(BaseHandler):
    async def get(self):
        await self.run_tool("list_flows", self.query_arguments())

    async def delete(self):
        await self.run_tool("clear_flows", {})


class WaitHandler(BaseHandler):
    async def get(self):
        await self.run_tool("wait_for_flow", self.query_arguments())


class EventsHandler(BaseHandler):
    async def get(self):
        await self.run_tool("list_events", self.query_arguments())


class EventsSendHandler(BaseHandler):
    async def post(self):
        await self.run_body_tool("send_event")


class EventsWaitHandler(BaseHandler):
    async def get(self):
        await self.run_tool("wait_for_event", self.query_arguments())


class FlowHandler(BaseHandler):
    async def get(self, flow_id):
        arguments = self.query_arguments()
        arguments["id"] = flow_id
        await self.run_tool("get_flow", arguments)


class LogHandler(BaseHandler):
    async def get(self):
        await self.run_tool("read_log", self.query_arguments())


class DeviceHandler(BaseHandler):
    async def get(self):
        await self.run_tool("device_proxy", dict(self.query_arguments(), action="status"))

    async def post(self):
        await self.run_body_tool("device_proxy")


class RulesHandler(BaseHandler):
    async def get(self):
        await self.run_tool("list_rules", self.query_arguments())

    async def post(self):
        await self.run_body_tool("add_rule")

    async def delete(self):
        if self.request.query_arguments or self.request.body:
            self.write_json({"error": "DELETE /api/rules deletes every rule and takes no parameters; delete one rule "
                                      "with DELETE /api/rules/<id>"}, 400)
            return
        await self.run_tool("delete_rule", {"all": True})


class MonitorPinsHandler(BaseHandler):
    def get(self):
        try:
            self.write_json({"pinned_hosts": self.context["pins"].hosts()})
        except PinError as error:
            self.write_json({"error": str(error)}, 500)

    def post(self):
        try:
            body = self.json_body()
        except ToolError as error:
            self.write_json({"error": str(error)}, 400)
            return
        try:
            self.write_json({"pinned_hosts": self.context["pins"].set(body.get("host"), body.get("pinned"))})
        except PinError as error:
            log.warning("monitor pin refused: %s", error)
            self.write_json({"error": str(error)}, 500 if isinstance(error, PinStorageError) else 400)


class RuleHandler(BaseHandler):
    async def patch(self, rule_id):
        await self.run_body_tool("update_rule", id=rule_id)

    async def delete(self, rule_id):
        await self.run_tool("delete_rule", {"id": rule_id})


class CertificateHandler(BaseHandler):
    def get(self, kind):
        path = self.service.ca_paths()[kind]
        if not os.path.exists(path):
            self.write_json({"error": "certificate is not generated yet"}, 404)
            return
        content_type = "application/x-x509-ca-cert" if kind == "cer" else "application/x-pem-file"
        self.set_header("Content-Type", content_type)
        self.set_header("Content-Disposition", "attachment; filename=agentproxy-ca.{0}".format(kind))
        with open(path, "rb") as stream:
            self.finish(stream.read())


class ShutdownHandler(BaseHandler):
    def post(self):
        supplied = self.request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied, self.context["token"]):
            self.write_json({"error": "bad token"}, 403)
            return
        log.info("shutdown requested over API")
        self.write_json({"stopping": True})
        self.context["shutdown"]()


class McpHandler(BaseHandler):
    async def post(self):
        try:
            response = await mcp.handle_body(self.service, self.request.body)
        except asyncio.CancelledError:
            self.write_json(mcp.stopping_error(self.request.body), 503)
            return
        if response is None:
            self.set_status(202)
            self.finish()
            return
        self.write_json(response)

    def get(self):
        self.set_header("Allow", "POST")
        self.write_json({"error": "SSE stream is not offered; use POST"}, 405)

    def delete(self):
        self.set_header("Allow", "POST")
        self.write_json({"error": "sessions are not used"}, 405)


class PromptHookHandler(BaseHandler):
    async def post(self):
        try:
            hook_input = self.json_body()
            context = await asyncio.to_thread(claude_hooks.prompt_context, self.context["claude_env"], hook_input)
        except Exception:
            log.exception("prompt hook failed")
            context = ""
        self.write_json(claude_hooks.hook_output("UserPromptSubmit", context))


class MonitorHandler(BaseHandler):
    def get(self):
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        with open(os.path.join(WEB_DIR, "index.html"), "rb") as stream:
            self.finish(stream.read())


def make_app(context):
    routes = [
        (r"/", MonitorHandler),
        (r"/api/status", StatusHandler),
        (r"/api/flows", FlowsHandler),
        (r"/api/flows/wait", WaitHandler),
        (r"/api/flows/([^/]+)", FlowHandler),
        (r"/api/events", EventsHandler),
        (r"/api/events/wait", EventsWaitHandler),
        (r"/api/events/send", EventsSendHandler),
        (r"/api/log", LogHandler),
        (r"/api/device", DeviceHandler),
        (r"/api/rules", RulesHandler),
        (r"/api/rules/([^/]+)", RuleHandler),
        (r"/api/monitor/pins", MonitorPinsHandler),
        (r"/api/ca\.(pem|cer)", CertificateHandler),
        (r"/api/shutdown", ShutdownHandler),
        (r"/mcp", McpHandler),
        (r"/claude/hooks/user-prompt-submit", PromptHookHandler),
    ]
    return tornado.web.Application(
        [(pattern, handler, {"context": context}) for pattern, handler in routes], log_function=log_request)
