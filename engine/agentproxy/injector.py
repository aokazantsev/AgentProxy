class InjectError(Exception):
    pass


class WebSocketInjector:
    def __init__(self, master, recorder):
        self.master = master
        self.recorder = recorder

    def send(self, seq, to_client, content, is_text):
        flow = self.recorder.live_websocket(seq)
        if flow is None:
            raise InjectError("the WebSocket connection is closed or was not opened through the proxy")
        if not flow.live or flow.websocket is None:
            raise InjectError("the WebSocket connection is closing")
        self.master.commands.call("inject.websocket", flow, to_client, content, is_text)
