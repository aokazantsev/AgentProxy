import logging
import time
import uuid

from .store import FlowRecord
from .streamtap import StreamTap, decode_body, should_stream, should_stream_request

log = logging.getLogger("agentproxy.recorder")

UNDECODED_NOTE = "[AgentProxy] response body was passed through but not recorded: {0}"
TLS_CLIENT_ERROR = "TLS handshake with the client failed: {0}"


def _headers(headers):
    return [[name, value] for name, value in headers.items(multi=True)]


def _body(message, limit):
    if message is None:
        return b"", 0
    raw = message.raw_content or b""
    decoded = decode_body(raw, message.headers.get("content-encoding"), limit)
    if decoded is None:
        return raw[:limit], len(raw)
    return decoded


def _tapped(tap):
    if tap.decode_error is not None:
        note = UNDECODED_NOTE.format(tap.decode_error).encode("utf-8")
        return note, len(note)
    return bytes(tap.captured), tap.size


def build_record(flow, limit, tap=None, error=None, request_tap=None):
    request = flow.request
    request_body, request_size = _tapped(request_tap) if request_tap is not None else _body(request, limit)
    record = FlowRecord(
        id=flow.id,
        started=request.timestamp_start or time.time(),
        method=request.method,
        scheme=request.scheme,
        host=request.pretty_host,
        port=request.port,
        path=request.path,
        url=request.pretty_url,
        http_version=request.http_version,
        request_headers=_headers(request.headers),
        request_body=request_body,
        request_body_size=request_size,
        request_content_type=request.headers.get("content-type", ""),
        client=flow.client_conn.peername[0] if flow.client_conn and flow.client_conn.peername else "",
    )
    response = flow.response
    if response is not None:
        if tap is not None:
            response_body, response_size = _tapped(tap)
        else:
            response_body, response_size = _body(response, limit)
        record.status = response.status_code
        record.reason = response.reason
        record.response_headers = _headers(response.headers)
        record.response_body = response_body
        record.response_body_size = response_size
        record.response_content_type = response.headers.get("content-type", "")
        if response.timestamp_end and request.timestamp_start:
            record.duration_ms = int((response.timestamp_end - request.timestamp_start) * 1000)
    if flow.error is not None:
        record.error = flow.error.msg
    elif error is not None:
        record.error = error
    return record


def tls_failure_record(data):
    client = data.context.client
    server = data.context.server
    host = client.sni or (server.address[0] if server and server.address else "unknown")
    port = server.address[1] if server and server.address else 443
    return FlowRecord(
        id=str(uuid.uuid4()), started=time.time(), method="CONNECT", scheme="https",
        host=host, port=port, path="", url="https://{0}:{1}".format(host, port), request_headers=[],
        error=TLS_CLIENT_ERROR.format(data.conn.error or "handshake failed"),
        client=client.peername[0] if client.peername else "")


class Recorder:
    def __init__(self, store, body_limit):
        self.store = store
        self.body_limit = body_limit
        self.taps = {}
        self.request_taps = {}

    def requestheaders(self, flow):
        headers = flow.request.headers
        if should_stream_request(headers.get("content-length"), self.body_limit):
            tap = StreamTap(self.body_limit, headers.get("content-encoding"))
            self.request_taps[flow.id] = tap
            flow.request.stream = tap

    def responseheaders(self, flow):
        headers = flow.response.headers
        if should_stream(flow.request.method, flow.response.status_code, headers.get("content-type"),
                         headers.get("content-length"), self.body_limit):
            tap = StreamTap(self.body_limit, headers.get("content-encoding"))
            self.taps[flow.id] = tap
            flow.response.stream = tap

    def response(self, flow):
        self._save(flow)

    def error(self, flow):
        if flow.request is not None:
            self._save(flow)

    def http_connect_error(self, flow):
        reason = "CONNECT failed"
        if flow.response is not None:
            reason += ": {0} {1}".format(flow.response.status_code, flow.response.reason)
        self._save(flow, reason)

    def tls_failed_client(self, data):
        try:
            self.store.insert(tls_failure_record(data))
        except Exception:
            log.exception("TLS failure not recorded")

    def _save(self, flow, error=None):
        tap = self.taps.pop(flow.id, None)
        request_tap = self.request_taps.pop(flow.id, None)
        try:
            self.store.insert(build_record(flow, self.body_limit, tap, error, request_tap))
        except Exception:
            log.exception("flow %s not recorded", flow.id)
