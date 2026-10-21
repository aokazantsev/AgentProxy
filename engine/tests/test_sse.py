import gzip
import zlib

import pytest

from agentproxy.sse import MAX_EVENT_BYTES, MAX_LINE_BYTES, SseParser, SseTap, make_decoder


def parse(*chunks, finish=False):
    parser = SseParser()
    events = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    if finish:
        events.extend(parser.finish())
    return events


def summary(events):
    return [(event.kind, event.name, event.event_id, event.data) for event in events]


def test_events_are_split_by_blank_lines():
    events = parse(b"data: one\n\ndata: two\n\n")
    assert summary(events) == [("event", None, None, b"one"), ("event", None, None, b"two")]


def test_fields_are_collected_and_data_lines_joined():
    events = parse(b"event: token\nid: 7\nretry: 3000\ndata: a\ndata: b\n\n")
    assert summary(events) == [("event", "token", "7", b"a\nb")]
    assert events[0].retry == 3000 and events[0].size == 3


def test_space_after_colon_is_dropped_only_once():
    assert parse(b"data:  two\n\n")[0].data == b" two"
    assert parse(b"data:none\n\n")[0].data == b"none"
    assert parse(b"data\n\n")[0].data == b""


def test_line_endings_cr_lf_and_crlf_all_work():
    assert [e.data for e in parse(b"data: a\r\n\r\ndata: b\r\rdata: c\n\n")] == [b"a", b"b", b"c"]


def test_crlf_split_between_chunks_is_one_line_end():
    events = parse(b"data: a\r", b"\n\r", b"\ndata: b\n\n")
    assert [event.data for event in events] == [b"a", b"b"]


def test_event_split_at_every_byte_gives_the_same_events():
    whole = b"event: x\r\ndata: \xd0\x9f\xd1\x80\xd0\xb8\r\n\r\n: ping\n\ndata: z\n\n"
    expected = summary(parse(whole))
    assert summary(parse(*[whole[i:i + 1] for i in range(len(whole))])) == expected
    assert len(expected) == 3


def test_comments_are_events_of_their_own():
    events = parse(b": ping\n\ndata: x\n\n")
    assert [(e.kind, e.data) for e in events] == [("comment", b"ping"), ("event", b"x")]


def test_byte_order_mark_at_the_start_is_ignored():
    assert parse(b"\xef\xbb\xbfdata: x\n\n")[0].data == b"x"


def test_unknown_field_and_blank_events_are_dropped():
    assert parse(b"foo: bar\n\n\n\n") == []


def test_incomplete_event_is_returned_by_finish_only():
    assert parse(b"data: half") == []
    events = parse(b"data: half", finish=True)
    assert events[0].data == b"half" and events[0].incomplete is True


def test_invalid_utf8_stays_bytes_and_name_is_replaced():
    events = parse(b"event: \xff\ndata: \xff\xfe\n\n")
    assert events[0].data == b"\xff\xfe" and events[0].name == "�"


def test_large_event_is_cut_but_its_size_is_true():
    line = b"data: " + b"a" * (MAX_EVENT_BYTES + 5000) + b"\n\n"
    events = parse(line)
    assert len(events[0].data) <= MAX_EVENT_BYTES + 1 and events[0].truncated is True
    assert events[0].size == MAX_EVENT_BYTES + 5000


def test_line_without_end_does_not_grow_memory_and_parsing_goes_on():
    parser = SseParser()
    for _ in range(3):
        parser.feed(b"a" * MAX_LINE_BYTES)
    assert len(parser.tail) <= MAX_LINE_BYTES
    events = parser.feed(b"\n\ndata: ok\n\n")
    assert events[-1].data == b"ok"


def test_event_id_with_nul_is_ignored():
    assert parse(b"id: a\x00b\ndata: x\n\n")[0].event_id is None


def test_gzip_decoder_works_in_small_pieces():
    wire = gzip.compress(b"data: packed\n\n")
    decoder = make_decoder("gzip")
    output = b"".join(piece for start in range(len(wire)) for piece in decoder.feed(wire[start:start + 1]))
    assert output == b"data: packed\n\n"


def test_gzip_decoder_reads_concatenated_members():
    wire = gzip.compress(b"data: a\n\n") + gzip.compress(b"data: b\n\n")
    assert b"".join(make_decoder("gzip").feed(wire)) == b"data: a\n\ndata: b\n\n"


def test_deflate_with_and_without_zlib_header():
    body = b"data: d\n\n"
    raw = zlib.compressobj(wbits=-15)
    assert b"".join(make_decoder("deflate").feed(zlib.compress(body))) == body
    assert b"".join(make_decoder("deflate").feed(raw.compress(body) + raw.flush())) == body


