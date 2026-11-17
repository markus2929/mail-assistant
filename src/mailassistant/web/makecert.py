"""Create a self-signed TLS certificate for the dashboard on the LAN.

    python -m mailassistant.web.makecert [--name desktop.local] [--ip 192.168.1.20] [--out secrets]

Default names: this host's name, <name>.local, localhost; default addresses: the LAN address(es) found
and 127.0.0.1. Valid 825 days. Your browser warns once; compare the printed SHA-256 fingerprint with the
one the browser shows, then accept.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def lan_addresses() -> list[str]:
    found: set[str] = set()
    try:  # the address used for outgoing traffic (no packet is sent for UDP connect)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


def build(names: list[str], ips: list[str], days: int = 825) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "mail-assistant"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mail Assistant (self-signed)")])
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    fp = cert.fingerprint(hashes.SHA256()).hex(":").upper()
    return cert.public_bytes(serialization.Encoding.PEM), key_pem, fp


def main(argv: list[str] | None = None) -> int:
    host = socket.gethostname().split(".")[0]
    ap = argparse.ArgumentParser(prog="makecert")
    ap.add_argument("--name", action="append", help="DNS name (repeatable)")
    ap.add_argument("--ip", action="append", help="IP address (repeatable)")
    ap.add_argument("--out", default="secrets")
    a = ap.parse_args(argv)
    names = list(dict.fromkeys(a.name or [host, f"{host}.local", "localhost"]))
    ips = list(dict.fromkeys(a.ip or [*lan_addresses(), "127.0.0.1"]))
    cert, key, fp = build(names, ips)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("web_tls_cert", cert), ("web_tls_key", key)):
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    print(f"written: {out / 'web_tls_cert'}, {out / 'web_tls_key'}")
    print(f"names: {', '.join(names)}   addresses: {', '.join(ips)}")
    print(f"SHA-256 fingerprint: {fp}")
    print("\nPut in .env:")
    print(f"WEB_ALLOWED_HOSTS={','.join([*names, *ips])}")
    print(f"DASHBOARD_URL=https://{names[1] if len(names) > 1 else names[0]}:8443")
    return 0


if __name__ == "__main__":
    sys.exit(main())
