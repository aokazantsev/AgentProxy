import asyncio
from types import SimpleNamespace

import pytest
from mitmproxy.test import tflow
from mitmproxy.websocket import WebSocketMessage
from wsproto.frame_protocol import Opcode

from agentproxy import injector as injector_module
from agentproxy.eventrules import INCOMING
from agentproxy.injector import InjectError, WebSocketInjector
from agentproxy.recorder import LiveStream, Recorder
from agentproxy.rewriter import Rewriter
from agentproxy.rules import EVENTS_MARK, RuleSet
from agentproxy.service import ProxyService, ToolError
from agentproxy.store import EventFilter

from conftest import make_record
from test_eventrules import changer, events_rule


class Master:
    def __init__(self, deliver=None):
        self.calls = []
        self.deliver = deliver
        self.commands = SimpleNamespace(call=self.call)

    def call(self, *args):
        self.calls.append(args)
        if self.deliver is not None:
            asyncio.get_running_loop().call_soon(self.deliver, *args[1:])


def live_flow(store, recorder):
    flow = tflow.twebsocketflow()
    flow.websocket.messages.clear()
    seq = store.insert(make_record(1, id=flow.id, kind="websocket", open=True))
    recorder.streams[flow.id] = LiveStream(seq, flow)
    return flow, seq


def through_the_proxy(recorder, server_messages=()):
    rewriter = Rewriter(None)

    def deliver(flow, to_client, content, is_text):
        sent = [WebSocketMessage(Opcode.TEXT, False, text) for text in server_messages]
        sent.append(WebSocketMessage(Opcode.TEXT if is_text else Opcode.BINARY, not to_client, content, injected=True))
        for message in sent:
            flow.websocket.messages.append(message)
            rewriter.websocket_message(flow)
            recorder.websocket_message(flow)
    return deliver


def test_send_event_waits_until_the_message_passed_the_proxy(store):
    recorder = Recorder(store, 1024)
    flow, seq = live_flow(store, recorder)
    master = Master(through_the_proxy(recorder))
    asyncio.run(WebSocketInjector(master, recorder).send(seq, False, b"\x01", False))
    assert master.calls == [("inject.websocket", flow, False, b"\x01", False)]
    event = store.events(seq, EventFilter(), 10)[0]
    assert event["direction"] == "outgoing" and event["note"] == "injected"
    assert not recorder.streams[flow.id].injections


def test_send_event_goes_through_an_events_rule_that_has_not_closed_the_connection(store):
    recorder = Recorder(store, 1024)
    flow, seq = live_flow(store, recorder)
    events = changer(events_rule(close="before", event_contains="bye"))
    events.handle(INCOMING, "hello")
    flow.metadata[EVENTS_MARK] = events
    asyncio.run(WebSocketInjector(Master(through_the_proxy(recorder)), recorder).send(seq, True, b"late", True))
    assert store.events(seq, EventFilter(), 10)[0]["data"] == b"late"


def test_send_event_dropped_after_a_close_in_the_same_loop_pass_is_an_error(store):
    recorder = Recorder(store, 1024)
    flow, seq = live_flow(store, recorder)
    flow.metadata[EVENTS_MARK] = changer(events_rule(close="before", event_contains="bye"))
    master = Master(through_the_proxy(recorder, server_messages=[b"bye"]))
    with pytest.raises(InjectError, match="not delivered: an events rule closed"):
        asyncio.run(WebSocketInjector(master, recorder).send(seq, True, b"late", True))
    notes = [event["note"] for event in store.events(seq, EventFilter(), 10)]
    assert "injected, dropped" in notes


def test_send_event_into_a_connection_that_ends_first_is_an_error(store):
    recorder = Recorder(store, 1024)
    flow, seq = live_flow(store, recorder)

    def end(*args):
        asyncio.ensure_future(recorder.websocket_end(flow))

    with pytest.raises(InjectError, match="closed before the message went out"):
        asyncio.run(WebSocketInjector(Master(end), recorder).send(seq, True, b"late", True))
    assert store.summary(seq)["open"] is False


