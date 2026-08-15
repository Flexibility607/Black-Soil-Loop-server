#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 执行演示数据库迁移" >&2
  exit 1
fi

RELEASE_ROOT="${1:-/srv/black-soil-loop/current}"
SHOWCASE_ENV="/etc/black-soil-loop/showcase.env"
[[ -f "$SHOWCASE_ENV" ]] || { echo "演示数据集尚未配置，跳过迁移"; exit 0; }

(
  cd "$RELEASE_ROOT"
  runuser -u postgres -- env \
    DATABASE_URL=postgresql+psycopg:///black_soil_loop_showcase \
    WORKER_DATABASE_URL=postgresql+psycopg:///black_soil_loop_showcase \
    SHOWCASE_DATABASE_URL=postgresql+psycopg:///black_soil_loop_showcase \
    SERVICE_ROLE=migration \
    DATASET_ROLE=showcase \
    PGOPTIONS='-c role=blacksoil_showcase_owner' \
    .venv/bin/alembic -c alembic.ini upgrade head
)
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -f "$RELEASE_ROOT/deploy/postgres/04-grant-showcase.sql"
