import json

from agentproxy import settings


def test_missing_file_gives_defaults(tmp_path):
    current, problems = settings.load(str(tmp_path))
    assert current == settings.Settings()
    assert problems == []


def test_known_values_are_applied(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"proxy_port": 9000, "verify_upstream": False}))
    current, problems = settings.load(str(tmp_path))
    assert current.proxy_port == 9000
    assert current.verify_upstream is False
    assert problems == []


def test_unknown_keys_are_ignored_with_note(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"future_key": 1, "api_port": 9100}))
    current, problems = settings.load(str(tmp_path))
    assert current.api_port == 9100
    assert any("future_key" in problem for problem in problems)


def test_invalid_values_fall_back_to_defaults(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"proxy_port": 80, "max_flows": "many", "verify_upstream": 1}))
    current, problems = settings.load(str(tmp_path))
    assert current.proxy_port == settings.Settings().proxy_port
    assert current.max_flows == settings.Settings().max_flows
    assert current.verify_upstream is True
    assert len(problems) == 3


def test_same_ports_are_rejected():
    current, problems = settings.parse({"proxy_port": 9000, "api_port": 9000})
    assert current.proxy_port != current.api_port
    assert problems


def test_broken_json_gives_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{broken")
    current, problems = settings.load(str(tmp_path))
    assert current == settings.Settings()
    assert "unreadable" in problems[0]


def test_bom_is_accepted(tmp_path):
    (tmp_path / "settings.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"proxy_port": 9001}).encode())
    current, _ = settings.load(str(tmp_path))
    assert current.proxy_port == 9001


def test_proxy_passes_websocket_through_as_raw_tcp(tmp_path):
    from agentproxy.engine import proxy_options
    from agentproxy.settings import Settings
    opts = proxy_options(Settings(), str(tmp_path))
    assert opts.websocket is False
    assert opts.rawtcp is True
    assert opts.listen_host == "127.0.0.1"
