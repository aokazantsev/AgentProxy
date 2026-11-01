import logging
import os
import re
import shutil
import subprocess
import sys
import threading

from .deviceports import SERIAL_PATTERN, DevicePortError
from .flowlog import clean

log = logging.getLogger("agentproxy.device")

ADB_TIMEOUT_SECONDS = 15
ERROR_CHARS = 300
PROXY_OFF = ("", "null", ":0")
ACTIONS = ("enable", "disable", "status")
NO_REVERSE_MARKERS = ("not found", "no such reverse", "nothing to remove")
ADB_PATH_HOWTO = ("write the path with forward slashes (C:/Android/platform-tools/adb.exe): a single backslash breaks "
                  "settings.json; the engine reads the file at start, so restart it from the tray menu after the edit")


class DeviceError(Exception):
    def __init__(self, message, kind):
        Exception.__init__(self, message)
        self.kind = kind


def adb_candidates(configured, environ):
    if configured:
        return [configured]
    found = []
    on_path = shutil.which("adb")
    if on_path:
        found.append(on_path)
    roots = [environ.get("ANDROID_HOME"), environ.get("ANDROID_SDK_ROOT")]
    local = environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Android", "Sdk"))
    for root in roots:
        if root:
            found.append(os.path.join(root, "platform-tools", "adb.exe" if os.name == "nt" else "adb"))
    return found


def find_adb(configured="", environ=None):
    environ = os.environ if environ is None else environ
    candidates = adb_candidates(configured, environ)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    if configured:
        raise DeviceError("adb_path in settings.json points to a missing file ({0}): ask the user to fix the path "
                          "or remove the setting to search PATH and the Android SDK; {1}".format(
                              clean(configured, ERROR_CHARS), ADB_PATH_HOWTO), "adb_path points to a missing file")
    raise DeviceError("adb not found: it is not on PATH and not in ANDROID_HOME, ANDROID_SDK_ROOT or "
                      "%LOCALAPPDATA%\\Android\\Sdk\\platform-tools. Install Android platform-tools, or ask the user "
                      "to put the full path of adb.exe into adb_path in settings.json; " + ADB_PATH_HOWTO,
                      "adb not found")


def reconnect_advice(command):
    serial = command[command.index("-s") + 1] if "-s" in command[:-1] else "<serial>"
    return ("replug it or run adb -s {0} reconnect (adb kill-server and a bare adb reconnect drop every connected "
            "device), then repeat".format(serial))


def run_command(command, timeout=ADB_TIMEOUT_SECONDS):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise DeviceError("adb did not answer within {0} s: the device may be asleep or the connection is stuck; "
                          "{1}".format(timeout, reconnect_advice(command)), "adb timed out")
    except OSError as error:
        raise DeviceError("adb could not be started: {0}".format(clean(error, ERROR_CHARS)), "adb could not be started")
    text = (done.stdout + done.stderr).decode("utf-8", "replace")
    return done.returncode, text


def parse_devices(text):
    devices = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "\t" in line and SERIAL_PATTERN.fullmatch(parts[0]):
            devices.append((parts[0], parts[1]))
    return devices


def device_is_proxy_off(value):
    return value.strip() in PROXY_OFF


