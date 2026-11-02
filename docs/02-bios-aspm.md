# 02 · BIOS / ASPM 设置

> English: [02 · BIOS / ASPM settings](en/02-bios-aspm.md)

> 目标：让 CPU 直连根端口具备并启用 PCIe ASPM L1，使整卡空载从 50.3 W 降到 42 W 量级。
> 这一步是本项目的**前置条件**——不做这步，后面的节点掩藏只能拿到很小一部分收益。

## 0. 三个会要命的坑

1. **F10 保存退出后机器会黑屏挂死——这不是失败。**
   Battlemage 显卡热复位后的链路重训练窗口不足，任何**软复位**（F10 save & exit、OS `reboot`）
   都会黑屏挂死。标准流程是：F10 → 屏幕黑掉无反应（**预期**）→ 断电 ≥10 秒 → 上电（冷启动）→ 正常进系统。
   如果你用 KVM/智能插座，Power Off → 等 10 s → Power On。
2. **一次只改一项。** 改一项 → 保存 → 冷启 → 验证 → 再改下一项。否则出问题无法归因。
3. **先设"来电自启"，再动其他任何项。** 这样即使后续设置导致起不来，远程断电再上电还有一次自恢复机会。

### 禁手清单

违反任意一条，可能需要物理 Clear CMOS 才能救回来。

| 禁止项 | 原因 |
|---|---|
| L1 Substates / L1.1 / L1.2 = 任何非 Disabled 值 | 该平台 PEG 侧只支持 L0s/L1；开启 L1SS 实测会导致 POST 阶段全黑 + 断网 |
| `PEG ASPM` = L0s 或 L0sL1 | 只要 L1，L0s 会增加无谓的进出态 |
| `Native ASPM` = Enabled | 要让 BIOS 管链路（FADT 模式）。OS 接管路径在这张卡上从未走通过 |
| `PCH ASPM` / `DMI ASPM` = Enabled | 与目标无关，只增风险 |
| 一次改多项 | 无法归因，回退成本极高 |

## 1. 进 BIOS

开机/上电后连按 **Del**；进 EZ Mode 后按 **F7** 切 Advanced Mode。

## 2. 目标状态总表

按顺序执行，每行是一个独立的「改 → 存 → 冷启 → 验证」循环。
**Phase 1 和 Phase 2 可以合并为一次**（AC BACK 与 ASPM 无关，属安全项）。

| 阶段 | 菜单位置（Advanced Mode） | 项目 | 目标值 | 禁止值 |
|---|---|---|---|---|
| P0 | — | 对所有相关页截图存档 | — | — |
| P1 | Advanced → APM Configuration | Restore AC Power Loss | **Power On** | — |
| P2 | Advanced → Platform Misc Configuration | PCI Express Native Power Management | **Enabled** | — |
| P2 | 同上 | Native ASPM | **Disabled** | Enabled |
| P2 | 同上 | PEG ASPM | **L1** | Disabled / L0s / L0sL1 / L1SS |
| P2 | 同上 | PCH ASPM | **Disabled** | 任何 Enabled |
| P2 | 同上 | DMI ASPM | **Disabled**（若无此项则跳过） | 任何 Enabled |
| P2 | 同上 | L1 Substates（L1SS / L1.1 / L1.2） | **Disabled** | 任何非 Disabled |

> P2 各项在同一页内，但建议拆两轮：第一轮只改
> `PCI Express Native Power Management = Enabled` + `PEG ASPM = L1`；
> 其余项若本来就是 Disabled 就不要碰，若不是，第二轮再逐个拨回。

## 3. 操作流程

### Phase 0 · 存档

1. Del 进 BIOS，F7 进 Advanced Mode。
2. Advanced → Platform Misc Configuration，截图记录所有当前值。
3. Advanced → APM Configuration，同样记录。

### Phase 1 · 来电自启（安全项）

