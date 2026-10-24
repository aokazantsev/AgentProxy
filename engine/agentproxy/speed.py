import asyncio
import collections
import logging
import time

log = logging.getLogger("agentproxy.speed")

BYTES_PER_KBIT = 125
TICK = 0.05
MAX_PIECE = 1024 * 1024
HIGH_WATER = 64 * 1024
SCOPE_REQUEST = "request"
SCOPE_CONNECTION = "connection"
DOWNLOAD = 0
UPLOAD = 1

_active = None
_pumps = set()


class Pace:
    def __init__(self):
        self.rate = None
        self.version = 0
        self._due = 0.0

    def set(self, kbps):
        rate = kbps * BYTES_PER_KBIT if kbps else None
        if rate != self.rate:
            self.rate = rate
            self.version += 1
            self._due = time.monotonic()

    def piece(self, wanted):
        return max(1, min(wanted, int(self.rate * TICK), MAX_PIECE))

    def book(self, size):
        begin = max(self._due, time.monotonic() - TICK)
        self._due = begin + size / self.rate
        return begin


class Backlog:
    def __init__(self):
        self._parts = collections.deque()
        self.size = 0

    def add(self, data):
        if data:
            self._parts.append(memoryview(bytes(data)))
            self.size += len(data)

    def take(self, limit):
        piece = bytearray()
        while self._parts and len(piece) < limit:
            part = self._parts[0]
            need = limit - len(piece)
            if len(part) <= need:
                piece += part
                self._parts.popleft()
            else:
                piece += part[:need]
                self._parts[0] = part[need:]
        self.size -= len(piece)
        return bytes(piece)

    def clear(self):
        self._parts.clear()
        self.size = 0


def _limited(pace):
    return pace is not None and pace.rate is not None


async def _waited(current, pace, size, version=None):
    if version is None:
        version = pace.version
    elif version != pace.version:
        return False
    moment = pace.book(size)
    while current() is pace and pace.version == version:
        left = moment - time.monotonic()
        if left <= 0:
            return False
        await asyncio.sleep(min(left, TICK))
    return current() is pace


class PacedReader:
    def __init__(self, reader):
        self._reader = reader
        self.pace = None

    def __getattr__(self, name):
        return getattr(self._reader, name)

    async def read(self, n=-1):
        pace = self.pace
        if not _limited(pace):
            return await self._reader.read(n)
        version = pace.version
        data = await self._reader.read(pace.piece(n if n > 0 else MAX_PIECE))
        if data:
            await _waited(lambda: self.pace, pace, len(data), version)
        return data


class PacedWriter:
    def __init__(self, writer, on_progress):
        self._writer = writer
        self._on_progress = on_progress
        self.pace = None
        self.on_idle = None
        self.close_after_write = False
        self._backlog = Backlog()
        self._ending = None
        self._pumping = False
        self._room = asyncio.Event()
        self._room.set()

    def __getattr__(self, name):
        return getattr(self._writer, name)

    @property
    def pumping(self):
        return self._pumping

    def write(self, data):
        if self.close_after_write:
            self.close_after_write = False
            asyncio.get_running_loop().call_soon(self.close)
        if not self._pumping and not _limited(self.pace):
            self._writer.write(data)
            return
        self._backlog.add(data)
        if not self._pumping:
            self._pumping = True
            pump = asyncio.get_running_loop().create_task(self._run())
            if not pump.done():
                _pumps.add(pump)
                pump.add_done_callback(_pumps.discard)

    def write_eof(self):
        if not self._pumping:
            self._writer.write_eof()
        elif self._ending is None:
            self._ending = "eof"

    def close(self):
        if not self._pumping:
            self._writer.close()
        else:
            self._ending = "close"

    def is_closing(self):
        return self._ending == "close" or self._writer.is_closing()

    async def drain(self):
        while self._pumping and self._backlog.size > HIGH_WATER:
            self._room.clear()
            await self._room.wait()
        await self._writer.drain()

    async def _run(self):
        try:
            while self._backlog.size and not self._writer.is_closing():
                pace = self.pace
                if not _limited(pace):
                    size = min(self._backlog.size, MAX_PIECE)
                else:
                    size = pace.piece(self._backlog.size)
                    if await _waited(lambda: self.pace, pace, size):
                        continue
                    if self._writer.is_closing():
                        break
                self._writer.write(self._backlog.take(size))
                self._room.set()
                self._on_progress()
                await self._writer.drain()
        except (OSError, RuntimeError) as error:
            log.debug("paced write stopped: %s", error)
        finally:
            self._finish()

    def _finish(self):
        self._backlog.clear()
        self._pumping = False
        self._room.set()
        ending, self._ending = self._ending, None
        try:
            if ending == "close":
                self._writer.close()
            elif ending == "eof" and not self._writer.is_closing():
                self._writer.write_eof()
        except (OSError, RuntimeError) as error:
            log.debug("paced %s failed: %s", ending, error)
        if self.on_idle is not None:
            on_idle, self.on_idle = self.on_idle, None
            on_idle()


