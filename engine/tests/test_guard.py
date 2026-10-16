import asyncio
import socket
from types import SimpleNamespace

import pytest

from agentproxy.guard import OwnPortsGuard, is_loopback_address

PROXY_PORT = 18890
API_PORT = 18891


def fake_resolver(monkeypatch, answers):
    calls = []

    async def getaddrinfo(self, host, port, **kwargs):
        calls.append(host)
        answer = answers[host]
        if isinstance(answer, Exception):
            raise answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in answer]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return calls


def connect(host, port):
    data = SimpleNamespace(server=SimpleNamespace(address=(host, port), error=None))
    asyncio.run(OwnPortsGuard(PROXY_PORT, API_PORT).server_connect(data))
    return data.server


def mark(host, port):
    flow = SimpleNamespace(request=SimpleNamespace(host=host, port=port), metadata={})
    asyncio.run(OwnPortsGuard(PROXY_PORT, API_PORT).mark(flow))
    return flow.metadata


@pytest.mark.parametrize("host,port,marked", [
    ("127.0.0.1", PROXY_PORT, True), ("localhost", API_PORT, True), ("127.0.0.1", 3000, False),
])
def test_requests_to_own_ports_are_marked(host, port, marked):
    assert bool(mark(host, port)) is marked


@pytest.mark.parametrize("address,expected", [
    ("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("10.0.0.1", False), ("::ffff:10.0.0.1", False), ("example.com", False),
])
def test_loopback_addresses(address, expected):
    assert is_loopback_address(address) is expected


@pytest.mark.parametrize("host", ["[::1]", "localhost.", "LOCALHOST", "app.localhost", "127.0.0.5"])
def test_loopback_names_are_blocked_without_resolver(monkeypatch, host):
    calls = fake_resolver(monkeypatch, {})
    assert connect(host, API_PORT).error
    assert calls == []


def test_name_resolving_to_loopback_is_blocked(monkeypatch):
    fake_resolver(monkeypatch, {"local.example": ["93.184.216.34", "127.0.0.1"]})
    assert connect("local.example", PROXY_PORT).error


def test_unresolvable_name_on_own_port_is_blocked(monkeypatch):
    fake_resolver(monkeypatch, {"missing.example": socket.gaierror("no such host")})
    assert connect("missing.example", API_PORT).error


def test_remote_host_on_same_port_number_is_allowed(monkeypatch):
    fake_resolver(monkeypatch, {"api.example": ["93.184.216.34"]})
    server = connect("api.example", API_PORT)
    assert server.error is None and server.address == ("api.example", API_PORT)


def test_other_ports_skip_the_resolver(monkeypatch):
    calls = fake_resolver(monkeypatch, {})
    assert connect("anything.example", 443).error is None
    assert calls == []