def test_brotli_and_zstd_decoders():
    import brotli
    import zstandard
    body = b"data: x\n\n"
    assert b"".join(make_decoder("br").feed(brotli.compress(body))) == body
    wire = zstandard.ZstdCompressor().compress(body)
    decoder = make_decoder("zstd")
    assert b"".join(piece for start in range(0, len(wire), 3) for piece in decoder.feed(wire[start:start + 3])) == body


def test_unknown_encoding_is_refused():
    with pytest.raises(ValueError):
        make_decoder("compress")


def test_tap_forwards_chunks_untouched_and_reports_events():
    seen = []
    problems = []
    tap = SseTap(None, seen.append, problems.append)
    assert tap(b"data: a\n\nda") == b"data: a\n\nda"
    assert tap(b"") == b""
    assert tap(b"ta: b\n\ndata: c") == b"ta: b\n\ndata: c"
    tap.finish()
    assert [event.data for event in seen] == [b"a", b"b", b"c"] and seen[-1].incomplete
    assert tap.wire_size == 25 and not problems


def test_tap_with_unknown_encoding_counts_bytes_and_says_why():
    seen = []
    problems = []
    tap = SseTap("compress", seen.append, problems.append)
    assert tap(b"xyz") == b"xyz"
    assert tap.wire_size == 3 and not seen
    assert len(problems) == 1 and "compress" in problems[0]


def test_tap_stops_reading_after_broken_compressed_data():
    seen = []
    problems = []
    tap = SseTap("gzip", seen.append, problems.append)
    assert tap(b"this is not gzip") == b"this is not gzip"
    assert tap(b"more") == b"more"
    assert not seen and len(problems) == 1


def test_zstd_frames_in_one_chunk_are_all_read():
    import zstandard
    compressor = zstandard.ZstdCompressor()
    wire = b"".join(compressor.compress("data: {0}\n\n".format(index).encode()) for index in range(5))
    events = []
    tap = SseTap("zstd", events.append, lambda reason: events.append(reason))
    tap(wire)
    assert [event.data for event in events] == [b"0", b"1", b"2", b"3", b"4"]


def test_line_over_the_limit_keeps_its_start_however_it_is_split():
    line = b"data: " + b"a" * (MAX_LINE_BYTES + 1000) + b"\n\ndata: next\n\n"
    whole = parse(line)
    pieces = parse(*[line[start:start + 65536] for start in range(0, len(line), 65536)])
    for events in (whole, pieces):
        assert [len(event.data) for event in events] == [MAX_EVENT_BYTES, 4]
        assert events[0].truncated is True and events[1].truncated is False
        assert events[0].size == MAX_LINE_BYTES + 1000


def test_comment_over_the_limit_is_kept_cut():
    events = parse(b": " + b"c" * (MAX_LINE_BYTES + 10), b"c" * 10 + b"\ndata: x\n\n")
    assert events[0].kind == "comment" and events[0].truncated is True
    assert events[1].data == b"x" and events[1].truncated is False


def test_unknown_encoding_is_reported_with_the_first_chunk():
    problems = []
    tap = SseTap("compress", lambda event: None, problems.append)
    assert problems == []
    tap(b"x")
    assert len(problems) == 1 and "compress" in problems[0]


def test_highly_compressed_stream_keeps_being_read_over_time(monkeypatch):
    import zlib
    from agentproxy import sse
    clock = {"now": 1000.0}
    monkeypatch.setattr(sse.time, "monotonic", lambda: clock["now"])
    events = []
    problems = []
    tap = SseTap("deflate", events.append, problems.append)
    packer = zlib.compressobj()
    snapshot = b"data: " + b"{" + b"x" * 30000 + b"}" + bytes([10, 10])
    for second in range(200):
        tap(packer.compress(snapshot) + packer.flush(zlib.Z_SYNC_FLUSH))
        clock["now"] += 1
    assert problems == [] and len(events) == 200


def test_event_name_and_id_are_cut_to_a_kilobyte():
    from agentproxy.sse import MAX_FIELD_BYTES
    events = parse(b"event: " + b"e" * 5000 + b"\nid: " + b"i" * 5000 + b"\ndata: x\n\n")
    assert len(events[0].name) == MAX_FIELD_BYTES and len(events[0].event_id) == MAX_FIELD_BYTES


def test_long_line_in_tiny_chunks_is_read_whole():
    line = b"data: " + b"z" * 300000 + bytes([10, 10])
    events = parse(*[line[start:start + 256] for start in range(0, len(line), 256)])
    assert len(events) == 1 and events[0].size == 300000


class Clocks:
    def __init__(self, monkeypatch, work):
        from agentproxy import sse
        self.wall = 1000.0
        self.cpu = 0.0
        self.work = work
        monkeypatch.setattr(sse.time, "monotonic", lambda: self.wall)
        monkeypatch.setattr(sse.time, "perf_counter", lambda: self.cpu)


