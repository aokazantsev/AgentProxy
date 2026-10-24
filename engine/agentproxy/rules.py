import copy
import json
import logging
import math
import os
import re
import shutil
import threading
import time

from .streamtap import NO_BODY_STATUSES

log = logging.getLogger("agentproxy.rules")

FILE_NAME = "rules.json"
ACTIONS = ("respond", "rewrite", "drop", "delay", "sse")
TERMINAL_ACTIONS = ("respond", "drop", "sse")
MATCH_FIELDS = ("host", "path", "method", "url_regex")
VIEW_FIELDS = ("id", "created", "hits", "remaining", "active", "last_applied", "last_error")
HOST_WITH_PORT = re.compile(r"^(?:[^:\[\]]*|\[.*\]):\d+$")
SPEED_FIELDS = ("download_kbps", "upload_kbps")
COMMON_FIELDS = ("name", "enabled", "action", "delay_ms") + SPEED_FIELDS + ("times",) + MATCH_FIELDS
ACTION_FIELDS = {
    "respond": ("status", "headers", "body", "body_file", "content_type"),
    "rewrite": ("status", "request_headers", "response_headers", "response_body", "replace"),
    "drop": (),
    "delay": (),
    "sse": ("headers", "body"),
}
REWRITE_CHANGES = ACTION_FIELDS["rewrite"]
BODY_CHANGES = ("response_body", "replace")
ALL_FIELDS = COMMON_FIELDS + tuple(sorted({name for names in ACTION_FIELDS.values() for name in names}))
MAX_RULES = 200
MAX_DELAY_MS = 300000
MAX_KBPS = 1000000
MAX_TIMES = 1000000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_REPLACEMENTS = 100
MAX_TEXT = 16 * 1024 * 1024
MANAGED_HEADERS = ("content-length", "transfer-encoding", "x-agentproxy-proxied")
STREAM_FIXED_HEADERS = ("content-type", "content-encoding")
HOP_BY_HOP_HEADERS = ("connection", "keep-alive", "proxy-connection", "upgrade", "te", "trailer")
MAX_MATCHED_URL = 8192
CONTROL_CHARACTERS = re.compile("[\x00-\x08\x0a-\x1f\x7f]")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
TRUE_WORDS = ("true", "1", "yes")
FALSE_WORDS = ("false", "0", "no")
PLAN_MARK = "agentproxy_rule_plan"
APPLIED_MARK = "agentproxy_rules"
NO_REQUEST_STREAM_MARK = "agentproxy_no_request_stream"
NO_RESPONSE_STREAM_MARK = "agentproxy_no_response_stream"


class RuleError(Exception):
    pass


class RuleNotFound(RuleError):
    pass


