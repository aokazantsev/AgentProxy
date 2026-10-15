import base64

from agentproxy import bodies, changelog


def test_json_body_is_pretty_printed():
    rendered = bodies.render(b'{"a":1}', 7, "application/json; charset=utf-8", None)
    assert rendered == {"encoding": "text", "size": 7, "truncated": False, "content": '{\n  "a": 1\n}'}


def test_deeply_nested_json_stays_readable():
    body = b"[" * 100000 + b"]" * 100000
    rendered = bodies.render(body, len(body), "application/json", 50)
    assert rendered["content"] == "[" * 50


def test_truncated_json_is_not_reformatted():
    rendered = bodies.render(b'{"a":', 100, "application/json", None)
    assert rendered["content"] == '{"a":'
    assert rendered["truncated"] is True


def test_binary_body_is_base64():
    raw = bytes(range(256))
    rendered = bodies.render(raw, 256, "image/png", None)
    assert rendered["encoding"] == "base64"
    assert base64.b64decode(rendered["content"]) == raw


def test_max_chars_cuts_content():
    rendered = bodies.render(b"x" * 50, 50, "text/plain", 10)
    assert rendered["content"] == "x" * 10
    assert rendered["truncated"] is True


def test_unknown_type_utf8_is_text():
    assert bodies.is_textual("", "привет".encode("utf-8"))
    assert not bodies.is_textual("application/octet-stream", b"\x00\x01\xff")


CHANGELOG = """# Changelog

## 0.3 — 2026-11-01
- third

## 0.2 — 2026-10-20
- second a
- second b

## 0.1 — 2026-10-15
- first
"""


def test_changelog_parse():
    entries = changelog.parse(CHANGELOG)
    assert [entry["version"] for entry in entries] == ["0.3", "0.2", "0.1"]
    assert entries[1]["lines"] == ["- second a", "- second b"]


def test_changelog_newer_than_is_bounded():
    entries = changelog.parse(CHANGELOG)
    assert [e["version"] for e in changelog.newer_than(entries, "0.1", "0.2")] == ["0.2"]
    assert [e["version"] for e in changelog.newer_than(entries, None, "0.3")] == ["0.3", "0.2", "0.1"]
    assert changelog.newer_than(entries, "0.3", "0.3") == []


def test_changelog_render_limit():
    entries = changelog.parse(CHANGELOG)
    assert changelog.render(entries[:1]) == "0.3:\n- third"
    assert changelog.render(entries, limit_chars=5).endswith("…")


def test_version_key_orders_numerically():
    assert changelog.version_key("0.10") > changelog.version_key("0.9")
