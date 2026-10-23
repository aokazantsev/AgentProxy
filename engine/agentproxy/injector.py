class InjectError(Exception):
    pass


class WebSocketInjector:
    def __init__(self, master, recorder):
        self.master = master
        self.recorder = recorder

    def send(self, seq, to_client, content, is_text):
        flow = self.recorder.live_websocket(seq)
        if flow is None:
            raise InjectError("the WebSocket connection is closed or was not opened through the proxy: make the app "
                              "reconnect through the proxy and send into the new flow (list_flows with kind=websocket, "
                              "open=true)")
        if not flow.live or flow.websocket is None:
            raise InjectError("the WebSocket connection is closing: wait for the app to reconnect and send into "
                              "the new flow")
        self.master.commands.call("inject.websocket", flow, to_client, content, is_text)


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
