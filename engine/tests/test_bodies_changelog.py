import base64

import pytest

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


def test_text_cut_inside_a_character_stays_text():
    body = ("привет" * 100).encode("utf-8")[:301]
    rendered = bodies.render(body, 1200, "", None)
    assert rendered["encoding"] == "text" and rendered["content"].startswith("привет")


def test_body_is_shown_in_its_charset():
    body = '{"title":"Вклад"}'.encode("cp1251")
    shown = bodies.render(body, len(body), "application/json; charset=windows-1251", None)
    assert '"title": "Вклад"' in shown["content"]
    assert "Вклад" not in bodies.render(body, len(body), "application/json", None)["content"]
    assert bodies.render(b"abc", 3, "text/plain; charset=base64", None)["content"] == "abc"


@pytest.mark.parametrize("charset", ["punycode", "idna", "undefined", "utf-8\x00", "rot13"])
def test_unsafe_charset_from_the_server_falls_back_to_utf8(charset):
    import time
    body = b"-" + b"a" * 400000
    started = time.monotonic()
    shown = bodies.render(body, len(body), "text/plain; charset=" + charset, None)
    assert time.monotonic() - started < 1 and shown["content"] == body.decode("utf-8")


@pytest.mark.parametrize("charset, text", [("shift_jis", "こんにちは"), ("gbk", "你好"), ("iso-8859-2", "zażółć")])
def test_body_in_other_text_charsets_is_shown(charset, text):
    body = text.encode(charset)
    assert bodies.render(body, len(body), "text/plain; charset=" + charset, None)["content"] == text


def test_cut_body_is_decoded_only_as_far_as_shown():
    body = "Ж".encode("utf-8") * 100000
    shown = bodies.render(body, len(body), "text/plain", 10)
    assert shown["content"] == "Ж" * 10 and shown["truncated"] is True
    latin2 = "zażółć".encode("iso-8859-2")
    assert bodies.render(latin2, len(latin2), "text/plain; charset=iso-8859-2", 3)["content"] == "zaż"


def test_cut_body_in_a_wide_charset_is_still_marked_truncated():
    body = ("漢a" * 50).encode("iso-2022-jp")
    shown = bodies.render(body, len(body), "text/plain; charset=iso-2022-jp", 40)
    assert shown["content"] == ("漢a" * 20) and shown["truncated"] is True
    wide = "abcdefghijklmn".encode("utf-32")
    shown = bodies.render(wide, len(wide), "text/plain; charset=utf-32", 10)
    assert shown["content"] == "abcdefghij" and shown["truncated"] is True
