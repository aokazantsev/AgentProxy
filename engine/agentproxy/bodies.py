import base64
import json

TEXT_MARKERS = ("text/", "json", "xml", "javascript", "x-www-form-urlencoded", "graphql", "yaml", "html", "csv")


def is_textual(content_type, body):
    lowered = (content_type or "").lower()
    if any(marker in lowered for marker in TEXT_MARKERS):
        return True
    if not body:
        return True
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in body[:1024]


def render(body, size, content_type, max_chars):
    body = body or b""
    stored_complete = len(body) >= size
    if is_textual(content_type, body):
        text = body.decode("utf-8", errors="replace")
        if "json" in (content_type or "").lower() and stored_complete:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except ValueError:
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