def _unicode(name, value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise RuleError("{0} must be valid Unicode text".format(name))
    return value


def _text(name, value, limit=2000):
    if not isinstance(value, str) or not value:
        raise RuleError("{0} must be a non-empty string".format(name))
    if len(value) > limit:
        raise RuleError("{0} is longer than {1} characters".format(name, limit))
    return _unicode(name, value)


def _number(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuleError("{0} must be an integer".format(name))
    try:
        number = int(value)
    except ValueError:
        raise RuleError("{0} must be an integer".format(name))
    if not low <= number <= high:
        raise RuleError("{0} must be between {1} and {2}".format(name, low, high))
    return number


def _flag(name, value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in TRUE_WORDS + FALSE_WORDS:
        return value.strip().lower() in TRUE_WORDS
    raise RuleError("{0} must be true or false".format(name))


def _headers(name, value, removable):
    if not isinstance(value, dict) or not value:
        raise RuleError("{0} must be a non-empty object: header name -> value".format(name))
    result = {}
    for header, header_value in value.items():
        if not HEADER_NAME.fullmatch(header):
            raise RuleError("{0}: invalid header name {1!r}".format(name, header))
        if header.lower() in MANAGED_HEADERS:
            raise RuleError("{0}: {1} is computed by AgentProxy and cannot be set".format(name, header))
        if header.lower() in HOP_BY_HOP_HEADERS:
            raise RuleError("{0}: {1} is a connection header and cannot be set by a rule".format(name, header))
        if header_value is None and removable:
            result[header] = None
            continue
        if not isinstance(header_value, str) or CONTROL_CHARACTERS.search(header_value):
            raise RuleError("{0}: value of {1} must be a single-line string{2}".format(
                name, header, " or null to remove the header" if removable else ""))
        result[header] = _unicode(name, header_value)
    return result


def _replacements(value):
    if not isinstance(value, list) or not value:
        raise RuleError('replace must be a non-empty list of {"find": "...", "with": "..."}')
    if len(value) > MAX_REPLACEMENTS:
        raise RuleError("replace allows at most {0} items".format(MAX_REPLACEMENTS))
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"find", "with"}:
            raise RuleError('every replace item must be {"find": "...", "with": "..."}; texts over 300 characters '
                            'are shown in answers as find_preview/with_preview: pass them whole')
        find = _text("replace.find", item["find"], MAX_TEXT)
        if not isinstance(item["with"], str):
            raise RuleError("replace.with must be a string")
        result.append({"find": find, "with": _unicode("replace.with", item["with"])})
    return result


def _created(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return time.time()
    try:
        number = float(value)
    except OverflowError:
        return time.time()
    return number if math.isfinite(number) else time.time()


def check_body_file(path):
    if not os.path.isabs(path):
        raise RuleError("body_file must be an absolute path: {0}".format(path))
    if not os.path.isfile(path):
        raise RuleError("body_file does not exist: {0}".format(path))
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise RuleError("body_file is larger than {0} MB: {1}".format(MAX_FILE_BYTES // (1024 * 1024), path))


def normalize(raw, check_files=True):
    if not isinstance(raw, dict):
        raise RuleError("rule must be an object")
    unknown = sorted(name for name in raw if name not in ALL_FIELDS)
    shown = [name for name in unknown if name.endswith("_preview") or name.endswith("_chars")]
    if shown:
        raise RuleError("{0}: shown in answers only, for texts over 300 characters; pass the whole text in the field "
                        "itself or leave it out".format(", ".join(shown)))
    if unknown:
        raise RuleError("unknown field {0}; allowed: {1}".format(", ".join(unknown), ", ".join(ALL_FIELDS)))
    values = {name: value for name, value in raw.items() if value is not None}
    action = values.get("action")
    if action not in ACTIONS:
        raise RuleError("action must be one of {0}".format(", ".join(ACTIONS)))
    for name in values:
        if name not in COMMON_FIELDS and name not in ACTION_FIELDS[action]:
            owners = [other for other in ACTIONS if name in ACTION_FIELDS[other]]
            raise RuleError("{0} is used only with action {1}, not {2}".format(name, " or ".join(owners), action))
    rule = {"action": action}
    if not any(name in values for name in MATCH_FIELDS):
        raise RuleError("a rule needs at least one condition: {0}".format(", ".join(MATCH_FIELDS)))
    if isinstance(values.get("host"), str) and values["host"].startswith("[") and values["host"].endswith("]"):
        values["host"] = values["host"][1:-1]
    for name in ("host", "path"):
        if name in values:
            rule[name] = _text(name, values[name])
            if "*" in (rule[name] if name == "host" else rule[name].split("?", 1)[0]):
                raise RuleError("{0} is a substring, not a wildcard: drop a leading or trailing * (e.g. example.com, "
                                "/v1/), for * in the middle use url_regex, e.g. /v1/[^/]+/items".format(name))
    if "host" in rule and "/" in rule["host"]:
        raise RuleError("host is the name alone, without scheme and path: put the path in path, the port in url_regex")
    if "host" in rule and HOST_WITH_PORT.match(rule["host"]):
        raise RuleError("host is matched without the port: keep host without it and add url_regex for the port, "
                        "e.g. host=stage.example.com, url_regex=:8443/ (IPv6 addresses go without brackets)")
    if "method" in values:
        rule["method"] = _text("method", values["method"], 20).upper()
        if len(rule["method"]) > 20:
            raise RuleError("method is longer than 20 characters")
        if rule["method"] == "CONNECT":
            raise RuleError("method CONNECT never matches: rules apply to requests inside the tunnel")
    if "url_regex" in values:
        pattern = _text("url_regex", values["url_regex"])
        try:
            re.compile(pattern)
        except Exception as error:
            raise RuleError("url_regex is not a valid regular expression: {0}".format(error))
        rule["url_regex"] = pattern
    if "name" in values:
        rule["name"] = _text("name", values["name"], 200)
    rule["enabled"] = _flag("enabled", values.get("enabled", True))
    if "delay_ms" in values:
        rule["delay_ms"] = _number("delay_ms", values["delay_ms"], 0, MAX_DELAY_MS)
    for name in SPEED_FIELDS:
        if name in values:
            rule[name] = _number(name, values[name], 1, MAX_KBPS)
    if action == "delay" and not rule.get("delay_ms") and not limits_speed(rule):
        raise RuleError("action delay needs delay_ms > 0, download_kbps or upload_kbps")
    if "times" in values:
        rule["times"] = _number("times", values["times"], 1, MAX_TIMES)
    if "status" in values:
        rule["status"] = _number("status", values["status"], 200, 599)
    if action == "respond" and rule.get("status") in NO_BODY_STATUSES and (
            values.get("body") or "body_file" in values):
        raise RuleError("status {0} has no body; drop body or body_file".format(rule["status"]))
    if "headers" in values:
        rule["headers"] = _headers("headers", values["headers"], removable=False)
    for name in ("request_headers", "response_headers"):
        if name in values:
            rule[name] = _headers(name, values[name], removable=True)
    if action == "sse":
        fixed = sorted(name for name in rule.get("headers", {}) if name.lower() in STREAM_FIXED_HEADERS)
        if fixed:
            raise RuleError("headers: the stream is always text/event-stream and unpacked; drop {0}".format(
                ", ".join(fixed)))
    if "body" in values and "body_file" in values:
        raise RuleError("use either body or body_file, not both")
    for name in ("body", "response_body"):
        if name in values:
            if not isinstance(values[name], str) or len(values[name]) > MAX_TEXT:
                raise RuleError("{0} must be a string up to {1} characters".format(name, MAX_TEXT))
            rule[name] = _unicode(name, values[name])
    if "body_file" in values:
        path = _text("body_file", values["body_file"], 1000)
        if check_files:
            check_body_file(path)
        elif not os.path.isabs(path):
            raise RuleError("body_file must be an absolute path: {0}".format(path))
        rule["body_file"] = path
    if "content_type" in values:
        rule["content_type"] = _text("content_type", values["content_type"], 200)
        if CONTROL_CHARACTERS.search(rule["content_type"]):
            raise RuleError("content_type must be a single-line string")
    if "replace" in values:
        rule["replace"] = _replacements(values["replace"])
    if action == "rewrite" and not any(name in rule for name in REWRITE_CHANGES):
        raise RuleError("action rewrite needs at least one of {0}".format(", ".join(REWRITE_CHANGES)))
    return rule


def _answers_always(rule):
    return rule["enabled"] and rule["action"] in TERMINAL_ACTIONS and "times" not in rule


def _covers(wide, narrow):
    for field in MATCH_FIELDS:
        if field not in wide:
            continue
        if field not in narrow:
            return False
        if field == "host":
            covered = wide["host"].lower() in narrow["host"].lower()
        elif field == "path":
            covered = wide["path"] in narrow["path"]
        else:
            covered = wide[field] == narrow[field]
        if not covered:
            return False
    return True


def matches(rule, host, path, method, url):
    if "host" in rule and rule["host"].lower() not in host.lower():
        return False
    if "path" in rule and rule["path"] not in path:
        return False
    if "method" in rule and rule["method"] != method.upper():
        return False
    if "url_regex" in rule and not re.search(rule["url_regex"], url[:MAX_MATCHED_URL]):
        return False
    return True


def limits_speed(rule):
    return any(name in rule for name in SPEED_FIELDS)


def changes_body(rule):
    return rule["action"] == "rewrite" and any(name in rule for name in BODY_CHANGES)


class RuleSet:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, FILE_NAME)
        self._lock = threading.Lock()
        self._rules = []
        self._next_id = 1
        self._state = {}
        self.read_error = None
        self.problem = None
        self.problem_after_change = None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as stream:
                content = stream.read()
        except OSError as error:
            self.read_error = str(error)
            log.error("rules file %s unreadable, rules are off and cannot be changed: %s", self.path, error)
            return
        try:
            data = json.loads(content.decode("utf-8-sig"))
            if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
                raise ValueError("expected an object with a rules list")
        except (ValueError, RecursionError) as error:
            self._set_aside("is not valid UTF-8 JSON ({0})".format(error))
            return
        broken = []
        seen = set()
        turned_off = []
        unknown = set("file field " + name for name in data if name not in ("rules", "next_id"))
        for number, raw in enumerate(data["rules"], 1):
            try:
                if not isinstance(raw, dict):
                    raise RuleError("must be an object")
                fields = {name: value for name, value in raw.items() if name in ALL_FIELDS}
                foreign = [name for name in raw if name not in ALL_FIELDS and name not in VIEW_FIELDS]
                unknown.update(foreign)
                rule = normalize(fields, check_files=False)
                if not isinstance(raw.get("id"), str) or not raw["id"]:
                    raise RuleError("rule without id")
                _unicode("id", raw["id"])
                if raw["id"] in seen:
                    raise RuleError("duplicate id {0}".format(raw["id"]))
                rule["created"] = _created(raw.get("created"))
                if foreign and rule["enabled"]:
                    rule["enabled"] = False
                    turned_off.append(raw["id"])
            except Exception as error:
                broken.append("rule #{0}: {1}".format(number, error))
                continue
            seen.add(raw["id"])
            rule["id"] = raw["id"]
            self._rules.append(rule)
        next_id = data.get("next_id")
        self._next_id = next_id if isinstance(next_id, int) and not isinstance(next_id, bool) and next_id > 0 else 1
        self._next_id = max([self._next_id] + [self._number(rule["id"]) + 1 for rule in self._rules])
        unknown_text = ", ".join(sorted(unknown))
        if broken:
            reason = "had invalid rules ({0}), which are skipped".format("; ".join(broken))
            if unknown:
                reason += " and fields this version does not know ({0})".format(unknown_text)
            if self._set_aside(reason, keep=True):
                try:
                    self._save(self._rules, self._next_id)
                except RuleError as error:
                    log.error("rules file %s not cleaned: %s", self.path, error)
                    self.problem += "; the file could not be cleaned ({0})".format(error)
        elif unknown:
            self.problem = ("rules.json has fields this version does not know ({0}); they are ignored and dropped "
                            "on the next change".format(unknown_text))
            self.problem_after_change = ("fields this version does not know ({0}) were dropped from rules.json by a "
                                         "change".format(unknown_text))
        if turned_off:
            ids = ", ".join(turned_off)
            self.problem += "; rules with them are turned off, check and enable them: " + ids
            self.problem_after_change += "; rules with them were turned off at start: " + ids
        log.info("rules: %s loaded, %s enabled", len(self._rules), sum(1 for rule in self._rules if rule["enabled"]))

    def _set_aside(self, reason, keep=False):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = "{0}.{1}.broken".format(self.path, stamp)
        attempt = 1
        while os.path.lexists(target):
            attempt += 1
            target = "{0}.{1}-{2}.broken".format(self.path, stamp, attempt)
        try:
            existing = self._same_copy()
            if existing:
                target = existing
            elif keep:
                shutil.copyfile(self.path, target)
            else:
                os.replace(self.path, target)
            log.warning("rules file %s %s; copy kept as %s", self.path, reason, target)
            self.problem = "rules.json {0}; the original is kept as {1}".format(reason, target)
            self.problem_after_change = ("at engine start rules.json {0}; the original with this numbering is kept "
                                         "as {1}; the file has been rewritten since".format(reason, target))
            return True
        except OSError as error:
            log.error("rules file %s %s and could not be set aside: %s", self.path, reason, error)
            if keep:
                self.problem = ("rules.json {0}; the valid rules work, the invalid ones are skipped and could not be "
                                "copied aside ({1}), so they are lost on the next change".format(reason, error))
                self.problem_after_change = ("rules.json {0}; they could not be copied aside and were lost on a "
                                             "change".format(reason))
                if "fields this version does not know" in reason:
                    self.problem_after_change += "; the unknown fields were dropped as well"
                return False
            self.read_error = "{0}; copy to {1} failed: {2}".format(reason, target, error)
            return False

    def _same_copy(self):
        folder = os.path.dirname(self.path)
        prefix = os.path.basename(self.path) + "."
        with open(self.path, "rb") as stream:
            content = stream.read()
        for name in sorted(os.listdir(folder), reverse=True):
            if not (name.startswith(prefix) and name.endswith(".broken")):
                continue
            candidate = os.path.join(folder, name)
            try:
                if os.path.getsize(candidate) != len(content):
                    continue
                with open(candidate, "rb") as stream:
                    if stream.read() == content:
                        return candidate
            except OSError:
                continue
        return None

    def _changed(self):
        self.problem = self.problem_after_change

    @staticmethod
    def _number(rule_id):
        match = re.match(r"^r(\d{1,9})$", rule_id)
        return int(match.group(1)) if match else 0

    def _save(self, rules, next_id):
        self._check_readable()
        temporary = self.path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({"next_id": next_id, "rules": rules}, stream, ensure_ascii=False, indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as error:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise RuleError("rules not saved to {0}: {1}".format(self.path, error))

    def _check_readable(self):
        if self.read_error:
            raise RuleError("rules.json could not be read ({0}); fix or delete {1} and restart the engine".format(
                self.read_error, self.path))

    def _find(self, rule_id):
        for index, rule in enumerate(self._rules):
            if rule["id"] == rule_id:
                return index
        raise RuleNotFound("rule {0} not found; list_rules shows existing ids".format(rule_id))

    def _view(self, rule):
        state = self._state.get(rule["id"], {})
        view = copy.deepcopy(rule)
        view["hits"] = state.get("hits", 0)
        if "times" in rule:
            view["remaining"] = max(rule["times"] - view["hits"], 0)
        view["active"] = rule["enabled"] and view.get("remaining", 1) > 0
        view["last_applied"] = state.get("last_applied")
        if state.get("last_error"):
            view["last_error"] = state["last_error"]
        return view

    def list(self):
        with self._lock:
            return [self._view(rule) for rule in self._rules]

    def shadowed_by(self, rule_id):
        with self._lock:
            target = next(rule for rule in self._rules if rule["id"] == rule_id)
            if not target["enabled"]:
                return None
            for rule in self._rules:
                if rule["id"] == rule_id:
                    return None
                if _answers_always(rule) and _covers(rule, target):
                    return {"id": rule["id"], "action": rule["action"]}

    def shadowing(self, rule_id):
        with self._lock:
            ids = [rule["id"] for rule in self._rules]
            position = ids.index(rule_id)
            current = self._rules[position]
            if not _answers_always(current):
                return []
            return [rule["id"] for rule in self._rules[position + 1:] if rule["enabled"] and _covers(current, rule)]

    def get(self, rule_id):
        with self._lock:
            self._check_readable()
            return self._view(self._rules[self._find(rule_id)])

    def counts(self):
        with self._lock:
            views = [self._view(rule) for rule in self._rules]
        counts = {"total": len(views), "enabled": sum(1 for view in views if view["enabled"]),
                  "active": sum(1 for view in views if view["active"])}
        if self.read_error:
            counts["problem"] = ("rules.json could not be read ({0}); rules are off and cannot be changed until it "
                                 "is fixed or deleted and the engine restarts".format(self.read_error))
        elif self.problem:
            counts["problem"] = self.problem
        return counts

    def add(self, fields):
        rule = normalize(fields)
        with self._lock:
            if len(self._rules) >= MAX_RULES:
                raise RuleError("at most {0} rules; delete unused ones first".format(MAX_RULES))
            rule["id"] = "r{0}".format(self._next_id)
            rule["created"] = time.time()
            self._save(self._rules + [rule], self._next_id + 1)
            self._changed()
            self._rules.append(rule)
            self._next_id += 1
            return self._view(rule)

    def update(self, rule_id, changes, reset_hits=False):
        with self._lock:
            self._check_readable()
            index = self._find(rule_id)
            current = self._rules[index]
            merged = {name: value for name, value in current.items() if name not in ("id", "created")}
            if "action" in changes and changes["action"] != current["action"]:
                merged = {name: value for name, value in merged.items() if name in COMMON_FIELDS}
            if "enabled" in changes and changes["enabled"] is None:
                raise RuleError("enabled must be true or false")
            for one, other in (("body", "body_file"), ("body_file", "body")):
                if changes.get(one) is not None and other not in changes:
                    merged.pop(other, None)
            merged.update(changes)
            check_files = changes.get("body_file") is not None
            rule = normalize(merged, check_files=check_files)
            rule["id"] = current["id"]
            rule["created"] = current["created"]
            rules = list(self._rules)
            rules[index] = rule
            self._save(rules, self._next_id)
            self._rules = rules
            self._changed()
            if reset_hits:
                self._state.pop(rule_id, None)
            else:
                self._state.get(rule_id, {}).pop("last_error", None)
            return self._view(rule)

    def delete(self, rule_id):
        with self._lock:
            self._check_readable()
            index = self._find(rule_id)
            rules = [rule for position, rule in enumerate(self._rules) if position != index]
            self._save(rules, self._next_id)
            self._rules = rules
            self._changed()
            self._state.pop(rule_id, None)
            return [rule_id]

    def clear(self):
        with self._lock:
            removed = [rule["id"] for rule in self._rules]
            self._save([], self._next_id)
            self._changed()
            self._rules = []
            self._state = {}
            return removed

    def plan(self, host, path, method, url):
        chosen = []
        now = time.time()
        with self._lock:
            for rule in self._rules:
                if not rule["enabled"] or not matches(rule, host, path, method, url):
                    continue
                state = self._state.setdefault(rule["id"], {"hits": 0})
                if "times" in rule and state["hits"] >= rule["times"]:
                    continue
                state["hits"] += 1
                state["last_applied"] = now
                chosen.append(copy.deepcopy(rule))
                if rule["action"] in TERMINAL_ACTIONS:
                    break
        return chosen

    def may_answer(self, host):
        lowered = host.lower()
        with self._lock:
            for rule in self._rules:
                if not rule["enabled"] or rule["action"] not in TERMINAL_ACTIONS:
                    continue
                if "times" in rule and self._state.get(rule["id"], {}).get("hits", 0) >= rule["times"]:
                    continue
                if "host" in rule and rule["host"].lower() in lowered:
                    return True
        return False

    def may_limit(self, host):
        lowered = host.lower()
        with self._lock:
            for rule in self._rules:
                if not rule["enabled"] or not limits_speed(rule) or "host" not in rule:
                    continue
                if "times" in rule and self._state.get(rule["id"], {}).get("hits", 0) >= rule["times"]:
                    continue
                if rule["host"].lower() in lowered:
                    return True
        return False

    def clear_error(self, rule_id):
        with self._lock:
            self._state.get(rule_id, {}).pop("last_error", None)

    def note_error(self, rule_id, message):
        with self._lock:
            self._state.setdefault(rule_id, {"hits": 0})["last_error"] = message
