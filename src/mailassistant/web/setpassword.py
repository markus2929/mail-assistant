"""Set the dashboard password (one user). Writes a scrypt hash, never the password itself.

    python -m mailassistant.web.setpassword [secrets/web_password_hash]

Run it on the host (in the project folder) or in a container with the secrets folder mounted.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from mailassistant.web.auth import hash_password

MIN_LENGTH = 12


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0] if args else "secrets/web_password_hash")
    if not sys.stdin.isatty():
        print("this needs an interactive terminal (the password is typed hidden); run it in a normal "
              "terminal window, in the project folder. Nothing changed.", file=sys.stderr)
        return 1
    try:
        pw = getpass.getpass("New dashboard password (min. 12 characters): ")
        again = getpass.getpass("Repeat: ") if len(pw) >= MIN_LENGTH else ""
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled; nothing changed")
        return 1
    if len(pw) < MIN_LENGTH:
        print("too short; nothing changed")
        return 1
    if again != pw:
        print("passwords differ; nothing changed")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(hash_password(pw))
    print(f"stored in {target}. Restart the web service: docker compose up -d web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
