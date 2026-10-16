import io
import logging
import zlib

log = logging.getLogger("agentproxy.streamtap")

NO_BODY_STATUSES = (204, 304)
IDENTITY_ENCODINGS = ("", "identity", "none")
COUNTED_BYTES_LIMIT = 64 * 1024 * 1024
OUTPUT_STEP = 256 * 1024
INPUT_STEP = 64 * 1024
MAX_MEMBERS = 1000
ZSTD_MAX_WINDOW = 8 * 1024 * 1024
GZIP_MAGIC = b"\x1f\x8b"


def _zlib_pieces(data, raw_deflate_fallback):
    wbits = 47
    if raw_deflate_fallback and not _has_zlib_header(data[:2]):
        wbits = -15
    position = 0
    for member in range(MAX_MEMBERS + 1):
        if member and data[position:position + 2] != GZIP_MAGIC:
            return
        if member == MAX_MEMBERS:
            raise TooManyMembers(len(data) - position)
        decompressor = zlib.decompressobj(wbits)
        while position < len(data) and not decompressor.eof:
            pending = data[position:position + INPUT_STEP]
            position += len(pending)
            while True:
                piece = decompressor.decompress(pending, OUTPUT_STEP)
                if piece:
                    yield piece
                pending = decompressor.unconsumed_tail
                if decompressor.eof or (not pending and len(piece) < OUTPUT_STEP):
                    break
        if not decompressor.eof:
            return
        position -= len(decompressor.unused_data)


class TooManyMembers(Exception):
    def __init__(self, undecoded):
        Exception.__init__(self, "too many gzip members")
        self.undecoded = undecoded


def _has_zlib_header(head):
    try:
        zlib.decompressobj(47).decompress(head)
    except zlib.error:
        return False
    return True


def _brotli_pieces(data):
    import brotli
    decompressor = brotli.Decompressor()
    for start in range(0, len(data), INPUT_STEP):
        chunk = data[start:start + INPUT_STEP]
        while True:
            piece = decompressor.process(chunk, output_buffer_limit=OUTPUT_STEP)
            chunk = b""
            if not piece:
                break
            yield piece


def _zstd_pieces(data):
    import zstandard
    reader = zstandard.ZstdDecompressor(max_window_size=ZSTD_MAX_WINDOW).stream_reader(
        io.BytesIO(data), read_across_frames=True)
    while True:
        piece = reader.read(OUTPUT_STEP)
        if not piece:
            return
        yield piece


def _pieces(encoding, data):
    name = (encoding or "").strip().lower()
    if name in ("gzip", "x-gzip"):
        return _zlib_pieces(data, False)
    if name == "deflate":
        return _zlib_pieces(data, True)
    if name == "br":
        return _brotli_pieces(data)
    if name == "zstd":
        return _zstd_pieces(data)
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
        raw = bytes(self.wire)
        if total == 0 or (self.encoding or "").strip().lower() in IDENTITY_ENCODINGS:
            return raw[:self.limit], total
        captured = bytearray()
        size = 0
        counted_limit = max(self.limit, COUNTED_BYTES_LIMIT)
        try:
            for piece in _pieces(self.encoding, raw):
                size += len(piece)
                room = self.limit - len(captured)
                if room > 0:
                    captured.extend(piece[:room])
                if size >= counted_limit:
                    break
        except TooManyMembers as stop:
            size += stop.undecoded
        except Exception as error:
            log.warning("body kept as received, not decoded: %s", error)
            return raw[:self.limit], total
        return bytes(captured), size + total - len(raw)
