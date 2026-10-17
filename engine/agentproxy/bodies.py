import base64
import codecs
import json
import re

CHARSET = re.compile(r"charset\s*=\s*[\"']?([^\"';\s]+)", re.IGNORECASE)
TEXT_CHARSETS = ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be", "ascii",
                 "iso8859-1", "iso8859-2", "iso8859-5", "iso8859-15", "cp1250", "cp1251", "cp1252", "cp866", "koi8-r", "koi8-u",
                 "mac-cyrillic")
SLOW_CHARSETS = ("punycode", "idna", "unicode-escape", "raw-unicode-escape")

TEXT_MARKERS = ("text/", "json", "xml", "javascript", "x-www-form-urlencoded", "graphql", "yaml", "html", "csv")


def is_textual(content_type, body):
    lowered = (content_type or "").lower()
    if any(marker in lowered for marker in TEXT_MARKERS):
        return True
    if not body:
        return True
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason != "unexpected end of data":
            return False
    return b"\x00" not in body[:1024]


def _charset(content_type):
    match = CHARSET.search(content_type or "")
    if not match:
        return "utf-8"
    try:
        info = codecs.lookup(match.group(1))
    except (LookupError, ValueError):
        return "utf-8"
    if info.name in SLOW_CHARSETS or not getattr(info, "_is_text_encoding", True):
        return "utf-8"
    return info.name


def _decode(body, content_type):
    try:
        return body.decode(_charset(content_type), errors="replace")
    except (UnicodeError, LookupError):
        return body.decode("utf-8", errors="replace")


def render(body, size, content_type, max_chars, raw=False):
    body = body or b""
    stored_complete = len(body) >= size
    if is_textual(content_type, body):
        pretty = "json" in (content_type or "").lower() and stored_complete and not raw
        shown = body if pretty or max_chars is None else body[:max_chars * 4 + 4]
        text = _decode(shown, content_type)
        if len(shown) < len(body) and len(text) <= max_chars:
            text = _decode(body, content_type)
        if pretty:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except (ValueError, RecursionError):
                pass
        encoding = "text"
    else:
        text = base64.b64encode(body).decode("ascii")
        encoding = "base64"
    truncated = not stored_complete
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {"encoding": encoding, "size": size, "truncated": truncated, "content": text}
