import os
import socket

import pytest

from agentproxy.settings import Settings
from agentproxy.store import FlowRecord, FlowStore


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("AGENTPROXY_DATA_DIR", str(directory))
    return str(directory)


@pytest.fixture
def store(tmp_path):
    flow_store = FlowStore(str(tmp_path / "flows.db"), max_flows=1000)
    yield flow_store
    flow_store.close()


def make_record(index=1, **overrides):
    values = dict(
        id="flow-{0}".format(index), started=1000.0 + index, method="GET", scheme="https",
        host="api.example.com", port=443, path="/v1/items?page={0}".format(index),
        url="https://api.example.com/v1/items?page={0}".format(index),
        request_headers=[["accept", "application/json"]], status=200, reason="OK",
        response_headers=[["content-type", "application/json"]], response_body=b'{"ok":true}',
        response_body_size=11, response_content_type="application/json", duration_ms=12,
    )
    values.update(overrides)
    return FlowRecord(**values)


@pytest.fixture
def settings():
    return Settings(api_port=18891, proxy_port=18890)
