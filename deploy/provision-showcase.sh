#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 执行固定演示数据集配置" >&2
  exit 1
fi
if [[ $# -ne 1 ]]; then
  echo "用法: $0 <服务器源码目录>" >&2
  exit 2
fi

SOURCE_ROOT="$(realpath "$1")"
ENV_FILE="/etc/black-soil-loop/showcase.env"
CREDENTIAL_FILE="/root/black-soil-loop-showcase-credentials"
[[ -f "$SOURCE_ROOT/deploy/postgres/03-create-showcase.sql" ]] || { echo "缺少演示数据库脚本" >&2; exit 2; }
[[ ! -e "$ENV_FILE" ]] || { echo "演示配置已存在，拒绝覆盖" >&2; exit 3; }

random_hex() { openssl rand -hex "$1"; }
SHOWCASE_B01_PASSWORD="$(random_hex 24)"
SHOWCASE_B02_PASSWORD="$(random_hex 24)"
SHOWCASE_WORKER_PASSWORD="$(random_hex 24)"
SHOWCASE_DEMO_PASSWORD="$(random_hex 18)"

runuser -u postgres -- psql \
  -v showcase_b01_password="$SHOWCASE_B01_PASSWORD" \
  -v showcase_b02_password="$SHOWCASE_B02_PASSWORD" \
  -v showcase_worker_password="$SHOWCASE_WORKER_PASSWORD" \
  -f "$SOURCE_ROOT/deploy/postgres/03-create-showcase.sql"

umask 0077
TEMP_ENV="$(mktemp /etc/black-soil-loop/showcase.env.XXXXXXXX)"
trap 'rm -f -- "$TEMP_ENV"' EXIT
cat > "$TEMP_ENV" <<EOF
SHOWCASE_DATASET_ENABLED=false
PUBLIC_DASHBOARD_DATASET=live
SHOWCASE_DATABASE_URL=postgresql+psycopg://blacksoil_showcase_worker:${SHOWCASE_WORKER_PASSWORD}@127.0.0.1:5432/black_soil_loop_showcase
SHOWCASE_B01_DATABASE_URL=postgresql+psycopg://blacksoil_showcase_b01:${SHOWCASE_B01_PASSWORD}@127.0.0.1:5432/black_soil_loop_showcase
SHOWCASE_B02_DATABASE_URL=postgresql+psycopg://blacksoil_showcase_b02:${SHOWCASE_B02_PASSWORD}@127.0.0.1:5432/black_soil_loop_showcase
SHOWCASE_WORKER_DATABASE_URL=postgresql+psycopg://blacksoil_showcase_worker:${SHOWCASE_WORKER_PASSWORD}@127.0.0.1:5432/black_soil_loop_showcase
SHOWCASE_UPLOAD_DIR=/var/lib/black-soil-loop/showcase-uploads
SHOWCASE_ACCOUNT_USERNAMES=showcase-admin,showcase-enterprise,showcase-driver,showcase-store,showcase-third
SHOWCASE_CASE_KEY=changchun-fixed-showcase-v1
EOF
chown root:blacksoil "$TEMP_ENV"
chmod 0640 "$TEMP_ENV"
mv -f -- "$TEMP_ENV" "$ENV_FILE"
trap - EXIT

"$SOURCE_ROOT/deploy/migrate-showcase.sh" "$SOURCE_ROOT"
install -d -o blacksoil -g blacksoil -m 0750 /var/lib/black-soil-loop/showcase-uploads
printf 'showcase_accounts_password=%s\n' "$SHOWCASE_DEMO_PASSWORD" > "$CREDENTIAL_FILE"
chmod 0600 "$CREDENTIAL_FILE"
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/blacksoil-b01.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/blacksoil-b02.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/blacksoil-showcase-worker.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/blacksoil-showcase-reset.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/blacksoil-showcase-reset.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl disable --now blacksoil-showcase-worker.service blacksoil-showcase-reset.timer >/dev/null 2>&1 || true
echo "固定演示数据库已建立；账号密码仅保存在 $CREDENTIAL_FILE，演示 Worker 与恢复 timer 仍保持停用"
