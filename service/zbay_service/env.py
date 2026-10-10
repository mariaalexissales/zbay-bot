"""Reading a .env file, with the standard library.

Small on purpose. A line is KEY=VALUE; the value is whatever follows the first equals
sign, as written. No escapes and no ${interpolation}: a Windows path is the commonest
thing to go in here, and a backslash in one has to stay a backslash.
"""

import os
from pathlib import Path

NAME = ".env"

# this file is service/zbay_service/env.py, so the repo is two folders up from its own.
REPO = Path(__file__).resolve().parents[2]

TRUE = {"1", "true", "yes", "on"}


def parse(text):
    """The settings in a .env file's text, in order, as a dict."""
    found = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()

        key, equals, value = line.partition("=")
        key = key.strip()
        if not equals or not key or " " in key:
            continue

        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # quoted: everything between the quotes, hashes and all.
            value = value[1:-1]
        else:
            # a comment after a value has a space before its hash. a hash with none is part
            # of the value, which is what lets a token or a url with one in it through.
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()

        found[key] = value

    return found


def find(explicit=None, cwd=None):
    """The file to read: the one named, else .env here, else .env at the top of the repo."""
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None

    for folder in (Path(cwd) if cwd else Path.cwd(), REPO):
        path = folder / NAME
        if path.is_file():
            return path

    return None


def load(path, environ=None):
    """Put a file's settings into the environment. Returns the names it set.

    Only where there is nothing already: a variable set for real, by a host or a shell,
    is somebody's deliberate choice and a file left lying around must not undo it. A
    setting left blank in the file is the same as one left out.
    """
    environ = os.environ if environ is None else environ
    applied = []

    for key, value in parse(Path(path).read_text(encoding="utf-8-sig")).items():
        if value != "" and not environ.get(key):
            environ[key] = value
            applied.append(key)

    return applied


def flag(value):
    return str(value or "").strip().lower() in TRUE
