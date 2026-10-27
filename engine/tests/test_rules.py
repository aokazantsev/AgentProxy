import json
import os

import pytest

from agentproxy.rules import FILE_NAME, RuleError, RuleSet, matches, normalize


def by_id(rules, rule_id):
    return next(rule for rule in rules.list() if rule["id"] == rule_id)


def broken_copies(tmp_path):
    return sorted(str(path) for path in tmp_path.glob(FILE_NAME + ".*.broken"))


def rule_set(tmp_path):
    return RuleSet(str(tmp_path))


def respond(**extra):
    return dict({"action": "respond", "path": "/v1/items", "body": "{}"}, **extra)


@pytest.mark.parametrize("raw, message", [
    ({"action": "respond", "body": "x"}, "at least one condition"),
    ({"action": "teleport", "path": "/"}, "action must be one of"),
    ({"path": "/"}, "action must be one of"),
    ({"action": "respond", "path": "/", "colour": "red"}, "unknown field colour"),
    ({"action": "drop", "path": "/", "body": "x"}, "body is used only with action respond"),
    ({"action": "respond", "path": "/", "replace": [{"find": "a", "with": "b"}]}, "only with action rewrite"),
    ({"action": "delay", "path": "/"}, "delay needs delay_ms > 0"),
    ({"action": "rewrite", "path": "/"}, "rewrite needs at least one of"),
    ({"action": "respond", "path": "/", "status": 99}, "status must be between"),
    ({"action": "respond", "path": "/", "status": True}, "status must be an integer"),
    ({"action": "respond", "path": "/", "delay_ms": 300001}, "delay_ms must be between"),
    ({"action": "respond", "path": "/", "times": 0}, "times must be between"),
    ({"action": "respond", "path": "", "body": "x"}, "path must be a non-empty string"),
    ({"action": "respond", "url_regex": "("}, "not a valid regular expression"),
    ({"action": "respond", "path": "/", "headers": {"Content-Length": "5"}}, "computed by AgentProxy"),
    ({"action": "respond", "path": "/", "headers": {"Bad Name": "x"}}, "invalid header name"),
    ({"action": "respond", "path": "/", "headers": {"X-A": "a\r\nX-B: b"}}, "single-line string"),
    ({"action": "respond", "path": "/", "headers": {"X-A": None}}, "single-line string"),
    ({"action": "rewrite", "path": "/", "response_headers": {"Transfer-Encoding": None}}, "computed by"),
    ({"action": "respond", "path": "/", "body": "x", "body_file": "C:\\x.json"}, "either body or body_file"),
    ({"action": "respond", "path": "/", "body_file": "relative.json"}, "absolute path"),
    ({"action": "respond", "path": "/", "body_file": "C:\\no\\such\\file.json"}, "does not exist"),
    ({"action": "rewrite", "path": "/", "replace": []}, "non-empty list"),
    ({"action": "rewrite", "path": "/", "replace": [{"find": "", "with": "b"}]}, "replace.find"),
    ({"action": "rewrite", "path": "/", "replace": [{"find": "a"}]}, "every replace item"),
    ({"action": "respond", "path": "/", "enabled": "maybe"}, "enabled must be true or false"),
])
def test_normalize_rejects_with_reason(raw, message):
    with pytest.raises(RuleError, match=message):
        normalize(raw)


def test_normalize_keeps_known_fields_and_drops_nulls(tmp_path):
    body = tmp_path / "items.json"
    body.write_text("[]")
    rule = normalize({"action": "respond", "host": "API.example.com", "method": "post", "body_file": str(body),
                      "status": "503", "name": None, "enabled": "false"})
    assert rule == {"action": "respond", "host": "API.example.com", "method": "POST", "body_file": str(body),
                    "status": 503, "enabled": False}


def test_matches_uses_list_flows_semantics():
    rule = {"host": "Example.COM", "path": "/v1/items", "method": "GET"}
    assert matches(rule, "api.example.com", "/v1/items?page=2", "get", "https://api.example.com/v1/items?page=2")
    assert not matches(rule, "api.example.com", "/V1/items", "GET", "")
    assert not matches(rule, "api.example.com", "/v1/items", "POST", "")
    assert matches({"url_regex": r"items\?page=\d+$"}, "h", "/", "GET", "https://h/v1/items?page=12")
    assert not matches({"url_regex": r"^http:"}, "h", "/", "GET", "https://h/")


