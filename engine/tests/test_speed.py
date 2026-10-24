import asyncio
import time

import pytest

from agentproxy import speed
from agentproxy.speed import (Backlog, ConnectionSpeed, PacedReader, PacedWriter, Pace, RulePaces, SpeedLimits,
                              SCOPE_CONNECTION, SCOPE_REQUEST)


def paced(kbps):
    pace = Pace()
    pace.set(kbps)
    return pace


def run(coroutine):
    return asyncio.run(coroutine)


class FakeWriter:
    def __init__(self):
        self.pieces = []
        self.times = []
        self.closed = False
        self.eof = False
        self.broken = False

    def write(self, data):
        self.pieces.append(bytes(data))
        self.times.append(time.monotonic())

    def write_eof(self):
        self.eof = True

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed or self.broken

    async def drain(self):
        pass

    def get_extra_info(self, name, default=None):
        return "extra-" + name

    @property
    def data(self):
        return b"".join(self.pieces)


class FakeReader:
    def __init__(self, data):
        self.data = data
        self.sizes = []

    async def read(self, n=-1):
        size = len(self.data) if n < 0 else min(n, len(self.data))
        self.sizes.append(n)
        piece, self.data = self.data[:size], self.data[size:]
        return piece


async def until_flushed(writer):
    while writer._backlog.size or writer._pumping:
        await asyncio.sleep(0.01)


def test_pace_pieces_follow_rate():
    pace = Pace()
    pace.set(80)
    assert pace.rate == 10000
    assert pace.piece(10 ** 6) == 500
    assert pace.piece(3) == 3
    pace.set(1)
    assert pace.piece(10 ** 6) == 6
    pace.set(None)
    assert pace.rate is None


def test_backlog_takes_pieces_across_parts_in_order():
    backlog = Backlog()
    backlog.add(b"abc")
    backlog.add(b"")
    backlog.add(bytearray(b"defgh"))
    assert backlog.size == 8
    assert backlog.take(2) == b"ab"
    assert backlog.take(4) == b"cdef"
    assert backlog.take(10) == b"gh" and backlog.size == 0


def test_writer_without_limit_writes_through():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.write(b"hello")
        assert fake.data == b"hello" and not writer._pumping
        assert writer.get_extra_info("peername") == "extra-peername"
        writer.close()
        assert fake.closed
    run(scenario())


def test_writer_paces_bytes_in_order_and_reports_progress():
    async def scenario():
        fake = FakeWriter()
        progress = []
        writer = PacedWriter(fake, lambda: progress.append(1))
        writer.pace = paced(80)
        payload = bytes(range(256)) * 12
        started = time.monotonic()
        writer.write(payload[:1000])
        writer.write(payload[1000:])
        await until_flushed(writer)
        elapsed = time.monotonic() - started
        assert fake.data == payload
        assert 0.2 <= elapsed < 1.5
        assert max(len(piece) for piece in fake.pieces) <= 500
        assert len(progress) == len(fake.pieces)
    run(scenario())


def test_writer_defers_close_and_eof_until_flushed():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(800)
        writer.write(b"x" * 3000)
        writer.write_eof()
        writer.close()
        assert writer.is_closing() and not fake.closed
        await until_flushed(writer)
        assert fake.closed and fake.data == b"x" * 3000
    run(scenario())


def test_writer_eof_after_flush_when_not_closed():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(800)
        writer.write(b"y" * 500)
        writer.write_eof()
        assert not fake.eof
        await until_flushed(writer)
        assert fake.eof and not fake.closed
    run(scenario())


def test_writer_drops_backlog_when_peer_is_gone():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(8)
        writer.write(b"z" * 10000)
        await asyncio.sleep(0.12)
        fake.broken = True
        await asyncio.wait_for(until_flushed(writer), 2)
        assert writer._backlog.size == 0 and len(fake.data) < 10000
    run(scenario())


