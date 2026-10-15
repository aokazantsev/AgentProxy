import asyncio
import ipaddress
import logging
import socket

from mitmproxy import http

log = logging.getLogger("agentproxy.guard")

BLOCKED_BODY = b"AgentProxy: its own ports are not reachable through the proxy"


def is_loopback_address(address):
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


class OwnPortsGuard:
    def __init__(self, proxy_port, api_port):
        self.ports = {proxy_port, api_port}

    async def targets_own_port(self, host, port):
        if port not in self.ports:
            return False
        name = host.strip("[]").rstrip(".").lower()
        if name == "localhost" or name.endswith(".localhost") or is_loopback_address(name):
            return True
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return False
        return any(is_loopback_address(info[4][0]) for info in infos)

    async def request(self, flow):
        await self._check(flow)

    async def http_connect(self, flow):
        await self._check(flow)

    async def _check(self, flow):
        if await self.targets_own_port(flow.request.host, flow.request.port):
            log.warning("blocked request through the proxy to its own port %s", flow.request.port)
            flow.response = http.Response.make(403, BLOCKED_BODY, {"content-type": "text/plain"})