def test_add_assigns_ids_and_survives_reload(tmp_path):
    rules = rule_set(tmp_path)
    first = rules.add(respond(name="empty list"))
    second = rules.add({"action": "drop", "host": "ads.example"})
    assert (first["id"], second["id"]) == ("r1", "r2")
    assert first["hits"] == 0 and first["active"] is True and first["enabled"] is True
    reloaded = rule_set(tmp_path)
    assert [rule["id"] for rule in reloaded.list()] == ["r1", "r2"]
    assert by_id(reloaded, "r1")["name"] == "empty list"
    reloaded.delete("r2")
    assert rule_set(tmp_path).add({"action": "drop", "host": "x"})["id"] == "r3"


def test_update_merges_removes_and_drops_fields_of_old_action(tmp_path):
    rules = rule_set(tmp_path)
    rules.add(respond(status=500, delay_ms=100))
    updated = rules.update("r1", {"status": None, "enabled": False})
    assert "status" not in updated and updated["enabled"] is False and updated["delay_ms"] == 100
    switched = rules.update("r1", {"action": "drop"})
    assert switched["action"] == "drop" and "body" not in switched and switched["path"] == "/v1/items"
    with pytest.raises(RuleError, match="not found"):
        rules.update("r9", {"enabled": True})
    with pytest.raises(RuleError, match="at least one condition"):
        rules.update("r1", {"path": None})
    assert by_id(rules, "r1")["path"] == "/v1/items"
    assert by_id(rule_set(tmp_path), "r1")["action"] == "drop"


def test_update_does_not_recheck_existing_body_file(tmp_path):
    body = tmp_path / "mock.json"
    body.write_text("{}")
    rules = rule_set(tmp_path)
    rules.add({"action": "respond", "path": "/", "body_file": str(body)})
    os.remove(str(body))
    assert rules.update("r1", {"enabled": False})["enabled"] is False
    with pytest.raises(RuleError, match="does not exist"):
        rules.update("r1", {"body_file": str(body)})


def test_plan_applies_in_order_until_terminal(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "rewrite", "path": "/v1", "request_headers": {"X-Test": "1"}})
    rules.add({"action": "delay", "path": "/v1", "delay_ms": 10})
    rules.add(respond())
    rules.add({"action": "drop", "path": "/v1"})
    plan = rules.plan("api.example.com", "/v1/items", "GET", "https://api.example.com/v1/items")
    assert [rule["action"] for rule in plan] == ["rewrite", "delay", "respond"]
    assert [rule["hits"] for rule in rules.list()] == [1, 1, 1, 0]
    assert rules.plan("api.example.com", "/v2", "GET", "") == []


def test_times_builds_a_scenario_and_reset_hits_restarts_it(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "drop", "path": "/pay", "times": 2})
    rules.add({"action": "respond", "path": "/pay", "body": "ok"})
    actions = [rules.plan("h", "/pay", "POST", "")[-1]["action"] for _ in range(4)]
    assert actions == ["drop", "drop", "respond", "respond"]
    first = by_id(rules, "r1")
    assert first["remaining"] == 0 and first["active"] is False
    assert rules.counts() == {"total": 2, "enabled": 2, "active": 1}
    rules.update("r1", {}, reset_hits=True)
    assert by_id(rules, "r1")["remaining"] == 2
    assert rules.plan("h", "/pay", "POST", "")[-1]["action"] == "drop"


def test_disabled_rule_does_not_match(tmp_path):
    rules = rule_set(tmp_path)
    rules.add(respond(enabled=False))
    assert rules.plan("h", "/v1/items", "GET", "") == []


def test_plan_returns_copies(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "rewrite", "path": "/", "request_headers": {"X-A": "1"}})
    rules.plan("h", "/", "GET", "")[0]["request_headers"]["X-A"] = "changed"
    assert by_id(rules, "r1")["request_headers"] == {"X-A": "1"}


def test_clear_removes_all(tmp_path):
    rules = rule_set(tmp_path)
    rules.add(respond())
    rules.add(respond())
    assert rules.clear() == ["r1", "r2"]
    assert rule_set(tmp_path).list() == []


def test_rule_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("agentproxy.rules.MAX_RULES", 2)
    rules = rule_set(tmp_path)
    rules.add(respond())
    rules.add(respond())
    with pytest.raises(RuleError, match="at most 2 rules"):
        rules.add(respond())


def test_broken_json_is_set_aside(tmp_path):
    (tmp_path / FILE_NAME).write_text("{not json")
    rules = rule_set(tmp_path)
    assert rules.list() == [] and rules.read_error is None
    copies = broken_copies(tmp_path)
    assert len(copies) == 1 and open(copies[0]).read() == "{not json"
    assert rules.add(respond())["id"] == "r1"


