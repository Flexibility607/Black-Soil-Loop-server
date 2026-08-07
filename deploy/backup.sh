#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/black-soil-loop/backend.env"
BACKUP_DIR="/var/lib/black-soil-loop/backups"
[[ -f "$ENV_FILE" ]] || { echo "缺少 $ENV_FILE" >&2; exit 2; }

set -a
source "$ENV_FILE"
set +a
: "${BACKUP_DATABASE_URL:?缺少 BACKUP_DATABASE_URL}"
: "${BACKUP_ENCRYPTION_KEY:?缺少 BACKUP_ENCRYPTION_KEY}"

umask 0077
install -d -m 0750 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT="$BACKUP_DIR/black_soil_loop-$STAMP.dump.enc"

pg_dump --format=custom --no-owner --no-privileges "$BACKUP_DATABASE_URL" | \
  openssl enc -aes-256-cbc -salt -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -out "$OUTPUT"
sha256sum "$OUTPUT" > "$OUTPUT.sha256"

if [[ -n "${BACKUP_REMOTE:-}" ]]; then
  command -v rclone >/dev/null || { echo "已配置 BACKUP_REMOTE，但未安装 rclone" >&2; exit 3; }
  rclone copy "$OUTPUT" "$OUTPUT.sha256" "$BACKUP_REMOTE"
fi

echo "$OUTPUT"
