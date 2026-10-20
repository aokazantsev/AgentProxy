import os
import re

from agentproxy import rules, settings
from agentproxy.claude import integration
from agentproxy.service import RULE_PROPERTIES, RULES_REFERENCE, TOOLS

ENGINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agentproxy")
CLAUDE_DIR = os.path.join(ENGINE_DIR, "claude")
SKILL_MAX_CHARS = 8500
RULES_MAX_CHARS = 10000
COPY_WINDOW_WORDS = 6
ANSWER_FIELDS = {
    "rewriter.py": ("skipped", "replaced", "not_found", "error"),
    "service.py": ("history_matches", "hint", "counts"),
    "rules.py": ("problem",),
    "bodies.py": ("truncated",),
    "settings.py": ("max_body_bytes",),
    "store.py": ("rules", "url"),
}
NOT_NAME = re.compile(r"\.(log|json|exe|com|net)$")
NOT_FIELDS = {"charset", "null", "agentproxy", "network_security_config"}


def read(name):
    with open(os.path.join(CLAUDE_DIR, name), encoding="utf-8") as stream:
        return stream.read()


def documents():
    return {name: read(name) for name in integration.SKILL_FILES}


def schema_names():
    names = {tool["name"] for tool in TOOLS} | set(RULE_PROPERTIES["action"]["enum"])
    for tool in TOOLS:
        names |= set(tool["inputSchema"]["properties"])
    return names


def named_tokens(text):
    for token in re.findall(r"`([^`\n]+)`", text):
        key = re.match(r"([a-z][a-z0-9_]*): ", token)
        if key:
            yield key.group(1)
        elif re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", token) and not NOT_NAME.search(token):
            yield from token.split(".")


def test_skill_mentions_every_tool():
    skill = read("SKILL.md")
    for tool in TOOLS:
        assert "`{0}`".format(tool["name"]) in skill, tool["name"]


def test_names_in_texts_exist_in_schemas_or_answers():
    known = schema_names() | NOT_FIELDS | set(rules.VIEW_FIELDS)
    for fields in ANSWER_FIELDS.values():
        known |= set(fields)
    for name, text in documents().items():
        for token in named_tokens(text):
            assert token in known, (name, token)


def test_answer_fields_named_in_texts_exist_in_their_code():
    for source, fields in ANSWER_FIELDS.items():
        with open(os.path.join(ENGINE_DIR, source), encoding="utf-8") as stream:
            code = stream.read()
        for field in fields:
            assert re.search(r'"{0}"\s*:|\["{0}"\][^=\n]*=[^=]'.format(field), code), (source, field)


def test_schema_fields_match_normalize():
    assert set(RULE_PROPERTIES) == set(rules.ALL_FIELDS)


def test_skill_points_only_to_installed_files():
    for name, text in documents().items():
        targets = re.findall(r"\{\{SKILL_DIR\}\}[\\/]([\w.-]*\w)", text)
        assert set(targets) <= set(integration.SKILL_FILES), (name, targets)
    assert re.findall(r"\{\{SKILL_DIR\}\}[\\/]([\w.-]*\w)", read("SKILL.md"))


def test_reference_path_is_where_the_skill_is_installed():
    env = integration.Environment(claude_dir="~/.claude", claude_json="", data_dir="", install_dir="", python_exe="",
                                  proxy_port=0, api_port=0)
    reference = os.path.join(env.skill_dir, integration.SKILL_FILES[1]).replace(os.sep, "/")
    assert reference == "~/.claude/skills/agentproxy/rules.md"
    assert "(" + reference + "," in RULES_REFERENCE


def test_skill_name_matches_installed_folder():
    assert "\nname: {0}\n".format(integration.SKILL_NAME) in read("SKILL.md")


def test_skill_texts_stay_short():
    assert len(read("SKILL.md")) <= SKILL_MAX_CHARS
    assert len(read("rules.md")) <= RULES_MAX_CHARS


def words(text):
    return re.findall(r"[a-z0-9_]+", text.lower())


def test_schema_texts_are_not_pasted_into_skill_documents():
    windows = set()
    for tool in TOOLS:
        texts = [tool["description"]] + [prop.get("description", "") for prop in tool["inputSchema"]["properties"].values()]
        for text in texts:
            found = words(text)
            windows.update(" ".join(found[i:i + COPY_WINDOW_WORDS]) for i in range(len(found) - COPY_WINDOW_WORDS + 1))
    for name, text in documents().items():
        flat = " ".join(words(text))
        for window in windows:
            assert window not in flat, (name, window)


def test_templates_take_ports_from_placeholders():
    defaults = settings.Settings()
    for name, text in documents().items():
        for port in (defaults.proxy_port, defaults.api_port):
            assert str(port) not in text, (name, port)


def test_tool_descriptions_point_to_installed_reference():
    assert "skills/{0}/{1}".format(integration.SKILL_NAME, integration.SKILL_FILES[1]) in RULES_REFERENCE
    assert set(re.findall(r"skills/([\w-]+)", RULES_REFERENCE)) == {integration.SKILL_NAME}
    descriptions = {tool["name"]: tool["description"] for tool in TOOLS}
    for name in ("add_rule", "update_rule"):
        assert RULES_REFERENCE in descriptions[name], name
