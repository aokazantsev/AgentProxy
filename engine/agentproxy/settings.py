import json
import os
from dataclasses import dataclass, fields, replace

FILE_NAME = "settings.json"


@dataclass(frozen=True)
class Settings:
    proxy_port: int = 8890
    api_port: int = 8891
    max_flows: int = 5000
    max_body_bytes: int = 2 * 1024 * 1024
    verify_upstream: bool = True
    adb_path: str = ""
    yield_to_charles: bool = False


LIMITS = {
    "proxy_port": (1024, 65535),
    "api_port": (1024, 65535),
    "max_flows": (100, 100000),
    "max_body_bytes": (1024, 64 * 1024 * 1024),
}


def _coerce(name, value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise ValueError("{0} must be true or false".format(name))
    if isinstance(default, str):
        if isinstance(value, str):
            return value.strip()
        raise ValueError("{0} must be a string".format(name))
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("{0} must be an integer".format(name))
        low, high = LIMITS[name]
        if not low <= value <= high:
            raise ValueError("{0} must be between {1} and {2}".format(name, low, high))
        return value
    raise ValueError("{0} has unsupported type".format(name))


def parse(raw):
    defaults = Settings()
    known = {field.name: getattr(defaults, field.name) for field in fields(Settings)}
    values = {}
    problems = []
    for name, value in raw.items():
        if name not in known:
            problems.append("unknown setting {0} ignored".format(name))
            continue
        try:
            values[name] = _coerce(name, value, known[name])
        except ValueError as error:
            problems.append("{0}; default {1!r} used".format(error, known[name]))
    result = replace(defaults, **values)
    if result.proxy_port == result.api_port:
        problems.append("proxy_port equals api_port; default ports used")
        result = replace(result, proxy_port=defaults.proxy_port, api_port=defaults.api_port)
    return result, problems


def load(directory):
    path = os.path.join(directory, FILE_NAME)
    if not os.path.exists(path):
        return Settings(), []
    try:
        with open(path, encoding="utf-8-sig") as stream:
            raw = json.load(stream)
    except (OSError, ValueError) as error:
        return Settings(), ["settings.json unreadable ({0}); defaults used".format(error)]
    if not isinstance(raw, dict):
        return Settings(), ["settings.json is not an object; defaults used"]
    return parse(raw)