def test_invalid_rule_is_skipped_and_original_copy_kept(tmp_path):
    content = {"next_id": 5, "rules": [
        dict(respond(), id="r1", created=1.0),
        {"id": "r2", "action": "respond"},
        "garbage",
        dict(respond(), id="r4"),
    ]}
    (tmp_path / FILE_NAME).write_text(json.dumps(content))
    rules = rule_set(tmp_path)
    assert [rule["id"] for rule in rules.list()] == ["r1", "r4"]
    assert json.loads(open(broken_copies(tmp_path)[0]).read()) == content
    assert rules.add(respond())["id"] == "r5"


def test_missing_body_file_does_not_drop_rule_on_load(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"next_id": 2, "rules": [
        {"id": "r1", "action": "respond", "path": "/", "body_file": "C:\\gone\\mock.json"}]}))
    assert [rule["id"] for rule in rule_set(tmp_path).list()] == ["r1"]


def test_next_id_never_reuses_stored_ids(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"next_id": 1, "rules": [dict(respond(), id="r7")]}))
    assert rule_set(tmp_path).add(respond())["id"] == "r8"


def test_unreadable_file_turns_changes_off(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1")]}))
    real_open = open

    def locked(path, *args, **kwargs):
        if str(path).endswith(FILE_NAME):
            raise PermissionError("locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked)
    rules = rule_set(tmp_path)
    monkeypatch.setattr("builtins.open", real_open)
    assert rules.read_error and rules.list() == []
    with pytest.raises(RuleError, match="could not be read"):
        rules.add(respond())
    assert json.loads((tmp_path / FILE_NAME).read_text())["rules"][0]["id"] == "r1"


def test_failed_save_keeps_rules_unchanged(tmp_path, monkeypatch):
    rules = rule_set(tmp_path)
    rules.add(respond())

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("agentproxy.rules.os.replace", fail)
    with pytest.raises(RuleError, match="disk full"):
        rules.add(respond())
    with pytest.raises(RuleError, match="disk full"):
        rules.delete("r1")
    assert [rule["id"] for rule in rules.list()] == ["r1"]
    monkeypatch.undo()
    assert rules.add(respond())["id"] == "r2"


def test_note_error_is_shown(tmp_path):
    rules = rule_set(tmp_path)
    rules.add(respond())
    rules.note_error("r1", "body_file unreadable")
    assert by_id(rules, "r1")["last_error"] == "body_file unreadable"


@pytest.mark.parametrize("raw, message", [
    ({"action": "respond", "method": "connect"}, "CONNECT never matches"),
    ({"action": "rewrite", "path": "/", "request_headers": {"Connection": "close"}}, "connection header"),
    ({"action": "respond", "path": "/", "headers": {"Upgrade": "h2c"}}, "connection header"),
    ({"action": "respond", "path": "/", "status": 204, "body": "x"}, "status 204 has no body"),
    ({"action": "respond", "path": "/", "status": 101}, "status must be between 200 and 599"),
    ({"action": "rewrite", "path": "/", "status": 100}, "status must be between 200 and 599"),
    ({"action": "respond", "path": "/", "body": "\ud800"}, "valid Unicode"),
    ({"action": "respond", "path": "/", "headers": {"X-A": "\udc80"}}, "valid Unicode"),
    ({"action": "respond", "url_regex": "a{4294967296}"}, "not a valid regular expression"),
    ({"action": "cut", "path": "/"}, "exactly one of after_bytes, after_percent"),
    ({"action": "cut", "path": "/", "after_bytes": 10, "after_percent": 50}, "exactly one of"),
    ({"action": "cut", "path": "/", "after_bytes": 0}, "after_bytes must be between 1 and"),
    ({"action": "cut", "path": "/", "after_percent": 100}, "after_percent must be between 1 and 99"),
    ({"action": "delay", "path": "/", "delay_ms": 5, "after_bytes": 10}, "after_bytes is used only with action cut"),
])
def test_normalize_rejects_rules_that_cannot_work(raw, message):
    with pytest.raises(RuleError, match=message):
        normalize(raw)


def test_cut_rule_switches_between_bytes_and_percent(tmp_path):
    rules = rule_set(tmp_path)
    rule = rules.add({"action": "cut", "path": "/video", "after_bytes": "1000", "download_kbps": 500, "times": 1})
    assert (rule["after_bytes"], rule["download_kbps"]) == (1000, 500)
    rule = rules.update("r1", {"after_percent": 40})
    assert rule["after_percent"] == 40 and "after_bytes" not in rule
    rule = rules.update("r1", {"after_bytes": 7})
    assert rule["after_bytes"] == 7 and "after_percent" not in rule
    for _ in range(2):
        assert rules.plan("cdn.example.com", "/video", "GET", "https://cdn.example.com/video")[0]["id"] == "r1"
    assert by_id(rules, "r1")["hits"] == 0
    assert rules.take("r1") and not rules.take("r1")
    assert rules.plan("cdn.example.com", "/video", "GET", "https://cdn.example.com/video") == []
    assert by_id(rules, "r1")["remaining"] == 0


def test_not_utf8_file_is_set_aside_and_engine_starts(tmp_path):
    (tmp_path / FILE_NAME).write_bytes('{"rules": [{"id": "r1", "name": "Пример"}]}'.encode("cp1251"))
    rules = rule_set(tmp_path)
    assert rules.list() == [] and rules.read_error is None
    assert len(broken_copies(tmp_path)) == 1


def test_deeply_nested_file_is_set_aside(tmp_path):
    (tmp_path / FILE_NAME).write_text("[" * 100000 + "]" * 100000)
    assert rule_set(tmp_path).list() == []
    assert len(broken_copies(tmp_path)) == 1


def test_duplicate_ids_keep_the_first(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), dict(respond(), id="r1")]}))
    assert [rule["id"] for rule in rule_set(tmp_path).list()] == ["r1"]
    assert len(broken_copies(tmp_path)) == 1