class RulePaces:
    def __init__(self):
        self._paces = {}

    def get(self, rule_id, direction, kbps):
        pace = self._paces.get((rule_id, direction))
        if pace is None:
            pace = self._paces[(rule_id, direction)] = Pace()
        pace.set(kbps)
        return pace

    def drop(self, rule_id):
        for direction in (DOWNLOAD, UPLOAD):
            self._paces.pop((rule_id, direction), None)


class ConnectionSpeed:
    def __init__(self, reader, writer, paces):
        self.reader = reader
        self.writer = writer
        self.paces = paces
        self.scope = None
        self.rules = {}
        self.upload_done = False

    def limit(self, rules, scope):
        if scope == SCOPE_CONNECTION and self.scope == SCOPE_CONNECTION:
            rules = dict(self.rules, **rules)
        self.scope = scope
        self.rules = dict(rules)
        self.upload_done = False
        self._apply()

    def change(self, rule_id, limits):
        if limits is None or not any(limits):
            self.rules.pop(rule_id, None)
        else:
            self.rules[rule_id] = limits
        if self.rules:
            self._apply()
        else:
            self.lift()

    def lift_upload(self):
        if self.scope == SCOPE_REQUEST:
            self.upload_done = True
            self._apply()

    def lift(self):
        self.scope = None
        self.rules = {}
        self.upload_done = False
        self._apply()

    def _apply(self):
        self.writer.pace = self._slowest(DOWNLOAD)
        self.reader.pace = None if self.upload_done else self._slowest(UPLOAD)

    def _slowest(self, direction):
        chosen = [(limits[direction], rule_id) for rule_id, limits in self.rules.items() if limits[direction]]
        if not chosen:
            return None
        kbps, rule_id = min(chosen)
        return self.paces.get(rule_id, direction, kbps)


def rule_limits(rule):
    if rule is None or not rule.get("enabled", True):
        return None
    return rule.get("download_kbps"), rule.get("upload_kbps")


class SpeedLimits:
    def __init__(self):
        self.links = {}
        self.paces = RulePaces()

    def attach(self, handler):
        from mitmproxy.proxy.server import ConnectionIO
        io = handler.transports.get(handler.client)
        if not isinstance(io, ConnectionIO) or not isinstance(io.writer, asyncio.StreamWriter):
            return
        reader = PacedReader(io.reader)
        writer = PacedWriter(io.writer, handler.timeout_watchdog.register_activity)
        io.reader = reader
        io.writer = writer
        self.links[handler.client.id] = ConnectionSpeed(reader, writer, self.paces)

    def detach(self, client_id):
        link = self.links.get(client_id)
        if link is not None and link.writer.pumping:
            link.writer.on_idle = lambda: self.links.pop(client_id, None)
        else:
            self.links.pop(client_id, None)

    def get(self, client_id):
        return self.links.get(client_id)

    def change_rule(self, rule_id, limits):
        if limits is None or not any(limits):
            self.paces.drop(rule_id)
        for link in list(self.links.values()):
            if rule_id in link.rules:
                link.change(rule_id, limits)


def install(limits):
    global _active
    from mitmproxy.proxy import server
    _active = limits
    if getattr(server.ConnectionHandler.handle_client, "agentproxy_speed", False):
        return
    original = server.ConnectionHandler.handle_client

    async def handle_client(self):
        limits = _active
        if limits is None:
            await original(self)
            return
        try:
            limits.attach(self)
        except Exception:
            log.exception("speed limits not attached to a client connection")
        try:
            await original(self)
        finally:
            limits.detach(self.client.id)

    handle_client.agentproxy_speed = True
    server.ConnectionHandler.handle_client = handle_client
