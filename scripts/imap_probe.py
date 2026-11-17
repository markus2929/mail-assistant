#!/usr/bin/env python3
"""Read-only probe of Proton Bridge's IMAP server. Prints NO message content.

Shows: STARTTLS certificate fingerprint (to pin in Phase 2), capabilities, folder names exactly
as Bridge exposes them, and per-folder message count / UIDNEXT / UIDVALIDITY.
Run where the app runs (Bridge must be running):
  single machine:  python3 scripts/imap_probe.py
  home server:     docker compose run --rm --no-deps worker python scripts/imap_probe.py
Uses only the standard library; reads environment variables (as inside the containers), else ./.env, and the
IMAP password from /run/secrets/imap_password, else ./secrets/imap_password.
"""
import hashlib
import os
import imaplib
import re
import ssl
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def env(key: str, default: str | None = None) -> str | None:
    if os.environ.get(key):
        return os.environ[key].strip()
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    return default


host = env("IMAP_HOST", "127.0.0.1")
port = int(env("IMAP_PORT", "1143"))
user = env("IMAP_USERNAME")
secret = Path("/run/secrets/imap_password")
password = (secret if secret.is_file() else ROOT / "secrets" / "imap_password").read_text().strip()
if not user:
    raise SystemExit("IMAP_USERNAME missing in .env")

# Probe only: certificate is self-signed, so we do not verify here -- we print its fingerprint
# so that Phase 2 can pin it instead of disabling verification.
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

M = imaplib.IMAP4(host, port)
M.starttls(ssl_context=ctx)
der = M.sock.getpeercert(binary_form=True)
print("cert sha256:", hashlib.sha256(der).hexdigest())
M.login(user, password)
print("capabilities:", " ".join(M.capability()[1][0].decode().split()))

typ, folders = M.list()
rows = []
for raw in folders:
    m = re.match(rb'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)', raw)
    name = m["name"].decode().strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    rows.append((name, m["flags"].decode()))

print(f"\n{'folder':40} {'msgs':>6} {'uidnext':>8} {'uidvalidity':>12}  flags")
for name, flags in sorted(rows):
    typ, data = M.status(f'"{name}"', "(MESSAGES UIDNEXT UIDVALIDITY)")
    nums = dict(re.findall(r"(MESSAGES|UIDNEXT|UIDVALIDITY) (\d+)", data[0].decode()))
    print(f"{name:40} {nums.get('MESSAGES', '?'):>6} {nums.get('UIDNEXT', '?'):>8} "
          f"{nums.get('UIDVALIDITY', '?'):>12}  {flags}")
M.logout()