class DeviceControl:
    def __init__(self, get_settings, is_listening, runner=run_command, finder=find_adb, ports=None):
        self.get_settings = get_settings
        self.is_listening = is_listening
        self.runner = runner
        self.finder = finder
        self.ports = ports
        self.lock = threading.Lock()

    def apply(self, action, serial):
        if action not in ACTIONS:
            raise DeviceError("action must be one of {0}".format(", ".join(ACTIONS)), "unknown action")
        if serial is not None and not SERIAL_PATTERN.fullmatch(serial):
            raise DeviceError("serial has an unexpected form; take it from device_proxy status or adb devices",
                              "bad serial")
        with self.lock:
            settings = self.get_settings()
            self.adb = self.finder(settings.adb_path)
            serial = self._pick(serial)
            port = settings.proxy_port
            address = "127.0.0.1:{0}".format(port)
            previous = self._proxy_value(serial)
            if action == "enable":
                if not self.is_listening():
                    raise DeviceError("the AgentProxy proxy is not listening on port {0}, the device would lose its "
                                      "connection: check proxy_status and read_log, then repeat device_proxy enable "
                                      "once proxy_status reports running".format(port), "proxy not listening")
                target = self._assign(serial, port)
            else:
                target = self._known_port(serial, port)
            previous_target = self._reverse_target(serial, port)
            had_reverse = previous_target is not None and previous_target == (
                port if target is None and action == "disable" else target)
            if action == "enable":
                self._enable(serial, port, target, address, previous_target)
            elif action == "disable":
                self._disable(serial, port)
            answer = self._state(serial, port, target, address)
            answer["action"] = action
            answer["previous_proxy"] = None if device_is_proxy_off(previous) else previous.strip()
            answer["changed"] = action != "status" and (answer["previous_proxy"] != answer["device_proxy"]
                                                        or had_reverse != answer["reverse_active"])
            answer["hint"] = self._hint(action, answer)
            if action != "status":
                log.info("device proxy %s: proxy %s, reverse %s, pc port %s", action, self._proxy_kind(answer),
                         "on" if answer["reverse_active"] else "off", target or port)
            return answer

    def release(self):
        with self.lock:
            port, address = self._prepare()
            released = []
            for serial in self._ready_serials():
                try:
                    if self._proxy_value(serial).strip() != address:
                        continue
                    self._disable(serial, port)
                    released.append(serial)
                    log.info("device proxy released: pc port %s", self._known_port(serial, port) or port)
                except DeviceError as error:
                    log.info("device release skipped: %s", error.kind)
            return released

    def restore(self, serials):
        with self.lock:
            port, address = self._prepare()
            ready = self._ready_serials()
            restored = []
            for serial in serials:
                if serial not in ready:
                    continue
                try:
                    if not device_is_proxy_off(self._proxy_value(serial)):
                        continue
                    target = self._assign(serial, port)
                    self._enable(serial, port, target, address, self._reverse_target(serial, port))
                    restored.append(serial)
                    log.info("device proxy restored: pc port %s", target)
                except DeviceError as error:
                    log.info("device restore skipped: %s", error.kind)
            return restored

    def _prepare(self):
        settings = self.get_settings()
        self.adb = self.finder(settings.adb_path)
        return settings.proxy_port, "127.0.0.1:{0}".format(settings.proxy_port)

    def _assign(self, serial, port):
        if self.ports is None:
            return port
        try:
            return self.ports.ensure(serial)
        except DevicePortError as error:
            raise DeviceError(str(error), "device port: " + str(error))

    def _known_port(self, serial, port):
        return port if self.ports is None else self.ports.port_of(serial)

    def _ready_serials(self):
        _, text = self._adb(None, "devices")
        return [name for name, state in parse_devices(text) if state == "device"]

    def _adb(self, serial, *args, check=True):
        command = [self.adb] + (["-s", serial] if serial else []) + list(args)
        code, text = self.runner(command)
        if check and code != 0:
            step = " ".join(args[:4])
            raise DeviceError("adb {0} failed: {1}. Check the device with adb devices and repeat".format(
                step, clean(text, ERROR_CHARS) or "exit code {0}".format(code)),
                "adb {0} failed, exit code {1}".format(step, code))
        return code, text

    def _pick(self, serial):
        _, text = self._adb(None, "devices")
        devices = parse_devices(text)
        ready = [name for name, state in devices if state == "device"]
        if serial is not None:
            states = dict(devices)
            if serial not in states:
                raise DeviceError("device {0} is not connected: pass one of the connected devices ({1}) or reconnect it".format(
                    serial, ", ".join(name for name, _ in devices) or "none"), "device not connected")
            if states[serial] != "device":
                raise DeviceError(self._not_ready(serial, states[serial]), "device " + states[serial])
            return serial
        if len(ready) == 1:
            return ready[0]
        if not ready:
            if devices:
                raise DeviceError("; ".join(self._not_ready(name, state) for name, state in devices),
                                  "no ready device: " + ", ".join(sorted(set(state for _, state in devices))))
            raise DeviceError("no device: connect one by USB with debugging on, start an emulator, or adb connect "
                              "to a device on Wi-Fi, then repeat", "no device")
        raise DeviceError("several devices are connected ({0}): pass serial".format(", ".join(ready)),
                          "several devices")

    @staticmethod
    def _proxy_kind(state):
        if not state["device_proxy"]:
            return "off"
        return "agentproxy" if state["device_proxy"] == state["expected_proxy"] else "other"

    @staticmethod
    def _not_ready(serial, state):
        if state == "unauthorized":
            return "device {0} is unauthorized: ask the user to accept the debugging prompt on the device".format(serial)
        if state == "offline":
            return "device {0} is offline: replug it or run adb -s {0} reconnect".format(serial)
        return "device {0} is in state {1}, not ready: wait a few seconds and repeat, or replug it".format(serial, state)

    def _proxy_value(self, serial):
        _, text = self._adb(serial, "shell", "settings", "get", "global", "http_proxy")
        return text.strip().splitlines()[-1] if text.strip() else ""

    def _reverse_target(self, serial, port):
        _, text = self._adb(serial, "reverse", "--list")
        pattern = re.compile(r"tcp:{0}\s+tcp:(\d+)\b".format(port))
        for line in text.splitlines():
            found = pattern.search(line)
            if found:
                return int(found.group(1))
        return None

    def _reverse_active(self, serial, port, target):
        return target is not None and self._reverse_target(serial, port) == target

    def _enable(self, serial, port, target, address, previous_target):
        self._adb(serial, "reverse", "tcp:{0}".format(port), "tcp:{0}".format(target))
        try:
            self._adb(serial, "shell", "settings", "put", "global", "http_proxy", address)
        except DeviceError:
            if previous_target is None:
                self._adb(serial, "reverse", "--remove", "tcp:{0}".format(port), check=False)
            elif previous_target != target:
                self._adb(serial, "reverse", "tcp:{0}".format(port), "tcp:{0}".format(previous_target), check=False)
            raise

    def _disable(self, serial, port):
        self._adb(serial, "shell", "settings", "put", "global", "http_proxy", ":0")
        code, text = self._adb(serial, "reverse", "--remove", "tcp:{0}".format(port), check=False)
        if code != 0 and not any(marker in text.lower() for marker in NO_REVERSE_MARKERS):
            raise DeviceError("the device proxy is off, but adb reverse --remove failed: {0}. Run adb reverse --list "
                              "and remove the leftover by hand".format(clean(text, ERROR_CHARS)),
                              "adb reverse --remove failed, exit code {0}".format(code))

    def _state(self, serial, port, target, address):
        value = self._proxy_value(serial)
        proxy = None if device_is_proxy_off(value) else value.strip()
        reverse = self._reverse_active(serial, port, target)
        listening = self.is_listening()
        return {
            "serial": serial,
            "device_proxy": proxy,
            "expected_proxy": address,
            "pc_port": target,
            "reverse_active": reverse,
            "proxy_listening": listening,
            "ready": proxy == address and reverse and listening,
        }

    @staticmethod
    def _hint(action, state):
        if action == "disable":
            if state["device_proxy"]:
                return "the device proxy is still set: check the device and repeat"
            return None
        if state["ready"]:
            return ("the device proxy and adb reverse point to AgentProxy; apps that read the proxy only at start need "
                    "a restart; HTTPS needs the AgentProxy certificate on the device (agentproxy skill, connecting a "
                    "device)")
        if state["device_proxy"] == state["expected_proxy"] and not state["proxy_listening"]:
            return ("the device points to AgentProxy but the proxy is not listening, so the device has no "
                    "connection: check proxy_status and read_log, or run device_proxy disable")
        if state["device_proxy"] and state["device_proxy"] != state["expected_proxy"]:
            return "the device uses another proxy ({0}): run device_proxy enable to switch it to AgentProxy".format(
                state["device_proxy"])
        if state["device_proxy"] and state["pc_port"] is None:
            return ("the device has no port of its own on the PC yet (it was routed by an older AgentProxy or by "
                    "hand): its traffic is not marked with the device; run device_proxy enable")
        if state["device_proxy"] and not state["reverse_active"]:
            return ("adb reverse does not lead to this device's port {0} on the PC (missing, or left by an older "
                    "AgentProxy on the shared port): its traffic is not marked with the device or does not arrive; "
                    "run device_proxy enable".format(state["pc_port"]))
        return "the device proxy is off: run device_proxy enable to route its traffic through AgentProxy"
