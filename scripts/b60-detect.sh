#!/usr/bin/env bash
# =============================================================================
# b60-detect.sh —— 平台体检：把这台机器上跟 B60 低功耗相关的全部事实打出来
#
#   sudo bash scripts/b60-detect.sh
#
# 输出即配置：BDF / render 节点名 / 设备号 / hwmon 路径 / ASPM 现状 / L1 驻留。
# 把这里读到的值填进 B60_BDF、B60_RENDER_DEVS、B60_DRI_NODE 即可。
# 只读，不改任何东西。
# =============================================================================
set -uo pipefail

BDF="${B60_BDF:-}"
say() { printf '%s\n' "$*"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---- 1. 找到 B60 -------------------------------------------------------------
head_ "1. B60 设备"
if [ -z "$BDF" ]; then
  if command -v lspci >/dev/null 2>&1; then
    BDF=$(lspci -Dn 2>/dev/null | awk 'tolower($0) ~ /vga|display|3d/ && $0 ~ /8086/ {print $1; exit}')
  fi
fi
if [ -z "$BDF" ]; then
  say "  未找到 Intel 显示设备，请手工指定：B60_BDF=0000:03:00.0 bash $0"
  exit 1
fi
say "  BDF        : $BDF"
[ -r "/sys/bus/pci/devices/$BDF/vendor" ] && \
  say "  vendor:dev : $(cat "/sys/bus/pci/devices/$BDF/vendor") / $(cat "/sys/bus/pci/devices/$BDF/device")"
[ -e "/sys/bus/pci/devices/$BDF/driver" ] && \
  say "  驱动       : $(basename "$(readlink -f "/sys/bus/pci/devices/$BDF/driver")")"

# ---- 2. DRM 节点 -------------------------------------------------------------
head_ "2. DRM 节点"
CARD=""; RENDER=""
for d in /sys/class/drm/*/device; do
  [ -e "$d" ] || continue
  [ "$(basename "$(readlink -f "$d")")" = "$BDF" ] || continue
  node=$(basename "$(dirname "$d")")
  case "$node" in
    card[0-9]*)   CARD="$node" ;;
    renderD[0-9]*) RENDER="$node" ;;
  esac
done
say "  kernel 名  : card=${CARD:-<none>}  render=${RENDER:-<none>}"
for p in "/dev/dri/$CARD" "/dev/dri/$RENDER" "/dev/b60/card" "/dev/b60/render"; do
  [ -e "$p" ] || continue
  n=$(stat -Lc '%t:%T' "$p" 2>/dev/null || echo '??')
  say "  $p -> 设备号 $(printf '%d:%d' "0x${n%%:*}" "0x${n##*:}" 2>/dev/null || echo "$n")"
done
if [ -n "$RENDER" ]; then
  h=$(stat -Lc '%t:%T' "/dev/dri/$RENDER" 2>/dev/null || true)
  [ -n "$h" ] && say "  → B60_RENDER_DEVS=$(printf '%d:%d' "0x${h%%:*}" "0x${h##*:}")  B60_DRI_NODE=/dev/dri/$RENDER"
fi

# ---- 3. PCIe ASPM ------------------------------------------------------------
head_ "3. PCIe ASPM（根端口与卡上 switch）"
if command -v lspci >/dev/null 2>&1; then
  # 根端口 = sysfs 路径里 pci0000:xx 之后的第一跳（卡内 switch 会再往下一层）
  link=$(readlink -f "/sys/bus/pci/devices/$BDF" 2>/dev/null || true)
  root=$(printf '%s\n' "$link" | tr '/' '\n' | awk '/^pci[0-9a-f]+:/{getline; print; exit}')
  for d in $root "$BDF"; do
    [ -n "$d" ] || continue
    say "  --- $d"
    lspci -s "$d" -vv 2>/dev/null | grep -E "LnkCap:|LnkCtl:" | sed 's/^/      /'
  done
fi
say "  dmesg FADT : $( (dmesg 2>/dev/null || true) | grep -i 'FADT.*ASPM' | tail -1 )"
say "  说明：Native ASPM=Disabled 时由 BIOS 管链路，FADT 声明“不支持”属正常，"
say "        真正的判据是 LnkCap 是否有 ASPM L1、L1 驻留是否增长。"

# ---- 4. 封装态与 L1 驻留 -----------------------------------------------------
head_ "4. 链路驻留（需要 root）"
RES="/sys/kernel/debug/dri/$BDF/dgfx_pcie_link_residencies"
PKG="/sys/kernel/debug/dri/$BDF/dgfx_pkg_residencies"
if [ -r "$RES" ]; then
  cat "$RES"; sleep 5; say "  --- 5s 后"; cat "$RES"
else
  say "  读不到 $RES（需要 root，或内核未开 debugfs / 非 xe 驱动）"
fi
[ -r "$PKG" ] && { say "  --- pkg"; cat "$PKG"; }

# ---- 5. 功耗 -----------------------------------------------------------------
head_ "5. 功耗读数（hwmon，能量计数器差分）"
HWM=""
for h in /sys/class/hwmon/hwmon*; do
  [ -r "$h/energy1_input" ] || continue
  HWM="$h"; break
done
if [ -n "$HWM" ]; then
  say "  hwmon      : $HWM ($(cat "$HWM/name" 2>/dev/null))"
  a=$(cat "$HWM/energy1_input"); b=$(cat "$HWM/energy2_input" 2>/dev/null || echo 0)
  sleep 20
  a2=$(cat "$HWM/energy1_input"); b2=$(cat "$HWM/energy2_input" 2>/dev/null || echo 0)
  awk -v d1="$((a2-a))" -v d2="$((b2-b))" 'BEGIN{
    printf "  card       : %.2f W\n", d1/20e6;
    if (d2 > 0) printf "  pkg        : %.2f W\n", d2/20e6;
  }'
  say "  注意：无人持卡时高频读 hwmon 会把卡唤醒（实测 12W → 31W），别用 0.5s 轮询测待机。"
else
  say "  没有可用的 energy*_input（需要 xe 驱动 + 支持的 hwmon）"
fi

# ---- 6. 结论 -----------------------------------------------------------------
head_ "6. 建议写入的环境变量"
say "  B60_BDF=${BDF}"
[ -n "$RENDER" ] && say "  B60_DRI_NODE=/dev/dri/${RENDER}"
[ -n "${h:-}" ] && say "  B60_RENDER_DEVS=$(printf '%d:%d' "0x${h%%:*}" "0x${h##*:}")"
say ""
