#!/usr/bin/env bash
# B60 Power Guard 部署脚本（通用 systemd Linux，Debian 12 系已验证）
# 用法: bash deploy.sh            # 默认端口 41966，可用 PORT=xxxx 覆盖
# 回滚: docker rm -f b60-guard（掩藏状态不受影响；如需还原节点见 WebUI 开关）
set -euo pipefail

NAME=b60-guard
IMAGE=b60-guard:1.3
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # 仓库根目录（src/ 与 state.json 的父目录）
PORT=${PORT:-41966}
TZ_NAME=${TZ_NAME:-$(cat /etc/timezone 2>/dev/null || echo UTC)}

log() { echo "==> $*"; }
die() { echo "!! $*" >&2; exit 1; }

log "1/4 构建镜像（SKIP_BUILD=1 可跳过）"
if [ "${SKIP_BUILD:-0}" = "1" ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "跳过构建，复用 $IMAGE"
else
  docker build -t "$IMAGE" "$DIR/src"
fi
command -v nsenter >/dev/null 2>&1 || log "提示: 宿主机无 nsenter 则容器内需 util-linux（镜像已含）"

log "2/4 状态文件与数据目录"
touch "$DIR/state.json"
[ -s "$DIR/state.json" ] || echo '{"mask_enabled": true}' > "$DIR/state.json"
mkdir -p "$DIR/data"

log "3/4 启动容器"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --privileged --pid=host \
  -v /dev:/dev \
  -v /sys:/sys:ro \
  -v "$DIR/state.json:/app/state.json" \
  -v "$DIR/data:/app/data" \
  -e "TZ=$TZ_NAME" \
  -e RECORD_INTERVAL=15 \
  -e RETENTION_DAYS=30 \
  -p "$PORT:8000" \
  "$IMAGE"

log "4/4 就绪验证"
sleep 6
curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && echo "  healthz ✓"
curl -s "http://127.0.0.1:$PORT/api/status" | head -c 400; echo
echo
echo "==> 完成。面板: http://127.0.0.1:$PORT/ （局域网 http://<本机IP>:$PORT/ ）"
echo "    容器自启时自动应用 state.json 里的期望掩藏状态 → 兼作开机持久化"