def busy_tap(monkeypatch, work):
    clocks = Clocks(monkeypatch, work)
    events = []
    problems = []

    tap = SseTap(None, events.append, problems.append)
    parse = tap.parser.feed

    def slow_parse(piece):
        clocks.cpu += clocks.work
        return parse(piece)

    tap.parser.feed = slow_parse
    return clocks, tap, events, problems


LINE = b"data: x" + bytes([10, 10])


def test_reading_that_takes_over_a_second_in_a_second_stops(monkeypatch):
    clocks, tap, events, problems = busy_tap(monkeypatch, 0.45)
    tap(LINE)
    tap(LINE)
    assert problems == []
    unfinished = LINE + b"data: y" + bytes([10])
    tap(unfinished)
    assert len(problems) == 1 and "kept the proxy busy" in problems[0]
    assert tap(LINE) == LINE and len(events) == 3
    tap.finish()
    assert len(events) == 3 and tap.wire_size == 3 * len(LINE) + len(unfinished)


def run_seconds(clocks, tap, gaps):
    for gap in gaps:
        tap(LINE)
        clocks.wall += gap


def test_reading_busy_for_three_seconds_in_a_row_stops(monkeypatch):
    clocks, tap, events, problems = busy_tap(monkeypatch, 0.31)
    run_seconds(clocks, tap, [1, 1])
    assert problems == []
    tap(LINE)
    assert len(problems) == 1


def test_reading_just_under_the_share_never_stops(monkeypatch):
    clocks, tap, events, problems = busy_tap(monkeypatch, 0.29)
    run_seconds(clocks, tap, [1] * 10)
    assert problems == [] and len(events) == 10


def test_one_busy_second_then_quiet_ones_do_not_stop(monkeypatch):
    clocks, tap, events, problems = busy_tap(monkeypatch, 0.31)
    tap(LINE)
    clocks.wall += 1
    clocks.work = 0.001
    run_seconds(clocks, tap, [1] * 5)
    clocks.work = 0.31
    run_seconds(clocks, tap, [1, 1])
    assert problems == [] and len(events) == 8


def test_busy_seconds_count_again_after_a_two_second_pause(monkeypatch):
    clocks, tap, events, problems = busy_tap(monkeypatch, 0.31)
    run_seconds(clocks, tap, [1, 2, 1, 2])
    assert problems == []
    clocks, tap, events, problems = busy_tap(monkeypatch, 0.31)
    run_seconds(clocks, tap, [1.9, 1.9, 1])
    assert len(problems) == 1


def test_compression_bomb_of_empty_lines_stops_reading():
    import zlib
    events = []
    problems = []
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    wire = packer.compress(bytes([10]) * (400 * 1024 * 1024)) + packer.flush()
    tap = SseTap("gzip", events.append, problems.append)
    for start in range(0, len(wire), 16384):
        tap(wire[start:start + 16384])
        if problems:
            break
    assert len(problems) == 1 and "kept the proxy busy" in problems[0] and events == []


def test_time_spent_recording_events_is_not_counted(monkeypatch):
    clocks = Clocks(monkeypatch, 0.0)
    problems = []

    def slow_record(event):
        clocks.cpu += 0.4

    tap = SseTap(None, slow_record, problems.append)
    for second in range(5):
        tap(LINE * 3)
        tap(LINE)
        clocks.wall += 1
    assert problems == []


def test_byte_order_mark_split_across_first_chunks_is_ignored():
    bom = bytes([0xEF, 0xBB, 0xBF])
    for first in (bom[:1], bom[:2]):
        rest = (bom + b"data: a" + bytes([10, 10]))[len(first):]
        assert [event.data for event in parse(first, rest)] == [b"a"]


def test_gzip_member_header_split_between_chunks_keeps_reading():
    wire = gzip.compress(b"data: a" + bytes([10, 10])) + gzip.compress(b"data: b" + bytes([10, 10]))
    first = len(gzip.compress(b"data: a" + bytes([10, 10]))) + 1
    decoder = make_decoder("gzip")
    output = b"".join(decoder.feed(wire[:first])) + b"".join(decoder.feed(wire[first:]))
    assert output == b"data: a" + bytes([10, 10]) + b"data: b" + bytes([10, 10])


def test_raw_deflate_with_a_one_byte_first_chunk():
    packer = zlib.compressobj(wbits=-15)
    wire = packer.compress(b"data: d" + bytes([10, 10])) + packer.flush()
    decoder = make_decoder("deflate")
    output = b"".join(piece for start in range(len(wire)) for piece in decoder.feed(wire[start:start + 1]))
    assert output == b"data: d" + bytes([10, 10])


def test_absurd_retry_is_ignored_and_reading_goes_on():
    events = parse(b"retry: " + b"9" * 5000 + bytes([10]) + b"data: x" + bytes([10, 10]))
    assert [event.data for event in events] == [b"x"] and events[0].retry is None

