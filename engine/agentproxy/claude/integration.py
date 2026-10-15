import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .. import VERSION, changelog, paths
from .. import settings as settings_module
from . import jsonfile

SERVER_NAME = "agentproxy"
SKILL_NAME = "agentproxy"
STATE_FILE = "claude-integration.json"
CLI_MODULE = "agentproxy.cli"
HOOK_PATH = "/claude/hooks/user-prompt-submit"
WHATS_NEW_DAYS = 3
SKILL_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SKILL.md")


@dataclass(frozen=True)
class Environment:
    claude_dir: str
    claude_json: str
    data_dir: str
    install_dir: str
    python_exe: str
    proxy_port: int
    api_port: int
    claude_cli: str = None

    @property
    def settings_json(self):
        return os.path.join(self.claude_dir, "settings.json")

    @property
    def skill_file(self):
        return os.path.join(self.claude_dir, "skills", SKILL_NAME, "SKILL.md")

    @property
    def state_file(self):
        return os.path.join(self.data_dir, STATE_FILE)

    @property
    def mcp_url(self):
        return "http://127.0.0.1:{0}/mcp".format(self.api_port)

    @property
    def hook_url(self):
        return "http://127.0.0.1:{0}{1}".format(self.api_port, HOOK_PATH)


def detect(use_cli=True):
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        claude_dir = config_dir
        claude_json = os.path.join(config_dir, ".claude.json")
    else:
        home = os.path.expanduser("~")
        claude_dir = os.path.join(home, ".claude")
        claude_json = os.path.join(home, ".claude.json")
    data_dir = paths.data_dir()
    current, _ = settings_module.load(data_dir)
    return Environment(
        claude_dir=claude_dir, claude_json=claude_json, data_dir=data_dir, install_dir=paths.install_dir(),
        python_exe=paths.python_executable(), proxy_port=current.proxy_port, api_port=current.api_port,
        claude_cli=shutil.which("claude") if use_cli else None)


def session_start_hook(env):
    return {"type": "command", "command": env.python_exe,
            "args": ["-I", "-m", CLI_MODULE, "hook", "session-start"], "timeout": 10}


def prompt_hook(env):
    return {"type": "http", "url": env.hook_url, "timeout": 3}


def is_ours(hook):
    if not isinstance(hook, dict):
        return False
    if CLI_MODULE in (hook.get("args") or []):
        return True
    if CLI_MODULE in str(hook.get("command", "")):
        return True
    return str(hook.get("url", "")).endswith(HOOK_PATH)


def _strip_hooks(settings):
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [hook for hook in group["hooks"] if not is_ours(hook)]
            if len(kept) != len(group["hooks"]):
                changed = True
            if kept:
                kept_groups.append(dict(group, hooks=kept))
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return changed


def install_hooks(env):
    settings = jsonfile.load(env.settings_json, {})
    if not isinstance(settings, dict):
        raise jsonfile.JsonFileError(env.settings_json + " is not a JSON object; left untouched")
    _strip_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    hooks.setdefault("SessionStart", []).append({"hooks": [session_start_hook(env)]})
    hooks.setdefault("UserPromptSubmit", []).append({"hooks": [prompt_hook(env)]})
    jsonfile.save(env.settings_json, settings)


def remove_hooks(env):
    if not os.path.exists(env.settings_json):
        return False
    settings = jsonfile.load(env.settings_json, {})
    if not isinstance(settings, dict) or not _strip_hooks(settings):
        return False
    jsonfile.save(env.settings_json, settings)
    return True


def hooks_installed(env):
    try:
        settings = jsonfile.load(env.settings_json, {})
    except jsonfile.JsonFileError:
        return False
    found = set()
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if is_ours(hook):
                    found.add(event)
    return {"SessionStart", "UserPromptSubmit"} <= found


