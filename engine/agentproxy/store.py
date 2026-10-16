import json
import sqlite3
import threading
from dataclasses import dataclass, field

SCHEMA_VERSION = 2
MAX_HISTORY_BYTES = 512 * 1024 * 1024
PRUNE_EVERY_BYTES = 64 * 1024 * 1024
WAL_LIMIT_BYTES = 16 * 1024 * 1024
COUNTED_FIELDS = ("url", "path", "request_headers", "response_headers", "request_body", "response_body")

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
    client TEXT
);
"""

SUMMARY_COLUMNS = ("seq", "id", "started", "duration_ms", "method", "scheme", "host", "port", "path", "url",
                   "status", "error", "request_body_size", "response_body_size", "response_content_type")


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


@dataclass(frozen=True)
class FlowFilter:
    host: str = None
    path: str = None
    method: str = None
    status: int = None
    failed_only: bool = False
    since_seq: int = None
    text: str = None

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
        return True


def _storable(text):
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _text_lower(text):
    return text.lower() if isinstance(text, str) else text


def _storable_headers(headers):
    return json.dumps([[_storable(name), _storable(value)] for name, value in headers or []])


class FlowStore:
    def __init__(self, path, max_flows, max_bytes=MAX_HISTORY_BYTES):
        self.max_flows = max_flows
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.create_function("text_lower", 1, _text_lower, deterministic=True)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA journal_size_limit={0}".format(WAL_LIMIT_BYTES))
        self._migrate()
        self._closed = False
        self._listeners = []
        self._inserted_since_prune = 0
        self._bytes_since_prune = 0

    def _migrate(self):
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self._db.execute("DROP TABLE IF EXISTS flows")
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA user_version={0}".format(SCHEMA_VERSION))

    def add_listener(self, listener):
        self._listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)

    def insert(self, record):
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
        }
        for name, value in row.items():
            if isinstance(value, str):
                row[name] = _storable(value)
        row["stored_bytes"] = sum(len(row[name] or b"") for name in COUNTED_FIELDS)
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
        self._db.execute(
            "DELETE FROM flows WHERE seq <= (SELECT MAX(seq) FROM flows) - ?", (self.max_flows,))
        while self._stored_bytes() > self.max_bytes:
            self._db.execute(
                "DELETE FROM flows WHERE seq IN (SELECT seq FROM flows ORDER BY seq LIMIT "
                "(SELECT MAX(COUNT(*) / 10, 1) FROM flows))")

    def _stored_bytes(self):
        return self._db.execute("SELECT COALESCE(SUM(stored_bytes), 0) FROM flows").fetchone()[0]

    def summary(self, seq):
        with self._lock:
            row = self._db.execute(
                "SELECT {0} FROM flows WHERE seq = ?".format(", ".join(SUMMARY_COLUMNS)), (seq,)).fetchone()
        return dict(row) if row else None

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
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT {0} FROM flows{1} ORDER BY seq DESC LIMIT ?".format(", ".join(SUMMARY_COLUMNS), where)
        with self._lock:
            rows = self._db.execute(sql, params + [limit]).fetchall()
        return [dict(row) for row in rows]

    def get(self, flow_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM flows WHERE id = ? OR CAST(seq AS TEXT) = ?",
                                   (flow_id, flow_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
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
            removed = self._db.execute("DELETE FROM flows").rowcount
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return removed

    def close(self):
        with self._lock:
            self._closed = True
            self._db.close()
