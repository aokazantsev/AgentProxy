import logging
import zlib

log = logging.getLogger("agentproxy.streamtap")

NO_BODY_STATUSES = (204, 304)
COUNTED_BYTES_LIMIT = 64 * 1024 * 1024
OUTPUT_STEP = 256 * 1024
ZLIB_INPUT_STEP = 64 * 1024
ZSTD_INPUT_STEP = 64
MAX_MEMBERS = 1000
ZSTD_MAX_WINDOW = 8 * 1024 * 1024
GZIP_MAGIC = b"\x1f\x8b"


def _slices(data, step):
    view = memoryview(data)
    for start in range(0, len(view), step):
        yield view[start:start + step]


class _ZlibDecoder:
    def __init__(self, raw_deflate_fallback):
        self.wbits = None if raw_deflate_fallback else 47
        self.held = b""
        self.decompressor = None
        self.members = 0
        self.done = False

    def pieces(self, data):
        buffer = self.held + bytes(data)
        self.held = b""
        position = 0
        while position < len(buffer) and not self.done:
            if self.decompressor is None:
                if self.members >= MAX_MEMBERS:
                    raise ValueError("too many compressed members")
                if self.members == 0 and self.wbits is not None:
                    self.decompressor = zlib.decompressobj(self.wbits)
                    self.members = 1
                    continue
                head = buffer[position:position + 2]
                if len(head) < 2:
                    if self.wbits is None or GZIP_MAGIC.startswith(head):
                        self.held = head
                    else:
                        self.done = True
                    return
                if self.wbits is None:
                    self.wbits = 47 if _has_zlib_header(head) else -15
                elif head != GZIP_MAGIC:
                    self.done = True
                    return
                self.decompressor = zlib.decompressobj(self.wbits)
                self.members += 1
            pending = buffer[position:position + ZLIB_INPUT_STEP]
            position += len(pending)
            while True:
                piece = self.decompressor.decompress(pending, OUTPUT_STEP)
                if piece:
                    yield piece
                pending = self.decompressor.unconsumed_tail
                if self.decompressor.eof:
                    position -= len(self.decompressor.unused_data)
                    self.decompressor = None
                    break
                if not pending and len(piece) < OUTPUT_STEP:
                    break


def _has_zlib_header(head):
    try:
        zlib.decompressobj(47).decompress(head)
    except zlib.error:
        return False
    return True


class _BrotliDecoder:
    def __init__(self):
        import brotli
        self.decompressor = brotli.Decompressor()

    def pieces(self, data):
        for part in _slices(data, ZLIB_INPUT_STEP):
            chunk = bytes(part)
            while True:
                piece = self.decompressor.process(chunk, output_buffer_limit=OUTPUT_STEP)
                chunk = b""
                if piece:
                    yield piece
                if len(piece) < OUTPUT_STEP or self.decompressor.is_finished():
                    break


class _ZstdDecoder:
    def __init__(self):
        import zstandard
        self.zstandard = zstandard
        self.frames = 1
        self.decompressor = self._new()

    def _new(self):
        return self.zstandard.ZstdDecompressor(max_window_size=ZSTD_MAX_WINDOW).decompressobj()

    def pieces(self, data):
        for part in _slices(data, ZSTD_INPUT_STEP):
            piece = self._feed(part)
            if piece:
                yield piece
            while self.decompressor.eof and self.decompressor.unused_data:
                tail = self.decompressor.unused_data
                piece = self._feed(tail)
                if piece:
                    yield piece

    def _feed(self, part):
        if self.decompressor.eof:
            self.frames += 1
            if self.frames > MAX_MEMBERS:
                raise ValueError("too many compressed frames")
            self.decompressor = self._new()
        return self.decompressor.decompress(part)


def _decoder(encoding):
    name = (encoding or "").strip().lower()
    if name in ("", "identity"):
        return None
    if name in ("gzip", "x-gzip"):
        return _ZlibDecoder(False)
    if name == "deflate":
        return _ZlibDecoder(True)
    if name == "br":
        return _BrotliDecoder()
    if name == "zstd":
        return _ZstdDecoder()
    raise ValueError("unsupported content-encoding " + name)


def should_stream(method, status, content_type, content_length, limit):
    if method == "HEAD" or status in NO_BODY_STATUSES:
        return False
    if "text/event-stream" in (content_type or "").lower():
        return True
    return _over_limit(content_length, limit)


def should_stream_request(content_length, limit):
    return _over_limit(content_length, limit)


def _over_limit(content_length, limit):
    if content_length is None:
        return True
    try:
        return int(content_length) > limit
    except ValueError:
        return True


class StreamTap:
    def __init__(self, limit, content_encoding=None, wire_limit=None):
        self.limit = limit
        self.encoding = content_encoding
        self.wire_limit = limit if wire_limit is None else wire_limit
        self.wire = bytearray()
        self.wire_size = 0

    def __call__(self, chunk):
        if chunk:
            self.wire_size += len(chunk)
            room = self.wire_limit - len(self.wire)
            if room > 0:
                self.wire.extend(chunk[:room])
        return chunk

    def body(self, expected_size=0):
        total = max(self.wire_size, expected_size)
        if total == 0:
            return b"", 0, None
        missing = total - len(self.wire)
        try:
            decoder = _decoder(self.encoding)
        except (ValueError, ImportError) as error:
            return None, total, str(error)
        if decoder is None:
            return bytes(self.wire[:self.limit]), total, None
        captured = bytearray()
        size = 0
        counted_limit = max(self.limit, COUNTED_BYTES_LIMIT)
        try:
            for piece in decoder.pieces(self.wire):
                size += len(piece)
                room = self.limit - len(captured)
                if room > 0:
                    captured.extend(piece[:room])
                if size >= counted_limit:
                    break
        except Exception as error:
            log.warning("body not decoded: %s", error)
            return None, total, "body could not be decoded: {0}".format(error)
        return bytes(captured), size + missing, None

