import os
import sys

from . import APP_NAME


def data_dir():
    override = os.environ.get("AGENTPROXY_DATA_DIR")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def install_dir():
    override = os.environ.get("AGENTPROXY_INSTALL_DIR")
    if override:
        return override
    python_dir = os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(python_dir))


def python_executable():
    return os.path.join(install_dir(), "runtime", "python", "python.exe")


def ensure(directory):
    os.makedirs(directory, exist_ok=True)
    return directory
