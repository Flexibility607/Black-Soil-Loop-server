#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "用法: $0 <commit> <artifact.tar.gz>" >&2
  exit 2
fi

COMMIT="$1"
ARTIFACT="$(realpath "$2")"
RELEASE_ROOT="/srv/black-soil-loop/releases"
RELEASE_DIR="$RELEASE_ROOT/$COMMIT"
CURRENT_LINK="/srv/black-soil-loop/current"
ENV_FILE="/etc/black-soil-loop/backend.env"

[[ "$COMMIT" =~ ^[0-9A-Za-z._-]{7,64}$ ]] || { echo "非法发布编号" >&2; exit 2; }
[[ -f "$ARTIFACT" ]] || { echo "发布制品不存在" >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "生产环境变量文件不存在" >&2; exit 2; }
[[ ! -e "$RELEASE_DIR" ]] || { echo "该 release 已存在，拒绝覆盖" >&2; exit 2; }

PREVIOUS=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS="$(readlink -f "$CURRENT_LINK")"
fi

install -d -o blacksoil-deploy -g blacksoil -m 2775 "$RELEASE_DIR"
tar -xzf "$ARTIFACT" -C "$RELEASE_DIR"
python3 -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/pip" install --disable-pip-version-check "$RELEASE_DIR"
chmod +x "$RELEASE_DIR"/deploy/*.sh
chown -R blacksoil-deploy:blacksoil "$RELEASE_DIR"

set -a
source "$ENV_FILE"
set +a
"$RELEASE_DIR/deploy/backup.sh"
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -f "$RELEASE_DIR/deploy/postgres/00-normalize-ownership.sql"
(
  cd "$RELEASE_DIR"
  runuser -u postgres -- env \
    DATABASE_URL=postgresql+psycopg:///black_soil_loop \
    WORKER_DATABASE_URL=postgresql+psycopg:///black_soil_loop \
    SERVICE_ROLE=migration \
    .venv/bin/alembic -c alembic.ini upgrade head
)
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -f "$RELEASE_DIR/deploy/postgres/02-grant-permissions.sql"
if [[ -f /etc/black-soil-loop/showcase.env ]]; then
  "$RELEASE_DIR/deploy/migrate-showcase.sh" "$RELEASE_DIR"
fi

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
systemctl restart blacksoil-b01.service blacksoil-b02.service blacksoil-worker.service
if [[ -f /etc/black-soil-loop/showcase.env ]] && grep -Eq '^SHOWCASE_DATASET_ENABLED=(true|1|yes)$' /etc/black-soil-loop/showcase.env; then
  systemctl restart blacksoil-showcase-worker.service
  systemctl is-active --quiet blacksoil-showcase-worker.service
else
  systemctl stop blacksoil-showcase-worker.service >/dev/null 2>&1 || true
fi

HEALTHY=false
for _ in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8101/health/b01 >/dev/null && \
     curl --fail --silent http://127.0.0.1:8102/health/b02 >/dev/null; then
    HEALTHY=true
    break
  fi
  sleep 1
done

if [[ "$HEALTHY" != true ]]; then
  if [[ -n "$PREVIOUS" && "$PREVIOUS" == "$RELEASE_ROOT"/* ]]; then
    ln -sfn "$PREVIOUS" "$CURRENT_LINK"
    systemctl restart blacksoil-b01.service blacksoil-b02.service blacksoil-worker.service
  fi
  echo "新版本健康检查失败，代码已切回上一 release；数据库需要向前修复" >&2
  exit 1
fi

echo "发布成功: $COMMIT"
