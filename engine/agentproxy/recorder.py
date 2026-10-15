import logging
import time

from .store import FlowRecord

log = logging.getLogger("agentproxy.recorder")


def _headers(headers):
    return [[name, value] for name, value in headers.items(multi=True)]


def _body(message, limit):
    if message is None:
        return b"", 0
    try:
        content = message.get_content(strict=False)
    except ValueError:
        content = message.raw_content
    content = content or b""
    return content[:limit], len(content)


def build_record(flow, limit):
    request = flow.request
    request_body, request_size = _body(request, limit)
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
    return record


class Recorder:
    def __init__(self, store, body_limit):
        self.store = store
        self.body_limit = body_limit

    def response(self, flow):
        self._save(flow)

    def error(self, flow):
        if flow.request is not None:
            self._save(flow)

    def _save(self, flow):
        try:
            self.store.insert(build_record(flow, self.body_limit))
        except Exception:
            log.exception("flow %s not recorded", flow.id)
