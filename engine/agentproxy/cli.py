import argparse
import json
import logging
import sys

from . import VERSION, logs, paths
from .claude import hooks, integration
from .claude.jsonfile import JsonFileError

EXIT_OK = 0
EXIT_FAILED = 1


def emit(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_hook_input():
    try:
        text = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def command_status(args):
    env = integration.detect(use_cli=False)
    status = hooks.fetch_status(env)
    emit({"version": VERSION, "engine": status, "running": status is not None,
          "claude": integration.status(env)})
    return EXIT_OK if status is not None else EXIT_FAILED


def command_claude(args):
    env = integration.detect(use_cli=not args.no_cli)
    log = logging.getLogger("agentproxy.cli")
    try:
        if args.action == "install":
            result = integration.install(env)
        elif args.action == "uninstall":
            result = integration.uninstall(env)
        elif args.action == "refresh":
            result = {"result": integration.refresh(env)}
        else:
            result = integration.status(env)
    except (JsonFileError, OSError) as error:
        log.error("claude %s failed: %s", args.action, error)
        emit({"error": str(error)})
        return EXIT_FAILED
    log.info("claude %s: %s", args.action, result)
    emit(result)
    return EXIT_OK


def command_hook(args):
    env = integration.detect(use_cli=False)
    hook_input = read_hook_input()
    try:
        context = hooks.session_start_context(env, hook_input, hooks.fetch_status(env))
    except Exception as error:
        logging.getLogger("agentproxy.cli").exception("session-start hook failed")
        context = "AgentProxy {0}: хук старта сессии упал ({1}), подробности в {2}\\cli.log.".format(
            VERSION, error, env.data_dir)
    emit(hooks.hook_output("SessionStart", context))
    return EXIT_OK


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="agentproxy", description="AgentProxy {0}".format(VERSION))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="engine and Claude integration state as JSON")
    claude = sub.add_parser("claude", help="Claude Code integration: MCP server, skill, hooks")
    claude.add_argument("action", choices=["install", "uninstall", "refresh", "status"])
    claude.add_argument("--no-cli", action="store_true", help="edit ~/.claude.json directly instead of `claude mcp`")
    hook = sub.add_parser("hook", help="entry point for Claude Code hooks")
    hook.add_argument("event", choices=["session-start"])
    sub.add_parser("version")
    args = parser.parse_args(argv)
    if args.command == "version":
        sys.stdout.write(VERSION + "\n")
        return EXIT_OK
    logs.setup(paths.ensure(paths.data_dir()), file_name="cli.log")
    handlers = {"status": command_status, "claude": command_claude, "hook": command_hook}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
