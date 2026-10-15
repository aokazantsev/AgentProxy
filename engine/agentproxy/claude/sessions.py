import os
import time

from . import jsonfile

FILE_NAME = "claude-sessions.json"
MAX_SESSIONS = 300
MAX_AGE_SECONDS = 30 * 24 * 3600


class SeenVersions:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, FILE_NAME)

    def _load(self):
        try:
            data = jsonfile.load(self.path, {})
        except jsonfile.JsonFileError:
            data = {}
        return data if isinstance(data, dict) else {}

    def get(self, session_id):
        entry = self._load().get(session_id)
        return entry.get("version") if isinstance(entry, dict) else None

    def remember(self, session_id, version, now=None):
        now = now if now is not None else time.time()
        data = self._load()
        data[session_id] = {"version": version, "seen": now}
        fresh = {key: value for key, value in data.items()
                 if isinstance(value, dict) and now - value.get("seen", 0) <= MAX_AGE_SECONDS}
        if len(fresh) > MAX_SESSIONS:
            newest = sorted(fresh.items(), key=lambda item: item[1].get("seen", 0), reverse=True)[:MAX_SESSIONS]
            fresh = dict(newest)
        jsonfile.save(self.path, fresh)
