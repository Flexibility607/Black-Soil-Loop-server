#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "用法: $0 <版本号> <web-dist.tar.gz>" >&2
  exit 2
fi

VERSION="$1"
ARTIFACT="$(realpath "$2")"
RELEASE_ROOT="/srv/black-soil-loop-web/releases"
RELEASE_DIR="$RELEASE_ROOT/$VERSION"
CURRENT_LINK="/srv/black-soil-loop-web/current"

[[ "$VERSION" =~ ^[0-9A-Za-z._-]{5,64}$ ]] || { echo "非法版本号" >&2; exit 2; }
[[ -f "$ARTIFACT" ]] || { echo "网页制品不存在" >&2; exit 2; }
[[ ! -e "$RELEASE_DIR" ]] || { echo "该网页 release 已存在，拒绝覆盖" >&2; exit 2; }

PREVIOUS=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS="$(readlink -f "$CURRENT_LINK")"
fi

install -d -o blacksoil-deploy -g blacksoil -m 0755 "$RELEASE_DIR"
tar -xzf "$ARTIFACT" -C "$RELEASE_DIR"
[[ -f "$RELEASE_DIR/index.html" ]] || { echo "网页制品缺少 index.html" >&2; exit 3; }
[[ -f "$RELEASE_DIR/runtime-config.js" ]] || { echo "网页制品缺少 runtime-config.js" >&2; exit 3; }
[[ ! -d "$RELEASE_DIR/mock" ]] || { echo "生产网页制品禁止包含 Mock" >&2; exit 3; }
grep -Fq "https://api.flexibility607.cn/api/v1" "$RELEASE_DIR/runtime-config.js" || {
  echo "网页制品未连接生产 API" >&2
  exit 3
}
chown -R blacksoil-deploy:blacksoil "$RELEASE_DIR"
find "$RELEASE_DIR" -type d -exec chmod 0755 {} +
find "$RELEASE_DIR" -type f -exec chmod 0644 {} +

ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
if ! curl --fail --silent --show-error \
  --resolve demo.flexibility607.cn:443:127.0.0.1 \
  https://demo.flexibility607.cn/ >/dev/null; then
  if [[ -n "$PREVIOUS" && "$PREVIOUS" == "$RELEASE_ROOT"/* ]]; then
    ln -sfn "$PREVIOUS" "$CURRENT_LINK"
  fi
  echo "网页健康检查失败，已恢复上一 release" >&2
  exit 1
fi

echo "网页发布成功: $VERSION"
