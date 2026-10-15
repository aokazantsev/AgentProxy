import logging
import zlib

log = logging.getLogger("agentproxy.streamtap")

NO_BODY_STATUSES = (204, 304)
COUNTED_BYTES_LIMIT = 64 * 1024 * 1024
OUTPUT_STEP = 256 * 1024
ZSTD_INPUT_STEP = 64
GZIP_MAGIC = b"\x1f\x8b"


class _ZlibDecoder:
    def __init__(self, raw_deflate_fallback):
        self.fallback = raw_deflate_fallback
        self.started = False
        self.decompressor = zlib.decompressobj(47)

    def pieces(self, data):
        if not self.started:
            self.started = True
            if self.fallback:
                try:
                    first = self.decompressor.decompress(data[:2])
                except zlib.error:
                    self.decompressor = zlib.decompressobj(-15)
                else:
                    if first:
                        yield first
                    data = data[2:]
        pending = data
        while True:
            if self.decompressor.eof:
                pending = self.decompressor.unused_data + pending
                if not pending.startswith(GZIP_MAGIC):
                    return
                self.decompressor = zlib.decompressobj(47)
            piece = self.decompressor.decompress(pending, OUTPUT_STEP)
            pending = self.decompressor.unconsumed_tail
            if piece:
                yield piece
            elif not pending and not self.decompressor.eof:
                return


class _BrotliDecoder:
    def __init__(self):
        import brotli
        self.decompressor = brotli.Decompressor()

    def pieces(self, data):
        while True:
            piece = self.decompressor.process(data, output_buffer_limit=OUTPUT_STEP)
            data = b""
            if piece:
                yield piece
            if len(piece) < OUTPUT_STEP or self.decompressor.is_finished():
                return


class _ZstdDecoder:
    def __init__(self):
        import zstandard
        self.zstandard = zstandard
        self.decompressor = zstandard.ZstdDecompressor().decompressobj()

    def pieces(self, data):
        pending = data
        while pending:
            if self.decompressor.eof:
                pending = self.decompressor.unused_data + pending
                self.decompressor = self.zstandard.ZstdDecompressor().decompressobj()
            piece = self.decompressor.decompress(pending[:ZSTD_INPUT_STEP])
            pending = pending[ZSTD_INPUT_STEP:]
            if piece:
                yield piece


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