def test_drain_waits_for_room_in_backlog(monkeypatch):
    monkeypatch.setattr(speed, "HIGH_WATER", 1000)

    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(160)
        writer.write(b"d" * 3000)
        started = time.monotonic()
        await writer.drain()
        assert writer._backlog.size <= 1000
        assert time.monotonic() - started >= 0.04
        await until_flushed(writer)
    run(scenario())


def test_lifting_the_limit_flushes_the_rest_at_once():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(8)
        writer.write(b"q" * 5000)
        await asyncio.sleep(0.1)
        writer.pace = None
        await asyncio.wait_for(until_flushed(writer), 1)
        assert fake.data == b"q" * 5000
        writer.write(b"after")
        assert fake.pieces[-1] == b"after"
    run(scenario())


def test_lifted_limit_flushes_in_pieces_and_reports_progress():
    async def scenario():
        fake = FakeWriter()
        progress = []
        writer = PacedWriter(fake, lambda: progress.append(1))
        writer.pace = paced(8)
        writer.write(b"m" * (speed.MAX_PIECE * 2 + 10))
        await asyncio.sleep(0.06)
        writer.pace = None
        await asyncio.wait_for(until_flushed(writer), 2)
        assert max(len(piece) for piece in fake.pieces) <= speed.MAX_PIECE
        assert len(progress) == len(fake.pieces)
    run(scenario())


def test_waiting_piece_goes_at_once_when_the_limit_changes():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        pace = paced(8)
        pace.book(5000)
        writer.pace = pace
        writer.write(b"k" * 10)
        await asyncio.sleep(0.1)
        assert fake.data == b""
        writer.pace = None
        await asyncio.wait_for(until_flushed(writer), 0.3)
        assert fake.data == b"k" * 10
    run(scenario())


def test_writes_after_lifting_keep_their_order_behind_the_backlog():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(8)
        payload = bytes(range(256)) * 8
        writer.write(payload)
        await asyncio.sleep(0.06)
        writer.pace = None
        writer.write(b"tail")
        await asyncio.wait_for(until_flushed(writer), 1)
        assert fake.data == payload + b"tail"
    run(scenario())


def test_idle_rule_pace_gives_no_burst_to_the_next_transfer():
    async def scenario():
        pace = paced(80)
        pace.book(500)
        await asyncio.sleep(0.4)
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = pace
        started = time.monotonic()
        writer.write(b"i" * 3000)
        await until_flushed(writer)
        assert time.monotonic() - started >= 0.2
    run(scenario())


def test_reader_paces_reads():
    async def scenario():
        fake = FakeReader(b"r" * 2000)
        reader = PacedReader(fake)
        reader.pace = paced(80)
        started = time.monotonic()
        received = b""
        while len(received) < 2000:
            received += await reader.read(65535)
        assert time.monotonic() - started >= 0.1
        assert max(fake.sizes) <= 500
        reader.pace = None
        fake.data = b"rest"
        assert await reader.read(65535) == b"rest" and fake.sizes[-1] == 65535
    run(scenario())


def make_link(limits=None):
    paces = limits.paces if limits is not None else RulePaces()
    return ConnectionSpeed(PacedReader(FakeReader(b"")), PacedWriter(FakeWriter(), lambda: None), paces)


def test_request_scope_replaces_and_lifts_upload_after_body():
    link = make_link()
    link.limit({"r1": (100, 50)}, SCOPE_REQUEST)
    assert link.writer.pace.rate == 12500 and link.reader.pace.rate == 6250
    link.lift_upload()
    assert link.reader.pace is None and link.writer.pace.rate == 12500
    link.limit({"r2": (None, 20)}, SCOPE_REQUEST)
    assert link.writer.pace is None and set(link.rules) == {"r2"} and link.reader.pace.rate == 2500
    link.lift()
    assert link.scope is None and link.reader.pace is None


def test_connection_scope_takes_the_slowest_rule_per_direction():
    link = make_link()
    link.limit({"r1": (100, None)}, SCOPE_CONNECTION)
    link.limit({"r2": (300, 40)}, SCOPE_CONNECTION)
    assert link.writer.pace.rate == 12500 and link.reader.pace.rate == 5000 and set(link.rules) == {"r1", "r2"}
    link.lift_upload()
    assert link.reader.pace.rate == 5000


