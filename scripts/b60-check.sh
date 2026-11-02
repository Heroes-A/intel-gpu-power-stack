#!/usr/bin/env bash
# =============================================================================
# b60-check.sh —— 新容器接入 B60 后的一条命令自检
#
#   b60-check.sh <容器名>          检查该容器是否真的拿到了 B60 并可用
#   b60-check.sh --host            只检查宿主机（/dev/b60 与 /dev/dri 现状）
#
# 检查项：
#   1. 宿主机真卡节点 /dev/b60/render 存在且设备号与 B60_RENDER_DEVS 一致
#   2. 容器把 /dev/b60/render 映射进来（HostConfig.Devices 有该路径）
#   3. 容器内 /dev/dri 下能看到 render 节点
#   4. DRM_IOCTL_VERSION 能打开并读回驱动名（xe = 真卡；无 python3 时降级为「能打开」）
#   5. /api/status 里能看到该容器持卡（仅提示，不判失败：空闲进程可以不持卡）
#
# 退出码：0 = 全绿；1 = 有失败项
# =============================================================================
set -uo pipefail

API="${B60_API:-http://127.0.0.1:41966}"
REAL_NODE="${B60_REAL_NODE:-/dev/b60/render}"
REAL_DEVS="${B60_RENDER_DEVS:-226:129}"
DRI_NODE="${B60_DRI_NODE:-/dev/dri/renderD129}"
FAIL=0
OK=0

# 226:129 <-> e2:81：容器内若只有 stat(1)，读到的是十六进制，两种写法都认
REAL_HEX="$(printf '%x:%x' "${REAL_DEVS%%:*}" "${REAL_DEVS##*:}" 2>/dev/null || echo '')"

c_ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; OK=$((OK+1)); }
c_bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
c_warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
c_head() { printf '\n\033[1m%s\033[0m\n' "$*"; }

devnum() {
  if stat -Lc '%t:%T' "$1" >/dev/null 2>&1; then
    local h; h=$(stat -Lc '%t:%T' "$1")
    printf '%s:%s' "$((16#${h%%:*}))" "$((16#${h##*:}))"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import os,sys;s=os.stat(sys.argv[1]).st_rdev;print("%d:%d"%(os.major(s),os.minor(s)))' "$1"
  else
    printf 'unknown'
  fi
}

# DRM_IOCTL_VERSION 探针（DRM_IOCTL_VERSION = 0xC0406400）
write_probe() {
  cat >"$1" <<'PY'
import ctypes, fcntl, os, sys

DRM_IOCTL_VERSION = 0xC0406400

class drm_version(ctypes.Structure):
    _fields_ = [("version_major", ctypes.c_int),
                ("version_minor", ctypes.c_int),
                ("version_patchlevel", ctypes.c_int),
                ("name_len", ctypes.c_size_t),
                ("name", ctypes.c_void_p),
                ("date_len", ctypes.c_size_t),
                ("date", ctypes.c_void_p),
                ("desc_len", ctypes.c_size_t),
                ("desc", ctypes.c_void_p)]

path = sys.argv[1]
try:
    fd = os.open(path, os.O_RDWR)
except OSError as e:
    print("OPENFAIL %s (%s)" % (path, e.strerror)); sys.exit(1)

try:
    v = drm_version()
    nbuf = ctypes.create_string_buffer(256)
    dbuf = ctypes.create_string_buffer(256)
    sbuf = ctypes.create_string_buffer(512)
    v.name_len, v.name = 256, ctypes.cast(nbuf, ctypes.c_void_p)
    v.date_len, v.date = 256, ctypes.cast(dbuf, ctypes.c_void_p)
    v.desc_len, v.desc = 512, ctypes.cast(sbuf, ctypes.c_void_p)
    fcntl.ioctl(fd, DRM_IOCTL_VERSION, v, True)
    print("OK %d.%d.%d name=%s date=%s" % (
        v.version_major, v.version_minor, v.version_patchlevel,
        nbuf.value.decode(errors="replace"),
        dbuf.value.decode(errors="replace")))
except OSError as e:
    print("IOCTLFAIL %s" % e); sys.exit(1)
finally:
    os.close(fd)
PY
}

usage() { sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

MODE=""
CONT=""
case "${1:-}" in
  ""|-h|--help) usage ;;
  --host) MODE=host ;;
  *)      MODE=container; CONT="$1" ;;
esac

PROBE="$(mktemp -t b60probe.XXXXXX.py)"
trap 'rm -f "$PROBE"' EXIT
write_probe "$PROBE"

# ---------------------------------------------------------------- 1. 宿主机 --
c_head "1. 宿主机真卡节点"
if [ -e "$REAL_NODE" ]; then
  d=$(devnum "$REAL_NODE")
  if [ "$d" = "$REAL_DEVS" ]; then c_ok "$REAL_NODE 存在，设备号 $d"
  elif [ "$d" = "unknown" ]; then c_warn "$REAL_NODE 存在，设备号未知（stat 不可用）"
  else c_bad "$REAL_NODE 设备号 $d ≠ ${REAL_DEVS}，不是 B60"; fi
else
  c_bad "$REAL_NODE 不存在——b60-guard 未就位，先看面板 $API/"
fi

pol=$(curl -sS -m 8 "$API/api/status" 2>/dev/null \
      | sed -n 's/.*"dispatch"[^}]*"policy"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' | head -1)
if [ -n "$pol" ]; then
  if [ -e "$DRI_NODE" ]; then vis="已暴露"; else vis="已掩藏"; fi
  c_ok "b60-guard 在线，策略=${pol}，/dev/dri 中 B60 节点=$vis"
