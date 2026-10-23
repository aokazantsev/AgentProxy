import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("agentproxy.store")

SCHEMA_VERSION = 2
MAX_HISTORY_BYTES = 512 * 1024 * 1024
PRUNE_EVERY_BYTES = 64 * 1024 * 1024
WAL_LIMIT_BYTES = 16 * 1024 * 1024
COUNTED_FIELDS = ("url", "path", "request_headers", "response_headers", "request_body", "response_body")
ENGINE_STOPPED = "engine stopped while the stream was open"
MAX_EVENTS_PER_FLOW = 5000
EVENTS_BEFORE_TRIM = MAX_EVENTS_PER_FLOW + MAX_EVENTS_PER_FLOW // 10
MAX_STREAM_EVENT_BYTES = 64 * 1024 * 1024
DELETE_CHUNK = 500
EVENT_OVERHEAD_BYTES = 64
STREAM_COLUMNS = ("kind", "open", "event_count")
FINISH_COLUMNS = ("duration_ms", "status", "reason", "error", "response_headers", "response_body",
                  "response_body_size", "response_content_type", "rules", "stored_bytes", "http_version", "open")
EVENT_COLUMNS = ("n", "ts", "direction", "type", "name", "event_id", "size", "truncated", "note", "data")

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    started REAL NOT NULL,
    duration_ms INTEGER,
    stored_bytes INTEGER NOT NULL DEFAULT 0,
    method TEXT NOT NULL,
    scheme TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    path TEXT NOT NULL,
    url TEXT NOT NULL,
    http_version TEXT,
    status INTEGER,
    reason TEXT,
    error TEXT,
    request_headers TEXT NOT NULL,
    request_body BLOB,
    request_body_size INTEGER NOT NULL DEFAULT 0,
    request_content_type TEXT,
    response_headers TEXT,
    response_body BLOB,
    response_body_size INTEGER NOT NULL DEFAULT 0,
    response_content_type TEXT,
    client TEXT,
    rules TEXT,
    kind TEXT NOT NULL DEFAULT 'http',
    open INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    events_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_seq INTEGER NOT NULL,
    n INTEGER NOT NULL,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    type TEXT NOT NULL,
    name TEXT,
    event_id TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    data BLOB,
    stored INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_by_flow ON events (flow_seq, n);