def test_changing_action_drops_shared_fields_of_the_old_one(tmp_path):
    rules = rule_set(tmp_path)
    rules.add(respond(status=503))
    changed = rules.update("r1", {"action": "rewrite", "replace": [{"find": "a", "with": "b"}]})
    assert "status" not in changed and "body" not in changed
    with pytest.raises(RuleError, match="action must be one of"):
        rules.update("r1", {"action": ["drop"]})


def test_may_answer_considers_terminal_active_rules_and_host(tmp_path):
    rules = rule_set(tmp_path)
    assert not rules.may_answer("api.example.com")
    rules.add({"action": "rewrite", "host": "api.example.com", "status": 500})
    assert not rules.may_answer("api.example.com")
    rules.add({"action": "drop", "host": "Example.com", "times": 1})
    assert rules.may_answer("api.example.com") and not rules.may_answer("other.test")
    rules.plan("api.example.com", "/", "GET", "")
    assert not rules.may_answer("api.example.com")
    rules.add({"action": "respond", "path": "/x", "body": "y"})
    assert not rules.may_answer("other.test")
    rules.add({"action": "respond", "host": "other.test", "body": "y"})
    assert rules.may_answer("other.test")
    rules.update("r4", {"enabled": False})
    assert not rules.may_answer("other.test")


def test_regex_sees_only_the_start_of_very_long_urls():
    assert not matches({"url_regex": "tail$"}, "h", "/", "GET", "x" * 9000 + "tail")


def test_rules_that_break_python_are_skipped_on_load(tmp_path):
    content = {"rules": [dict(respond(), id="r1"), {"id": "r2", "action": "drop", "url_regex": "a{4294967296}"},
                         dict(respond(), id="r" + "9" * 5000)]}
    (tmp_path / FILE_NAME).write_text(json.dumps(content))
    rules = rule_set(tmp_path)
    assert [rule["id"] for rule in rules.list()] == ["r1", "r" + "9" * 5000]
    assert "original is kept" in rules.counts()["problem"]
    assert rules.add(respond())["id"] == "r2"


def test_invalid_rules_are_set_aside_once_and_file_is_cleaned(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))
    rule_set(tmp_path)
    rule_set(tmp_path)
    assert len(broken_copies(tmp_path)) == 1
    assert [rule["id"] for rule in json.loads((tmp_path / FILE_NAME).read_text())["rules"]] == ["r1"]


