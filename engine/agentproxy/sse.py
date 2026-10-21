import re
import time
import zlib
from dataclasses import dataclass

from .streamtap import (GZIP_MAGIC, IDENTITY_ENCODINGS, INPUT_STEP, MAX_MEMBERS, OUTPUT_STEP, ZSTD_INPUT_STEP,
                        ZSTD_MAX_WINDOW, TooManyMembers, _has_zlib_header)

MAX_EVENT_BYTES = 256 * 1024
MAX_BUSY_SHARE = 0.3
MAX_BUSY_SECONDS = 1.0
BUSY_WINDOWS = 3
BUSY_NOTE = ("reading the stream kept the proxy busy: more than {0} s in a second, or more than {1} s a second for {2} "
             "seconds")
MAX_LINE_BYTES = 1024 * 1024
MAX_FIELD_BYTES = 1024
LINE_END = re.compile(rb"\r\n|\r|\n")
BOM = b"\xef\xbb\xbf"


@dataclass
class SseEvent:
    kind: str
    data: bytes = b""
    size: int = 0
    name: str = None
    event_id: str = None
    retry: int = None
    truncated: bool = False
    incomplete: bool = False


class SseParser:
    def __init__(self):
        self.tail = bytearray()
        self.started = False
        self.skip_lf = False
        self.discarding = False
        self.cut_data = False
        self._reset()

    def _reset(self):
        self.name = None
        self.event_id = None
        self.retry = None
        self.lines = []
        self.size = 0
        self.stored = 0
        self.truncated = False
        self.seen = False

    def feed(self, chunk):
        events = []
        if not chunk:
            return events
        if self.started and not self.skip_lf and b"\n" not in chunk and b"\r" not in chunk:
            self._extend(chunk, events)
            return events
        data = bytes(self.tail) + chunk
        self.tail = bytearray()
        if self.skip_lf:
            self.skip_lf = False
            if data[:1] == b"\n":
                data = data[1:]
        if not self.started and data:
            if len(data) < len(BOM) and BOM.startswith(data):
                self.tail = bytearray(data)
                return events
            self.started = True
            if data.startswith(BOM):
                data = data[len(BOM):]
        position = 0
        for match in LINE_END.finditer(data):
            line = data[position:match.start()]
            position = match.end()
            if self.discarding:
                self.discarding = False
                self._skip(len(line))
                continue
            self._line(line, events)
        if data.endswith(b"\r"):
            self.skip_lf = True
        self._extend(data[position:], events)
        return events

    def _extend(self, rest, events):
        if self.discarding:
            self._skip(len(rest))
            return
        self.tail += rest
        if len(self.tail) > MAX_LINE_BYTES:
            self._line(bytes(self.tail[:MAX_LINE_BYTES]), events, cut=True)
            self.discarding = True
            self._skip(len(self.tail) - MAX_LINE_BYTES)
            self.tail = bytearray()

    def _skip(self, count):
        if self.cut_data:
            self.size += count

    def finish(self):
        events = []
        if self.tail and not self.discarding:
            self._line(bytes(self.tail), events)
            self.tail = bytearray()
        if self.seen:
            events.append(self._take(incomplete=True))
        return events

    def _take(self, incomplete=False):
        event = SseEvent("event", b"\n".join(self.lines)[:MAX_EVENT_BYTES], max(self.size - 1, 0), self.name,
                         self.event_id, self.retry, self.truncated or self.size - 1 > MAX_EVENT_BYTES, incomplete)
        self._reset()
        return event

    def _line(self, line, events, cut=False):
        self.cut_data = False
        if not line:
            if self.seen:
                events.append(self._take())
            return
        if line.startswith(b":"):
            body = line[1:]
            if body.startswith(b" "):
                body = body[1:]
            events.append(SseEvent("comment", body[:MAX_EVENT_BYTES], len(body),
                                   truncated=cut or len(body) > MAX_EVENT_BYTES))
            return
        field, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            self.name = value[:MAX_FIELD_BYTES].decode("utf-8", "replace")
            self.seen = True
        elif field == b"data":
            self.seen = True
            self.cut_data = cut
            self.truncated = self.truncated or cut
            self.size += len(value) + 1
            if self.stored < MAX_EVENT_BYTES:
                self.lines.append(value)
                self.stored += len(value) + 1
        elif field == b"id":
            if b"\x00" not in value:
                self.event_id = value[:MAX_FIELD_BYTES].decode("utf-8", "replace")
                self.seen = True
        elif field == b"retry":
            if value.isdigit() and len(value) <= 10:
                self.retry = int(value)
                self.seen = True


