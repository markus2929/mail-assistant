# Running on a home server (everything except Ollama)

Layout: an always-on **server** runs PostgreSQL, Proton Mail Bridge, the worker, the scheduler and the dashboard
(`docker-compose.yml`). The **desktop** (the machine with the GPU) only runs Ollama plus a small HTTPS proxy in front
of it (`docker-compose.ollama-proxy.yml`). Mail sync, calendar, dashboard, notifications and voice capture then work
24/7; model work waits in the queue while the desktop is off and continues when it is back.

```
 server                                             desktop
 ┌───────────────────────────────────────┐         ┌──────────────────────────────┐
 │ db  bridge  worker ──HTTPS+token────────────────▶ ollama-proxy ─▶ Ollama        │
 │ scheduler   web :8443 ◀── browser/HA  │         │ :11435          127.0.0.1    │
 └───────────────────────────────────────┘         └──────────────────────────────┘
```

Requirements: an x86-64 server with Docker + Compose plugin and git, 2-4 GB free RAM, disk encryption (your mail
is stored there), and **fixed LAN addresses** for server and desktop (router reservation).
Below, `SERVER` and `DESKTOP` stand for those addresses (for example `192.168.1.30` and `192.168.1.20`).

## 1. Code and image on the server
```sh
git clone https://github.com/markus2929/mail-assistant.git && cd mail-assistant
cp .env.example .env
docker compose build                       # app image + Bridge image (official Proton package, checksum-pinned)
```

## 2. Secrets on the server
Moving from a single-machine install: copy these from the old `secrets/` folder (same values keep your dashboard
password, Google authorisation and Home Assistant set-up working): `fernet_key`, `google_client`,
`web_password_hash`, `ha_token`, `capture_token`. Fresh install: see the README for creating them.

New on the server:
```sh
DBPW=$(openssl rand -hex 24)
printf '%s' "$DBPW" > secrets/db_password
printf '%s' "postgresql+psycopg://mailassistant:$DBPW@mailassistant-db:5432/mailassistant" > secrets/database_url
touch secrets/imap_password secrets/ollama_token          # filled in steps 3 and 4
docker run --rm --user "$(id -u)" -v "$PWD/secrets:/out" mailassistant:dev \
  python -m mailassistant.web.makecert --out /out --name "$(hostname)" --name "$(hostname).local" --ip SERVER
chmod 600 secrets/*
```
`makecert` prints the certificate fingerprint (compare it in your browser later) and the `WEB_ALLOWED_HOSTS` /
`DASHBOARD_URL` lines for `.env`.

## 3. Proton Mail Bridge on the server
```sh
docker compose run --rm bridge init
```
This opens Bridge's own command line. Type `login` and follow the prompts (Proton address, password, 2FA), then
`info` - it shows the IMAP **username** and the Bridge-generated **password**. Leave with `exit`.
```sh
printf '%s' 'THE-PASSWORD-SHOWN-BY-INFO' > secrets/imap_password && chmod 600 secrets/imap_password
docker compose up -d bridge
```
Bridge now downloads your mailbox in the background (can take hours the first time). The `bridge` volume holds your
Proton session and keychain - treat it like a secret.

## 4. Ollama proxy on the desktop
On the desktop, in a clone of this repository (Ollama itself keeps listening on 127.0.0.1 only):
```sh
.venv/bin/python -m mailassistant.web.makecert --out /tmp/ollama-proxy-cert --name "$(hostname)" --ip DESKTOP
mv /tmp/ollama-proxy-cert/web_tls_cert secrets/ollama_proxy_cert
mv /tmp/ollama-proxy-cert/web_tls_key  secrets/ollama_proxy_key
printf 'OLLAMA_PROXY_TOKEN=%s\nOLLAMA_PROXY_BIND=DESKTOP\n' "$(openssl rand -hex 32)" > secrets/ollama_proxy.env
chmod 600 secrets/ollama_proxy*
docker compose -f docker-compose.ollama-proxy.yml up -d
```
Note the **fingerprint** `makecert` printed and the token (`grep TOKEN secrets/ollama_proxy.env`). The proxy runs as
uid 1000; if your user has another uid, change `user:` in `docker-compose.ollama-proxy.yml`.

Check from the server (`200` with the token, `403` without):
```sh
curl -sk -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer THE-TOKEN" https://DESKTOP:11435/api/tags
curl -sk -o /dev/null -w '%{http_code}\n' https://DESKTOP:11435/api/tags
```
On the server, put the token in `secrets/ollama_token` (just the token).

## 5. `.env` on the server
Start from your old `.env` (or `.env.example`) and set:
```sh
IMAP_HOST=bridge
IMAP_PORT=1144
OLLAMA_BASE_URL=https://DESKTOP:11435
OLLAMA_CERT_SHA256=<fingerprint from step 4>
WEB_ALLOWED_HOSTS=<line printed in step 2>
DASHBOARD_URL=<line printed in step 2>
```
Remove `WEB_HOST` if present (the compose file sets it). If port 8443 is already used on the server (a UniFi
controller uses it, for example), add `WEB_PUBLISH_PORT=8444` and use that port in `DASHBOARD_URL` and in Home Assistant. Then the Bridge certificate pin:
```sh
docker compose run --rm --no-deps worker python scripts/imap_probe.py     # prints "cert sha256: ..."
```
and set `BRIDGE_CERT_SHA256=` to that value.

