import asyncio
import logging
import re
import secrets
import time

log = logging.getLogger("agentproxy.streamhost")

STREAM_HEADER = "X-AgentProxy-Stream"
LOOPBACK = "127.0.0.1"
MAX_STREAMS = 50
STREAMS_PER_PORT = 5
KEEPALIVE_SECONDS = 240
CLOSE_GRACE_SECONDS = 5
KEEPALIVE = b": keepalive\n\n"
PENDING_SECONDS = 60
HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT = 10
MAX_BUFFERED_BYTES = 8 * 1024 * 1024
LINE_BREAK = re.compile(r"\r\n|\r|\n")
NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
DEFAULT_HEADERS = (("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"))


class StreamRefused(Exception):
    pass


def build_frame(data=None, event=None, event_id=None, retry=None):
    lines = []
    if event is not None:
        lines.append("event: " + event)
    if event_id is not None:
        lines.append("id: " + event_id)
    if retry is not None:
        lines.append("retry: {0}".format(retry))
    if data is not None:
        lines.extend("data: " + line for line in LINE_BREAK.split(data))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _chunk(content):
    return "{0:x}\r\n".format(len(content)).encode("ascii") + content + b"\r\n"


class MockStream:
    def __init__(self, token, flow_id, rule_id, port, headers, body):
        self.token = token
        self.port = port
        self.flow_id = flow_id
        self.rule_id = rule_id
        self.headers = headers
        self.body = body
        self.created = time.monotonic()
        self.writer = None
        self.closed = False

    @property
    def is_open(self):
        return self.writer is not None and not self.closed and not self.writer.is_closing()

    def head(self):
        names = {name.lower() for name in self.headers}
        lines = ["HTTP/1.1 200 OK"]
        lines.extend("{0}: {1}".format(name, value) for name, value in DEFAULT_HEADERS if name.lower() not in names)
        lines.extend("{0}: {1}".format(name, value) for name, value in self.headers.items())
        lines.append("Transfer-Encoding: chunked")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    def send(self, content):
        if not self.is_open:
            raise StreamRefused("the stream is closed")
        if self.writer.transport.get_write_buffer_size() > MAX_BUFFERED_BYTES:
            raise StreamRefused("the app does not read the stream: more than {0} MB wait to be delivered".format(
                MAX_BUFFERED_BYTES // (1024 * 1024)))
        self.writer.write(_chunk(content))

    def close(self):
        if not self.is_open:
            return
        self.closed = True
        try:
            self.writer.write(b"0\r\n\r\n")
            self.writer.close()
        except OSError:
            return
        asyncio.get_running_loop().call_later(CLOSE_GRACE_SECONDS, self._abort_unread)

    def _abort_unread(self):
        transport = self.writer.transport
        if transport.get_write_buffer_size() > 0:
            transport.abort()


class StreamHost:
    def __init__(self):
        self.ports = []
        self._servers = []
        self._pending = {}
        self._by_flow = {}

    async def start(self):
        try:
            for _ in range(MAX_STREAMS // STREAMS_PER_PORT):
                server = await asyncio.start_server(self._serve, LOOPBACK, 0, limit=HEAD_LIMIT)
                self._servers.append(server)
                self.ports.append(server.sockets[0].getsockname()[1])
        except OSError as error:
            self.stop()
            self.ports = []
            log.error("stream listeners not started, rules with action sse will answer 503: %s", error)
            return False
        log.info("stream listeners on %s ports %s", LOOPBACK, ", ".join(str(port) for port in self.ports))
        return True

    def stop(self):
        for server in self._servers:
            server.close()
        for stream in list(self._by_flow.values()):
            stream.close()
        self._pending.clear()
        self._by_flow.clear()

    def reserve(self, flow_id, rule_id, headers, body):
        if not self.ports:
            raise StreamRefused("the stream listeners are not running: restart the engine and read_log")
        self._prune()
        if len(self._by_flow) >= MAX_STREAMS:
            raise StreamRefused("{0} streams of sse rules are open already: close some or restart the app that holds "
                                "them".format(MAX_STREAMS))
        load = {port: 0 for port in self.ports}
        for other in self._by_flow.values():
            load[other.port] += 1
        port = min(self.ports, key=lambda candidate: load[candidate])
        stream = MockStream(secrets.token_urlsafe(24), flow_id, rule_id, port, headers, body)
        self._pending[stream.token] = stream
        self._by_flow[flow_id] = stream
        return stream

    def release(self, flow_id):
        stream = self._by_flow.pop(flow_id, None)
        if stream is None:
            return
        self._pending.pop(stream.token, None)
        stream.close()

    def close_rules(self, rule_ids):
        for stream in list(self._by_flow.values()):
            if stream.rule_id in rule_ids:
                self.release(stream.flow_id)

    def find(self, flow_id):
        stream = self._by_flow.get(flow_id)
        return stream if stream is not None and stream.is_open else None

    def _prune(self):
        limit = time.monotonic() - PENDING_SECONDS
        for token, stream in list(self._pending.items()):
            if stream.created < limit:
                del self._pending[token]
                if self._by_flow.get(stream.flow_id) is stream:
                    del self._by_flow[stream.flow_id]

    @staticmethod
    def _token(head):
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == STREAM_HEADER.lower().encode("ascii"):
                return value.strip().decode("ascii", "replace")
        return None

    async def _serve(self, reader, writer):
        stream = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            token = self._token(head)
            stream = self._pending.pop(token, None) if token else None
            if stream is None:
                writer.write(NOT_FOUND)
                await writer.drain()
                return
            stream.writer = writer
            writer.write(stream.head())
            if stream.body:
                writer.write(_chunk(stream.body.encode("utf-8")))
            await writer.drain()
            while True:
                try:
                    received = await asyncio.wait_for(reader.read(65536), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    if stream.is_open:
                        writer.write(_chunk(KEEPALIVE))
                    continue
                if not received:
                    break
        except (asyncio.TimeoutError, asyncio.LimitOverrunError) as error:
            if stream is None:
                log.warning("stream listener dropped a request before its headers ended: %s", error.__class__.__name__)
        except (asyncio.IncompleteReadError, OSError):
            pass
        finally:
            if stream is not None:
                stream.closed = True
                if self._by_flow.get(stream.flow_id) is stream:
                    del self._by_flow[stream.flow_id]
            try:
                writer.close()
            except OSError:
                pass