1. Advanced → APM Configuration → **Restore AC Power Loss = Power On**。
2. F10 → 黑屏挂死（预期）→ 断电 10 s → 上电。
3. 进系统后确认能远程登录。

### Phase 2 · 开 PEG ASPM L1（核心项）

1. 冷启后再次 Del 进 BIOS。
2. Advanced → Platform Misc Configuration：
   - `PCI Express Native Power Management` → **Enabled**
   - `PEG ASPM` → **L1**
   - 确认 `Native ASPM` / `PCH ASPM` / `DMI ASPM` / `L1 Substates` 全为 **Disabled**
3. F10 → 黑屏挂死（预期）→ 断电 10 s → 上电。
4. 观察能否进系统。

## 4. 验收（逐条执行）

```bash
# 判据 1：FADT 那句会保留 —— 这是设计，不是失败
sudo dmesg | grep -i 'FADT.*ASPM'
# Native ASPM=Disabled 就是"BIOS 管 ASPM"模式，FADT 会继续声明 unsupported，
# 由 BIOS 自行配置链路。所以这行保留属正常，真正的判据看下面。

# 判据 2：根端口能力位出现 ASPM L1
sudo lspci -s 00:01.0 -vv | grep -E 'LnkCap|LnkCtl'
# 期望 LnkCap 里出现 "ASPM L1"，不再是 "not supported"

# 判据 3：上游 LnkCtl 变 Enabled
sudo lspci -s 01:00.0 -vv | grep LnkCtl
# 期望 ASPM L1 Enabled

# 判据 4：L1 驻留开始增长（间隔 10 s 读两次，第二次应大于第一次）
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies; sleep 10
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies
# 期望 L1 RESIDENCY 增长（原来恒 0）

# 判据 5：功耗复测（20 s 能量差分）
E1=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1); sleep 20
E2=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1)
awk "BEGIN{printf \"card = %.1f W\n\", ($E2-$E1)/20e6}"
# 期望从 50.3 W 落进 42 W 量级
```

> `hwmon` 编号在不同机器上可能不是 `hwmon4`，用
> `grep -l . /sys/class/hwmon/hwmon*/energy1_input` 找到有能量计数器的那一个。

## 5. 判定与回退

| 情况 | 判定 | 动作 |
|---|---|---|
| 进系统，判据 2–5 全过 | 成功 | 收工，记录新基线 |
| 判据 2 不过（仍 not supported） | BIOS 没吃设置 | 重进 BIOS 核对 P2 实际值 |
| 判据 2 过、3 不过 | PEG ASPM 没生效 | 确认 PEG ASPM 是 `L1` 而不是 `Auto` |
| 1–4 过但功耗没降 | 链路通了，封装态没进 | 保留现状（无害），继续做节点掩藏再看 |
| **冷启 2 次**仍黑屏 + 断网 | 该组合不兼容 | 回 BIOS 把 PEG ASPM 拨回 Disabled；进不去 BIOS 只能物理 Clear CMOS（CLRTC 跳线） |

回退时恢复：`PEG ASPM = Disabled`、`PCI Express Native Power Management = Disabled`。
`Restore AC Power Loss = Power On` **不要回退**。

## 6. 绝对禁止（汇总）

1. 禁 OS 内 `reboot`；禁把 F10 后黑屏当事故；一切重启走断电 → 等 10 s → 上电。
2. 禁开 L1 Substates / L0s / Native ASPM / PCH ASPM / DMI ASPM。
3. 禁一次改多项。
4. 禁 `update-grub`、`apt upgrade`、`apt install grub*`。
5. 禁动 `/etc/udev/rules.d/` 中与本项目无关的规则。
6. 遇到需要 Clear CMOS 的情形，停下来找人到场，不要用软件手段硬闯。

---

← [01 平台与基线](01-platform-baseline.md) · 下一篇：[03 内核观测](03-kernel-observation.md) →