def test_late_pass_of_a_timed_out_message_does_not_answer_the_next_send(store, monkeypatch):
    monkeypatch.setattr(injector_module, "CONFIRM_SECONDS", 0.05)
    recorder = Recorder(store, 1024)
    flow, seq = live_flow(store, recorder)
    flow.metadata[EVENTS_MARK] = changer(events_rule(close="before", event_contains="bye"))
    with pytest.raises(InjectError, match="it may still go out"):
        asyncio.run(WebSocketInjector(Master(), recorder).send(seq, True, b"late", True))
    rewriter = Rewriter(None)

    def deliver(flow, to_client, content, is_text):
        for message in (WebSocketMessage(Opcode.TEXT, False, b"late", injected=True),
                        WebSocketMessage(Opcode.TEXT, False, b"bye"),
                        WebSocketMessage(Opcode.TEXT, False, content, injected=True)):
            flow.websocket.messages.append(message)
            rewriter.websocket_message(flow)
            recorder.websocket_message(flow)

    monkeypatch.setattr(injector_module, "CONFIRM_SECONDS", 5)
    with pytest.raises(InjectError, match="not delivered"):
        asyncio.run(WebSocketInjector(Master(deliver), recorder).send(seq, True, b"next", True))
    assert not recorder.streams[flow.id].injections


@pytest.mark.parametrize("flow, refusal", [
    (None, "not opened through the proxy"),
    (SimpleNamespace(live=False, websocket=object(), metadata={}), "is closing"),
    (SimpleNamespace(live=True, websocket=None, metadata={}), "is closing"),
])
def test_send_event_refuses_a_websocket_that_is_gone(flow, refusal):
    master = Master()
    injector = WebSocketInjector(master, SimpleNamespace(live_websocket=lambda seq: flow))
    with pytest.raises(InjectError, match=refusal):
        asyncio.run(injector.send(1, True, b"x", True))
    assert master.calls == []


def test_websocket_send_refused_by_the_proxy_is_a_tool_error(store, settings, tmp_path):
    service = ProxyService(store, settings, str(tmp_path), str(tmp_path / "certs"), RuleSet(str(tmp_path)))
    seq = store.insert(make_record(1, kind="websocket", open=True))

    class Injector:
        async def send(self, *args):
            raise InjectError("an events rule closed this WebSocket connection")

    service.injector = Injector()
    with pytest.raises(ToolError, match="events rule closed"):
        asyncio.run(service.send_event({"flow": str(seq), "data": "ping"}))


def test_two_sends_in_flight_get_their_own_outcomes(store):
    recorder = Recorder(store, 1024)
    flow, seq = live_flow(store, recorder)
    flow.metadata[EVENTS_MARK] = changer(events_rule(close="before", event_contains="bye"))
    passes = [through_the_proxy(recorder), through_the_proxy(recorder, server_messages=[b"bye"])]
    injector = WebSocketInjector(Master(lambda *args: passes.pop(0)(*args)), recorder)

    async def scenario():
        return await asyncio.gather(injector.send(seq, True, b"first", True), injector.send(seq, True, b"second", True),
                                    return_exceptions=True)

    first, second = asyncio.run(scenario())
    assert first is None
    assert isinstance(second, InjectError) and "not delivered" in str(second)


def test_connection_that_ends_answers_every_send_in_flight(store):
    keeper = Recorder(store, 1024)
    flow, seq = live_flow(store, keeper)
    calls = []

    def end_after_both(*args):
        calls.append(args)
        if len(calls) == 2:
            asyncio.ensure_future(keeper.websocket_end(flow))

    injector = WebSocketInjector(Master(end_after_both), keeper)

    async def scenario():
        return await asyncio.wait_for(asyncio.gather(injector.send(seq, True, b"one", True),
                                                     injector.send(seq, True, b"two", True),
                                                     return_exceptions=True), 2)

    outcomes = asyncio.run(scenario())
    assert all(isinstance(outcome, InjectError) and "closed before the message went out" in str(outcome)
               for outcome in outcomes)