def test_read_problem_is_reported_in_counts(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text("{}")

    def fail(*args):
        raise OSError("denied")

    monkeypatch.setattr("agentproxy.rules.os.replace", fail)
    rules = rule_set(tmp_path)
    assert "could not be read" in rules.counts()["problem"]
    with pytest.raises(RuleError, match="could not be read"):
        rules.add(respond())


def test_last_error_is_cleared_by_update_and_success(tmp_path):
    rules = rule_set(tmp_path)
    rules.add(respond())
    rules.note_error("r1", "boom")
    rules.update("r1", {"name": "fixed"})
    assert "last_error" not in by_id(rules, "r1")
    rules.note_error("r1", "boom")
    rules.clear_error("r1")
    assert "last_error" not in by_id(rules, "r1")


def test_surrogate_in_saved_text_is_a_rule_error_not_a_crash(tmp_path):
    rules = rule_set(tmp_path)
    with pytest.raises(RuleError, match="valid Unicode"):
        rules.add({"action": "respond", "host": "a", "body": "\ud800"})
    assert not (tmp_path / (FILE_NAME + ".tmp")).exists()


def test_fields_from_a_newer_version_are_ignored_not_fatal(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1", future_field=1)]}))
    rules = rule_set(tmp_path)
    assert [rule["id"] for rule in rules.list()] == ["r1"]
    assert "future_field" in rules.counts()["problem"] and broken_copies(tmp_path) == []


def test_failed_copy_of_invalid_rules_keeps_rules_changeable(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("agentproxy.rules.shutil.copyfile", fail)
    rules = rule_set(tmp_path)
    assert "valid rules work" in rules.counts()["problem"]
    assert rules.update("r1", {"enabled": False})["enabled"] is False


def test_unsaved_cleanup_does_not_multiply_copies(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))
    real_replace = os.replace

    def refuse(source, target):
        if str(target).endswith(FILE_NAME):
            raise PermissionError("read-only")
        return real_replace(source, target)

    stamps = iter(["20261017-000001", "20261017-000002"])
    monkeypatch.setattr("agentproxy.rules.os.replace", refuse)
    monkeypatch.setattr("agentproxy.rules.time.strftime", lambda pattern: next(stamps))
    first = rule_set(tmp_path)
    rule_set(tmp_path)
    assert len(broken_copies(tmp_path)) == 1
    assert "could not be cleaned" in first.counts()["problem"]
    assert not (tmp_path / (FILE_NAME + ".tmp")).exists()


def test_bad_created_and_surrogate_id_are_handled(tmp_path):
    (tmp_path / FILE_NAME).write_text(
        '{"rules": [{"id": "r1", "action": "drop", "path": "/", "created": NaN},'
        ' {"id": "r2\\ud800", "action": "drop", "path": "/"}]}')
    rules = rule_set(tmp_path)
    assert [rule["id"] for rule in rules.list()] == ["r1"]
    created = rules.list()[0]["created"]
    assert created == created and created < float("inf")


def test_created_far_out_of_range_does_not_stop_the_engine(tmp_path):
    (tmp_path / FILE_NAME).write_text('{"rules": [{"id": "r1", "action": "drop", "path": "/", "created": 1'
                                      + "0" * 400 + '}]}')
    rules = rule_set(tmp_path)
    assert [rule["id"] for rule in rules.list()] == ["r1"]


def test_rule_that_is_not_an_object_is_named_by_position(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), "oops"]}))
    problem = rule_set(tmp_path).counts()["problem"]
    assert "rule #2: must be an object" in problem and "#2: rule #2" not in problem


def test_problem_text_follows_the_change(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1", future_field=1)]}))
    rules = rule_set(tmp_path)
    assert "dropped on the next change" in rules.counts()["problem"]
    rules.update("r1", {"enabled": False})
    assert "were dropped" in rules.counts()["problem"]


def test_unknown_fields_next_to_invalid_rule_are_named(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1", future_field=1),
                                                            {"id": "r2", "action": "throttle", "path": "/"}]}))
    problem = rule_set(tmp_path).counts()["problem"]
    assert "future_field" in problem and "original is kept" in problem and "had invalid rules" in problem


def test_unreadable_old_copy_does_not_block_setting_aside(tmp_path):
    (tmp_path / (FILE_NAME + ".20260101-000000.broken")).mkdir()
    (tmp_path / FILE_NAME).write_text("")
    rules = rule_set(tmp_path)
    assert rules.read_error is None and rules.add(respond())["id"] == "r1"


def test_problem_after_change_for_set_aside_file(tmp_path):
    (tmp_path / FILE_NAME).write_text("{broken")
    rules = rule_set(tmp_path)
    rules.add(respond())
    assert "is kept as" in rules.counts()["problem"]


