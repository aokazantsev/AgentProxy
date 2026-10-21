import json

import logging

from agentproxy import api, logs, settings


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


def test_proxy_reads_websocket_messages_and_passes_other_tcp_raw(tmp_path):
    from agentproxy.engine import proxy_options
    from agentproxy.settings import Settings
    opts = proxy_options(Settings(), str(tmp_path))
    assert opts.websocket is True
    assert opts.rawtcp is True
    assert opts.listen_host == "127.0.0.1"
    assert opts.ssl_insecure is False
    assert proxy_options(Settings(verify_upstream=False), str(tmp_path)).ssl_insecure is True


class FakeHandler:
    def __init__(self, status):
        self.status = status
        self.request = type("Request", (), {"request_time": lambda self: 0.01, "method": "GET",
                                            "path": "/api/flows/wait", "uri": "/api/flows/wait?path=/x?code=secret",
                                            "remote_ip": "127.0.0.1"})()

    def get_status(self):
        return self.status


def test_stopping_answer_is_not_logged_as_error(caplog):
    caplog.set_level(logging.INFO, logger="tornado.access")
    for status in (200, 404, 500, 503):
        api.log_request(FakeHandler(status))
    levels = {record.message.split()[0]: record.levelname for record in caplog.records}
    assert levels == {"200": "INFO", "404": "WARNING", "500": "ERROR", "503": "INFO"}
    assert all("secret" not in record.getMessage() and "GET /api/flows/wait (127.0.0.1)" in record.getMessage()
               for record in caplog.records)


def test_log_keeps_lines_with_raw_bytes(tmp_path):
    logs.setup(str(tmp_path))
    try:
        logging.getLogger("agentproxy.test").warning("encoding %s", b"gz\xffip".decode("utf-8", "surrogateescape"))
    finally:
        for handler in list(logging.getLogger().handlers):
            handler.close()
            logging.getLogger().removeHandler(handler)
    assert "encoding gz\\udcffip" in open(str(tmp_path / "engine.log"), encoding="utf-8").read()
