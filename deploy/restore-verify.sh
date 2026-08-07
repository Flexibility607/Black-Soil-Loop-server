#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/black-soil-loop/backend.env"
BACKUP_DIR="/var/lib/black-soil-loop/backups"
[[ -f "$ENV_FILE" ]] || { echo "缺少 $ENV_FILE" >&2; exit 2; }

set -a
source "$ENV_FILE"
set +a
: "${BACKUP_ENCRYPTION_KEY:?缺少 BACKUP_ENCRYPTION_KEY}"

LATEST="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'black_soil_loop-*.dump.enc' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
[[ -n "$LATEST" && -f "$LATEST" ]] || { echo "没有可验证的备份" >&2; exit 2; }
sha256sum --check "$LATEST.sha256"

VERIFY_DB="black_soil_restore_$(date -u +%Y%m%d%H%M%S)"
DECRYPTED="$(mktemp /var/lib/postgresql/${VERIFY_DB}.XXXXXXXX.dump)"
cleanup() {
  runuser -u postgres -- dropdb --if-exists "$VERIFY_DB" >/dev/null 2>&1 || true
  rm -f -- "$DECRYPTED"
}
trap cleanup EXIT

openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -in "$LATEST" -out "$DECRYPTED"
chown postgres:postgres "$DECRYPTED"
runuser -u postgres -- createdb "$VERIFY_DB"
runuser -u postgres -- pg_restore --exit-on-error --no-owner --no-privileges --dbname "$VERIFY_DB" "$DECRYPTED"
runuser -u postgres -- psql --dbname "$VERIFY_DB" --tuples-only --command "SELECT count(*) FROM core.enterprises" >/dev/null
echo "备份恢复验证通过: $LATEST"