else
  c_warn "读不到 $API/api/status（容器检查仍可继续）"
fi

if [ "$MODE" = "host" ]; then
  c_head "2. 宿主机直接打开真卡"
  out=$(python3 "$PROBE" "$REAL_NODE" 2>&1 || true)
  case "$out" in
    OK*) c_ok "$out" ;;
    *)   c_bad "$out" ;;
  esac
  printf '\n'
  [ "$FAIL" -eq 0 ] && { printf '\033[32m全部通过\033[0m（%d 项）\n' "$OK"; exit 0; }
  printf '\033[31m有 %d 项失败\033[0m\n' "$FAIL"; exit 1
fi

# ---------------------------------------------------------------- 2. 容器映射 -
c_head "2. 容器 $CONT 的设备映射"
command -v docker >/dev/null 2>&1 || { c_bad "本机没有 docker 命令"; exit 1; }
if docker inspect "$CONT" >/dev/null 2>&1; then
  c_ok "容器存在"
else
  c_bad "容器 $CONT 不存在"; printf '\n有 %d 项失败\n' "$FAIL"; exit 1
fi

map=$(docker inspect -f '{{range .HostConfig.Devices}}{{.PathOnHost}}:{{.PathInContainer}} {{end}}' "$CONT" 2>/dev/null)
binds=$(docker inspect -f '{{range .HostConfig.Binds}}{{.}} {{end}}' "$CONT" 2>/dev/null)
case "$map" in
  *"$REAL_NODE"*) c_ok "已映射真卡（devices）：$map" ;;
  *"/dev/dri"*)   c_bad "映射的是宿主机 /dev/dri 而非 ${REAL_NODE}：$map
      掩藏态下 /dev/dri 里没有 B60，容器看到的是核显 → 改成 /dev/b60/render" ;;
  *)
    case "$binds" in
      *"$REAL_NODE"*) c_warn "用 -v 绑定了真卡：${binds}（能用，但推荐 devices 写法）" ;;
      *"/dev/dri"*)   c_bad "用 -v 绑定了宿主机 /dev/dri：${binds}
      掩藏态下这里只有核显 → 改成 /dev/b60/render" ;;
      *)              c_bad "没有映射任何 GPU 设备（devices=${map:-<空>} binds=${binds:-<空>}）" ;;
    esac ;;
esac

# ---------------------------------------------------------------- 3. 容器内节点
c_head "3. 容器内 /dev/dri"
if docker exec "$CONT" true >/dev/null 2>&1; then
  nodes=$(docker exec "$CONT" sh -c 'ls -l /dev/dri 2>/dev/null' 2>&1 || true)
  if [ -n "$nodes" ]; then printf '%s\n' "$nodes" | sed 's/^/      /'; fi
  rnode=$(docker exec "$CONT" sh -c 'ls /dev/dri/renderD* 2>/dev/null | head -1' 2>&1 || true)
  if [ -n "$rnode" ]; then
    c_ok "容器内 render 节点：$rnode"
    # 关键判据：容器内那个节点的设备号必须与 B60 一致，否则拿到的是核显
    rd=$(docker exec "$CONT" sh -c "stat -Lc '%t:%T' '$rnode' 2>/dev/null || echo unknown" 2>/dev/null)
    case "$rd" in
      "$REAL_HEX"|"$REAL_DEVS") c_ok "容器内设备号 $rd = B60 真卡" ;;
      unknown)       c_warn "容器内 stat 不可用，跳过设备号校验" ;;
      *)             c_bad "容器内设备号 $rd ≠ ${REAL_DEVS} —— 拿到的是核显，不是 B60" ;;
    esac
  else c_bad "容器内 /dev/dri 下没有 render 节点"; fi
else
  c_warn "无法在容器内执行命令（无 shell 或已退出），跳过容器内检查"
  rnode=""
fi

# ---------------------------------------------------------------- 4. ioctl --
c_head "4. DRM_IOCTL_VERSION（能否真正打开设备）"
if [ -n "$rnode" ] && docker exec "$CONT" sh -c 'command -v python3 >/dev/null 2>&1'; then
  out=$(docker exec -i "$CONT" python3 - "$rnode" <"$PROBE" 2>&1 || true)
  case "$out" in
    OK*xe*) c_ok "容器内读回：$out" ;;
    OK*)    c_warn "容器内读回：${out}（驱动名非 xe，确认是不是核显）" ;;
    *)      c_bad "容器内失败：$out" ;;
  esac
elif [ -n "$rnode" ]; then
  c_warn "容器内没有 python3，改在宿主机对 $REAL_NODE 做同一探测"
  out=$(python3 "$PROBE" "$REAL_NODE" 2>&1 || true)
  case "$out" in
    OK*xe*) c_ok "宿主机读回：$out" ;;
    *)      c_bad "宿主机探测失败：$out" ;;
  esac
fi

# ---------------------------------------------------------------- 5. 面板可见性
c_head "5. 面板持卡记录（仅供参考）"
if curl -sS -m 8 "$API/api/status" 2>/dev/null | grep -q "$CONT"; then
  c_ok "面板 /api/status 中出现 $CONT"
else
  c_warn "面板暂时没有 $CONT 的持卡记录（空闲进程可能不持卡，跑一个推理/转码再看）"
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32m全部通过\033[0m（%d 项）—— 这个容器接对了\n' "$OK"; exit 0
fi
printf '\033[31m有 %d 项失败\033[0m（通过 %d 项）—— 见上级提示\n' "$FAIL" "$OK"; exit 1
