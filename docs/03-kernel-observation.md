# 03 · 内核侧观测

> English: [03 · Kernel-side observation](en/03-kernel-observation.md)

> 这一篇只讲"怎么看到真相"。
> 后面所有判断都建立在这几个文件上；读错它们会得出完全相反的结论。

## 1. 链路驻留：`dgfx_pcie_link_residencies`

```bash
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies
```

输出形如：

```
L1 RESIDENCY: 1980ms   L1 ENTRY: 42   ...
```

- **L1 RESIDENCY 是最重要的单一指标**：它直接告诉你链路有多少时间真的睡在 L1。
- 读法：间隔 10 秒读两次，比较增量。健康值 ≥90%。
- 恒为 0 说明链路一次都没进过 L1——要么 BIOS 没开能力，要么有人一直在访问链路。
- 这是 `xe` 驱动的 debugfs 节点；`i915` 下路径不同，本项目未验证。

## 2. 封装电源态：`dgfx_pkg_residencies`

```bash
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pkg_residencies
```

反映 GPU 封装在各电源态（G2 / G8 等）的停留时间。
链路进了 L1 但功耗仍高，通常要看这里——封装态没跟上，功耗就下不来。

## 3. 功耗：hwmon 能量计数器

```bash
ls /sys/class/hwmon/hwmon*/energy1_input
# energy1 = 整卡，energy2 = pkg（单位 µJ，更新粒度约 100 ms）

A=$(cat /sys/class/hwmon/hwmon4/energy1_input); sleep 20
B=$(cat /sys/class/hwmon/hwmon4/energy1_input)
awk "BEGIN{printf \"%.2f W\n\", ($B-$A)/20e6}"
```

- **必须做区间差分**，不要拿单次读数当功率。
- 采样窗 ≥15 s 才稳定；0.5 s 窗看到的是噪声（见 [07](07-measurement.md)）。
- ⚠️ **无人持卡时别高频读**：每 0.5 s 读一次会把设备从挂起态反复唤醒，实测 12 W → 31 W。

## 4. 谁在用 GPU：DRM 客户端与 fdinfo

```bash
# 当前打开该 DRM 设备的进程
sudo ls -l /proc/*/fd 2>/dev/null | grep -i 'dri/'
# 或直接看 debugfs
sudo cat /sys/kernel/debug/dri/0000:03:00.0/clients
```

判断"是否在真干活"，看 fdinfo 里的引擎占用：

```bash
sudo cat /proc/<pid>/fdinfo/<fd> | grep -E 'drm-engine|drm-client-id'
```

- 所有 `drm-engine-*` 增量为 0 → 进程只是 `open()` 着卡，**没有计算负载**。
- 这也是本项目"持卡 ≠ 唤醒"结论的原始证据。

`b60-guard` 的消费者画像就是这条链路：`pid` → `/proc/<pid>/cgroup` → 容器名 / systemd 单元 → 引擎占用。

## 5. runtime PM 状态

```bash
cat /sys/bus/pci/devices/0000:03:00.0/power/runtime_status     # suspended / active
cat /sys/bus/pci/devices/0000:03:00.0/power/runtime_usage      # 0 = 没人持卡
```

- `runtime_usage == 0` 且 `runtime_status == suspended` → 卡在挂起态，此时**任何**对 hwmon 的读都会唤醒它。
- `b60-guard` 的自适应采样就是拿 `runtime_usage` 当开关：有人持卡 2 s、无人 15 s。

## 6. 常见误读

| 误读 | 实际含义 |
|---|---|
| 瞬时功耗 0 W 或 43 W 来回跳 = 省电失效 | 亚秒级电源态自激振荡，看 15 s 均值才是真相 |
| L1 驻留 99% 但功耗 30 W | 链路睡了，但封装域没睡；或被监控高频唤醒 |
| `FADT ... doesn't support PCIe ASPM` 还在 | Native ASPM=Disabled 时的正常现象，判据看 `LnkCap` |
| 持卡进程存在 = 唤醒 | 引擎占用为 0 时不影响 L1 |

---

← [02 BIOS / ASPM](02-bios-aspm.md) · 下一篇：[04 节点掩藏](04-node-masking.md) →
