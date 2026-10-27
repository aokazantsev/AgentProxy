import functools

from .rules import APPLIED_MARK

CUT_MARK = "agentproxy_body_cut"
CUT_MESSAGE = "AgentProxy rule {0} cut the response after {1} bytes of body"
NOT_REACHED = "not cut: the body has {0} bytes, not more than {1}"
NO_BODY = "not cut: response has no body"
BROKE_EARLY = "not cut: the answer broke after {0} bytes of body, before byte {1}"
USED_UP = ("not cut: when the answer passed byte {0}, the rule was used up by other answers, disabled, deleted or "
           "changed to another action")


def cut_point(rule, length):
    if "after_bytes" in rule:
        point = rule["after_bytes"]
    elif length is None:
        return None, "not cut: after_percent needs Content-Length, the response has none; use after_bytes"
    else:
        point = max(1, length * rule["after_percent"] // 100)
    if length is not None and point >= length:
        return None, "not cut: byte {0} is not before the end of the body (Content-Length {1})".format(point, length)
    return point, None


class BodyCutter:
    def __init__(self, flow, rule_id, point, take):
        self.flow = flow
        self.rule_id = rule_id
        self.point = point
        self.take = take
        self.sent = 0
        self.fired = False
        self.given_up = False
        self.reported = False

    def _entry(self):
        return next(entry for entry in self.flow.metadata[APPLIED_MARK] if entry["id"] == self.rule_id)

    @property
    def message(self):
        return CUT_MESSAGE.format(self.rule_id, self.point)

    def __call__(self, chunk):
        if not chunk or self.fired:
            return []
        if self.given_up:
            return chunk
        room = self.point - self.sent
        if len(chunk) <= room:
            self.sent += len(chunk)
            return chunk
        if not self.take(self.rule_id):
            self.given_up = True
            self._entry()["skipped"] = USED_UP.format(self.point)
            return chunk
        self.fired = True
        self._entry()["cut_after_bytes"] = self.point
        return bytes(chunk[:room]) if room else []

    def finish(self):
        if not self.fired and not self.given_up:
            self._entry()["skipped"] = NOT_REACHED.format(self.sent, self.point) if self.sent else NO_BODY

    def broken(self):
        if not self.fired and not self.given_up:
            self._entry()["skipped"] = BROKE_EARLY.format(self.sent, self.point)


def install():
    from mitmproxy.proxy.layers import http
    original = http.HttpStream.state_stream_response_body
    if getattr(original, "agentproxy_cut", False):
        return

    @functools.wraps(original)
    def state_stream_response_body(self, event):
        yield from original(self, event)
        cutter = self.flow.metadata.get(CUT_MARK)
        if cutter is None or not cutter.fired or cutter.reported:
            return
        cutter.reported = True
        message = cutter.message
        yield http.SendHttp(http.RequestProtocolError(self.stream_id, message, http.ErrorCode.CANCEL),
                            self.context.server)
        yield from self.handle_protocol_error(
            http.ResponseProtocolError(self.stream_id, message, http.ErrorCode.GENERIC_SERVER_ERROR))

    state_stream_response_body.agentproxy_cut = True
    http.HttpStream.state_stream_response_body = state_stream_response_body
