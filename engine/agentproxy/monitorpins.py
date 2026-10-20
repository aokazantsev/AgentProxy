import json
import os
import threading

FILE_NAME = "monitor.json"
MAX_PINS = 200
MAX_HOST_LENGTH = 255


class PinError(Exception):
    pass


class PinStorageError(PinError):
    pass


class MonitorPins:
    def __init__(self, directory):
        self.path = os.path.join(directory, FILE_NAME)
        self.lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8-sig") as stream:
                raw = json.load(stream)
        except FileNotFoundError:
            return []
        except ValueError:
            return []
        except OSError as error:
            raise PinStorageError("{0} is unreadable: {1}".format(FILE_NAME, error.strerror or error))
        hosts = raw.get("pinned_hosts") if isinstance(raw, dict) else None
        if not isinstance(hosts, list):
            return []
        valid = []
        for host in hosts:
            if isinstance(host, str) and 0 < len(host) <= MAX_HOST_LENGTH and host not in valid:
                valid.append(host)
        return valid[:MAX_PINS]

    def _write(self, hosts):
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"pinned_hosts": hosts}, stream, ensure_ascii=False, indent=1)
        os.replace(temporary, self.path)

    def hosts(self):
        with self.lock:
            return self._read()

    def set(self, host, pinned):
        if not isinstance(host, str) or not 0 < len(host) <= MAX_HOST_LENGTH:
            raise PinError("host must be a non-empty string up to {0} characters".format(MAX_HOST_LENGTH))
        try:
            host.encode("utf-8")
        except UnicodeEncodeError:
            raise PinError("host must be valid Unicode text")
        if not isinstance(pinned, bool):
            raise PinError("pinned must be true or false")
        with self.lock:
            hosts = self._read()
            if pinned and host not in hosts:
                if len(hosts) >= MAX_PINS:
                    raise PinError("at most {0} pinned hosts".format(MAX_PINS))
                hosts.append(host)
            elif not pinned:
                hosts = [item for item in hosts if item != host]
            try:
                self._write(hosts)
            except OSError as error:
                raise PinStorageError("{0} was not saved: {1}".format(FILE_NAME, error.strerror or error))
            return hosts
