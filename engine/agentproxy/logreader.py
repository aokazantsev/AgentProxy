import os
import re
import time

SOURCES = {
    "engine": ("engine.log.3", "engine.log.2", "engine.log.1", "engine.log"),
    "tray": ("log.old.txt", "log.txt"),
}
RECORD_START = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  ")
ENGINE_LEVEL = re.compile(r"^.{19}  (\w+)")
LEVELS = {"info": ("INFO", "WARNING", "ERROR", "CRITICAL"), "warning": ("WARNING", "ERROR", "CRITICAL"),
          "error": ("ERROR", "CRITICAL")}
RECORD_CHARS = 2000
RECORD_HEAD_CHARS = 500
TEXT_CHARS = 40000
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _read_records(path):
    try:
        with open(path, "rb") as stream:
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return [], None
    records = []
    for line in text.splitlines():
        if RECORD_START.match(line):
            records.append(line)
        elif records:
            records[-1] += "\n" + line
    return records, len(text.encode("utf-8"))


def _shorten(record):
    if len(record) <= RECORD_CHARS:
        return record
    return record[:RECORD_HEAD_CHARS] + "\n…\n" + record[RECORD_HEAD_CHARS - RECORD_CHARS:]


def _level_of(record):
    match = ENGINE_LEVEL.match(record)
    return match.group(1) if match else ""


def read(directory, source, lines, level=None, contains=None, since_minutes=None):
    files = []
    records = []
    for name in SOURCES[source]:
        found, size = _read_records(os.path.join(directory, name))
        if size is not None:
            files.append({"name": name, "bytes": size})
            records.extend(found)
    oldest = RECORD_START.match(records[0]).group(1) if records else None
    wanted = LEVELS[level] if level else None
    needle = contains.lower() if contains else None
    threshold = None
    if since_minutes is not None:
        threshold = time.strftime(TIME_FORMAT, time.localtime(time.time() - since_minutes * 60))
    matched = [record for record in records
               if (wanted is None or _level_of(record) in wanted)
               and (needle is None or needle in record.lower())
               and (threshold is None or RECORD_START.match(record).group(1) >= threshold)]
    chosen = matched[-lines:]
    shown = [_shorten(record) for record in chosen]
    while len(shown) > 1 and sum(len(record) + 1 for record in shown) > TEXT_CHARS:
        shown.pop(0)
    return {"source": source, "now": time.strftime(TIME_FORMAT), "files": files, "oldest": oldest,
            "matched": len(matched), "returned": len(shown), "text": "\n".join(shown)}
