import asyncio
import codecs
import json
import logging
import mimetypes
import re
import time

from .bodies import CHARSET, SLOW_CHARSETS, TEXT_CHARSETS
from .cutter import CUT_MARK, NO_BODY, BodyCutter, cut_point
from .eventrules import INCOMING, OUTGOING, EventChanger, EventStreams, SseEditor
from .guard import OWN_PORT_MARK
from .recorder import is_event_stream, is_websocket_handshake
from .rules import (APPLIED_MARK, EVENTS_MARK, MAX_DELAY_MS, MAX_FILE_BYTES, NO_REQUEST_STREAM_MARK,
                    NO_RESPONSE_STREAM_MARK, PLAN_MARK, TERMINAL_ACTIONS, RuleError, changes_body, limits_speed)
from .speed import SCOPE_CONNECTION, SCOPE_REQUEST, rule_limits
from .streamhost import DEFAULT_HEADERS as DEFAULT_STREAM_HEADERS, LOOPBACK, STREAM_HEADER, StreamRefused
from .streamtap import IDENTITY_ENCODINGS, NO_BODY_STATUSES, decoded_pieces

log = logging.getLogger("agentproxy.rewriter")

EDITOR_MARK = "agentproxy_body_editor"
LABEL = re.compile(r"[A-Za-z0-9._-]{1,32}")
BROKEN_BODY = "the response broke before its end"
LIMIT_TEXT = "larger than {0} MB".format(MAX_FILE_BYTES // (1024 * 1024))
UPLOAD_TOO_LARGE = "request body is {0}; the rule cannot buffer it"
UPLOAD_GREW = "request body without length grew {0}; it went on to the server, the client got no answer"
JSON_SNIFF_BYTES = 1024 * 1024
PACK_NAMES = {"x-gzip": "gzip"}
PLAIN_TYPES = ("text/plain", "application/octet-stream")
RESPOND_ERROR_MARK = "agentproxy_respond_error"
DELAY_MARK = "agentproxy_delay"
ORIGIN_MARK = "agentproxy_stream_origin"
SPEED_MARK = "agentproxy_speed"
NO_LINK = "speed limit not applied: the client connection is not TCP"
NO_CLOSE_LINK = "the stream was not closed: the client connection is not TCP"
CLOSE_FALLBACK_SECONDS = 1.0
COMPRESSED_STREAM = ("events not changed: the stream is compressed ({0}); add a rewrite rule with request_headers "
                     "Accept-Encoding: identity for this stream")
SERVED_STREAM = "events not changed: the stream comes from an sse rule, send_event writes it"
HTTP1 = b"http/1.1"
HTTP2 = b"h2"
BUFFERING_ACTIONS = ("respond", "sse")
MIME_TYPES = mimetypes.MimeTypes()
ORDER_MARKED_CHARSETS = {"utf-16": (b"\xff\xfe", b"\xfe\xff"),
                         "utf-32": (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")}
PARALLEL_EDITS = 2
WAIT_STEP = 0.5
NOT_FOUND_CHARS = 100


class BodySkipped(Exception):
    pass


def _entry(flow, rule):
    return next(entry for entry in flow.metadata[APPLIED_MARK] if entry["id"] == rule["id"])


def _set_headers(headers, changes):
    for name, value in changes.items():
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value


def _read_file(path):
    with open(path, "rb") as stream:
        content = stream.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise OSError("file is larger than {0} MB".format(MAX_FILE_BYTES // (1024 * 1024)))
    return content


def _looks_like_json(content):
    if len(content) > JSON_SNIFF_BYTES:
        trimmed = content[len(codecs.BOM_UTF8):] if content.startswith(codecs.BOM_UTF8) else content
        edges = trimmed.lstrip()[:1] + trimmed.rstrip()[-1:]
        return edges in (b"{}", b"[]") and _is_utf8(content)
    try:
        json.loads(content.decode("utf-8-sig"))
        return True
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False


def _is_utf8(content):
    try:
        content.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def guess_content_type(content, path=None):
    guessed = MIME_TYPES.guess_type(path)[0] if path else None
    if guessed and guessed.startswith("text/") and _is_utf8(content):
        guessed += "; charset=utf-8"
    if guessed and guessed.split(";")[0] not in PLAIN_TYPES and len(content) > JSON_SNIFF_BYTES:
        return guessed
    if _looks_like_json(content):
        return "application/json; charset=utf-8"
    if guessed:
        return guessed
    if not content:
        return None
    if b"\x00" in content[:1024] or not _is_utf8(content):
        return "application/octet-stream"
    return "text/plain; charset=utf-8"


def _response_headers(rule, content_type):
    headers = dict(rule.get("headers", {}))
    if not rule.get("content_type") and any(name.lower() == "content-type" for name in headers):
        return headers
    content_type = rule.get("content_type") or content_type
    if content_type:
        for name in list(headers):
            if name.lower() == "content-type":
                del headers[name]
        headers["Content-Type"] = content_type
    return headers


def _declared_length(headers):
    try:
        return int(headers.get("content-length", "0"))
    except ValueError:
        return 0


def _has_body(headers):
    return "transfer-encoding" in headers or _declared_length(headers) > 0


def _has_no_body(method, status):
    return method == "HEAD" or status < 200 or status in NO_BODY_STATUSES


def _unpack(raw, encoding):
    if encoding in IDENTITY_ENCODINGS or not raw:
        return raw
    unpacked = bytearray()
    try:
        for piece in decoded_pieces(encoding, raw):
            unpacked.extend(piece)
            if len(unpacked) > MAX_FILE_BYTES:
                raise BodySkipped(LIMIT_TEXT + " unpacked")
    except BodySkipped:
        raise
    except Exception as error:
        kind = error.__class__
        name = kind.__name__ if kind.__module__ == "builtins" else kind.__module__ + "." + kind.__name__
        raise BodySkipped("{0} body could not be unpacked ({1})".format(_label(encoding), name))
    return bytes(unpacked)



def _label(value):
    return value if LABEL.fullmatch(value) else "(unusual value)"

def _text_charset(name):
    try:
        normalized = codecs.lookup(name).name
    except (LookupError, ValueError):
        return None
    return normalized if normalized in TEXT_CHARSETS else None


def _writable_charset(name):
    try:
        return codecs.lookup(name).name not in SLOW_CHARSETS
    except (LookupError, ValueError):
        return False


def _pack(data, encoding):
    if encoding in IDENTITY_ENCODINGS:
        return data
    from mitmproxy.net import encoding as content_codings
    packer = content_codings.custom_encode.get(PACK_NAMES.get(encoding, encoding))
    if packer is None:
        raise ValueError("unsupported content-encoding " + encoding)
    return packer(data)


def _encode_body(flow, rule, content_type):
    text = rule.get("body", "")
    match = CHARSET.search(content_type)
    charset = match.group(1) if match else "utf-8"
    if not text:
        return b""
    try:
        if not _writable_charset(charset):
            raise LookupError(charset)
        return text.encode(charset)
    except (UnicodeError, LookupError):
        _entry(flow, rule)["error"] = "body cannot be encoded as {0}; sent as utf-8".format(_label(charset))
        return text.encode("utf-8")


async def _wait(flow, seconds):
    from mitmproxy.connection import ConnectionState
    left = seconds
    while left > 0 and flow.client_conn.state != ConnectionState.CLOSED:
        step = min(left, WAIT_STEP)
        await asyncio.sleep(step)
        left -= step


class BodyDiscarder:
    def __call__(self, chunk):
        return []


class BodyEditor:
    def __init__(self, flow, rules):
        response = flow.response
        self.flow = flow
        self.rules = rules
        self.encoding = response.headers.get("content-encoding", "").strip().lower()
        match = CHARSET.search(response.headers.get("content-type", ""))
        self.charset = match.group(1) if match else "utf-8"

    def skip(self, reason):
        for rule in self.rules:
            entry = _entry(self.flow, rule)
            entry.pop("replaced", None)
            entry.pop("not_found", None)
            entry.setdefault("skipped", "body not changed: " + reason)

    def _decode(self, raw, rule):
        try:
            charset = _text_charset(self.charset)
            if charset is None:
                raise BodySkipped("charset {0} is not supported for editing".format(_label(self.charset)))
            unpacked = _unpack(raw, self.encoding)
            marks = ORDER_MARKED_CHARSETS.get(charset)
            if marks and unpacked and not unpacked.startswith(marks):
                raise BodySkipped("{0} body has no byte order mark".format(_label(charset)))
            return unpacked.decode(self.charset)
        except BodySkipped as reason:
            _entry(self.flow, rule)["skipped"] = "body not changed: {0}".format(reason)
        except UnicodeDecodeError:
            _entry(self.flow, rule)["skipped"] = "body not changed: body is not {0} text".format(_label(self.charset))
        return None

    def edit(self, raw):
        text = None
        replaced_by = {}
        changed = False
        counts = {}
        for position, rule in enumerate(self.rules):
            if "response_body" in rule:
                text = rule["response_body"]
                changed = True
                for earlier in self.rules[:position]:
                    replaced_by[earlier["id"]] = rule["id"]
            elif text is None:
                text = self._decode(raw, rule)
                if text is None:
                    continue
            if "replace" in rule:
                replaced = 0
                missing = []
                for item in rule["replace"]:
                    count = text.count(item["find"])
                    if len(text) + count * (len(item["with"]) - len(item["find"])) > MAX_FILE_BYTES:
                        self.skip(LIMIT_TEXT + " after replace")
                        return raw
                    replaced += count
                    if not count:
                        missing.append(item["find"][:NOT_FOUND_CHARS])
                    text = text.replace(item["find"], item["with"])
                counts[rule["id"]] = (replaced, missing)
                changed = changed or replaced > 0
        for rule in self.rules:
            if rule["id"] in counts:
                entry = _entry(self.flow, rule)
                entry["replaced"], missing = counts[rule["id"]]
                if missing:
                    entry["not_found"] = missing
        if not changed:
            return raw
        try:
            if not _writable_charset(self.charset):
                raise LookupError(self.charset)
            encoded = text.encode(self.charset)
            if len(encoded) > MAX_FILE_BYTES:
                self.skip(LIMIT_TEXT + " after replace")
                return raw
            packed = _pack(encoded, self.encoding)
        except (UnicodeEncodeError, LookupError, ValueError) as error:
            self.skip("new body cannot be encoded as {0} ({1})".format(
                " ".join(_label(part) for part in (self.charset, self.encoding) if part), error.__class__.__name__))
            return raw
        for rule in self.rules:
            if rule["id"] in replaced_by:
                entry = _entry(self.flow, rule)
                entry.pop("replaced", None)
                entry.pop("not_found", None)
                entry.setdefault("skipped", "body not changed: replaced by response_body of rule {0}".format(
                    replaced_by[rule["id"]]))
        return packed


class Rewriter:
    def __init__(self, rule_set, streams=None, speeds=None, events=None):
        self.rules = rule_set
        self.streams = streams
        self.speeds = speeds
        self.events = events if events is not None else EventStreams()
        self.lazy_clients = set()
        self.http1_clients = set()
        self.edits = asyncio.Semaphore(PARALLEL_EDITS)

    def http_connect(self, flow):
        if flow.metadata.get(OWN_PORT_MARK):
            return
        if self.rules.may_answer(flow.request.host):
            from mitmproxy import http
            now = time.time()
            flow.response = http.Response(flow.request.data.http_version, 200, b"Connection established",
                                          http.Headers(), b"", None, now, now)
            self.lazy_clients.add(flow.client_conn.id)

    def tls_clienthello(self, data):
        client = data.context.client
        offers = tuple(client.alpn_offers or ())
        if (self.speeds is not None and client.sni and HTTP2 in offers and HTTP1 in offers
                and self.rules.needs_http1(client.sni)):
            client.alpn_offers = tuple(offer for offer in offers if offer != HTTP2)
            self.http1_clients.add(client.id)
        if client.id not in self.lazy_clients:
            return
        self.lazy_clients.discard(client.id)
        data.establish_server_tls_first = False

    def tls_start_client(self, tls_start):
        client_id = tls_start.context.client.id
        if client_id not in self.http1_clients:
            return
        self.http1_clients.discard(client_id)
        settings = tls_start.ssl_conn.get_app_data() if tls_start.ssl_conn is not None else None
        if isinstance(settings, dict):
            settings["http2"] = False

    def client_disconnected(self, client):
        self.lazy_clients.discard(client.id)
        self.http1_clients.discard(client.id)

    def _limit_speed(self, flow, plan):
        link = self.speeds.get(flow.client_conn.id) if self.speeds is not None else None
        limited = [rule for rule in plan if limits_speed(rule)]
        whole_connection = flow.request.is_http2
        if not limited:
            if link is not None and not whole_connection:
                link.lift()
            return
        if link is None:
            for rule in limited:
                _entry(flow, rule)["skipped"] = NO_LINK
            return
        scope = SCOPE_CONNECTION if whole_connection else SCOPE_REQUEST
        link.limit({rule["id"]: rule_limits(rule) for rule in limited}, scope)
        flow.metadata[SPEED_MARK] = link
        for rule in limited:
            _entry(flow, rule)["limited"] = scope

    async def requestheaders(self, flow):
        if flow.metadata.get(OWN_PORT_MARK):
            return
        request = flow.request
        plan = self.rules.plan(request.pretty_host, request.path, request.method, request.pretty_url)
        if not plan:
            self._limit_speed(flow, plan)
            return
        flow.metadata[PLAN_MARK] = plan
        flow.metadata[APPLIED_MARK] = [dict({"id": rule["id"], "action": rule["action"]},
                                            **({"name": rule["name"]} if "name" in rule else {})) for rule in plan]
        self._limit_speed(flow, plan)
        for rule in plan:
            if "request_headers" in rule:
                _set_headers(request.headers, rule["request_headers"])
        if plan[-1]["action"] in TERMINAL_ACTIONS:
            flow.metadata[NO_REQUEST_STREAM_MARK] = True
            if plan[-1]["action"] in BUFFERING_ACTIONS and _declared_length(request.headers) > MAX_FILE_BYTES:
                self._refuse_upload(flow, plan[-1], UPLOAD_TOO_LARGE)
                return
        delay = min(sum(rule.get("delay_ms", 0) for rule in plan), MAX_DELAY_MS) / 1000.0
        if plan[-1]["action"] == "drop" and _has_body(request.headers):
            if not delay:
                flow.kill()
            elif "transfer-encoding" in request.headers or _declared_length(request.headers) > MAX_FILE_BYTES:
                _entry(flow, plan[-1])["skipped"] = "delay not applied: request body has no length or is larger than {0} MB".format(
                    MAX_FILE_BYTES // (1024 * 1024))
                flow.kill()
            else:
                flow.metadata[DELAY_MARK] = delay
            return
        if plan[-1]["action"] == "drop":
            if delay:
                await _wait(flow, delay)
            flow.kill()
        elif delay:
            flow.metadata[DELAY_MARK] = delay

    @staticmethod
    def _refuse_upload(flow, rule, message):
        _entry(flow, rule)["error"] = message.format(LIMIT_TEXT)
        flow.kill()

    async def request(self, flow):
        link = flow.metadata.get(SPEED_MARK)
        if link is not None:
            link.lift_upload()
        plan = flow.metadata.get(PLAN_MARK)
        if not plan or flow.error is not None:
            return
        delay = flow.metadata.pop(DELAY_MARK, 0)
        if plan[-1]["action"] in BUFFERING_ACTIONS and flow.request.stream:
            self._refuse_upload(flow, plan[-1], UPLOAD_GREW)
            return
        if delay:
            await _wait(flow, delay)
        if plan[-1]["action"] == "drop":
            flow.kill()
        elif plan[-1]["action"] == "respond":
            await self._respond(flow, plan[-1])
        elif plan[-1]["action"] == "sse":
            self._serve_stream(flow, plan[-1])

    def _serve_stream(self, flow, rule):
        from mitmproxy import http
        try:
            current = self.rules.get(rule["id"])
        except RuleError:
            current = None
        if current is None or not current["enabled"] or current["action"] != "sse":
            self._refuse_stream(flow, rule, "the rule was deleted, disabled or changed while the request waited")
            return
        if flow.request.method == "HEAD":
            headers = dict(current.get("headers", {}))
            named = {name.lower() for name in headers}
            headers.update((name, value) for name, value in DEFAULT_STREAM_HEADERS if name.lower() not in named)
            flow.response = http.Response.make(200, b"", headers)
            flow.metadata[NO_RESPONSE_STREAM_MARK] = True
            return
        try:
            if self.streams is None:
                raise StreamRefused("no stream listener")
            stream = self.streams.reserve(flow.id, rule["id"], current.get("headers", {}), current.get("body", ""))
        except StreamRefused as error:
            self.rules.note_error(rule["id"], str(error))
            self._refuse_stream(flow, rule, str(error))
            return
        self.rules.clear_error(rule["id"])
        request = flow.request
        flow.metadata[ORIGIN_MARK] = (request.scheme, request.host, request.port, request.host_header)
        request.scheme = "http"
        request.host = LOOPBACK
        request.port = stream.port
        request.headers[STREAM_HEADER] = stream.token

    @staticmethod
    def _refuse_stream(flow, rule, message):
        from mitmproxy import http
        log.warning("rule %s: %s", rule["id"], message)
        _entry(flow, rule)["error"] = message
        text = "AgentProxy rule {0}: {1}".format(rule["id"], message).encode("utf-8")
        flow.response = http.Response.make(503, text, {"Content-Type": "text/plain; charset=utf-8"})
        flow.metadata[NO_RESPONSE_STREAM_MARK] = True

    def _restore_request(self, flow):
        origin = flow.metadata.pop(ORIGIN_MARK, None)
        if origin is None:
            return
        request = flow.request
        scheme, host, port, host_header = origin
        request.scheme = scheme
        request.host = host
        request.port = port
        if host_header is not None:
            request.host_header = host_header
        request.headers.pop(STREAM_HEADER, None)

    def error(self, flow):
        self._restore_request(flow)
        editor = flow.metadata.get(EDITOR_MARK)
        if editor is not None and flow.response is not None:
            editor.skip(BROKEN_BODY)
        cutter = flow.metadata.get(CUT_MARK)
        if cutter is not None:
            cutter.broken()
        self.events.release(flow.id)
        if self.streams is not None:
            self.streams.release(flow.id)

    def _closer(self, flow):
        def close(moment, direction):
            link = self.speeds.get(flow.client_conn.id) if self.speeds is not None else None
            if link is None:
                return NO_CLOSE_LINK
            loop = asyncio.get_running_loop()
            if moment == "after" and direction == INCOMING:
                link.writer.close_after_write = True
                loop.call_later(CLOSE_FALLBACK_SECONDS, link.writer.close)
            else:
                loop.call_soon(link.writer.close)
            return None
        return close

    def _start_events(self, flow, refusal=None):
        if flow.metadata.get(OWN_PORT_MARK) or flow.metadata.get(EVENTS_MARK):
            return None
        plan = flow.metadata.get(PLAN_MARK)
        if plan and plan[-1]["action"] == "sse":
            refusal = SERVED_STREAM
        request = flow.request
        rules = self.rules.plan_events(request.pretty_host, request.path, request.method, request.pretty_url,
                                       count=refusal is None, websocket=is_websocket_handshake(flow))
        if not rules:
            return None
        changer = EventChanger(flow, rules, self._closer(flow), whole_connection=request.is_http2)
        if refusal is not None:
            changer.give_up(refusal)
            return None
        flow.metadata[EVENTS_MARK] = changer
        self.events.add(flow.id, changer)
        return changer

    def _edit_event_stream(self, flow):
        response = flow.response
        if (flow.metadata.get(NO_RESPONSE_STREAM_MARK) or not 200 <= response.status_code < 300
                or not is_event_stream(flow.request, response)):
            return
        encoding = response.headers.get("content-encoding", "").strip().lower()
        refusal = COMPRESSED_STREAM.format(_label(encoding)) if encoding not in IDENTITY_ENCODINGS else None
        changer = self._start_events(flow, refusal)
        if changer is None:
            return
        response.headers.pop("content-length", None)
        if flow.request.http_version == "HTTP/1.1" and response.http_version != "HTTP/1.0":
            response.headers["transfer-encoding"] = "chunked"
        response.stream = SseEditor(changer)

    def websocket_message(self, flow):
        changer = flow.metadata.get(EVENTS_MARK)
        if changer is None or not flow.websocket.messages:
            return
        message = flow.websocket.messages[-1]
        if changer.closed:
            message.drop()
            return
        if message.injected:
            return
        text, deliver = changer.handle(OUTGOING if message.from_client else INCOMING,
                                       message.text if message.is_text else None)
        if not deliver:
            message.drop()
        elif message.is_text and text != message.text:
            message.text = text

    def websocket_end(self, flow):
        self.events.release(flow.id)

    async def _respond(self, flow, rule):
        from mitmproxy import http
        status = rule.get("status", 200)
        path = rule.get("body_file")
        as_is = False
        guessed = None
        declared = next((value for name, value in _response_headers(rule, None).items()
                         if name.lower() == "content-type"), "")
        if path:
            try:
                async with self.edits:
                    content = await asyncio.to_thread(_read_file, path)
                    if not declared:
                        guessed = await asyncio.to_thread(guess_content_type, content, path)
                as_is = True
            except OSError as error:
                message = "body_file {0} unreadable: {1}".format(path, error)
                log.warning("rule %s: %s", rule["id"], message)
                self.rules.note_error(rule["id"], message)
                _entry(flow, rule)["error"] = message
                flow.metadata[RESPOND_ERROR_MARK] = True
                status = 502
                content = "AgentProxy rule {0}: body_file unreadable, see list_rules".format(rule["id"]).encode("utf-8")
                rule = {"id": rule["id"], "content_type": "text/plain; charset=utf-8"}
            else:
                self.rules.clear_error(rule["id"])
        else:
            content = _encode_body(flow, rule, declared)
            if not declared:
                guessed = await asyncio.to_thread(guess_content_type, content, None)
        headers = _response_headers(rule, guessed)
        if not as_is and status not in NO_BODY_STATUSES:
            content = await self._pack_body(flow, rule, headers, content)
        response = http.Response.make(status, b"")
        response.headers = http.Headers([(name.encode("utf-8"), value.encode("utf-8"))
                                         for name, value in headers.items()])
        response.raw_content = content
        if status not in NO_BODY_STATUSES:
            response.headers["content-length"] = str(len(content))
        if flow.request.method == "HEAD":
            response.raw_content = b""
        flow.response = response
        flow.metadata[NO_RESPONSE_STREAM_MARK] = True

    async def _pack_body(self, flow, rule, headers, content):
        names = [name for name in headers if name.lower() == "content-encoding"]
        encoding = headers[names[0]].strip().lower() if names else ""
        try:
            async with self.edits:
                return await asyncio.to_thread(_pack, content, encoding)
        except ValueError:
            for name in names:
                del headers[name]
            _entry(flow, rule)["error"] = "unsupported content-encoding {0}; body sent unpacked".format(encoding)
            return content

    def responseheaders(self, flow):
        self._restore_request(flow)
        self._rewrite_response(flow)
        self._edit_event_stream(flow)
        self._start_cut(flow)

    def _start_cut(self, flow):
        plan = flow.metadata.get(PLAN_MARK)
        rules = [rule for rule in plan or [] if rule["action"] == "cut"]
        if not rules:
            return
        length = None
        if "content-length" in flow.response.headers and "transfer-encoding" not in flow.response.headers:
            length = _declared_length(flow.response.headers)
        refusal = self._cut_refusal(flow, plan, length)
        chosen = None
        for rule in rules:
            point, reason = (None, refusal) if refusal else cut_point(rule, length)
            if reason:
                _entry(flow, rule)["skipped"] = reason
            elif chosen is None or point < chosen[1]:
                if chosen is not None:
                    _entry(flow, chosen[0])["skipped"] = "not cut: rule {0} cuts earlier".format(rule["id"])
                chosen = (rule, point)
            else:
                _entry(flow, rule)["skipped"] = "not cut: rule {0} cuts earlier".format(chosen[0]["id"])
        if chosen is None:
            return
        response = flow.response
        if length is None and "transfer-encoding" not in response.headers and flow.request.http_version == "HTTP/1.1":
            response.headers["transfer-encoding"] = "chunked"
            response.headers["connection"] = "close"
            if response.http_version == "HTTP/1.0":
                response.http_version = "HTTP/1.1"
        cutter = BodyCutter(flow, chosen[0]["id"], chosen[1], self.rules.take)
        flow.metadata[CUT_MARK] = cutter
        response.stream = cutter

    @staticmethod
    def _cut_refusal(flow, plan, length):
        response = flow.response
        if plan[-1]["action"] in BUFFERING_ACTIONS:
            return "not cut: the answer comes from rule {0}, not from the server".format(plan[-1]["id"])
        if _has_no_body(flow.request.method, response.status_code) or length == 0:
            return NO_BODY
        if flow.metadata.get(EDITOR_MARK) is not None:
            return "not cut: the body is changed by a rewrite rule and goes to the app whole"
        if flow.metadata.get(EVENTS_MARK) is not None or is_event_stream(flow.request, response):
            return "not cut: event stream; break it with action events and close"
        if length is None and "transfer-encoding" not in response.headers and flow.request.http_version == "HTTP/1.0":
            return "not cut: the response has no length and the client speaks HTTP/1.0, so closing is its normal end"
        return None

    def _rewrite_response(self, flow):
        plan = flow.metadata.get(PLAN_MARK)
        if not plan:
            return
        response = flow.response
        method = flow.request.method
        had_body = not _has_no_body(method, response.status_code)
        event_stream = "text/event-stream" in response.headers.get("content-type", "").lower()
        if flow.metadata.get(RESPOND_ERROR_MARK):
            for rule in plan:
                if rule["action"] == "rewrite" and ("status" in rule or "response_headers" in rule or changes_body(rule)):
                    _entry(flow, rule)["skipped"] = "response changes not applied: the respond rule could not read its body_file"
            return
        if plan[-1]["action"] == "sse":
            for rule in plan:
                if rule["action"] == "rewrite" and ("status" in rule or "response_headers" in rule or changes_body(rule)):
                    _entry(flow, rule)["skipped"] = "response changes not applied: an sse rule answers with its own stream"
            return
        for rule in plan:
            if rule["action"] != "rewrite":
                continue
            if "status" in rule:
                from mitmproxy.net.http import status_codes
                response.status_code = rule["status"]
                response.reason = status_codes.RESPONSES.get(rule["status"], "")
            if "response_headers" in rule:
                _set_headers(response.headers, rule["response_headers"])
        has_body = not _has_no_body(method, response.status_code)
        body_rules = [rule for rule in plan if changes_body(rule)]
        if not has_body:
            for rule in body_rules:
                _entry(flow, rule)["skipped"] = "body not changed: response has no body"
            if had_body:
                self._discard_body(flow)
            elif response.status_code < 200 or response.status_code == 204:
                response.headers.pop("content-length", None)
                response.headers.pop("transfer-encoding", None)
            return
        if not had_body:
            response.headers.pop("transfer-encoding", None)
            response.headers["content-length"] = "0"
        if not body_rules:
            return
        if event_stream or "text/event-stream" in response.headers.get("content-type", "").lower():
            for rule in body_rules:
                _entry(flow, rule)["skipped"] = "body not changed: event stream; events of a stream change under action events"
            return
        self._start_editing(flow, body_rules)

    @staticmethod
    def _discard_body(flow):
        response = flow.response
        response.headers.pop("content-length", None)
        response.headers.pop("transfer-encoding", None)
        response.stream = BodyDiscarder()
        flow.metadata[NO_RESPONSE_STREAM_MARK] = True

    @staticmethod
    def _start_editing(flow, rules):
        editor = BodyEditor(flow, rules)
        if _declared_length(flow.response.headers) > MAX_FILE_BYTES:
            editor.skip(LIMIT_TEXT)
            return
        flow.metadata[NO_RESPONSE_STREAM_MARK] = True
        flow.metadata[EDITOR_MARK] = editor

    async def response(self, flow):
        response = flow.response
        cutter = flow.metadata.get(CUT_MARK)
        if cutter is not None:
            cutter.finish()
        if is_websocket_handshake(flow):
            self._start_events(flow)
            return
        if flow.metadata.get(EVENTS_MARK) is not None:
            self.events.release(flow.id)
        if isinstance(response.stream, BodyDiscarder):
            response.raw_content = b""
            return
        editor = flow.metadata.get(EDITOR_MARK)
        if editor is None:
            return
        if response.stream:
            editor.skip(LIMIT_TEXT + "; body and its size not recorded")
            return
        raw = response.raw_content or b""
        async with self.edits:
            new = await asyncio.to_thread(editor.edit, raw)
        if new != raw:
            response.raw_content = new
        if "transfer-encoding" not in response.headers:
            response.headers["content-length"] = str(len(response.raw_content or b""))
