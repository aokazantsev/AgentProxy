import logging
import zlib

log = logging.getLogger("agentproxy.streamtap")

NO_BODY_STATUSES = (204, 304)
COUNTED_BYTES_LIMIT = 64 * 1024 * 1024
OUTPUT_STEP = 256 * 1024
ZLIB_INPUT_STEP = 64 * 1024
ZSTD_INPUT_STEP = 256
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
                if not pending:
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
                self.decompressor = self._new()
                piece = self._feed(tail)
                if piece:
                    yield piece

    def _feed(self, part):
        if self.decompressor.eof:
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
    def __init__(self, limit, content_encoding=None):
        self.limit = limit
        self.captured = bytearray()
        self.size = 0
        self.decode_error = None
        self.counted_limit = max(limit, COUNTED_BYTES_LIMIT)
        try:
            self.decoder = _decoder(content_encoding)
        except (ValueError, ImportError) as error:
            self.decoder = None
            self.decode_error = str(error)

    def __call__(self, chunk):
        if chunk and self.decode_error is None:
            self._capture(chunk)
        return chunk

    def _capture(self, chunk):
        if self.decoder is None:
            self._keep(chunk)
            return
        if self.size >= self.counted_limit:
            return
        try:
            for piece in self.decoder.pieces(chunk):
                self._keep(piece)
                if self.size >= self.counted_limit:
                    return
        except Exception as error:
            self.decode_error = "body could not be decoded: {0}".format(error)
            log.warning("stream body not decoded: %s", error)

    def _keep(self, data):
        self.size += len(data)
        room = self.limit - len(self.captured)
        if room > 0:
            self.captured.extend(data[:room])


def decode_body(raw, encoding, limit):
    tap = StreamTap(limit, encoding)
    tap(raw)
    if tap.decode_error is not None:
        return None
    return bytes(tap.captured), tap.size