"""

ADDED_COLUMNS = (
    ("rules", "TEXT"),
    ("kind", "TEXT NOT NULL DEFAULT 'http'"),
    ("open", "INTEGER NOT NULL DEFAULT 0"),
    ("event_count", "INTEGER NOT NULL DEFAULT 0"),
    ("events_bytes", "INTEGER NOT NULL DEFAULT 0"),
)

SUMMARY_COLUMNS = ("seq", "id", "started", "duration_ms", "method", "scheme", "host", "port", "path", "url",
                   "status", "error", "request_body_size", "response_body_size", "response_content_type", "rules",
                   "kind", "open", "event_count")


@dataclass
class FlowRecord:
    id: str
    started: float
    method: str
    scheme: str
    host: str
    port: int
    path: str
    url: str
    request_headers: list
    request_body: bytes = b""
    request_body_size: int = 0
    request_content_type: str = ""
    duration_ms: int = None
    http_version: str = ""
    status: int = None
    reason: str = ""
    error: str = None
    response_headers: list = field(default_factory=list)
    response_body: bytes = b""
    response_body_size: int = 0
    response_content_type: str = ""
    client: str = ""
    rules: list = field(default_factory=list)
    kind: str = "http"
    open: bool = False


@dataclass(frozen=True)
class FlowFilter:
    host: str = None
    path: str = None
    method: str = None
    status: int = None
    failed_only: bool = False
    since_seq: int = None
    text: str = None
    rule: str = None
    kind: str = None
    open_only: bool = False

    def matches(self, record):
        if self.host and self.host.lower() not in record["host"].lower():
            return False
        if self.path and self.path not in record["path"]:
            return False
        if self.method and self.method.upper() != record["method"].upper():
            return False
        if self.status is not None and record["status"] != self.status:
            return False
        if self.failed_only and not (record["error"] or (record["status"] or 0) >= 400):
            return False
        if self.since_seq is not None and record["seq"] <= self.since_seq:
            return False
        if self.text and self.text.lower() not in record["url"].lower():
            return False
        if self.rule and not any(entry.get("id") == self.rule for entry in record["rules"]):
            return False
        if self.kind and self.kind != record.get("kind", "http"):
            return False
        if self.open_only and not record.get("open"):
            return False
        return True


@dataclass(frozen=True)
class EventFilter:
    direction: str = None
    type: str = None
    name: str = None
    contains: str = None
    since_n: int = None

    def matches(self, event):
        if self.direction and self.direction != event["direction"]:
            return False
        if self.type and self.type != event["type"]:
            return False
        if self.name and self.name != event["name"]:
            return False
        if self.since_n is not None and event["n"] <= self.since_n:
            return False
        return not self.contains or _has_text(event["data"], self.contains.lower())


def _storable(text):
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _text_lower(text):
    return text.lower() if isinstance(text, str) else text


def _has_text(data, needle):
    return needle in (data or b"").decode("utf-8", "replace").lower()


def _rules(text):
    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _row(row):
    result = dict(row)
    result["rules"] = _rules(result["rules"])
    result.pop("events_bytes", None)
    if result["kind"] == "http":
        for name in STREAM_COLUMNS:
            del result[name]
    else:
        result["open"] = bool(result["open"])
    return result


def _event(row):
    result = {name: row[name] for name in EVENT_COLUMNS}
    result["truncated"] = bool(result["truncated"])
    return result


def _storable_headers(headers):
    return json.dumps([[_storable(name), _storable(value)] for name, value in headers or []])


class FlowStore:
    def __init__(self, path, max_flows, max_bytes=MAX_HISTORY_BYTES):
        self.max_flows = max_flows
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        try:
            self._db = self._open(path)
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError as error:
            log.warning("history %s is damaged and is started anew: %s", path, error)
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.replace(path + suffix, path + suffix + ".broken")
            self._db = self._open(path)
        self._closed = False
        self._listeners = []
        self._event_listeners = []
        self._inserted_since_prune = 0
        self._bytes_since_prune = 0

    @staticmethod
    def _open(path):
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.create_function("text_lower", 1, _text_lower, deterministic=True)
            db.create_function("has_text", 2, _has_text, deterministic=True)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA journal_size_limit={0}".format(WAL_LIMIT_BYTES))
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                db.execute("DROP TABLE IF EXISTS flows")
            db.executescript(SCHEMA)
            columns = [row[1] for row in db.execute("PRAGMA table_info(flows)").fetchall()]
            for name, definition in ADDED_COLUMNS:
                if name not in columns:
                    db.execute("ALTER TABLE flows ADD COLUMN {0} {1}".format(name, definition))
            db.execute("DELETE FROM events WHERE flow_seq NOT IN (SELECT seq FROM flows)")
            db.execute("UPDATE flows SET open = 0, error = COALESCE(error, ?) WHERE open = 1", (ENGINE_STOPPED,))
            db.execute("PRAGMA user_version={0}".format(SCHEMA_VERSION))
        except sqlite3.DatabaseError:
            db.close()
            raise
        return db

    def add_listener(self, listener):
        self._listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)

    def add_event_listener(self, listener):
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener):
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    @staticmethod
    def _flow_row(record):
        row = {
            "id": record.id, "started": record.started, "duration_ms": record.duration_ms,
            "method": record.method, "scheme": record.scheme, "host": record.host, "port": record.port,
            "path": record.path, "url": record.url, "http_version": record.http_version,
            "status": record.status, "reason": record.reason, "error": record.error,
            "request_headers": _storable_headers(record.request_headers), "request_body": record.request_body,
            "request_body_size": record.request_body_size, "request_content_type": record.request_content_type,
            "response_headers": _storable_headers(record.response_headers), "response_body": record.response_body,
            "response_body_size": record.response_body_size,
            "response_content_type": record.response_content_type, "client": record.client,
            "rules": json.dumps(record.rules) if record.rules else None,
            "kind": record.kind, "open": 1 if record.open else 0,
        }
        for name, value in row.items():
            if isinstance(value, str):
                row[name] = _storable(value)
        row["stored_bytes"] = sum(len(row[name] or b"") for name in COUNTED_FIELDS)
        return row

    def insert(self, record):
        row = self._flow_row(record)
        names = ", ".join(row)
        marks = ", ".join(":" + name for name in row)
        with self._lock:
            if self._closed:
                return None
            cursor = self._db.execute(
                "INSERT OR REPLACE INTO flows ({0}) VALUES ({1})".format(names, marks), row)
            seq = cursor.lastrowid
            self._inserted_since_prune += 1
            self._bytes_since_prune += row["stored_bytes"]
            if self._inserted_since_prune >= 100 or self._bytes_since_prune >= PRUNE_EVERY_BYTES:
                self._prune()
        summary = self.summary(seq)
        for listener in list(self._listeners):
            listener(summary)
        return seq

    def _prune(self):
        self._inserted_since_prune = 0
        self._bytes_since_prune = 0
        self._delete_flows("open = 0 AND seq <= (SELECT MAX(seq) FROM flows) - ?", (self.max_flows,))
        while self._stored_bytes() > self.max_bytes:
            largest = self._db.execute(
                "SELECT seq, event_count, events_bytes, (SELECT COALESCE(SUM(events_bytes), 0) FROM flows WHERE open = 1) "
                "FROM flows WHERE open = 1 ORDER BY events_bytes DESC LIMIT 1").fetchone()
            if largest is not None and largest[3] > self.max_bytes // 2 and largest[2] > 0:
                self._trim_events(largest[0], largest[1], largest[2] - max(largest[2] // 10, 1))
                left = self._db.execute("SELECT events_bytes FROM flows WHERE seq = ?", (largest[0],)).fetchone()[0]
                if left < largest[2]:
                    continue
            removed = self._delete_flows(
                "seq IN (SELECT seq FROM flows WHERE open = 0 ORDER BY seq LIMIT "
                "(SELECT MAX(COUNT(*) / 10, 1) FROM flows WHERE open = 0))", ())
            if not removed:
                break

    def _delete_flows(self, condition, params):
        seqs = [row[0] for row in self._db.execute("SELECT seq FROM flows WHERE " + condition, params).fetchall()]
        for start in range(0, len(seqs), DELETE_CHUNK):
            chunk = seqs[start:start + DELETE_CHUNK]
            marks = ", ".join("?" * len(chunk))
            self._db.execute("DELETE FROM events WHERE flow_seq IN ({0})".format(marks), chunk)
            self._db.execute("DELETE FROM flows WHERE seq IN ({0})".format(marks), chunk)
        return len(seqs)

    def _stored_bytes(self):
        return self._db.execute(
            "SELECT COALESCE(SUM(stored_bytes + events_bytes), 0) FROM flows").fetchone()[0]

    def finish(self, record):
        row = self._flow_row(record)
        row["open"] = 0
        assignments = ", ".join("{0} = :{0}".format(name) for name in FINISH_COLUMNS)
        with self._lock:
            if self._closed:
                return None
            self._db.execute("UPDATE flows SET {0} WHERE id = :id".format(assignments),
                             dict({name: row[name] for name in FINISH_COLUMNS}, id=record.id))
            found = self._db.execute("SELECT seq FROM flows WHERE id = ?", (record.id,)).fetchone()
        if found is None:
            return None
        for listener in list(self._event_listeners):
            listener(found["seq"], None)
        return found["seq"]

    def finish_open(self, reason):
        with self._lock:
            if not self._closed:
                self._db.execute("UPDATE flows SET open = 0, error = COALESCE(error, ?) WHERE open = 1", (reason,))

    def add_event(self, seq, direction, kind, data=b"", size=None, name=None, event_id=None, truncated=False,
                  note=None, timestamp=None, limit=None):
        stored_data = data if limit is None else data[:limit]
        row = {
            "flow_seq": seq, "ts": time.time() if timestamp is None else timestamp, "direction": direction, "type": kind,
            "name": _storable(name) if name is not None else None,
            "event_id": _storable(event_id) if event_id is not None else None,
            "size": len(data) if size is None else size,
            "truncated": 1 if truncated or len(stored_data) < len(data) else 0,
            "note": _storable(note) if note is not None else None,
            "data": stored_data,
        }
        row["stored"] = len(stored_data) + len(row["name"] or "") + len(row["event_id"] or "") + EVENT_OVERHEAD_BYTES
        with self._lock:
            if self._closed:
                return None
            flow = self._db.execute("SELECT event_count, events_bytes FROM flows WHERE seq = ?", (seq,)).fetchone()
            if flow is None:
                return None
            row["n"] = flow["event_count"] + 1
            names = ", ".join(row)
            marks = ", ".join(":" + name for name in row)
            self._db.execute("INSERT INTO events ({0}) VALUES ({1})".format(names, marks), row)
            self._db.execute("UPDATE flows SET event_count = ?, events_bytes = events_bytes + ? WHERE seq = ?",
                             (row["n"], row["stored"], seq))
            stream_bytes = flow["events_bytes"] + row["stored"]
            too_many = False
            if row["n"] > EVENTS_BEFORE_TRIM:
                first = self._db.execute("SELECT MIN(n) FROM events WHERE flow_seq = ?", (seq,)).fetchone()[0]
                too_many = row["n"] - first + 1 > EVENTS_BEFORE_TRIM
            if too_many or stream_bytes > MAX_STREAM_EVENT_BYTES:
                self._trim_events(seq, row["n"], MAX_STREAM_EVENT_BYTES)
            self._bytes_since_prune += row["stored"]
            if self._bytes_since_prune >= PRUNE_EVERY_BYTES:
                self._prune()
        event = _event(row)
        for listener in list(self._event_listeners):
            listener(seq, event)
        return event

    def _trim_events(self, seq, last_n, byte_cap):
        cutoff = last_n - MAX_EVENTS_PER_FLOW
        while True:
            first, count = self._db.execute("SELECT MIN(n), COUNT(*) FROM events WHERE flow_seq = ?", (seq,)).fetchone()
            size = self._db.execute("SELECT events_bytes FROM flows WHERE seq = ?", (seq,)).fetchone()[0]
            if count <= 1 or (first > cutoff and size <= byte_cap):
                return
            if size > byte_cap:
                cutoff = max(cutoff, first + max(count // 10, 1) - 1)
            freed = self._db.execute("SELECT COALESCE(SUM(stored), 0) FROM events WHERE flow_seq = ? AND n <= ?",
                                     (seq, cutoff)).fetchone()[0]
            self._db.execute("DELETE FROM events WHERE flow_seq = ? AND n <= ?", (seq, cutoff))
            self._db.execute("UPDATE flows SET events_bytes = events_bytes - ? WHERE seq = ?", (freed, seq))

    def events(self, seq, event_filter, limit, tail=False, data_bytes=None):
        clauses = ["flow_seq = ?"]
        params = [seq]
        if event_filter.direction:
            clauses.append("direction = ?")
            params.append(event_filter.direction)
        if event_filter.type:
            clauses.append("type = ?")
            params.append(event_filter.type)
        if event_filter.name:
            clauses.append("name = ?")
            params.append(event_filter.name)
        if event_filter.since_n is not None:
            clauses.append("n > ?")
            params.append(event_filter.since_n)
        if event_filter.contains:
            clauses.append("has_text(data, ?)")
            params.append(event_filter.contains.lower())
        columns = [name for name in EVENT_COLUMNS if name != "data"]
        columns.append("data" if data_bytes is None else "substr(data, 1, ?) AS data")
        sql = "SELECT {0} FROM events WHERE {1} ORDER BY n {2} LIMIT ?".format(
            ", ".join(columns), " AND ".join(clauses), "DESC" if tail else "ASC")
        head = [] if data_bytes is None else [data_bytes]
        with self._lock:
            rows = self._db.execute(sql, head + params + [limit]).fetchall()
        events = [_event(row) for row in rows]
        return events[::-1] if tail else events

    def summary(self, seq):
        with self._lock:
            row = self._db.execute(
                "SELECT {0} FROM flows WHERE seq = ?".format(", ".join(SUMMARY_COLUMNS)), (seq,)).fetchone()
        return _row(row) if row else None

    def find_summary(self, flow_id):
        with self._lock:
            row = self._db.execute(
                "SELECT {0} FROM flows WHERE id = ? OR CAST(seq AS TEXT) = ?".format(", ".join(SUMMARY_COLUMNS)),
                (flow_id, flow_id)).fetchone()
        return _row(row) if row else None

    def query(self, flow_filter, limit):
        clauses = []
        params = []
        if flow_filter.host:
            clauses.append("instr(text_lower(host), ?) > 0")
            params.append(flow_filter.host.lower())
        if flow_filter.path:
            clauses.append("instr(path, ?) > 0")
            params.append(flow_filter.path)
        if flow_filter.method:
            clauses.append("upper(method) = ?")
            params.append(flow_filter.method.upper())
        if flow_filter.status is not None:
            clauses.append("status = ?")
            params.append(flow_filter.status)
        if flow_filter.failed_only:
            clauses.append("(error IS NOT NULL OR status >= 400)")
        if flow_filter.since_seq is not None:
            clauses.append("seq > ?")
            params.append(flow_filter.since_seq)
        if flow_filter.text:
            clauses.append("instr(text_lower(url), ?) > 0")
            params.append(flow_filter.text.lower())
        if flow_filter.rule:
            clauses.append("instr(rules, ?) > 0")
            params.append(json.dumps({"id": flow_filter.rule})[1:-1])
        if flow_filter.kind:
            clauses.append("kind = ?")
            params.append(flow_filter.kind)
        if flow_filter.open_only:
            clauses.append("open = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT {0} FROM flows{1} ORDER BY seq DESC LIMIT ?".format(", ".join(SUMMARY_COLUMNS), where)
        with self._lock:
            rows = self._db.execute(sql, params + [limit]).fetchall()
        return [_row(row) for row in rows]

    def activity(self, limit):
        with self._lock:
            rows = self._db.execute("SELECT started, duration_ms, method, scheme, host, error, open FROM flows "
                                    "ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, flow_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM flows WHERE id = ? OR CAST(seq AS TEXT) = ?",
                                   (flow_id, flow_id)).fetchone()
        if row is None:
            return None
        result = _row(row)
        del result["stored_bytes"]
        result["request_headers"] = json.loads(result["request_headers"])
        result["response_headers"] = json.loads(result["response_headers"] or "[]")
        return result

    def count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM flows").fetchone()[0]

    def last_seq(self):
        with self._lock:
            value = self._db.execute("SELECT MAX(seq) FROM flows").fetchone()[0]
        return value or 0

    def clear(self):
        with self._lock:
            removed = self._delete_flows("open = 0", ())
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return removed

    def close(self):
        with self._lock:
            self._closed = True
            self._db.close()
