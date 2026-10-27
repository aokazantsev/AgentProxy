import asyncio
import logging
import time
import uuid
from collections import deque

from .cutter import CUT_MARK
from .guard import OWN_PORT_MARK
from .rules import APPLIED_MARK, EVENTS_MARK, NO_REQUEST_STREAM_MARK, NO_RESPONSE_STREAM_MARK
from .sse import MAX_EVENT_BYTES, SseTap
from .store import ENGINE_STOPPED, FlowRecord
from .streamtap import IDENTITY_ENCODINGS, NO_BODY_STATUSES, StreamTap, should_stream, should_stream_request

log = logging.getLogger("agentproxy.recorder")

TLS_CLIENT_ERROR = "TLS handshake with the client failed: {0}"
CONNECT_FAILED = "CONNECT failed"
CONNECT_ADVICE = " If you plan to redirect"
CONNECT_DETAIL_CHARS = 300
STREAM_DONE_MARK = "agentproxy_stream_done"
INCOMING = "incoming"
OUTGOING = "outgoing"
MAX_EVENTS_PER_SECOND = 500
MAX_BYTES_PER_SECOND = 4 * 1024 * 1024
SKIPPED_NOTE = ("{0} events not recorded: the stream sent more than {1} events or {2} MB per second (they still reached "
                "the other side)")
LIMIT_NOTE = ("more than {0} events or {1} MB this second: the rest of the second is not recorded, a note with their count "
              "follows")
UNPACKING_AT_ONCE = 2
INJECTION_DELIVERED = "delivered"
INJECTION_DROPPED = "dropped"
INJECTION_CLOSED = "closed"


def is_event_stream(request, response):
    if request.method == "HEAD" or response.status_code in NO_BODY_STATUSES:
        return False
    return "text/event-stream" in response.headers.get("content-type", "").lower()


def is_websocket_handshake(flow):
    return flow.response.status_code == 101 and getattr(flow, "websocket", None) is not None


class LiveStream:
    def __init__(self, seq, flow=None):
        self.seq = seq
        self.tap = None
        self.flow = flow
        self.window = float("-inf")
        self.count = 0
        self.bytes = 0
        self.skipped = 0
        self.injections = deque()


def _headers(headers):
    return [[name, value] for name, value in headers.items(multi=True)]


def _encoded(message):
    return message is not None and (message.headers.get("content-encoding") or "").strip().lower() not in IDENTITY_ENCODINGS


def _content_length(message):
    try:
        return int(message.headers.get("content-length", "0"))
    except ValueError:
        return 0


def _body(message, limit):
    if message is None:
        return b"", 0
    if message.raw_content is None:
        return b"", _content_length(message)
    raw = message.raw_content
    tap = StreamTap(limit, message.headers.get("content-encoding"), wire_limit=len(raw))
    tap(raw)
    return tap.body()


def build_record(flow, limit, tap=None, error=None, request_tap=None):
    request = flow.request
    if request_tap is not None:
        request_body, request_size = request_tap.body(_content_length(request))
    else:
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
        rules=list(flow.metadata.get(APPLIED_MARK) or []),
    )
    response = flow.response
    if response is not None:
        if tap is not None:
            response_body, response_size = tap.body(_content_length(response))
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
    now = time.time()
    return FlowRecord(
        duration_ms=int((now - client.timestamp_start) * 1000) if client.timestamp_start else None,
        id=str(uuid.uuid4()), started=now, method="CONNECT", scheme="https",
        host=host, port=port, path="", url="https://{0}:{1}".format(host, port), request_headers=[],
        error=TLS_CLIENT_ERROR.format(data.conn.error or "handshake failed"),
        client=client.peername[0] if client.peername else "")


def connect_failure(response):
    if response is None:
        return CONNECT_FAILED
    reason = "{0}: {1} {2}".format(CONNECT_FAILED, response.status_code, response.reason)
    try:
        text = response.get_text(strict=False) or ""
    except Exception:
        text = ""
    detail = " ".join(text.split(CONNECT_ADVICE)[0].split())[:CONNECT_DETAIL_CHARS]
    return "{0} ({1})".format(reason, detail) if detail else reason


