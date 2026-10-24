from types import SimpleNamespace

import pytest

from agentproxy.eventrules import INCOMING
from agentproxy.injector import InjectError, WebSocketInjector
from agentproxy.rules import EVENTS_MARK, RuleSet
from agentproxy.service import ProxyService, ToolError

from conftest import make_record
from test_eventrules import changer, events_rule


class Master:
    def __init__(self):
        self.calls = []
        self.commands = SimpleNamespace(call=lambda *args: self.calls.append(args))


def injector_for(flow):
    master = Master()
    return master, WebSocketInjector(master, SimpleNamespace(live_websocket=lambda seq: flow))


def test_send_event_goes_through_an_events_rule_that_has_not_closed_the_connection():
    events = changer(events_rule(close="before", event_contains="bye"))
    events.handle(INCOMING, "hello")
    flow = SimpleNamespace(live=True, websocket=object(), metadata={EVENTS_MARK: events})
    master, injector = injector_for(flow)
    injector.send(1, True, b"late", True)
    assert master.calls == [("inject.websocket", flow, True, b"late", True)]


def test_send_event_without_an_events_rule_injects_in_the_asked_direction():
    flow = SimpleNamespace(live=True, websocket=object(), metadata={})
    master, injector = injector_for(flow)
    injector.send(1, False, b"\x01", False)
    assert master.calls == [("inject.websocket", flow, False, b"\x01", False)]


@pytest.mark.parametrize("flow, refusal", [
    (None, "not opened through the proxy"),
    (SimpleNamespace(live=False, websocket=object(), metadata={}), "is closing"),
    (SimpleNamespace(live=True, websocket=None, metadata={}), "is closing"),
])
def test_send_event_refuses_a_websocket_that_is_gone(flow, refusal):
    master, injector = injector_for(flow)
    with pytest.raises(InjectError, match=refusal):
        injector.send(1, True, b"x", True)
    assert master.calls == []


def test_websocket_send_refused_by_the_proxy_is_a_tool_error(store, settings, tmp_path):
    service = ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))
    seq = store.insert(make_record(1, kind="websocket", open=True))

    class Injector:
        def send(self, *args):
            raise InjectError("an events rule closed this WebSocket connection")

    service.injector = Injector()
    with pytest.raises(ToolError, match="events rule closed"):
        service.send_event({"flow": str(seq), "data": "ping"})
