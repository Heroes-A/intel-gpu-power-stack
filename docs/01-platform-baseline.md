# 01 · 平台、拓扑与基线

> English: [01 · Platform, topology and baseline](en/01-platform-baseline.md)

> 所有数字来自同一台机器、同一次开机周期内的连续测量。
> 换了主板/BIOS 版本，绝对值会变，但结论的方向一般不变。

## 1. 参考平台

| 项 | 值 |
|---|---|
| 主机 | 私有云 NAS（Debian 12 系，用户态 headless，无显示器） |
| 主板 | ASUS PRIME Z790-P D4，BIOS 1836 |
| CPU | i5-13500T |
| 显卡 | Intel Arc Pro B60 24 GB（BMG / Battlemage），无显示输出，插 CPU 直连 x16 |
| 驱动 | `xe`（内核自带） |
| 负载 | 容器化推理（OpenVINO Model Server）、语音转写；GPU 利用率长期 ≈ 0 |

## 2. PCIe 拓扑

```
CPU
 └─ 00:01.0            根端口，Gen5 x8        ← ASPM 能力由 BIOS 决定
     └─ 01:00.0        卡上 PCIe switch 上行
         └─ 02:01.0    卡内下行（Gen1 x1，固有）
             └─ 03:00.0  B60 本体（BDF 0000:03:00.0）
```

要点：**L1 要求链路每一段都允许**。断在任何一段，整条链路都进不去。
本项目遇到的断点在 `00:01.0`——BIOS 默认没有给根端口开放 ASPM 能力。

## 3. 初始诊断（改动前）

```text
$ lspci -s 00:01.0 -vv | grep -E 'LnkCap|LnkCtl'
LnkCap: Port #0, Speed 32GT/s, Width x8, ASPM not supported
LnkCtl: ASPM Disabled; RCB 64 bytes, Disabled- CommClk+

$ dmesg | grep -i 'FADT.*ASPM'
ACPI FADT declares the system doesn't support PCIe ASPM, so disable it

$ cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies
L1 RESIDENCY: 0            ← 恒为 0
```

空载功耗 **50.3 W**，构成：

| 域 | 功耗 | 说明 |
|---|---|---|
| `pkg` | 34.7 W | GPU 核心 + 封装，受 ASPM / 封装电源态影响 |
| VRAM | 15.6 W | 24 GB GDDR6 自刷新，**物理地板，压不动** |
| 合计 | 50.3 W | 与评测机 15–22 W 的差距主要在平台与固件 |

## 4. 为什么"利用率 0 就该省电"在这里不成立

桌面/Windows 下常见的行为是"利用率接近 0 → 自动降频降功耗"。这个直觉在本机只对了一半：

- GPU 内部确实会降到极低功耗态（`pkg` 可到 1.1 W 量级，见 [07 测量方法](07-measurement.md)）；
- 但**PCIe 链路层**是否进入 L1，取决于上游根端口的 ASPM 配置和有没有人周期性访问它；
- 只要链路停在高功耗态、或者每几秒被 MMIO 读唤醒一次，`pkg` 就反复被抬起来。

换句话说：**省电的瓶颈不在 GPU，在链路和轮询者。**

## 5. 收益上限在哪

- 50.3 W → 8.3 W 已经接近这台机器的现实上限。
- 浅睡（链路进 L1、封装未深睡）时，24 GB 显存自刷新撑住约 15.6 W 的地板；
  只有整卡进深睡，读数才会落到 8 W 量级——这也是为什么"链路 L1 驻留"和"整卡功耗"
  必须一起看，单看一个都会误判。
- 想再往下只能动硬件（换平台/换卡），软件层没有空间。

---

下一篇：[02 BIOS / ASPM 设置](02-bios-aspm.md) →
