import asyncio
import ipaddress
import logging
import socket

log = logging.getLogger("agentproxy.guard")

BLOCKED_MESSAGE = "AgentProxy: its own ports are not reachable through the proxy"
OWN_PORT_MARK = "agentproxy_own_port"
PROXIED_HEADER = "X-AgentProxy-Proxied"


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

    async def requestheaders(self, flow):
        if flow.request.port in self.ports:
            flow.request.headers[PROXIED_HEADER] = "1"
        await self.mark(flow)

    def tcp_message(self, flow):
        peer = flow.server_conn.peername
        if not (peer and peer[1] in self.ports and is_loopback_address(peer[0]) and flow.messages[-1].from_client):
            return
        if not flow.metadata.get(OWN_PORT_MARK):
            flow.metadata[OWN_PORT_MARK] = True
            log.warning("silenced raw TCP through the proxy to its own port %s", peer[1])
        flow.messages[-1].content = b""

    async def http_connect(self, flow):
        await self.mark(flow)

    async def mark(self, flow):
        request = flow.request
        if request.port in self.ports and await self.reaches_loopback(request.host, request.port):
            flow.metadata[OWN_PORT_MARK] = True

    async def server_connect(self, data):
        server = data.server
        if not server.address or server.address[1] not in self.ports:
            return
        if await self.reaches_loopback(server.address[0], server.address[1]):
            log.warning("blocked connection through the proxy to its own port %s", server.address[1])
            server.error = BLOCKED_MESSAGE

    async def reaches_loopback(self, host, port):
        name = host.strip("[]").rstrip(".").lower()
        if name == "localhost" or name.endswith(".localhost") or is_loopback_address(name):
            return True
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return True
        return any(is_loopback_address(info[4][0]) for info in infos)
