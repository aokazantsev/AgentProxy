import asyncio
import ipaddress
import logging
import socket

log = logging.getLogger("agentproxy.guard")

BLOCKED_MESSAGE = "AgentProxy: its own ports are not reachable through the proxy"


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
