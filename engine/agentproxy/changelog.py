import re

HEADING = re.compile(r"^##\s+(\d+(?:\.\d+)*)\b(.*)$")


def version_key(text):
    return tuple(int(part) for part in text.split("."))


def parse(text):
    entries = []
    current = None
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            current = {"version": match.group(1), "title": match.group(2).strip(" —-"), "lines": []}
            entries.append(current)
            continue
        if line.startswith("## ") or line.startswith("# "):
            current = None
            continue
        if current is not None and line.strip():
            current["lines"].append(line.rstrip())
    return entries


def newer_than(entries, version, up_to):
    try:
        low = version_key(version) if version else None
    except ValueError:
        low = None
    high = version_key(up_to)
    result = []
    for entry in entries:
        key = version_key(entry["version"])
        if key <= high and (low is None or key > low):
            result.append(entry)
    return result


def render(entries, limit_chars=4000):
    parts = []
    for entry in entries:
        parts.append("{0}:".format(entry["version"]))
        parts.extend(entry["lines"])
    text = "\n".join(parts)
    if len(text) > limit_chars:
        text = text[:limit_chars].rstrip() + "\n…"
    return text


def read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return parse(stream.read())
    except OSError:
        return []