class _ZlibDecoder:
    def __init__(self, raw_deflate_fallback):
        self.fallback = raw_deflate_fallback
        self.decompressor = None
        self.members = 0
        self.dead = False
        self.carry = b""

    def feed(self, chunk):
        pending = self.carry + chunk
        self.carry = b""
        while pending and not self.dead:
            if self.decompressor is None:
                if len(pending) < 2:
                    self.carry = pending
                    return
                if self.members >= MAX_MEMBERS:
                    self.dead = True
                    raise TooManyMembers(len(pending))
                if self.members and pending[:2] != GZIP_MAGIC:
                    self.dead = True
                    return
                wbits = 47
                if self.fallback and not self.members and not _has_zlib_header(pending[:2]):
                    wbits = -15
                self.decompressor = zlib.decompressobj(wbits)
                self.members += 1
            piece = self.decompressor.decompress(pending, OUTPUT_STEP)
            if piece:
                yield piece
            pending = self.decompressor.unconsumed_tail
            if self.decompressor.eof:
                pending = self.decompressor.unused_data + pending
                self.decompressor = None


class _BrotliDecoder:
    def __init__(self):
        import brotli
        self.decompressor = brotli.Decompressor()

    def feed(self, chunk):
        for start in range(0, len(chunk), INPUT_STEP):
            part = chunk[start:start + INPUT_STEP]
            while True:
                piece = self.decompressor.process(part, output_buffer_limit=OUTPUT_STEP)
                part = b""
                if not piece:
                    break
                yield piece


class _ZstdDecoder:
    def __init__(self):
        import zstandard
        self.context = zstandard.ZstdDecompressor(max_window_size=ZSTD_MAX_WINDOW)
        self.decompressor = self.context.decompressobj()

    def feed(self, chunk):
        pending = chunk
        while pending:
            part = pending[:ZSTD_INPUT_STEP]
            pending = pending[ZSTD_INPUT_STEP:]
            output = self.decompressor.decompress(part)
            for begin in range(0, len(output), OUTPUT_STEP):
                yield output[begin:begin + OUTPUT_STEP]
            if self.decompressor.eof:
                pending = self.decompressor.unused_data + pending
                self.decompressor = self.context.decompressobj()


class _Identity:
    def feed(self, chunk):
        yield chunk


def make_decoder(encoding):
    name = (encoding or "").strip().lower()
    if name in IDENTITY_ENCODINGS:
        return _Identity()
    if name in ("gzip", "x-gzip"):
        return _ZlibDecoder(False)
    if name == "deflate":
        return _ZlibDecoder(True)
    if name == "br":
        return _BrotliDecoder()
    if name == "zstd":
        return _ZstdDecoder()
    raise ValueError("unsupported content-encoding " + name)



class SseTap:
    def __init__(self, encoding, on_event, on_problem):
        self.parser = SseParser()
        self.on_event = on_event
        self.on_problem = on_problem
        self.encoding = encoding
        self.wire_size = 0
        self.decoder = None
        self.parsing = True
        self.window = float("-inf")
        self.busy = 0.0
        self.busy_windows = 0

    def _stop(self, reason):
        if self.parsing:
            self.parsing = False
            self.on_problem("events not read after {0} bytes: {1}".format(self.wire_size, reason))

    def __call__(self, chunk):
        if chunk:
            self.wire_size += len(chunk)
            if self.parsing:
                try:
                    self._read(chunk)
                except Exception as error:
                    self._stop(error.__class__.__name__)
        return chunk

    def _read(self, chunk):
        if self.decoder is None:
            try:
                self.decoder = make_decoder(self.encoding)
            except ValueError as error:
                self._stop(str(error))
                return
        self._tick()
        started = time.perf_counter()
        handling = 0.0
        for piece in self.decoder.feed(chunk):
            for event in self.parser.feed(piece):
                handed = time.perf_counter()
                self.on_event(event)
                handling += time.perf_counter() - handed
            if self.busy + time.perf_counter() - started - handling > MAX_BUSY_SECONDS:
                self._stop(BUSY_NOTE.format(MAX_BUSY_SECONDS, MAX_BUSY_SHARE, BUSY_WINDOWS))
                return
        self.busy += time.perf_counter() - started - handling
        if self.busy > MAX_BUSY_SHARE and self.busy_windows >= BUSY_WINDOWS - 1:
            self._stop(BUSY_NOTE.format(MAX_BUSY_SECONDS, MAX_BUSY_SHARE, BUSY_WINDOWS))

    def _tick(self):
        now = time.monotonic()
        if now - self.window >= 1:
            heavy = self.busy > MAX_BUSY_SHARE and now - self.window < 2
            self.busy_windows = self.busy_windows + 1 if heavy else 0
            self.window = now
            self.busy = 0.0

    def finish(self):
        if self.parsing:
            self.parsing = False
            for event in self.parser.finish():
                self.on_event(event)
