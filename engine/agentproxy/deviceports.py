import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("agentproxy.deviceports")

FILE_NAME = "device-ports.json"
MAX_DEVICES = 32
SERIAL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]*")
MAX_SERIAL_LENGTH = 200
HIGHEST_PORT = 65535
LISTEN_WAIT_SECONDS = 5
LISTEN_POLL_SECONDS = 0.1


class DevicePortError(Exception):
    pass


def valid_serial(value):
    return isinstance(value, str) and len(value) <= MAX_SERIAL_LENGTH and bool(SERIAL_PATTERN.fullmatch(value))


class DevicePorts:
    def __init__(self, directory, proxy_port, api_port, is_free):
        self.path = os.path.join(directory, FILE_NAME)
        self.reserved = {proxy_port, api_port}
        self.first = proxy_port + 1
        self.is_free = is_free
        self.lock = threading.Lock()
        self._ports = self._read()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8-sig") as stream:
                raw = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            log.warning("%s unreadable, device ports are assigned anew: %s", FILE_NAME, error)
            return {}
        entries = raw.get("ports") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return {}
        ports = {}
        for serial, port in entries.items():
            if (valid_serial(serial) and isinstance(port, int) and not isinstance(port, bool)
                    and 1024 <= port <= HIGHEST_PORT and port not in self.reserved and port not in ports.values()
                    and len(ports) < MAX_DEVICES):
                ports[serial] = port
        return ports

    def _write(self):
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"ports": self._ports}, stream, indent=1)
        os.replace(temporary, self.path)

    def mapping(self):
        with self.lock:
            return dict(self._ports)

    def ports(self):
        with self.lock:
            return sorted(self._ports.values())

    def device_of(self, port):
        with self.lock:
            for serial, assigned in self._ports.items():
                if assigned == port:
                    return serial
        return None

    def port_of(self, serial):
        with self.lock:
            return self._ports.get(serial)

    def device_of_client(self, client):
        sockname = getattr(client, "sockname", None) if client is not None else None
        return self.device_of(sockname[1]) if sockname else None

    def assign(self, serial, avoid_current=False):
        if not valid_serial(serial):
            raise DevicePortError("serial has an unexpected form")
        with self.lock:
            current = self._ports.get(serial)
            previous = dict(self._ports)
            if current is not None and not avoid_current:
                if list(self._ports)[-1] != serial:
                    self._ports.pop(serial)
                    self._ports[serial] = current
                    try:
                        self._write()
                    except OSError as error:
                        self._ports = previous
                        log.warning("%s order not saved: %s", FILE_NAME, error.strerror or error)
                return current
            evicted = None
            if current is None and len(self._ports) >= MAX_DEVICES:
                evicted = self._ports.pop(next(iter(self._ports)))
            port = self._free_port(set(self._ports.values()) | self.reserved | {current, evicted},
                                   max(list(self._ports.values()) + [current or 0, evicted or 0]))
            if port is None:
                self._ports = previous
                raise DevicePortError("no free port above {0} for the device".format(self.first))
            if current is None:
                self._ports[serial] = port
            else:
                self._ports = {name: port if name == serial else value for name, value in self._ports.items()}
            try:
                self._write()
            except OSError as error:
                self._ports = previous
                raise DevicePortError("{0} was not saved: {1}".format(FILE_NAME, error.strerror or error))
            log.info("device port %s assigned%s%s", port, " (port {0} was busy)".format(current) if current else "",
                     ", port {0} of the device used longest ago freed".format(evicted) if evicted else "")
            return port

    def _free_port(self, taken, last):
        start = last + 1 if self.first <= last < HIGHEST_PORT else self.first
        for port in list(range(start, HIGHEST_PORT + 1)) + list(range(self.first, start)):
            if port not in taken and self.is_free(port):
                return port
        return None

    def check_free(self):
        moved = []
        for serial, port in self.mapping().items():
            if not self.is_free(port):
                try:
                    moved.append((port, self.assign(serial, avoid_current=True)))
                except DevicePortError as error:
                    log.warning("device port %s busy and not reassigned: %s", port, error)
        return moved


class DeviceListeners:
    def __init__(self, ports, set_ports, is_listening, is_paused, loop, wait=LISTEN_WAIT_SECONDS):
        self.ports = ports
        self.set_ports = set_ports
        self.is_listening = is_listening
        self.is_paused = is_paused
        self.loop = loop
        self.wait = wait
        self.active = set(ports.ports())

    def port_of(self, serial):
        return self.ports.port_of(serial)

    def ensure(self, serial):
        port = self.ports.assign(serial)
        if self._listen(port):
            return port
        if self.ports.is_free(port):
            raise DevicePortError("the proxy did not start listening on port {0} for the device: read_log shows why"
                                  .format(port))
        port = self.ports.assign(serial, avoid_current=True)
        if self._listen(port):
            return port
        raise DevicePortError("the proxy did not start listening on port {0} for the device: read_log shows why"
                              .format(port))

    def _listen(self, port):
        assigned = set(self.ports.ports())
        if assigned != self.active:
            self.active = assigned
            self.loop.call_soon_threadsafe(self.set_ports, sorted(self.active))
        if self.is_paused():
            return True
        deadline = time.monotonic() + self.wait
        while True:
            if self.is_listening(port):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(LISTEN_POLL_SECONDS)
