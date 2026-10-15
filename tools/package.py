import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN_ROOTS = [os.path.join(ROOT, "engine", "agentproxy"), os.path.join(ROOT, "runtime", "python")]


def clean():
    removed = 0
    for base in CLEAN_ROOTS:
        for folder, dirs, _ in os.walk(base):
            for name in list(dirs):
                if name == "__pycache__":
                    shutil.rmtree(os.path.join(folder, name))
                    dirs.remove(name)
                    removed += 1
    print("removed {0} __pycache__ folders".format(removed))


def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    print("sha256 {0}  {1} ({2:.1f} MB)".format(digest.hexdigest(), os.path.basename(path), os.path.getsize(path) / 1048576))


def main():
    command = sys.argv[1]
    if command == "clean":
        clean()
    elif command == "checksum":
        checksum(sys.argv[2])
    else:
        raise SystemExit("unknown command " + command)


if __name__ == "__main__":
    main()