def test_problem_after_change_for_cleaned_and_lost_rules(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))
    rules = rule_set(tmp_path)
    rules.add(respond())
    problem = rules.counts()["problem"]
    assert problem.startswith("at engine start") and "rewritten since" in problem
    other = tmp_path / "other"
    other.mkdir()
    (other / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("agentproxy.rules.shutil.copyfile", fail)
    lost = RuleSet(str(other))
    lost.add(respond())
    assert "were lost" in lost.counts()["problem"]


def test_header_name_with_line_break_is_rejected():
    with pytest.raises(RuleError, match="invalid header name"):
        normalize({"action": "respond", "path": "/", "headers": {"X-A\n": "v"}})


def test_unreadable_file_names_the_reason_on_update(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1")]}))
    real_open = open

    def locked(path, *args, **kwargs):
        if str(path).endswith(FILE_NAME):
            raise PermissionError("locked")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked)
    rules = rule_set(tmp_path)
    monkeypatch.setattr("builtins.open", real_open)
    for action in (lambda: rules.update("r1", {"enabled": False}), lambda: rules.delete("r1")):
        with pytest.raises(RuleError, match="could not be read"):
            action()


def test_copies_in_the_same_second_do_not_overwrite_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr("agentproxy.rules.time.strftime", lambda pattern: "20261017-000000")
    (tmp_path / FILE_NAME).write_text("{bad1")
    rule_set(tmp_path)
    (tmp_path / FILE_NAME).write_text("{bad2 different")
    rule_set(tmp_path)
    contents = sorted(open(path).read() for path in broken_copies(tmp_path))
    assert contents == ["{bad1", "{bad2 different"]


def test_header_values_with_control_characters_are_rejected():
    with pytest.raises(RuleError, match="single-line string"):
        normalize({"action": "respond", "path": "/", "headers": {"X-A": "a\x00b"}})
    assert normalize({"action": "respond", "path": "/", "headers": {"X-A": "a\tb"}})["headers"] == {"X-A": "a\tb"}


def test_problem_after_change_is_in_the_past(tmp_path):
    (tmp_path / FILE_NAME).write_text("{broken")
    rules = rule_set(tmp_path)
    rules.add(respond())
    problem = rules.counts()["problem"]
    assert problem.startswith("at engine start") and "rewritten since" in problem


def test_cleanup_failure_text_goes_away_after_a_change(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))
    real_replace = os.replace

    def refuse(source, target):
        if str(target).endswith(FILE_NAME):
            raise PermissionError("read-only")
        return real_replace(source, target)

    monkeypatch.setattr("agentproxy.rules.os.replace", refuse)
    rules = rule_set(tmp_path)
    assert "could not be cleaned" in rules.counts()["problem"]
    monkeypatch.setattr("agentproxy.rules.os.replace", real_replace)
    rules.update("r1", {"enabled": False})
    assert "could not be cleaned" not in rules.counts()["problem"]


def test_unknown_fields_lost_with_failed_copy_are_mentioned(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1", future_field=1), {"id": "r2"}]}))

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("agentproxy.rules.shutil.copyfile", fail)
    rules = rule_set(tmp_path)
    rules.update("r1", {"enabled": False})
    assert "dropped as well" in rules.counts()["problem"]


def test_unknown_top_level_keys_are_reported(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1")], "settings": {}}))
    assert "file field settings" in rule_set(tmp_path).counts()["problem"]


def test_content_type_with_line_break_is_rejected():
    with pytest.raises(RuleError, match="content_type must be a single-line string"):
        normalize({"action": "respond", "path": "/", "content_type": "text/html\r\nSet-Cookie: a=b"})


def test_lost_rules_are_still_named_after_a_change(tmp_path, monkeypatch):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [dict(respond(), id="r1"), {"id": "r2"}]}))

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("agentproxy.rules.shutil.copyfile", fail)
    rules = rule_set(tmp_path)
    rules.add(respond())
    assert "rule #2" in rules.counts()["problem"]


def test_update_keeps_the_count_unless_asked(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "drop", "path": "/v1", "times": 2})
    rules.plan("host", "/v1", "GET", "http://host/v1")
    rules.update("r1", {"name": "x"})
    assert by_id(rules, "r1")["hits"] == 1 and by_id(rules, "r1")["remaining"] == 1
    rules.update("r1", {"name": "y"}, reset_hits=True)
    assert by_id(rules, "r1")["hits"] == 0 and by_id(rules, "r1")["remaining"] == 2


def test_file_with_byte_order_mark_is_read(tmp_path):
    (tmp_path / FILE_NAME).write_bytes(b"\xef\xbb\xbf" + json.dumps({"rules": [dict(respond(), id="r1")]}).encode())
    assert [rule["id"] for rule in rule_set(tmp_path).list()] == ["r1"] and broken_copies(tmp_path) == []


def test_creation_time_survives_a_restart(tmp_path):
    first = rule_set(tmp_path).add(respond())
    assert by_id(rule_set(tmp_path), "r1")["created"] == first["created"]


