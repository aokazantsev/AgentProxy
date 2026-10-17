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


@pytest.mark.parametrize("port,labelled", [(API_PORT, True), (PROXY_PORT, True), (443, False)])
def test_requests_to_own_port_numbers_are_labelled_for_the_api(monkeypatch, port, labelled):
    from mitmproxy.test import tflow
    from agentproxy.guard import PROXIED_HEADER
    fake_resolver(monkeypatch, {"rebind.example": ["93.184.216.34"]})
    flow = tflow.tflow()
    flow.request.host = "rebind.example"
    flow.request.port = port
    asyncio.run(OwnPortsGuard(PROXY_PORT, API_PORT).requestheaders(flow))
    assert (PROXIED_HEADER in flow.request.headers) is labelled


@pytest.mark.parametrize("peer,port,from_client,silenced", [
    ("127.0.0.1", API_PORT, True, True), ("::1", PROXY_PORT, True, True), ("127.0.0.1", API_PORT, False, False),
    ("127.0.0.1", 443, True, False), ("93.184.216.34", API_PORT, True, False)])
def test_raw_tcp_reaching_own_ports_is_silenced(peer, port, from_client, silenced):
    from mitmproxy.test import tflow
    flow = tflow.ttcpflow()
    flow.server_conn.address = ("rebind.example", port)
    flow.server_conn.peername = (peer, port)
    flow.messages[-1].from_client = from_client
    flow.messages[-1].content = b"\r\nGET /api/rules HTTP/1.1\r\n\r\n"
    OwnPortsGuard(PROXY_PORT, API_PORT).tcp_message(flow)
    assert (flow.messages[-1].content == b"") is silenced
