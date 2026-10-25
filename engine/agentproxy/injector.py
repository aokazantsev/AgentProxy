import asyncio

from .recorder import INJECTION_CLOSED, INJECTION_DROPPED
from .rules import EVENTS_MARK

CONFIRM_SECONDS = 5
RULE_CLOSED = ("an events rule closed this WebSocket connection: wait for the app to reconnect and send into the new "
               "flow")


class InjectError(Exception):
    pass


class WebSocketInjector:
    def __init__(self, master, recorder):
        self.master = master
        self.recorder = recorder

    async def send(self, seq, to_client, content, is_text):
        flow = self.recorder.live_websocket(seq)
        if flow is None:
            raise InjectError("the WebSocket connection is closed or was not opened through the proxy: make the app "
                              "reconnect through the proxy and send into the new flow (list_flows with kind=websocket, "
                              "open=true)")
        if not flow.live or flow.websocket is None:
            raise InjectError("the WebSocket connection is closing: wait for the app to reconnect and send into "
                              "the new flow")
        changer = flow.metadata.get(EVENTS_MARK)
        if changer is not None and changer.closed:
            raise InjectError(RULE_CLOSED)
        waiter = self.recorder.expect_injection(flow.id)
        self.master.commands.call("inject.websocket", flow, to_client, content, is_text)
        try:
            outcome = await asyncio.wait_for(waiter, CONFIRM_SECONDS)
        except asyncio.TimeoutError:
            raise InjectError("the proxy did not pass the message on within {0} s: it may still go out, so check "
                              "list_events of the flow before sending again".format(CONFIRM_SECONDS))
        if outcome == INJECTION_DROPPED:
            raise InjectError("the message was not delivered: " + RULE_CLOSED)
        if outcome == INJECTION_CLOSED:
            raise InjectError("the WebSocket connection closed before the message went out: wait for the app to "
                              "reconnect and send into the new flow")


class StreamLookup:
    def __init__(self, host, recorder):
        self.host = host
        self.recorder = recorder

    def find(self, seq):
        for flow_id, live in list(self.recorder.streams.items()):
            if live.seq == seq:
                return self.host.find(flow_id)
        return None

    def close_rules(self, rule_ids):
        self.host.close_rules(rule_ids)
