#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 执行主机初始化" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWAP_FILE="/var/lib/black-soil-loop/swapfile"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  nginx postgresql postgresql-client python3 python3-venv python3-pip \
  certbot python3-certbot-nginx openssl curl

getent group blacksoil >/dev/null || groupadd --system blacksoil
id -u blacksoil >/dev/null 2>&1 || useradd --system --gid blacksoil --home /var/lib/black-soil-loop --shell /usr/sbin/nologin blacksoil
id -u blacksoil-deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --groups blacksoil blacksoil-deploy

install -d -o blacksoil-deploy -g blacksoil -m 2775 /srv/black-soil-loop/releases
install -d -o blacksoil -g blacksoil -m 0750 /var/lib/black-soil-loop /var/lib/black-soil-loop/backups
install -d -o root -g blacksoil -m 0750 /etc/black-soil-loop
install -d -o www-data -g www-data -m 0755 /var/www/letsencrypt
install -d -o blacksoil-deploy -g blacksoil -m 2775 /srv/black-soil-loop-web/releases

if ! swapon --show=NAME --noheadings | grep -Fxq "$SWAP_FILE"; then
  if [[ ! -f "$SWAP_FILE" ]]; then
    fallocate -l 2G "$SWAP_FILE"
    chmod 0600 "$SWAP_FILE"
    mkswap "$SWAP_FILE"
  fi
  swapon "$SWAP_FILE"
fi
grep -Fq "$SWAP_FILE none swap sw 0 0" /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab

if [[ ! -f /etc/black-soil-loop/backend.env ]]; then
  install -o root -g blacksoil -m 0640 "$SCRIPT_DIR/backend.env.example" /etc/black-soil-loop/backend.env
fi

install -o root -g root -m 0644 "$SCRIPT_DIR"/systemd/*.service /etc/systemd/system/
install -o root -g root -m 0644 "$SCRIPT_DIR"/systemd/*.timer /etc/systemd/system/
install -o root -g root -m 0644 "$SCRIPT_DIR/nginx/black-soil-loop.bootstrap.conf" /etc/nginx/sites-available/black-soil-loop
ln -sfn /etc/nginx/sites-available/black-soil-loop /etc/nginx/sites-enabled/black-soil-loop

nginx -t
systemctl daemon-reload
systemctl enable blacksoil-b01.service blacksoil-b02.service blacksoil-worker.service
systemctl enable blacksoil-scheduler.timer blacksoil-backup.timer blacksoil-restore-verify.timer

echo "主机基础准备完成。请填写 /etc/black-soil-loop/backend.env，再建立 PostgreSQL 角色和 TLS 证书。"
