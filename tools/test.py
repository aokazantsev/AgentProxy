import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, "runtime", "python", "python.exe")
DEV = os.path.join(ROOT, "runtime", "dev")
TESTS = os.path.join(ROOT, "engine", "tests")

BOOTSTRAP = (
    "import sys; sys.path.insert(0, {dev!r}); sys.dont_write_bytecode = True; import pytest; "
    "sys.exit(pytest.main({args!r}))"
)


def main():
    if not os.path.exists(PYTHON):
        raise SystemExit("runtime missing: run `py tools/runtime.py fetch` first")
    args = [TESTS, "-p", "no:cacheprovider", "-q"] + sys.argv[1:]
    code = subprocess.call([PYTHON, "-I", "-c", BOOTSTRAP.format(dev=DEV, args=args)], cwd=ROOT)
    sys.exit(code)


if __name__ == "__main__":
    main()