class Recorder:
    def __init__(self, store, body_limit, flow_log=None):
        self.store = store
        self.flow_log = flow_log
        self.body_limit = body_limit
        self.taps = {}
        self.request_taps = {}
        self.streams = {}
        self.unpacking = asyncio.Semaphore(UNPACKING_AT_ONCE)

    async def _record(self, flow, tap=None, error=None, request_tap=None):
        packed_request = _encoded(flow.request) and (request_tap is not None or bool(flow.request.raw_content))
        packed_response = _encoded(flow.response) and (tap is not None or bool(flow.response.raw_content))
        if not (packed_request or packed_response):
            return build_record(flow, self.body_limit, tap, error, request_tap)
        async with self.unpacking:
            return await asyncio.to_thread(build_record, flow, self.body_limit, tap, error, request_tap)

    def requestheaders(self, flow):
        if flow.metadata.get(NO_REQUEST_STREAM_MARK):
            return
        headers = flow.request.headers
        if should_stream_request(headers.get("content-length"), self.body_limit):
            tap = StreamTap(self.body_limit, headers.get("content-encoding"))
            self.request_taps[flow.id] = tap
            flow.request.stream = tap

    async def responseheaders(self, flow):
        if flow.metadata.get(NO_RESPONSE_STREAM_MARK):
            return
        if is_event_stream(flow.request, flow.response) and await self._open_event_stream(flow):
            return
        headers = flow.response.headers
        cutter = flow.metadata.get(CUT_MARK)
        if cutter is not None:
            tap = StreamTap(self.body_limit, headers.get("content-encoding"))
            self.taps[flow.id] = tap
            flow.response.stream = lambda chunk: tap(cutter(chunk))
            return
        if callable(flow.response.stream):
            return
        if should_stream(flow.request.method, flow.response.status_code, headers.get("content-type"),
                         headers.get("content-length"), self.body_limit):
            tap = StreamTap(self.body_limit, headers.get("content-encoding"))
            self.taps[flow.id] = tap
            flow.response.stream = tap

    def tcp_message(self, flow):
        flow.messages.clear()

    async def response(self, flow):
        if is_websocket_handshake(flow) and await self._open_stream(flow, "websocket") is not None:
            return
        await self._save(flow)

    def websocket_message(self, flow):
        messages = list(flow.websocket.messages)
        flow.websocket.messages.clear()
        live = self.streams.get(flow.id)
        if live is None:
            return
        for message in messages:
            if message.injected and live.injections:
                self._settle_injection(live, INJECTION_DROPPED if message.dropped else INJECTION_DELIVERED)
            if not self._admit(live, len(message.content)):
                continue
            notes = [name for name, active in (("injected", message.injected), ("dropped", message.dropped)) if active]
            self._add_event(live, OUTGOING if message.from_client else INCOMING,
                            "text" if message.is_text else "binary", message.content, note=", ".join(notes) or None,
                            timestamp=message.timestamp)
        self._add_rule_notes(live, flow)

    def _add_rule_notes(self, live, flow):
        changer = flow.metadata.get(EVENTS_MARK)
        if changer is not None:
            for direction, note in changer.take_notes():
                self._add_event(live, direction, "note", b"", note=note)

    def expect_injection(self, flow_id):
        waiter = asyncio.get_running_loop().create_future()
        self.streams[flow_id].injections.append(waiter)
        return waiter

    @staticmethod
    def _settle_injection(live, outcome):
        waiter = live.injections.popleft()
        if not waiter.done():
            waiter.set_result(outcome)

    async def websocket_end(self, flow):
        live = self.streams.get(flow.id)
        if live is not None:
            socket = flow.websocket
            code = socket.close_code
            self._report_skipped(live)
            self._add_event(live, OUTGOING if socket.closed_by_client else INCOMING, "close",
                            (socket.close_reason or "").encode("utf-8", "replace"),
                            name=str(code) if code is not None else None, timestamp=socket.timestamp_end)
        await self._save(flow)

    def done(self):
        try:
            self.store.finish_open(ENGINE_STOPPED)
        except Exception:
            log.exception("open streams not closed")

    async def error(self, flow):
        if flow.request is not None:
            await self._save(flow)

    async def http_connect_error(self, flow):
        await self._save(flow, connect_failure(flow.response))

    def tls_failed_client(self, data):
        server = data.context.server
        if not (server and server.address):
            return
        record = tls_failure_record(data)
        try:
            seq = self.store.insert(record)
        except Exception:
            log.exception("TLS failure not recorded")
            return
        if self.flow_log is not None:
            try:
                self.flow_log.tls_failed(record, seq)
            except Exception:
                log.exception("TLS failure not logged")

    async def _open_event_stream(self, flow):
        live = await self._open_stream(flow, "sse")
        if live is None:
            return False
        editor = flow.response.stream if callable(flow.response.stream) else None
        live.tap = SseTap(flow.response.headers.get("content-encoding"),
                          lambda event: self._sse_event(live, event),
                          lambda reason: self._add_event(live, INCOMING, "note", b"", note=reason))
        flow.response.stream = live.tap if editor is None else self._after_editor(flow, live, editor)
        return True

    def _after_editor(self, flow, live, editor):
        def stream(chunk):
            data = editor(chunk)
            if data:
                live.tap(data)
            self._add_rule_notes(live, flow)
            return data
        return stream

    async def _open_stream(self, flow, kind):
        if flow.metadata.get(OWN_PORT_MARK):
            return None
        try:
            record = await self._record(flow, request_tap=self.request_taps.get(flow.id))
            record.kind = kind
            record.open = True
            record.duration_ms = None
            record.response_body = b""
            record.response_body_size = 0
            seq = self.store.insert(record)
        except Exception:
            log.exception("flow %s not recorded", flow.id)
            return None
        if seq is None:
            return None
        live = LiveStream(seq, flow if kind == "websocket" else None)
        self.streams[flow.id] = live
        return live

    def live_websocket(self, seq):
        for live in self.streams.values():
            if live.seq == seq and live.flow is not None:
                return live.flow
        return None

    def _admit(self, live, size):
        now = time.monotonic()
        if now - live.window >= 1:
            self._report_skipped(live)
            live.window = now
            live.count = 0
            live.bytes = 0
        stored = min(size, MAX_EVENT_BYTES)
        if live.count >= MAX_EVENTS_PER_SECOND or (live.count and live.bytes + stored > MAX_BYTES_PER_SECOND):
            if not live.skipped:
                self._add_event(live, INCOMING, "note", b"", note=LIMIT_NOTE.format(
                    MAX_EVENTS_PER_SECOND, MAX_BYTES_PER_SECOND // (1024 * 1024)))
            live.skipped += 1
            return False
        live.count += 1
        live.bytes += stored
        return True

    def _report_skipped(self, live):
        if live.skipped:
            self._add_event(live, INCOMING, "note", b"", note=SKIPPED_NOTE.format(
                live.skipped, MAX_EVENTS_PER_SECOND, MAX_BYTES_PER_SECOND // (1024 * 1024)))
            live.skipped = 0

    def _add_event(self, live, direction, kind, data, **fields):
        try:
            self.store.add_event(live.seq, direction, kind, data, limit=MAX_EVENT_BYTES, **fields)
        except Exception:
            log.exception("event of stream %s not recorded", live.seq)

    def _sse_event(self, live, event):
        if not self._admit(live, len(event.data)):
            return
        notes = []
        if event.retry is not None:
            notes.append("retry {0}".format(event.retry))
        if event.incomplete:
            notes.append("incomplete at the end of the stream")
        self._add_event(live, INCOMING, event.kind, event.data, size=event.size, name=event.name,
                        event_id=event.event_id, truncated=event.truncated, note=", ".join(notes) or None)

    async def _finish_stream(self, flow, live, error, request_tap):
        if live.tap is not None:
            try:
                live.tap.finish()
            except Exception:
                log.exception("stream %s not closed cleanly", flow.id)
        self._report_skipped(live)
        record = await self._record(flow, error=error, request_tap=request_tap)
        record.open = False
        record.response_body = b""
        record.response_body_size = live.tap.wire_size if live.tap is not None else 0
        socket = getattr(flow, "websocket", None)
        ended = socket.timestamp_end if socket is not None and socket.timestamp_end else time.time()
        if flow.request.timestamp_start:
            record.duration_ms = int((ended - flow.request.timestamp_start) * 1000)
        return self.store.finish(record)

    async def _save(self, flow, error=None):
        tap = self.taps.pop(flow.id, None)
        request_tap = self.request_taps.pop(flow.id, None)
        live = self.streams.pop(flow.id, None)
        if live is not None:
            while live.injections:
                self._settle_injection(live, INJECTION_CLOSED)
        if flow.metadata.get(OWN_PORT_MARK) or flow.metadata.get(STREAM_DONE_MARK):
            return
        if live is not None:
            flow.metadata[STREAM_DONE_MARK] = True
            try:
                seq = await self._finish_stream(flow, live, error, request_tap)
            except Exception:
                log.exception("flow %s not recorded", flow.id)
                return
            self._log_saved(flow, seq, error)
            return
        try:
            seq = self.store.insert(await self._record(flow, tap, error, request_tap))
        except Exception:
            log.exception("flow %s not recorded", flow.id)
            return
        self._log_saved(flow, seq, error)

    def _log_saved(self, flow, seq, error):
        if self.flow_log is not None:
            try:
                self.flow_log.saved(flow, seq, error)
            except Exception:
                log.exception("flow %s not logged", flow.id)
