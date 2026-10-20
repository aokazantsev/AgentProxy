import logging

from .rules import APPLIED_MARK

log = logging.getLogger("agentproxy.flows")

FIRST_LOGGED = 20
EVERY_NTH = 50
MAX_KEYS = 500
HOST_CHARS = 100
REASON_CHARS = 200


def clean(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


class Throttle:
    def __init__(self, first=FIRST_LOGGED, every=EVERY_NTH):
        self.first = first
        self.every = every
        self.counts = {}

    def count(self, key):
        if len(self.counts) >= MAX_KEYS and key not in self.counts:
            self.counts.clear()
        number = self.counts.get(key, 0) + 1
        self.counts[key] = number
        return number if number <= self.first or number % self.every == 0 else 0


def _entry_text(entry):
    parts = [entry["id"], entry["action"]]
    if "replaced" in entry:
        parts.append("replaced={0}".format(entry["replaced"]))
    if entry.get("not_found"):
        parts.append("not_found={0}".format(len(entry["not_found"])))
    for name in ("error", "skipped"):
        if entry.get(name):
            parts.append("{0}={1}".format(name, clean(entry[name], REASON_CHARS)))
    return " ".join(parts)


def _has_problem(entry):
    return bool(entry.get("error") or entry.get("skipped") or entry.get("not_found"))


class FlowLog:
    def __init__(self):
        self.throttle = Throttle()

    def saved(self, flow, seq, error=None):
        if seq is None or flow.request is None:
            return
        entries = flow.metadata.get(APPLIED_MARK) or []
        failure = flow.error.msg if flow.error is not None else error
        if not entries and not failure:
            return
        request = flow.request
        head = "#{0} {1} {2}".format(seq, clean(request.method, 20), clean(request.pretty_host, HOST_CHARS))
        outcome = "-> {0}".format(flow.response.status_code) if flow.response is not None else "-> no response"
        if failure:
            outcome += " ({0})".format(clean(failure, REASON_CHARS))
        if not entries:
            number = self.throttle.count(("failed", clean(failure, REASON_CHARS)))
            if number:
                log.info("flow %s %s%s", head, outcome, self._nth(number))
            return
        problem = any(_has_problem(entry) for entry in entries)
        key = ("rules", tuple(_entry_text(entry) if _has_problem(entry) else entry["id"] for entry in entries))
        number = self.throttle.count(key)
        if number:
            log.log(logging.WARNING if problem else logging.INFO, "rules %s %s: %s%s", head, outcome,
                    "; ".join(_entry_text(entry) for entry in entries), self._nth(number))

    def tls_failed(self, record, seq):
        if seq is None:
            return
        number = self.throttle.count(("tls", clean(record.error, REASON_CHARS)))
        if number:
            log.warning("tls #%s CONNECT %s: %s%s", seq, clean(record.host, HOST_CHARS), clean(record.error, REASON_CHARS),
                        self._nth(number))

    @staticmethod
    def _nth(number):
        return " [occurrence {0} of this kind]".format(number) if number > FIRST_LOGGED else ""


def describe_rule(rule):
    parts = [rule["action"]]
    if "host" in rule:
        parts.append("host " + clean(rule["host"], HOST_CHARS))
    if "method" in rule:
        parts.append("method " + clean(rule["method"], 20))
    if not rule.get("enabled", True):
        parts.append("disabled")
    return ", ".join(parts)
