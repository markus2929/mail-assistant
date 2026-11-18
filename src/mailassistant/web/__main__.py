"""Start the dashboard. WEB_TLS=self (default): built-in certificate from `web.makecert`;
proxy: plain HTTP for a TLS-terminating reverse proxy; off: plain HTTP, allowed on a loopback address only.
In every mode X-Forwarded-For is trusted only from WEB_TRUSTED_PROXIES (addresses or ranges, default 127.0.0.1),
so a reverse proxy in front of the built-in HTTPS (e.g. SWAG -> https://mail-assistant:8443) passes the real
client address to the login throttle and the logs."""
import sys
from pathlib import Path

import uvicorn

from mailassistant.config import get_settings
from mailassistant.web.app import is_loopback


def main() -> int:
    s = get_settings()
    kw: dict = {"host": s.web_host, "port": s.web_port, "log_level": "info", "server_header": False,
                "proxy_headers": True, "forwarded_allow_ips": s.web_trusted_proxies}
    if s.web_tls == "self":
        missing = [f for f in (s.web_tls_cert_file, s.web_tls_key_file) if not Path(f).is_file()]
        if missing:
            print(f"TLS certificate missing ({', '.join(missing)}): run `python -m mailassistant.web.makecert` "
                  "and add the web_tls_* secrets, or set WEB_TLS=off with WEB_HOST=127.0.0.1", file=sys.stderr)
            return 2
        kw.update(ssl_certfile=s.web_tls_cert_file, ssl_keyfile=s.web_tls_key_file)
    elif s.web_tls == "proxy":
        pass   # plain HTTP behind the proxy; X-Forwarded-* trusted only from WEB_TRUSTED_PROXIES (above)
    elif s.web_tls == "off":
        if not is_loopback(s.web_host):
            print("refusing to serve mail over plain HTTP on the network: WEB_TLS=off needs WEB_HOST=127.0.0.1",
                  file=sys.stderr)
            return 2
    else:
        print(f"unknown WEB_TLS={s.web_tls!r} (self|proxy|off)", file=sys.stderr)
        return 2
    if s.web_password_hash is None:
        print("note: no dashboard password set yet (python -m mailassistant.web.setpassword); login is impossible",
              file=sys.stderr)
    uvicorn.run("mailassistant.web.app:app", **kw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
