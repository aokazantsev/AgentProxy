import argparse
import asyncio
import ctypes
import json
import logging
import os
import platform
import secrets
import socket
import sys
import threading

from . import VERSION, logs, paths
from . import settings as settings_module
from .api import make_app
from .claude import integration
from .guard import OwnPortsGuard
from .recorder import Recorder
from .service import ProxyService
from .store import FlowStore

log = logging.getLogger("agentproxy.engine")

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_PORT_BUSY = 3
EXIT_DATA_DIR = 4
RUNTIME_FILE = "engine-runtime.json"
LOCAL_HOST = "127.0.0.1"
PROBE_INTERVAL_SECONDS = 5
STARTUP_PROBE_SECONDS = 0.25


def port_problem(host, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, port))
    except OSError as error:
        return str(error)
    finally:
        probe.close()
    return None


def proxy_listening(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def watch_parent(pid, on_exit):
    synchronize = 0x00100000
    infinite = 0xFFFFFFFF
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        log.warning("parent process %s not found; stopping", pid)
        on_exit()
        return

    def wait():
        kernel32.WaitForSingleObject(handle, infinite)
        kernel32.CloseHandle(handle)
        log.info("parent process %s exited; stopping", pid)
        on_exit()

    threading.Thread(target=wait, name="parent-watch", daemon=True).start()


def write_runtime(data_dir, payload):
    path = os.path.join(data_dir, RUNTIME_FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    os.replace(temporary, path)
    return path


async def probe_proxy(service):
    seen_listening = False
    while True:
        running = await asyncio.to_thread(proxy_listening, LOCAL_HOST, service.settings.proxy_port)
        if running != service.proxy_running:
            log.info("proxy %s", "listening" if running else "not listening")
        service.proxy_running = running
        seen_listening = seen_listening or running
        await asyncio.sleep(PROBE_INTERVAL_SECONDS if seen_listening else STARTUP_PROBE_SECONDS)


async def serve(current, data_dir, parent_pid):
    from mitmproxy import options
    from mitmproxy.tools.dump import DumpMaster

    cert_dir = paths.ensure(os.path.join(data_dir, "certs"))
    store = FlowStore(os.path.join(data_dir, "flows.db"), current.max_flows)
    service = ProxyService(store, current, data_dir, cert_dir)
    opts = options.Options(listen_host=LOCAL_HOST, listen_port=current.proxy_port, confdir=cert_dir,
                           ssl_insecure=not current.verify_upstream, websocket=False)
    master = DumpMaster(opts, with_termlog=False, with_dumper=False)
    master.addons.add(OwnPortsGuard(current.proxy_port, current.api_port))
    master.addons.add(Recorder(store, current.max_body_bytes))
    loop = asyncio.get_running_loop()

    def shutdown():
        loop.call_soon_threadsafe(master.shutdown)

    token = secrets.token_hex(16)
    context = {"service": service, "token": token, "shutdown": shutdown,
               "claude_env": integration.detect(use_cli=False)}
    server = make_app(context).listen(current.api_port, address=LOCAL_HOST, xheaders=False)
    runtime_path = write_runtime(data_dir, {"token": token})
    if parent_pid:
        watch_parent(parent_pid, shutdown)
    prober = asyncio.ensure_future(probe_proxy(service))
    log.info("api on %s:%s, proxy on %s:%s", LOCAL_HOST, current.api_port, LOCAL_HOST, current.proxy_port)
    try:
        await master.run()
    finally:
        prober.cancel()
        server.stop()
        store.close()
        if os.path.exists(runtime_path):
            os.remove(runtime_path)
        log.info("engine stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentproxy-engine")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(argv)
    data_dir = paths.data_dir()
    try:
        paths.ensure(data_dir)
    except OSError as error:
        sys.stderr.write("data dir unavailable: {0}\n".format(error))
        return EXIT_DATA_DIR
    logs.setup(data_dir)
    import mitmproxy.version
    log.info("AgentProxy engine %s starting: python %s, mitmproxy %s, %s, user %s, data %s", VERSION,
             platform.python_version(), mitmproxy.version.VERSION, platform.platform(),
             os.environ.get("USERNAME", "?"), data_dir)
    current, problems = settings_module.load(data_dir)
    for problem in problems:
        log.warning("settings: %s", problem)
    log.info("settings: %s", current)
    for name, port in (("proxy", current.proxy_port), ("api", current.api_port)):
        problem = port_problem(LOCAL_HOST, port)
        if problem:
            log.error("PORT BUSY: %s port %s:%s is in use (%s); change it in settings.json or close the program "
                      "holding it", name, LOCAL_HOST, port, problem)
            return EXIT_PORT_BUSY
    try:
        asyncio.run(serve(current, data_dir, args.parent_pid))
    except SystemExit as stop:
        if stop.code in (None, 0):
            return EXIT_OK
        if port_problem(LOCAL_HOST, current.proxy_port):
            log.error("PORT BUSY: proxy port %s:%s was taken while starting", LOCAL_HOST, current.proxy_port)
            return EXIT_PORT_BUSY
        log.critical("CRASH: mitmproxy stopped on startup errors (exit %s), see the errors above", stop.code)
        return EXIT_CRASH
    except Exception:
        log.critical("CRASH: engine failed", exc_info=True)
        return EXIT_CRASH
    return EXIT_OK