## 6. Move the data (from a single-machine install)
Skip this for a fresh install. On the **old machine** stop the app (not the database) and make a dump:
```sh
docker compose stop web worker scheduler
docker compose exec -T db pg_dump -U mailassistant -h 127.0.0.1 -p 5433 -Fc mailassistant > mailassistant.dump
scp mailassistant.dump SERVER:mail-assistant/
```
On the **server** - restore into the empty database *before* starting anything else:
```sh
docker compose up -d db                                  # wait until `docker compose ps` shows it healthy
docker compose exec -T db pg_restore -U mailassistant -d mailassistant --no-owner < mailassistant.dump
shred -u mailassistant.dump                              # it contains your mail; also delete it on the old machine
```
The dump contains everything, including the (encrypted) Google authorisation - no need to authorise again.

## 7. Start and check
```sh
docker compose up -d
docker compose ps -a        # migrate exited (0); db, bridge, web, worker, scheduler running
```
Open `https://SERVER:8443` (accept the certificate after comparing the fingerprint from step 2) and log in with your
usual password. On **Status**: worker, Bridge, Google and Ollama should report ok within a few minutes. New mail keeps
syncing; with the desktop off, Ollama shows "not reachable - … AI work waits in the queue".

## 8. Switch Home Assistant and clean up
- Home Assistant `rest_command` URL -> `https://SERVER:8443/api/capture` (and `DASHBOARD_URL` is used in
  notifications). Restart Home Assistant.
- On the old machine: `docker compose down` for the app stack. Keep its database volume for a couple of weeks as a
  backup, then remove it (`docker volume rm mailassistant_mailassistant-pgdata`). The Proton Bridge app there may
  stay for a mail client.
- From now on update with `git pull && docker compose build && docker compose up -d` on the server.

## Reaching the dashboard by name from another container (e.g. Home Assistant)
Put a `docker-compose.override.yml` next to `docker-compose.yml` (it is git-ignored) that adds the `web` service to
that container's network with an alias, then use `https://mail-assistant:8443/...` (the internal port):
```yaml
services:
  web:
    networks:
      default: {}
      homeassistant:
        aliases: [mail-assistant]
networks:
  homeassistant:
    external: true
    name: <the other container's network>   # docker inspect -f '{{json .NetworkSettings.Networks}}' homeassistant
```
Add `mail-assistant` to `WEB_ALLOWED_HOSTS`. Keep `database_url` on `mailassistant-db` (not `db`): the other network
may have its own container called `db`.

## Behind a reverse proxy (SWAG / nginx), home network + VPN only
To get a proper certificate instead of the self-signed one, put the dashboard behind your reverse proxy. Keep the
dashboard's own HTTPS (`WEB_TLS=self`) and let the proxy connect to `https://mail-assistant:8443` over the shared
Docker network (see the previous section). Example for SWAG: [examples/swag-assistant.subdomain.conf](examples/swag-assistant.subdomain.conf)
- it only allows your home network and VPN range; everything else gets 403. Then:
- add the subdomain to SWAG's `SUBDOMAINS` (certificate) and recreate the SWAG container. With DockSTARTer, do not
  edit the generated `docker-compose.yml`: set it in `docker-compose.override.yml` (`swag: environment: SUBDOMAINS: ...`)
  or in `.env.app.swag`;
- in `.env`: the full name in `WEB_ALLOWED_HOSTS`, `WEB_TRUSTED_PROXIES=<the proxy's Docker network range>` (so the
  login throttle sees the real client address) and, if you like, `DASHBOARD_URL=https://<subdomain>`.
Note: certificate names are public (certificate transparency logs) - the name is visible, the dashboard is not reachable.

## More mailboxes (Microsoft 365, other IMAP)
Extra accounts are added on the server with `python -m mailassistant.mail.accounts` in the worker container; see
the README section "Extra mail accounts" and [MICROSOFT_SETUP.md](MICROSOFT_SETUP.md). The Microsoft sign-in uses
a code you enter on any device, so it works on a server without a browser.

## Backups
`scripts/backup.sh [DIR]` writes a database dump (checked with `pg_restore --list`) and a copy of `.env` +
`secrets/` to `DIR` (default `~/backups/mail-assistant`) and removes backups older than `BACKUP_KEEP_DAYS` (14).
Both files hold private data (mail, keys): they are created readable by you only; keep `DIR` on another disk or
machine, ideally encrypted. Run it nightly from your crontab (`crontab -e`):
```sh
30 2 * * * cd ~/mail-assistant && scripts/backup.sh >> ~/backups/mail-assistant/backup.log 2>&1
```
The `bridge` volume needs no backup (sign in to Bridge again instead).

Restore (into an empty database, e.g. after reinstalling):
```sh
tar -xzf config-<date>.tar.gz              # .env + secrets/ (fernet_key decrypts the stored sign-ins)
docker compose up -d db && docker compose exec -T db pg_restore -U mailassistant -d mailassistant --clean --if-exists < db-<date>.dump
docker compose up -d
```
