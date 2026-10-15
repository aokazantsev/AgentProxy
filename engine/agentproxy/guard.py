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


def is_ip_literal(host):
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


class OwnPortsGuard:
    def __init__(self, proxy_port, api_port):
        self.ports = {proxy_port, api_port}

    async def resolve_own_port(self, host, port):
        name = host.strip("[]").rstrip(".").lower()
        if name == "localhost" or name.endswith(".localhost") or is_loopback_address(name):
            return True, []
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return False, []
        addresses = [info[4][0] for info in infos]
        return any(is_loopback_address(address) for address in addresses), addresses

    async def targets_own_port(self, host, port):
        if port not in self.ports:
            return False
        blocked, _ = await self.resolve_own_port(host, port)
        return blocked

    async def server_connect(self, data):
        server = data.server
        if not server.address or server.address[1] not in self.ports:
            return
        host, port = server.address[0], server.address[1]
        blocked, addresses = await self.resolve_own_port(host, port)
        if blocked or not addresses:
            log.warning("blocked connection through the proxy to its own port %s", port)
            server.error = "AgentProxy: its own ports are not reachable through the proxy"
        elif not is_ip_literal(host):
            server.sni = server.sni or host
            server.address = (addresses[0], port)

    async def request(self, flow):
        await self._check(flow)

    async def http_connect(self, flow):
        await self._check(flow)

    async def _check(self, flow):
        if await self.targets_own_port(flow.request.host, flow.request.port):
            log.warning("blocked request through the proxy to its own port %s", flow.request.port)
            flow.response = http.Response.make(403, BLOCKED_BODY, {"content-type": "text/plain"})