def test_next_request_keeps_the_bookings_of_the_shared_rule():
    limits = SpeedLimits()
    first, second = make_link(limits), make_link(limits)
    first.limit({"r1": (800, None)}, SCOPE_REQUEST)
    started = time.monotonic()
    first.writer.pace.book(50000)
    second.limit({"r1": (800, None)}, SCOPE_REQUEST)
    first.limit({"r1": (800, None)}, SCOPE_REQUEST)
    first.lift_upload()
    assert second.writer.pace.book(1) - started > 0.4


def test_removed_rule_drops_its_paces():
    limits = SpeedLimits()
    link = make_link(limits)
    limits.links = {"a": link}
    link.limit({"r1": (800, 100)}, SCOPE_REQUEST)
    limits.change_rule("r1", None)
    assert not limits.paces._paces


def test_rule_pace_is_shared_by_its_connections():
    limits = SpeedLimits()
    first, second = make_link(limits), make_link(limits)
    first.limit({"r1": (100, None)}, SCOPE_REQUEST)
    second.limit({"r1": (100, None)}, SCOPE_REQUEST)
    assert first.writer.pace is second.writer.pace


def test_request_under_two_rules_goes_at_the_slowest_and_counts_only_there():
    limits = SpeedLimits()
    both, whole_only = make_link(limits), make_link(limits)
    both.limit({"r1": (500, None), "r2": (100, None)}, SCOPE_REQUEST)
    whole_only.limit({"r1": (500, None)}, SCOPE_REQUEST)
    assert both.writer.pace is limits.paces.get("r2", speed.DOWNLOAD, 100)
    started = time.monotonic()
    both.writer.pace.book(62500)
    assert whole_only.writer.pace.book(1) - started < 0.1


class ChattyReader:
    def __init__(self, messages):
        self.messages = messages

    async def read(self, n=-1):
        await asyncio.sleep(0)
        return self.messages.pop(0) if self.messages else b""


def test_new_speed_wakes_a_piece_booked_at_the_old_one():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        pace = paced(8)
        pace.book(5000)
        writer.pace = pace
        writer.write(b"v" * 10)
        await asyncio.sleep(0.1)
        assert fake.data == b""
        pace.set(8000)
        await asyncio.wait_for(until_flushed(writer), 0.3)
        assert fake.data == b"v" * 10
    run(scenario())


def test_new_speed_rebooks_instead_of_sending_the_old_piece():
    async def scenario():
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        pace = paced(800)
        writer.pace = pace
        writer.write(b"o" * 20000)
        await asyncio.sleep(0.12)
        sent = len(fake.data)
        pace.set(8)
        await asyncio.sleep(0.2)
        assert len(fake.data) - sent <= 500
        writer.pace = None
        await until_flushed(writer)
    run(scenario())


def test_waiting_read_goes_at_once_when_the_limit_changes_and_eof_books_nothing():
    async def scenario():
        pace = paced(8)
        pace.book(5000)
        reader = PacedReader(ChattyReader([b"r" * 10]))
        reader.pace = pace
        task = asyncio.ensure_future(reader.read(65535))
        await asyncio.sleep(0.1)
        assert not task.done()
        reader.pace = None
        assert await asyncio.wait_for(task, 0.3) == b"r" * 10
        reader.pace = pace
        assert await asyncio.wait_for(reader.read(65535), 0.3) == b""
    run(scenario())


def test_lower_upload_speed_releases_the_piece_already_read():
    async def scenario():
        pace = paced(8000)
        pace.book(500000)
        reader = PacedReader(ChattyReader([b"u" * 50000]))
        reader.pace = pace
        task = asyncio.ensure_future(reader.read(65535))
        await asyncio.sleep(0.1)
        pace.set(8)
        assert await asyncio.wait_for(task, 0.3) == b"u" * 50000
    run(scenario())


