#!/usr/bin/env bash
# Nightly backup of the mail assistant (home-server layout).
#   scripts/backup.sh [BACKUP_DIR]          (default: $BACKUP_DIR or ~/backups/mail-assistant)
# Writes, per run:  db-YYYY-MM-DD_HHMM.dump  (pg_dump custom format; restore: see docs/SERVER_SETUP.md "Backups")
#                   config-YYYY-MM-DD_HHMM.tar.gz  (.env + secrets/: without fernet_key the stored Google/Microsoft
#                                                  sign-ins in the dump cannot be decrypted)
# Both contain private data (mail, keys): files are created 0600 in a 0700 directory. Keep the directory on another
# disk or machine, ideally encrypted. Old backups are removed after $BACKUP_KEEP_DAYS (default 14).
# Cron example (02:30 every night):
#   30 2 * * * cd ~/mail-assistant && scripts/backup.sh >> ~/backups/mail-assistant/backup.log 2>&1
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
dest="${1:-${BACKUP_DIR:-$HOME/backups/mail-assistant}}"
keep="${BACKUP_KEEP_DAYS:-14}"
stamp="$(date +%F_%H%M)"
mkdir -p "$dest"
chmod 700 "$dest"

dump="$dest/db-$stamp.dump"
tmp="$dump.partial"
trap 'rm -f "$tmp"' EXIT
docker compose exec -T db pg_dump -U mailassistant -Fc mailassistant > "$tmp"
# a dump that pg_restore cannot list is not a backup
docker compose exec -T db pg_restore --list < "$tmp" > /dev/null
mv "$tmp" "$dump"

tar -czf "$dest/config-$stamp.tar.gz" .env secrets

find "$dest" -maxdepth 1 -type f \( -name 'db-*.dump' -o -name 'config-*.tar.gz' \) -mtime +"$keep" -delete
echo "$(date -Is) backup ok: $(du -h "$dump" | cut -f1) $dump"
