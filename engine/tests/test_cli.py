import json
import os
import subprocess
import sys

import pytest

from agentproxy import VERSION
from agentproxy.claude import integration

from conftest import free_port, git_bash


@pytest.fixture
def cli_env(tmp_path):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proxy_port = free_port()
    api_port = free_port()
    while api_port == proxy_port:
        api_port = free_port()
    (data_dir / "settings.json").write_text(json.dumps({"proxy_port": proxy_port, "api_port": api_port}))
    env = dict(os.environ)
    env.update({"CLAUDE_CONFIG_DIR": str(claude_dir), "AGENTPROXY_DATA_DIR": str(data_dir),
                "PATH": os.path.dirname(sys.executable)})
    return {"env": env, "claude_dir": str(claude_dir), "data_dir": str(data_dir)}


def run_cli(cli_env, *args, stdin=None):
    completed = subprocess.run([sys.executable, "-I", "-m", "agentproxy.cli"] + list(args), env=cli_env["env"],
                               input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=120)
    return completed.returncode, completed.stdout, completed.stderr


def test_claude_install_status_uninstall_round_trip(cli_env):
    code, out, _ = run_cli(cli_env, "claude", "install", "--no-cli")
    assert code == 0
    assert json.loads(out)["version"] == VERSION
    code, out, _ = run_cli(cli_env, "claude", "status")
    status = json.loads(out)
    assert code == 0
    assert status["installed"] and status["mcp"] and status["skill"] and status["hooks"]
    code, out, _ = run_cli(cli_env, "claude", "uninstall", "--no-cli")
    assert code == 0
    assert json.loads(out) == {"mcp": True, "skill": True, "hooks": True}
    _, out, _ = run_cli(cli_env, "claude", "status")
    assert json.loads(out)["installed"] is False


def test_installed_session_start_hook_runs_and_answers(cli_env):
    run_cli(cli_env, "claude", "install", "--no-cli")
    with open(os.path.join(cli_env["claude_dir"], "settings.json"), encoding="utf-8") as stream:
        settings = json.load(stream)
    hook = [h for group in settings["hooks"]["SessionStart"] for h in group["hooks"] if integration.is_ours(h)][0]
    assert "args" not in hook
    bash = git_bash()
    if bash is None:
        pytest.skip("Git Bash is not installed")
    completed = subprocess.run([bash, "-c", hook["command"]], env=cli_env["env"], input='{"session_id": "cli-test"}',
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "AgentProxy" in output["hookSpecificOutput"]["additionalContext"]


def test_hook_survives_garbage_stdin(cli_env):
    code, out, _ = run_cli(cli_env, "hook", "session-start", stdin="not json")
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_status_without_engine_fails_fast(cli_env):
    code, out, _ = run_cli(cli_env, "status")
    assert code == 1
    assert json.loads(out)["running"] is False


def test_claude_install_reports_broken_settings(cli_env):
    with open(os.path.join(cli_env["claude_dir"], "settings.json"), "w", encoding="utf-8") as stream:
        stream.write("{broken")
    code, out, _ = run_cli(cli_env, "claude", "install", "--no-cli")
    assert code == 1
    assert "not valid JSON" in json.loads(out)["error"]
