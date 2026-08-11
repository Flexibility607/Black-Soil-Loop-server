#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 执行首次密钥与数据库配置" >&2
  exit 1
fi
if [[ $# -ne 1 ]]; then
  echo "用法: $0 <服务器源码目录>" >&2
  exit 2
fi

SOURCE_ROOT="$(realpath "$1")"
ROLE_SQL="$SOURCE_ROOT/deploy/postgres/01-create-roles.sql"
ENV_FILE="/etc/black-soil-loop/backend.env"
CREDENTIAL_FILE="/root/black-soil-loop-initial-credentials"
[[ -f "$ROLE_SQL" ]] || { echo "缺少数据库角色脚本" >&2; exit 2; }

if [[ -f "$ENV_FILE" ]] && ! grep -Fq 'CHANGE_ME' "$ENV_FILE"; then
  echo "生产环境变量已配置，拒绝覆盖" >&2
  exit 3
fi

random_hex() {
  openssl rand -hex "$1"
}

B01_PASSWORD="$(random_hex 24)"
B02_PASSWORD="$(random_hex 24)"
WORKER_PASSWORD="$(random_hex 24)"
JWT_SECRET="$(random_hex 32)"
DEVICE_API_KEY="$(random_hex 32)"
BACKUP_ENCRYPTION_KEY="$(random_hex 32)"
DEMO_SEED_PASSWORD="$(random_hex 16)"
VOICE_RATE_LIMIT_HMAC_SECRET="$(random_hex 32)"

runuser -u postgres -- psql \
  -v b01_password="$B01_PASSWORD" \
  -v b02_password="$B02_PASSWORD" \
  -v worker_password="$WORKER_PASSWORD" < "$ROLE_SQL"

umask 0077
TEMP_ENV="$(mktemp /etc/black-soil-loop/backend.env.XXXXXXXX)"
trap 'rm -f -- "$TEMP_ENV"' EXIT
cat > "$TEMP_ENV" <<EOF
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://blacksoil_worker:${WORKER_PASSWORD}@127.0.0.1:5432/black_soil_loop
B01_DATABASE_URL=postgresql+psycopg://blacksoil_b01:${B01_PASSWORD}@127.0.0.1:5432/black_soil_loop
B02_DATABASE_URL=postgresql+psycopg://blacksoil_b02:${B02_PASSWORD}@127.0.0.1:5432/black_soil_loop
WORKER_DATABASE_URL=postgresql+psycopg://blacksoil_worker:${WORKER_PASSWORD}@127.0.0.1:5432/black_soil_loop
BACKUP_DATABASE_URL=postgresql://blacksoil_worker:${WORKER_PASSWORD}@127.0.0.1:5432/black_soil_loop
JWT_SECRET=${JWT_SECRET}
DEVICE_API_KEY=${DEVICE_API_KEY}
BACKUP_ENCRYPTION_KEY=${BACKUP_ENCRYPTION_KEY}
BACKUP_REMOTE=
ACCESS_TOKEN_MINUTES=15
REFRESH_TOKEN_DAYS=7
IDLE_TIMEOUT_MINUTES=30
CORS_ORIGINS=https://loop.flexibility607.cn,https://demo.flexibility607.cn
PUBLIC_BASE_URL=https://api.flexibility607.cn
COOKIE_DOMAIN=.flexibility607.cn
UPLOAD_DIR=/var/lib/black-soil-loop/uploads
UPLOAD_MAX_BYTES=10485760
OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
VOICE_ASSISTANT_ENABLED=false
VOICE_PUBLIC_ENABLED=false
ASSISTANT_PUBLIC_DB_QUOTA_ENABLED=false
VOICE_STT_PROVIDER=aliyun
ALIYUN_NLS_ACCESS_KEY_ID=
ALIYUN_NLS_ACCESS_KEY_SECRET=
ALIYUN_NLS_APP_KEY=
ALIYUN_NLS_ENDPOINT=https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr
ALIYUN_NLS_VOCABULARY_ID=
VOICE_MAX_SECONDS=30
VOICE_MAX_BYTES=2097152
VOICE_PUBLIC_PER_MINUTE=3
VOICE_PUBLIC_PER_HOUR=20
VOICE_PUBLIC_PER_DAY=50
VOICE_PUBLIC_TEXT_PER_MINUTE=10
VOICE_PUBLIC_TEXT_PER_DAY=200
VOICE_GLOBAL_PER_DAY=500
VOICE_MAX_CONCURRENCY=2
VOICE_LEASE_SECONDS=60
VOICE_RATE_LIMIT_HMAC_SECRET=${VOICE_RATE_LIMIT_HMAC_SECRET}
RATE_LIMIT_PER_MINUTE=120
AUTH_RATE_LIMIT_PER_MINUTE=20
DEVICE_RATE_LIMIT_PER_MINUTE=600
WECHAT_APP_ID=
WECHAT_APP_SECRET=
DEMO_SEED_PASSWORD=${DEMO_SEED_PASSWORD}
EOF
chown root:blacksoil "$TEMP_ENV"
chmod 0640 "$TEMP_ENV"
mv -f -- "$TEMP_ENV" "$ENV_FILE"
trap - EXIT

install -d -o blacksoil -g blacksoil -m 0750 /var/lib/black-soil-loop/uploads
printf 'admin_password=%s\n' "$DEMO_SEED_PASSWORD" > "$CREDENTIAL_FILE"
chmod 0600 "$CREDENTIAL_FILE"

echo "首次配置完成；演示管理员密码仅保存在 $CREDENTIAL_FILE"