def _run_cli(env, args):
    completed = subprocess.run([env.claude_cli] + args, capture_output=True, text=True, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def install_mcp(env):
    if env.claude_cli:
        _run_cli(env, ["mcp", "remove", "--scope", "user", SERVER_NAME])
        code, output = _run_cli(env, ["mcp", "add", "--scope", "user", "--transport", "http", SERVER_NAME, env.mcp_url])
        if code == 0:
            return "cli"
    data = jsonfile.load(env.claude_json, {})
    if not isinstance(data, dict):
        raise jsonfile.JsonFileError(env.claude_json + " is not a JSON object; left untouched")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers[SERVER_NAME] = {"type": "http", "url": env.mcp_url}
    jsonfile.save(env.claude_json, data)
    return "file"


def remove_mcp(env):
    if env.claude_cli:
        code, _ = _run_cli(env, ["mcp", "remove", "--scope", "user", SERVER_NAME])
        if code == 0:
            return True
    if not os.path.exists(env.claude_json):
        return False
    data = jsonfile.load(env.claude_json, {})
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return False
    del servers[SERVER_NAME]
    jsonfile.save(env.claude_json, data)
    return True


def mcp_installed(env):
    try:
        data = jsonfile.load(env.claude_json, {})
    except jsonfile.JsonFileError:
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return isinstance(entry, dict) and entry.get("url") == env.mcp_url


def render_skill(env):
    with open(SKILL_TEMPLATE, encoding="utf-8") as stream:
        text = stream.read()
    values = {
        "{{VERSION}}": VERSION, "{{PROXY_PORT}}": str(env.proxy_port), "{{API_PORT}}": str(env.api_port),
        "{{DATA_DIR}}": env.data_dir, "{{INSTALL_DIR}}": env.install_dir,
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def install_skill(env):
    os.makedirs(os.path.dirname(env.skill_file), exist_ok=True)
    with open(env.skill_file, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(render_skill(env))


def remove_skill(env):
    folder = os.path.dirname(env.skill_file)
    if os.path.isdir(folder):
        shutil.rmtree(folder)
        return True
    return False


def skill_current(env):
    try:
        with open(env.skill_file, encoding="utf-8") as stream:
            return stream.read() == render_skill(env)
    except OSError:
        return False


def read_state(env):
    try:
        state = jsonfile.load(env.state_file, None)
    except jsonfile.JsonFileError:
        return None
    return state if isinstance(state, dict) else None


def install(env, now=None):
    previous = read_state(env)
    method = install_mcp(env)
    install_skill(env)
    install_hooks(env)
    old_version = previous.get("version") if previous else None
    state = {
        "version": VERSION,
        "previous_version": old_version if old_version and old_version != VERSION else (previous or {}).get("previous_version"),
        "updated_at": now if now is not None else time.time(),
        "claude_dir": env.claude_dir,
        "mcp_method": method,
    }
    if previous and old_version == VERSION:
        state["updated_at"] = previous.get("updated_at", state["updated_at"])
    jsonfile.save(env.state_file, state)
    return state


def uninstall(env):
    removed = {"mcp": remove_mcp(env), "skill": remove_skill(env), "hooks": remove_hooks(env)}
    if os.path.exists(env.state_file):
        os.remove(env.state_file)
    return removed


def status(env):
    state = read_state(env)
    return {
        "installed": state is not None,
        "version": state.get("version") if state else None,
        "mcp": mcp_installed(env),
        "skill": skill_current(env),
        "hooks": hooks_installed(env),
        "claude_dir": env.claude_dir,
    }


def refresh(env, now=None):
    state = read_state(env)
    if state is None:
        return "not installed"
    current = status(env)
    if state.get("version") == VERSION and current["mcp"] and current["skill"] and current["hooks"]:
        return "up to date"
    install(env, now)
    return "refreshed"


def whats_new(env, since_version):
    entries = changelog.newer_than(changelog.read(os.path.join(env.install_dir, "CHANGELOG.md")), since_version, VERSION)
    return changelog.render(entries)


def update_notice(env, since_version):
    if not since_version or since_version == VERSION:
        return ""
    lines = ["AgentProxy обновлён {0} → {1}. Перечитай скилл agentproxy: инструменты и правила могли поменяться."
             .format(since_version, VERSION)]
    changes = whats_new(env, since_version)
    if changes:
        lines.append("Что нового:")
        lines.append(changes)
    return "\n".join(lines)


def recent_update(env, now=None):
    state = read_state(env)
    if not state or not state.get("previous_version"):
        return None
    now = now if now is not None else time.time()
    if now - state.get("updated_at", 0) > WHATS_NEW_DAYS * 24 * 3600:
        return None
    return state["previous_version"]
