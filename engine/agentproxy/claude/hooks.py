import json
import os
import urllib.error
import urllib.request

from .. import VERSION
from . import integration
from .sessions import SeenVersions

STATUS_TIMEOUT_SECONDS = 1.5


def fetch_status(env):
    url = "http://127.0.0.1:{0}/api/status".format(env.api_port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=STATUS_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def describe(env, status):
    if status is None:
        exe = os.path.join(env.install_dir, "AgentProxy.exe")
        return ("AgentProxy {0} установлен, но движок не отвечает на 127.0.0.1:{1}: MCP-инструменты agentproxy "
                "недоступны. Запуск без ожидания: explorer.exe \"{2}\" (не вызывать exe напрямую из Bash — команда повиснет), или значок в трее. Журналы: {3}\\log.txt, {3}\\engine.log."
                .format(VERSION, env.api_port, exe, env.data_dir))
    proxy = status.get("proxy") if isinstance(status.get("proxy"), dict) else {}
    state = "работает" if proxy.get("running") is True else "запущен, но прокси не слушает порт (см. engine.log)"
    flows = status.get("flows")
    return ("AgentProxy {0} {1}: прокси 127.0.0.1:{2}, MCP-сервер agentproxy, монитор http://127.0.0.1:{3}/, "
            "записано запросов: {4}. Как работать — скилл agentproxy."
            .format(VERSION, state, env.proxy_port, env.api_port,
                    flows if isinstance(flows, int) and not isinstance(flows, bool) else "?"))


def session_start_context(env, hook_input, status=None, now=None):
    parts = [describe(env, status)]
    session_id = hook_input.get("session_id") if isinstance(hook_input, dict) else None
    seen = SeenVersions(env.data_dir)
    previous = seen.get(session_id) if session_id else None
    if previous is None or previous == VERSION:
        previous = integration.recent_update(env, now)
    if previous:
        parts.append(integration.update_notice(env, previous))
    if session_id:
        seen.remember(session_id, VERSION, now)
    return "\n\n".join(part for part in parts if part)


def prompt_context(env, hook_input, now=None):
    session_id = hook_input.get("session_id") if isinstance(hook_input, dict) else None
    if not session_id:
        return ""
    seen = SeenVersions(env.data_dir)
    previous = seen.get(session_id)
    if previous == VERSION:
        return ""
    seen.remember(session_id, VERSION, now)
    if previous is None:
        return ""
    return integration.update_notice(env, previous)


def hook_output(event, context):
    if not context:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
