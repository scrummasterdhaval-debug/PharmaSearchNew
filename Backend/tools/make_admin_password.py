"""Create the admin sign-in credentials.

    python tools/make_admin_password.py <username>              print them
    python tools/make_admin_password.py <username> --write-env  save them to Backend/.env

The password is read without echoing and never written anywhere; only its
scrypt digest is kept, which is what the server checks against.

--write-env is the simplest route on a single machine: the server reads
Backend/.env at startup, so the credentials only have to be set up once.
The file is git-ignored.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from services.admin_auth import hash_password  # noqa: E402

ENV_PATH = BACKEND / ".env"


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Set these keys in the .env, keeping every other line as it was."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    remaining = dict(values)
    updated = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    args = [arg for arg in argv if arg != "--write-env"]
    to_env = "--write-env" in argv
    if len(args) != 1:
        print(__doc__.strip())
        return 2
    username = args[0].strip()
    if not username:
        print("A user name is required.")
        return 2

    password = getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("Use at least 12 characters. Nothing was written.")
        return 1
    if password != getpass.getpass("Repeat the password: "):
        print("The two entries did not match. Nothing was written.")
        return 1

    values = {
        "PHARMASEARCH_ADMIN_USERNAME": username,
        "PHARMASEARCH_ADMIN_PASSWORD_HASH": hash_password(password),
        "PHARMASEARCH_SESSION_SECRET": secrets.token_urlsafe(48),
    }

    print()
    if to_env:
        write_env(values)
        print(f"Saved to {ENV_PATH}. Restart the server, then sign in as {username}.")
    else:
        print("Set these three variables where the server runs, then restart it:")
        print()
        for key, value in values.items():
            print(f"{key}={value}")
        print()
        print("Or run again with --write-env to save them to Backend/.env instead.")
    print("Keep them out of source control. Changing the session secret signs everyone out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
