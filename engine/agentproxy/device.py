import logging
import os
import re
import shutil
import subprocess
import sys
import threading

from .flowlog import clean

log = logging.getLogger("agentproxy.device")

ADB_TIMEOUT_SECONDS = 15
ERROR_CHARS = 300
SERIAL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]*")
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


def run_command(command, timeout=ADB_TIMEOUT_SECONDS):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise DeviceError("adb did not answer within {0} s: the device may be asleep or the connection is stuck; "
                          "replug it, run adb kill-server, then repeat".format(timeout), "adb timed out")
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
    def __init__(self, get_settings, is_listening, runner=run_command, finder=find_adb):
        self.get_settings = get_settings
        self.is_listening = is_listening
        self.runner = runner
        self.finder = finder
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
            had_reverse = self._reverse_active(serial, port)
            if action == "enable":
                if not self.is_listening():
                    raise DeviceError("the AgentProxy proxy is not listening on port {0}, the device would lose its "
                                      "connection: check proxy_status and read_log, then repeat device_proxy enable "
                                      "once proxy_status reports running".format(port), "proxy not listening")
                self._enable(serial, port, address, had_reverse)
            elif action == "disable":
                self._disable(serial, port)
            answer = self._state(serial, port, address)
            answer["action"] = action
            answer["previous_proxy"] = None if device_is_proxy_off(previous) else previous.strip()
            answer["changed"] = action != "status" and (answer["previous_proxy"] != answer["device_proxy"]
                                                        or had_reverse != answer["reverse_active"])
            answer["hint"] = self._hint(action, answer)
            if action != "status":
                log.info("device proxy %s: proxy %s, reverse %s", action, self._proxy_kind(answer),
                         "on" if answer["reverse_active"] else "off")
            return answer

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
            return "device {0} is offline: replug it or run adb reconnect".format(serial)
        return "device {0} is in state {1}, not ready: wait a few seconds and repeat, or replug it".format(serial, state)

    def _proxy_value(self, serial):
        _, text = self._adb(serial, "shell", "settings", "get", "global", "http_proxy")
        return text.strip().splitlines()[-1] if text.strip() else ""

    def _reverse_active(self, serial, port):
        _, text = self._adb(serial, "reverse", "--list")
        pattern = re.compile(r"tcp:{0}\s+tcp:{0}\b".format(port))
        return any(pattern.search(line) for line in text.splitlines())

    def _enable(self, serial, port, address, had_reverse):
        self._adb(serial, "reverse", "tcp:{0}".format(port), "tcp:{0}".format(port))
        try:
            self._adb(serial, "shell", "settings", "put", "global", "http_proxy", address)
        except DeviceError:
            if not had_reverse:
                self._adb(serial, "reverse", "--remove", "tcp:{0}".format(port), check=False)
            raise

    def _disable(self, serial, port):
        self._adb(serial, "shell", "settings", "put", "global", "http_proxy", ":0")
        code, text = self._adb(serial, "reverse", "--remove", "tcp:{0}".format(port), check=False)
        if code != 0 and not any(marker in text.lower() for marker in NO_REVERSE_MARKERS):
            raise DeviceError("the device proxy is off, but adb reverse --remove failed: {0}. Run adb reverse --list "
                              "and remove the leftover by hand".format(clean(text, ERROR_CHARS)),
                              "adb reverse --remove failed, exit code {0}".format(code))

    def _state(self, serial, port, address):
        value = self._proxy_value(serial)
        proxy = None if device_is_proxy_off(value) else value.strip()
        reverse = self._reverse_active(serial, port)
        listening = self.is_listening()
        return {
            "serial": serial,
            "device_proxy": proxy,
            "expected_proxy": address,
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
        if state["device_proxy"] and not state["reverse_active"]:
            return "the proxy is set but adb reverse is missing, the device has no connection: run device_proxy enable"
        return "the device proxy is off: run device_proxy enable to route its traffic through AgentProxy"
