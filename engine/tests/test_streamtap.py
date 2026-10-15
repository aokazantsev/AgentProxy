import zlib

import brotli
import pytest
import zstandard

from agentproxy.streamtap import StreamTap, should_stream


@pytest.mark.parametrize("method,status,content_type,length,expected", [
    ("GET", 200, "text/event-stream", None, True),
    ("GET", 200, "application/json", None, True),
    ("GET", 200, "application/json", "100", False),
    ("GET", 200, "application/json", "5000", True),
    ("GET", 200, "application/json", "garbage", True),
    ("HEAD", 200, "application/json", None, False),
    ("GET", 204, "", None, False),
    ("GET", 304, "", None, False),
])
def test_should_stream(method, status, content_type, length, expected):
    assert should_stream(method, status, content_type, length, 1024) is expected


def feed(tap, data, pieces=7):
    step = max(1, len(data) // pieces)
    passed = b"".join(tap(data[i:i + step]) for i in range(0, len(data), step))
    return passed


def gzip_bytes(raw):
    compressor = zlib.compressobj(wbits=31)
    return compressor.compress(raw) + compressor.flush()


@pytest.mark.parametrize("encoding,encode", [
    (None, lambda raw: raw),
    ("gzip", gzip_bytes),
    ("deflate", zlib.compress),
    ("br", brotli.compress),
    ("zstd", lambda raw: zstandard.ZstdCompressor().compress(raw)),
])
def test_tap_passes_wire_bytes_and_captures_decoded_prefix(encoding, encode):
    raw = b"0123456789" * 500
    wire = encode(raw)
    tap = StreamTap(1000, encoding)
    assert feed(tap, wire) == wire
    assert tap.decode_error is None
    assert bytes(tap.captured) == raw[:1000]
    assert tap.size == len(raw)


def test_tap_reports_undecodable_body():
    tap = StreamTap(100, "gzip")
    assert tap(b"not gzip at all") == b"not gzip at all"
    assert tap.decode_error is not None


def test_tap_reports_unknown_encoding():
    tap = StreamTap(100, "compress")
    assert tap(b"x") == b"x"
    assert "unsupported" in tap.decode_error
