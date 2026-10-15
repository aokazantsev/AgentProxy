import logging
import zlib

log = logging.getLogger("agentproxy.streamtap")

NO_BODY_STATUSES = (204, 304)


def _decoder(encoding):
    name = (encoding or "").strip().lower()
    if name in ("", "identity"):
        return None
    if name in ("gzip", "x-gzip", "deflate"):
        decompressor = zlib.decompressobj(47)
        return decompressor.decompress
    if name == "br":
        import brotli
        return brotli.Decompressor().process
    if name == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress
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
        try:
            self.decode = _decoder(content_encoding)
        except (ValueError, ImportError) as error:
            self.decode = None
            self.decode_error = str(error)

    def __call__(self, chunk):
        if self.decode_error is None:
            self._capture(chunk)
        return chunk

    def _capture(self, chunk):
        if self.decode is None:
            data = chunk
        else:
            try:
                data = self.decode(chunk)
            except Exception as error:
                self.decode_error = "body could not be decoded: {0}".format(error)
                log.warning("stream body not decoded: %s", error)
                return
        self.size += len(data)
        room = self.limit - len(self.captured)
        if room > 0:
            self.captured.extend(data[:room])
