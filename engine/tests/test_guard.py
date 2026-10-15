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


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("address,expected", [
    ("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("10.0.0.1", False), ("::ffff:10.0.0.1", False), ("example.com", False),
])
def test_loopback_addresses(address, expected):
    assert is_loopback_address(address) is expected


def test_name_resolving_to_loopback_targets_own_port(monkeypatch):
    fake_resolver(monkeypatch, {"rebind.example": ["127.0.0.1"]})
    assert run(OwnPortsGuard(PROXY_PORT, API_PORT).targets_own_port("rebind.example", API_PORT)) is True


def test_name_resolving_elsewhere_is_allowed(monkeypatch):
    fake_resolver(monkeypatch, {"api.example": ["93.184.216.34"]})
    assert run(OwnPortsGuard(PROXY_PORT, API_PORT).targets_own_port("api.example", API_PORT)) is False


def test_unresolvable_name_is_left_to_the_connection(monkeypatch):
    fake_resolver(monkeypatch, {"missing.example": socket.gaierror("no such host")})
    assert run(OwnPortsGuard(PROXY_PORT, API_PORT).targets_own_port("missing.example", API_PORT)) is False


def test_other_ports_skip_the_resolver(monkeypatch):
    calls = fake_resolver(monkeypatch, {})
    assert run(OwnPortsGuard(PROXY_PORT, API_PORT).targets_own_port("anything.example", 443)) is False
    assert calls == []


@pytest.mark.parametrize("host", ["[::1]", "localhost.", "app.localhost", "127.0.0.5"])
def test_loopback_names_need_no_resolver(monkeypatch, host):
    calls = fake_resolver(monkeypatch, {})
    assert run(OwnPortsGuard(PROXY_PORT, API_PORT).targets_own_port(host, PROXY_PORT)) is True
    assert calls == []


def connection(host, port):
    return SimpleNamespace(server=SimpleNamespace(address=(host, port), sni=None, error=None))


def test_server_connect_pins_the_checked_address(monkeypatch):
    calls = fake_resolver(monkeypatch, {"rebind.example": ["93.184.216.34"]})
    data = connection("rebind.example", API_PORT)
    run(OwnPortsGuard(PROXY_PORT, API_PORT).server_connect(data))
    assert data.server.error is None
    assert data.server.address == ("93.184.216.34", API_PORT)
    assert data.server.sni == "rebind.example"
    assert calls == ["rebind.example"]


def test_server_connect_kills_connection_resolving_to_loopback(monkeypatch):
    fake_resolver(monkeypatch, {"rebind.example": ["127.0.0.1"]})
    data = connection("rebind.example", API_PORT)
    run(OwnPortsGuard(PROXY_PORT, API_PORT).server_connect(data))
    assert data.server.error


def test_server_connect_kills_unresolvable_own_port(monkeypatch):
    fake_resolver(monkeypatch, {"missing.example": socket.gaierror("no such host")})
    data = connection("missing.example", PROXY_PORT)
    run(OwnPortsGuard(PROXY_PORT, API_PORT).server_connect(data))
    assert data.server.error


def test_server_connect_leaves_other_ports_and_literals(monkeypatch):
    fake_resolver(monkeypatch, {"93.184.216.34": ["93.184.216.34"]})
    other = connection("api.example", 443)
    run(OwnPortsGuard(PROXY_PORT, API_PORT).server_connect(other))
    assert other.server.address == ("api.example", 443) and other.server.error is None
    literal = connection("93.184.216.34", API_PORT)
    run(OwnPortsGuard(PROXY_PORT, API_PORT).server_connect(literal))
    assert literal.server.address == ("93.184.216.34", API_PORT) and literal.server.sni is None
    assert literal.server.error is None
