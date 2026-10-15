import gzip
import zlib

import brotli
import pytest
import zstandard
from mitmproxy.test import tflow

from agentproxy import recorder
from agentproxy.streamtap import COUNTED_BYTES_LIMIT, StreamTap, decode_body, should_stream


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
    assert tap(b"") == b""
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


def raw_deflate(raw):
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(raw) + compressor.flush()


def zstd_bytes(raw):
    return zstandard.ZstdCompressor().compress(raw)


@pytest.mark.parametrize("encoding,wire", [
    ("deflate", raw_deflate(b"abc" * 1000)),
    ("gzip", gzip.compress(b"abc" * 500) + gzip.compress(b"abc" * 500)),
    ("zstd", zstd_bytes(b"abc" * 500) + zstd_bytes(b"abc" * 500)),
    ("gzip", gzip.compress(b"abc" * 1000) + b"\0\0trailing junk"),
])
def test_tap_decodes_variants_mitmproxy_accepts(encoding, wire):
    tap = StreamTap(10000, encoding)
    assert feed(tap, wire) == wire
    assert tap.decode_error is None
    assert bytes(tap.captured) == b"abc" * 1000
    assert tap.size == 3000


@pytest.mark.parametrize("encoding,encode", [
    ("gzip", gzip.compress),
    ("br", brotli.compress),
    ("zstd", lambda raw: zstandard.ZstdCompressor(level=19).compress(raw)),
])
def test_tap_stops_decoding_a_compression_bomb(encoding, encode):
    wire = encode(b"\0" * (COUNTED_BYTES_LIMIT * 2))
    tap = StreamTap(1000, encoding)
    feed(tap, wire, pieces=3)
    assert tap.decode_error is None
    assert len(tap.captured) == 1000
    assert COUNTED_BYTES_LIMIT <= tap.size < COUNTED_BYTES_LIMIT * 2


def test_decode_body_returns_prefix_and_full_size():
    assert decode_body(gzip.compress(b"x" * 5000), "gzip", 100) == (b"x" * 100, 5000)
    assert decode_body(b"plain", None, 3) == (b"pla", 5)
    assert decode_body(b"not gzip", "gzip", 100) is None


def test_record_keeps_note_when_stream_was_not_decoded():
    flow = tflow.tflow(resp=True)
    tap = StreamTap(100, "gzip")
    tap(b"not gzip at all")
    record = recorder.build_record(flow, 100, tap)
    assert record.response_body.startswith(b"[AgentProxy] response body was passed through but not recorded")
    assert record.response_body_size == 0


def test_record_decodes_compressed_body_within_limit():
    flow = tflow.tflow(resp=True)
    flow.response.headers["content-encoding"] = "br"
    flow.response.raw_content = brotli.compress(b"\0" * (COUNTED_BYTES_LIMIT * 2))
    record = recorder.build_record(flow, 1000, None)
    assert record.response_body == b"\0" * 1000
    assert record.response_body_size >= COUNTED_BYTES_LIMIT


def test_record_keeps_raw_body_that_does_not_decode():
    flow = tflow.tflow(resp=True)
    flow.response.headers["content-encoding"] = "gzip"
    flow.response.raw_content = b"not gzip"
    record = recorder.build_record(flow, 1000, None)
    assert record.response_body == b"not gzip" and record.response_body_size == 8


def test_tap_reports_undecodable_body():
    tap = StreamTap(100, "gzip")
    assert tap(b"not gzip at all") == b"not gzip at all"
    assert tap.decode_error is not None


def test_tap_reports_unknown_encoding():
    tap = StreamTap(100, "compress")
    assert tap(b"x") == b"x"
    assert "unsupported" in tap.decode_error