def test_method_is_measured_after_upper_case():
    with pytest.raises(RuleError, match="method is longer"):
        normalize(respond(method="\ufb03" * 7))


def test_host_condition_is_checked():
    assert not matches({"host": "api.example.com"}, "cdn.example.com", "/v1", "GET", "https://cdn.example.com/v1")


def test_invalid_rules_stay_in_the_file_when_their_copy_fails(tmp_path, monkeypatch):
    path = tmp_path / FILE_NAME
    path.write_text(json.dumps({"rules": [{"id": "r1", "action": "drop", "path": "/a"}, {"id": "r2"}]}))
    original = path.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("agentproxy.rules.shutil.copyfile", fail)
    rule_set(tmp_path)
    assert path.read_bytes() == original


def test_too_large_body_file_is_refused_when_added(tmp_path, monkeypatch):
    mock = tmp_path / "big.json"
    mock.write_text("[" + "1," * 20 + "1]")
    monkeypatch.setattr("agentproxy.rules.MAX_FILE_BYTES", 10)
    with pytest.raises(RuleError, match="larger than"):
        rule_set(tmp_path).add({"action": "respond", "path": "/a", "body_file": str(mock)})


@pytest.mark.parametrize("wide, narrow, covered", [
    ({"path": "/pay"}, {"path": "/pay/now"}, True),
    ({"path": "/pay/now"}, {"path": "/pay"}, False),
    ({"host": "Example.com"}, {"host": "api.example.com", "path": "/"}, True),
    ({"host": "api.example.com"}, {"path": "/"}, False),
    ({"method": "POST"}, {"method": "POST", "path": "/"}, True),
    ({"method": "POST"}, {"method": "GET"}, False),
    ({"url_regex": "a"}, {"url_regex": "b"}, False),
    ({"url_regex": "a"}, {"url_regex": "a", "path": "/"}, True),
])
def test_earlier_terminal_rule_shadows_only_what_it_covers(tmp_path, wide, narrow, covered):
    rules = rule_set(tmp_path)
    rules.add(dict({"action": "drop"}, **wide))
    rules.add(dict({"action": "delay", "delay_ms": 10}, **narrow))
    assert (rules.shadowed_by("r2") is not None) is covered
    assert rules.shadowed_by("r1") is None


def test_applied_rule_records_when_it_last_applied(tmp_path, monkeypatch):
    rules = rule_set(tmp_path)
    rules.add({"action": "drop", "path": "/path"})
    assert rules.list()[0]["last_applied"] is None
    monkeypatch.setattr("agentproxy.rules.time.time", lambda: 1234.5)
    rules.plan("example.com", "/path", "GET", "http://example.com/path")
    assert rules.list()[0]["last_applied"] == 1234.5


@pytest.mark.parametrize("word, expected", [("yes", True), ("1", True), ("No", False), ("0", False)])
def test_enabled_accepts_word_flags(word, expected):
    assert normalize({"action": "drop", "path": "/", "enabled": word})["enabled"] is expected


def test_rule_with_times_does_not_shadow_what_follows(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "drop", "path": "/pay", "times": 2})
    rules.add({"action": "respond", "path": "/pay", "body": "{}"})
    assert rules.shadowed_by("r2") is None and rules.shadowing("r1") == []


@pytest.mark.parametrize("host", ["localhost:3000", "10.0.2.2:8080", "[::1]:3000"])
def test_host_with_port_is_refused(host):
    with pytest.raises(RuleError, match="without the port"):
        normalize({"action": "drop", "host": host})


@pytest.mark.parametrize("host", ["::1", "fe80::1", "example.com"])
def test_host_without_port_is_kept(host):
    assert normalize({"action": "drop", "host": host})["host"] == host


@pytest.mark.parametrize("field, value", [("host", "*.example.com"), ("path", "/v1/*")])
def test_wildcards_are_refused(field, value):
    with pytest.raises(RuleError, match="not a wildcard"):
        normalize({"action": "drop", field: value})


def test_bracketed_ipv6_host_is_unwrapped():
    assert normalize({"action": "drop", "host": "[::1]"})["host"] == "::1"


@pytest.mark.parametrize("host", ["[]", "[:8080]"])
def test_bracketed_host_is_checked_after_unwrapping(host):
    with pytest.raises(RuleError):
        normalize({"action": "drop", "host": host})


