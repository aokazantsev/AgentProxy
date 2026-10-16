import logging
import logging.handlers
import os
import sys
import traceback

FORMAT = "%(asctime)s  %(levelname)-5s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 1024 * 1024
BACKUPS = 3
QUIET_LOGGERS = ("mitmproxy", "tornado.access", "asyncio", "hpack", "urllib3")


def setup(directory, file_name="engine.log"):
    os.makedirs(directory, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(directory, file_name), maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8",
        errors="backslashreplace")
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    sys.excepthook = _log_uncaught


def _log_uncaught(kind, value, trace):
    logging.getLogger("agentproxy").critical(
        "CRASH: %s", "".join(traceback.format_exception(kind, value, trace)).rstrip())
