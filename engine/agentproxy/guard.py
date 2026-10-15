import logging

from mitmproxy import http

log = logging.getLogger("agentproxy.guard")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class OwnPortsGuard:
    def __init__(self, proxy_port, api_port):
        self.ports = {proxy_port, api_port}

    def is_blocked(self, host, port):
        return host.lower() in LOOPBACK_HOSTS and port in self.ports

    def request(self, flow):
        if self.is_blocked(flow.request.host, flow.request.port):
            log.warning("blocked request through the proxy to its own port %s", flow.request.port)
            flow.response = http.Response.make(
                403, b"AgentProxy: its own ports are not reachable through the proxy", {"content-type": "text/plain"})
