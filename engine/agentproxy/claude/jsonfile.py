import json
import os
import tempfile


class JsonFileError(Exception):
    pass


def real_path(path):
    return os.path.realpath(path)


def load(path, default):
    target = real_path(path)
    if not os.path.exists(target):
        return default
    try:
        with open(target, encoding="utf-8-sig") as stream:
            text = stream.read()
    except OSError as error:
        raise JsonFileError("{0}: {1}".format(path, error))
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except ValueError as error:
        raise JsonFileError("{0} is not valid JSON ({1}); left untouched".format(path, error))


def save(path, data):
    save_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def save_text(path, text):
    target = real_path(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".agentproxy-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