class SlowReader:
    def __init__(self, data, delay):
        self.data = data
        self.delay = delay

    async def read(self, n=-1):
        await asyncio.sleep(self.delay)
        return self.data


def test_lower_upload_speed_while_waiting_for_data_does_not_book_the_old_piece():
    async def scenario():
        pace = paced(8000)
        reader = PacedReader(SlowReader(b"w" * 50000, 0.1))
        reader.pace = pace
        task = asyncio.ensure_future(reader.read(65535))
        await asyncio.sleep(0.03)
        pace.set(8)
        assert await asyncio.wait_for(task, 0.3) == b"w" * 50000
    run(scenario())


def test_small_reads_book_only_their_bytes():
    async def scenario():
        pace = paced(8000)
        readers = [PacedReader(ChattyReader([b"s" * 200] * 10)) for _ in range(10)]
        for reader in readers:
            reader.pace = pace

        async def drain(reader):
            while await reader.read(65535):
                pass

        started = time.monotonic()
        await asyncio.gather(*(drain(reader) for reader in readers))
        assert time.monotonic() - started < 0.5
    run(scenario())


def test_disabled_rule_has_no_limits():
    assert speed.rule_limits({"enabled": False, "download_kbps": 5}) is None
    assert speed.rule_limits({"download_kbps": 5}) == (5, None)


def test_changed_rule_keeps_others_and_applies_new_speed():
    limits = SpeedLimits()
    first, second = make_link(limits), make_link(limits)
    limits.links = {"a": first, "b": second}
    first.limit({"r1": (100, None), "r2": (500, None)}, SCOPE_CONNECTION)
    second.limit({"r2": (500, None)}, SCOPE_REQUEST)
    limits.change_rule("r1", (40, None))
    assert first.writer.pace.rate == 5000 and second.writer.pace.rate == 62500
    limits.change_rule("r1", None)
    assert first.writer.pace.rate == 62500 and set(first.rules) == {"r2"}
    limits.change_rule("r2", (None, None))
    assert first.scope is None and second.scope is None and first.writer.pace is None


def test_detach_waits_for_the_pump():
    async def scenario():
        limits = SpeedLimits()
        link = make_link(limits)
        limits.links = {"a": link}
        link.limit({"r1": (800, None)}, SCOPE_REQUEST)
        link.writer.write(b"p" * 2000)
        link.writer.close()
        limits.detach("a")
        assert limits.get("a") is link
        limits.change_rule("r1", None)
        await asyncio.wait_for(until_flushed(link.writer), 1)
        assert limits.get("a") is None and link.writer._writer.closed
        limits.detach("missing")
    run(scenario())


def test_install_patches_handler_once(monkeypatch):
    from mitmproxy.proxy import server
    monkeypatch.setattr(server.ConnectionHandler, "handle_client", server.ConnectionHandler.handle_client)
    monkeypatch.setattr(speed, "_active", None)
    speed.install(SpeedLimits())
    patched = server.ConnectionHandler.handle_client
    speed.install(SpeedLimits())
    assert server.ConnectionHandler.handle_client is patched and patched.agentproxy_speed


@pytest.mark.parametrize("kind", ["udp", "missing"])
def test_attach_skips_connections_it_cannot_pace(kind):
    from mitmproxy.proxy.server import ConnectionIO

    class Client:
        id = "c1"

    class Handler:
        client = Client()
        transports = {}

    if kind == "udp":
        Handler.transports = {Handler.client: ConnectionIO(handler=None, reader=object(), writer=object())}
    limits = SpeedLimits()
    limits.attach(Handler())
    assert limits.get("c1") is None


def test_writer_keeps_pacing_under_eager_task_factory():
    async def scenario():
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
        fake = FakeWriter()
        writer = PacedWriter(fake, lambda: None)
        writer.pace = paced(8000)
        writer.write(b"h" * 10)
        writer.write(b"b" * 3000)
        await asyncio.wait_for(until_flushed(writer), 2)
        assert fake.data == b"h" * 10 + b"b" * 3000
    run(scenario())
