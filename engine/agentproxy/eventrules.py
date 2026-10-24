import re

from .rules import APPLIED_MARK

INCOMING = "incoming"
OUTGOING = "outgoing"
MAX_BLOCK_BYTES = 1024 * 1024
LINE_END = re.compile(rb"\r\n|\r|\n")
TEXT_LINE_END = re.compile("\r\n|\r|\n")
BOM = b"\xef\xbb\xbf"
BINARY_SKIPPED = "binary messages are not changed; event_contains never matches them"
TOO_LARGE = "an event is larger than {0} MB: it and the rest of the stream passed unchanged".format(
    MAX_BLOCK_BYTES // (1024 * 1024))
CLOSED_NOTE = "rule {0} closed the stream {1}"
WHOLE_CONNECTION = "; the app's whole HTTP/2 connection was closed, with its other requests"


def _field(line):
    if line.startswith(b":"):
        return None
    return line.partition(b":")[0]


def _value(line):
    value = line.partition(b":")[2]
    return value[1:] if value.startswith(b" ") else value


class EventChanger:
    def __init__(self, flow, rules, closer, whole_connection=False):
        self.flow = flow
        self.rules = rules
        self.closer = closer
        self.whole_connection = whole_connection
        self.closed = False
        self.notes = []
        self.counted = {INCOMING: 0, OUTGOING: 0}
        for rule in rules:
            self._entry(rule)

    def _entry(self, rule):
        applied = self.flow.metadata.setdefault(APPLIED_MARK, [])
        for entry in applied:
            if entry["id"] == rule["id"]:
                return entry
        entry = dict({"id": rule["id"], "action": rule["action"]}, **({"name": rule["name"]} if "name" in rule else {}))
        applied.append(entry)
        return entry

    def update(self, rule_id, rule):
        if rule is None or not rule["enabled"] or rule["action"] != "events":
            self.rules = [current for current in self.rules if current["id"] != rule_id]
        else:
            self.rules = [rule if current["id"] == rule_id else current for current in self.rules]

    def give_up(self, reason):
        for rule in self.rules:
            self._entry(rule).setdefault("skipped", reason)
        self.rules = []

    def handle(self, direction, text):
        if self.closed:
            return text, False
        self.counted[direction] += 1
        closing = None
        edits = []
        for rule in self.rules:
            if rule.get("direction", INCOMING) != direction:
                continue
            if text is None:
                if "event_contains" in rule:
                    continue
                if "replace" in rule or "event_data" in rule:
                    self._entry(rule).setdefault("skipped", BINARY_SKIPPED)
            elif "event_contains" in rule and rule["event_contains"] not in text:
                continue
            else:
                text = self._edit(rule, text, edits)
            if "close" in rule and (closing is None or rule["close"] == "before" and closing["close"] == "after"):
                closing = rule
        deliver = closing is None or not self._close(closing, direction)
        if deliver:
            for entry, replaced in edits:
                entry["changed"] = entry.get("changed", 0) + 1
                if replaced is not None:
                    entry["replaced"] = entry.get("replaced", 0) + replaced
        return text, deliver

    def _edit(self, rule, text, edits):
        if "event_data" in rule:
            edits.append((self._entry(rule), None))
            return rule["event_data"]
        if "replace" not in rule:
            return text
        replaced = 0
        for item in rule["replace"]:
            replaced += text.count(item["find"])
            text = text.replace(item["find"], item["with"])
        if replaced:
            edits.append((self._entry(rule), replaced))
        return text

    def _close(self, rule, direction):
        entry = self._entry(rule)
        problem = self.closer(rule["close"], direction)
        if problem:
            entry["error"] = problem
            return False
        self.closed = True
        moment = "{0} {1} event {2}".format(rule["close"], direction, self.counted[direction])
        entry["closed"] = moment + (WHOLE_CONNECTION if self.whole_connection else "")
        self.notes.append(CLOSED_NOTE.format(rule["id"], entry["closed"]))
        return rule["close"] == "before"

    def take_notes(self):
        notes, self.notes = self.notes, []
        return notes


class SseEditor:
    def __init__(self, changer):
        self.changer = changer
        self.pending = bytearray()
        self.scan = 0
        self.line_start = 0
        self.started = False
        self.passing = False

    def __call__(self, chunk):
        if self.passing:
            return chunk or []
        if self.changer.closed or not chunk:
            return []
        self.pending += chunk
        out = bytearray()
        if not self.started:
            if len(self.pending) < len(BOM) and BOM.startswith(bytes(self.pending)):
                return []
            self.started = True
            if self.pending.startswith(BOM):
                out += BOM
                del self.pending[:len(BOM)]
        while not self.changer.closed:
            end = self._block_end()
            if end is None:
                break
            block = bytes(self.pending[:end])
            del self.pending[:end]
            self.scan = self.line_start = 0
            out += self._edit(block)
        comments = self._leading_comments()
        if comments:
            out += self.pending[:comments]
            del self.pending[:comments]
            self.scan -= comments
            self.line_start -= comments
        if self.changer.closed:
            self.pending.clear()
        elif len(self.pending) > MAX_BLOCK_BYTES:
            out += self.pending
            self.pending.clear()
            self.passing = True
            self.changer.give_up(TOO_LARGE)
        return bytes(out) if out else []

    def _block_end(self):
        for match in LINE_END.finditer(self.pending, self.scan):
            empty = match.start() == self.line_start
            if match.group() == b"\r" and match.end() == len(self.pending) and not empty:
                self.scan = match.start()
                return None
            self.line_start = self.scan = match.end()
            if empty:
                return match.end()
        self.scan = len(self.pending)
        return None

    def _leading_comments(self):
        if not self.pending.startswith(b":"):
            return 0
        end = 0
        for match in LINE_END.finditer(self.pending, 0, self.line_start):
            if self.pending[end:end + 1] != b":":
                break
            end = match.end()
        return end

    def _edit(self, block):
        lines = []
        position = 0
        for match in LINE_END.finditer(block):
            lines.append((block[position:match.start()], match.group()))
            position = match.end()
        data_lines = [index for index, (content, _) in enumerate(lines) if _field(content) == b"data"]
        if not data_lines:
            return block
        text = b"\n".join(_value(lines[index][0]) for index in data_lines).decode("utf-8", "surrogateescape")
        new, deliver = self.changer.handle(INCOMING, text)
        if not deliver:
            return b""
        if new == text:
            return block
        first = data_lines[0]
        prefix = b"data: " if lines[first][0][5:6] == b" " else b"data:"
        terminator = lines[first][1]
        skipped = set(data_lines[1:])
        out = bytearray()
        for index, (content, ending) in enumerate(lines):
            if index == first:
                for part in TEXT_LINE_END.split(new):
                    start = b"data: " if part.startswith(" ") else prefix
                    out += start + part.encode("utf-8", "surrogateescape") + terminator
            elif index not in skipped:
                if not content and ending.startswith(b"\n") and out.endswith(b"\r"):
                    out += b"\n"
                out += content + ending
        return bytes(out)


class EventStreams:
    def __init__(self):
        self.live = {}

    def add(self, flow_id, changer):
        self.live[flow_id] = changer

    def release(self, flow_id):
        self.live.pop(flow_id, None)

    def change_rule(self, rule_id, rule):
        for changer in list(self.live.values()):
            changer.update(rule_id, rule)