def test_rule_with_unknown_fields_is_loaded_turned_off(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [
        {"id": "r1", "action": "drop", "host": "api.example.com", "query": "debug=1"},
        {"id": "r2", "action": "drop", "path": "/b"}]}))
    rules = rule_set(tmp_path)
    assert by_id(rules, "r1")["enabled"] is False and by_id(rules, "r2")["enabled"] is True
    assert "turned off" in rules.counts()["problem"] and "r1" in rules.counts()["problem"]


def test_disabled_rule_is_not_reported_as_shadowed(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "respond", "path": "/pay", "body": "{}"})
    rules.add({"action": "drop", "path": "/pay/now", "enabled": False})
    assert rules.shadowed_by("r2") is None


def test_lazy_connect_ignores_case_of_the_connect_host(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "respond", "host": "example.com", "body": "{}"})
    assert rules.may_answer("API.Example.COM")


@pytest.mark.parametrize("raw", [{"action": "drop", "path": "/b", "query": "x"},
                                 {"id": "r1", "action": "drop", "path": "/b", "query": "x"}])
def test_skipped_rule_is_not_listed_as_turned_off(tmp_path, raw):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [{"id": "r1", "action": "drop", "path": "/a"}, raw]}))
    rules = rule_set(tmp_path)
    assert [(rule["id"], rule["enabled"]) for rule in rules.list()] == [("r1", True)]
    assert "turned off" not in rules.counts()["problem"]


def test_rule_already_off_with_unknown_fields_is_not_listed_as_turned_off(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [
        {"id": "r1", "action": "drop", "path": "/a", "query": "x", "enabled": False}]}))
    problem = rule_set(tmp_path).counts()["problem"]
    assert "query" in problem and "turned off" not in problem


def test_view_fields_in_the_file_do_not_turn_a_rule_off(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [
        {"id": "r1", "action": "drop", "path": "/a", "hits": 3, "active": True, "last_applied": 1.0}]}))
    rules = rule_set(tmp_path)
    assert by_id(rules, "r1")["enabled"] is True and rules.counts().get("problem") is None


def test_turned_off_note_is_past_tense_after_a_change(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [{"id": "r1", "action": "drop", "path": "/a", "query": "x"}]}))
    rules = rule_set(tmp_path)
    rules.update("r1", {"enabled": True})
    problem = rules.counts()["problem"]
    assert "were turned off at start: r1" in problem and "check and enable" not in problem


def test_star_in_the_query_is_kept():
    assert normalize({"action": "drop", "path": "/rest/v1/items?select=*"})["path"] == "/rest/v1/items?select=*"


@pytest.mark.parametrize("host", ["https://api.example.com", "api.example.com/v1", "http://localhost:3000/"])
def test_host_given_as_url_is_refused(host):
    with pytest.raises(RuleError, match="name alone"):
        normalize({"action": "drop", "host": host})


def test_disabled_later_rules_are_not_listed_as_covered(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "respond", "host": "a.com", "body": "{}", "enabled": False})
    rules.add({"action": "respond", "host": "a.com", "path": "/x", "body": "{}", "enabled": False})
    rules.update("r1", {"enabled": True})
    assert rules.shadowing("r1") == []


def test_proxied_mark_cannot_be_removed_by_a_rule():
    with pytest.raises(RuleError, match="computed by AgentProxy"):
        normalize({"action": "rewrite", "host": "x", "request_headers": {"X-AgentProxy-Proxied": None}})


def test_relative_body_file_in_the_file_is_refused(tmp_path):
    (tmp_path / FILE_NAME).write_text(json.dumps({"rules": [
        {"id": "r1", "action": "respond", "host": "a", "body_file": "rel.txt"}]}))
    rules = rule_set(tmp_path)
    assert rules.list() == [] and "absolute path" in rules.counts()["problem"]


def test_take_counts_only_a_live_cut_rule_within_times(tmp_path):
    rules = rule_set(tmp_path)
    rules.add({"action": "cut", "path": "/video", "after_bytes": 10, "times": 2})
    rules.add({"action": "cut", "path": "/video", "after_bytes": 10})
    rules.add({"action": "delay", "path": "/video", "delay_ms": 5})
    rules.add({"action": "cut", "path": "/other", "after_bytes": 10})
    assert [rules.take("r1"), rules.take("r1"), rules.take("r1")] == [True, True, False]
    assert all(rules.take("r2") for _ in range(5)) and by_id(rules, "r2")["hits"] == 5
    assert not rules.take("r3") and by_id(rules, "r3")["hits"] == 0
    rules.update("r2", {"enabled": False})
    assert not rules.take("r2") and by_id(rules, "r2")["hits"] == 5
    rules.delete("r4")
    assert not rules.take("r4") and not rules.take("r99")
