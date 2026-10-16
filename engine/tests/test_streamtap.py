import gzip
import os
import time
import tracemalloc
import zlib

import brotli
import pytest
import zstandard
from mitmproxy.test import tflow

from agentproxy import recorder
from agentproxy.streamtap import COUNTED_BYTES_LIMIT, MAX_MEMBERS, OUTPUT_STEP, StreamTap, should_stream, should_stream_request


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


@pytest.mark.parametrize("length,expected", [(None, True), ("100", False), ("5000", True), ("bad", True)])
def test_should_stream_request(length, expected):
    assert should_stream_request(length, 1024) is expected


def feed(tap, data, pieces=7):
    step = max(1, len(data) // pieces)
    passed = b"".join(tap(data[i:i + step]) for i in range(0, len(data), step))
    assert tap(b"") == b""
    return passed


def whole(limit, encoding, wire):
    tap = StreamTap(limit, encoding, wire_limit=len(wire))
    tap(wire)
    return tap.body()


def gzip_bytes(raw):
    compressor = zlib.compressobj(wbits=31)
    return compressor.compress(raw) + compressor.flush()


def raw_deflate(raw):
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(raw) + compressor.flush()


def zstd_bytes(raw):
    return zstandard.ZstdCompressor().compress(raw)


@pytest.mark.parametrize("encoding,encode", [
    (None, lambda raw: raw),
    ("none", lambda raw: raw),
    ("gzip", gzip_bytes),
    ("GZIP", gzip_bytes),
    ("deflate", zlib.compress),
    ("br", brotli.compress),
    ("zstd", zstd_bytes),
])
def test_tap_passes_wire_bytes_and_keeps_decoded_prefix(encoding, encode):
    raw = b"0123456789" * 500
    wire = encode(raw)
    tap = StreamTap(1000, encoding, wire_limit=len(wire))
    assert feed(tap, wire) == wire
    assert tap.body() == (raw[:1000], len(raw))


@pytest.mark.parametrize("encoding,wire", [
    ("deflate", raw_deflate(b"abc" * 1000)),
    ("gzip", gzip.compress(b"abc" * 500) + gzip.compress(b"abc" * 500)),
    ("zstd", zstd_bytes(b"abc" * 500) + zstd_bytes(b"abc" * 500)),
    ("gzip", gzip.compress(b"abc" * 1000) + b"\0\0trailing junk"),
])
def test_tap_decodes_variants_mitmproxy_accepts(encoding, wire):
    for pieces in (1, 7, len(wire)):
        tap = StreamTap(10000, encoding)
        assert feed(tap, wire, pieces) == wire
        assert tap.body() == (b"abc" * 1000, 3000)


@pytest.mark.parametrize("size", [OUTPUT_STEP, OUTPUT_STEP + 1, OUTPUT_STEP + 200, 2 * OUTPUT_STEP + 7])
def test_raw_deflate_keeps_its_tail(size):
    raw = (b"abcdefgh" * (size // 8 + 1))[:size]
    assert whole(size, "deflate", raw_deflate(raw)) == (raw, size)


def test_zstd_frames_split_anywhere_are_all_decoded():
    first, second = os.urandom(300), os.urandom(20)
    wire = zstd_bytes(first) + zstd_bytes(second)
    for cut in range(1, len(wire)):
        tap = StreamTap(1000, "zstd")
        tap(wire[:cut])
        tap(wire[cut:])
        assert tap.body() == (first + second, 320), cut


def test_zstd_frame_with_huge_window_is_kept_as_received():
    params = zstandard.ZstdCompressionParameters(window_log=27, write_content_size=False)
    compressor = zstandard.ZstdCompressor(compression_params=params).compressobj()
    wire = compressor.compress(b"a" * 100) + compressor.flush()
    assert whole(1000, "zstd", wire) == (wire, len(wire))


def test_endless_gzip_members_stop_but_keep_what_was_decoded():
    wire = gzip.compress(b"x") * (MAX_MEMBERS + 50)
    assert whole(100000, "gzip", wire) == (b"x" * MAX_MEMBERS, MAX_MEMBERS)


@pytest.mark.parametrize("encoding,encode", [
    ("zstd", lambda raw: zstandard.ZstdCompressor(level=1).compress(raw)),
    ("gzip", lambda raw: gzip.compress(raw, compresslevel=1)),
    ("br", lambda raw: brotli.compress(raw, quality=1)),
])
def test_large_body_decodes_in_linear_time(encoding, encode):
    wire = encode(os.urandom(8 * 1024 * 1024))
    started = time.monotonic()
    assert whole(1000, encoding, wire)[1] == 8 * 1024 * 1024
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("encoding,encode", [
    ("gzip", gzip.compress),
    ("br", brotli.compress),
    ("zstd", lambda raw: zstandard.ZstdCompressor(level=19).compress(raw)),
])
def test_tap_stops_decoding_a_compression_bomb(encoding, encode):
    wire = encode(b"\0" * (COUNTED_BYTES_LIMIT * 4))
    tracemalloc.start()
    body, size = whole(1000, encoding, wire)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert body == b"\0" * 1000
    assert COUNTED_BYTES_LIMIT <= size <= COUNTED_BYTES_LIMIT + OUTPUT_STEP
    assert peak < 32 * 1024 * 1024


@pytest.mark.parametrize("encoding,start", [
    ("br", brotli.compress(os.urandom(4096))[:36]),
    ("zstd", zstd_bytes(os.urandom(4096))[:40]),
])
def test_idle_compressed_streams_hold_only_their_bytes(encoding, start):
    tracemalloc.start()
    taps = []
    for _ in range(200):
        tap = StreamTap(2 * 1024 * 1024, encoding)
        tap(start)
        taps.append(tap)
    used = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert used < 2 * 1024 * 1024


def test_wire_beyond_the_kept_prefix_counts_as_lower_bound():
    raw = os.urandom(5000)
    tap = StreamTap(1000, None, wire_limit=1000)
    feed(tap, raw)
    assert len(tap.wire) == 1000
    assert tap.body() == (raw[:1000], 5000)
    packed = gzip.compress(raw)
    tap = StreamTap(10000, "gzip", wire_limit=2000)
    feed(tap, packed)
    assert len(tap.wire) == 2000
    body, size = tap.body()
    assert body == raw[:len(body)] and size > len(body)


def test_empty_body_with_unknown_encoding_is_empty():
    assert StreamTap(100, "compress").body() == (b"", 0)


@pytest.mark.parametrize("encoding,wire", [
    ("gzip", b"not gzip at all"),
    ("compress", b"x"),
    ("gzip, br", b"xy"),
    ("UTF-8", b'{"plain": true}'),
])
def test_undecodable_body_is_kept_as_received(encoding, wire):
    tap = StreamTap(100, encoding)
    assert tap(wire) == wire
    assert tap.body() == (wire, len(wire))


def test_record_keeps_body_that_does_not_decode():
    flow = tflow.tflow(resp=True)
    flow.response.headers["content-encoding"] = "none"
    flow.response.raw_content = b'{"plain": true}'
    record = recorder.build_record(flow, 1000, None)
    assert record.response_body == b'{"plain": true}' and record.response_body_size == 15


def test_record_takes_streamed_request_body_from_its_tap():
    flow = tflow.tflow(resp=True)
    request_tap = StreamTap(4, None)
    request_tap(b"uploaded")
    record = recorder.build_record(flow, 4, None, None, request_tap)
    assert record.request_body == b"uplo" and record.request_body_size == 8


def test_record_of_cut_upload_keeps_announced_size():
    flow = tflow.tflow(resp=True)
    flow.request.headers["content-length"] = "200000"
    request_tap = StreamTap(1000, None)
    request_tap(b"z" * 2000)
    record = recorder.build_record(flow, 1000, None, None, request_tap)
    assert record.request_body == b"z" * 1000 and record.request_body_size == 200000


def test_record_decodes_compressed_body_within_limit():
    flow = tflow.tflow(resp=True)
    flow.response.headers["content-encoding"] = "br"
    flow.response.raw_content = brotli.compress(b"\0" * (COUNTED_BYTES_LIMIT * 2))
    record = recorder.build_record(flow, 1000, None)
    assert record.response_body == b"\0" * 1000
    assert record.response_body_size >= COUNTED_BYTES_LIMIT


def test_tcp_messages_are_not_kept():
    keeper = recorder.Recorder(None, 100)
    tcp = tflow.ttcpflow()
    assert tcp.messages
    keeper.tcp_message(tcp)
    assert tcp.messages == []
