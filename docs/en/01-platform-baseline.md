# 01 · Platform, topology and baseline

> 中文: [01 · 平台、拓扑与基线](../01-platform-baseline.md)

> Every number here comes from the same machine, measured continuously within a single boot cycle.
> Change the motherboard or BIOS version and the absolute values will shift, but the direction of
> the conclusions generally holds.

## 1. Reference platform

| Item | Value |
|---|---|
| Host | Private-cloud NAS (Debian 12 family, headless userspace, no display) |
| Motherboard | ASUS PRIME Z790-P D4, BIOS 1836 |
| CPU | i5-13500T |
| GPU | Intel Arc Pro B60 24 GB (BMG / Battlemage), no display output, in a CPU-attached x16 slot |
| Driver | `xe` (in-tree) |
| Workload | Containerized inference (OpenVINO Model Server), speech transcription; GPU utilization ≈ 0 |

## 2. PCIe topology

```
CPU
 └─ 00:01.0            root port, Gen5 x8        ← ASPM capability decided by BIOS
     └─ 01:00.0        upstream port of the card's PCIe switch
         └─ 02:01.0    downstream port inside the card (Gen1 x1, fixed)
             └─ 03:00.0  the B60 itself (BDF 0000:03:00.0)
```

Key point: **L1 requires every segment of the link to allow it.** One segment says no and the
whole link can't enter L1. In this project the break was at `00:01.0` — the BIOS did not expose
ASPM capability on the root port by default.

## 3. Initial diagnosis (before any change)

```text
$ lspci -s 00:01.0 -vv | grep -E 'LnkCap|LnkCtl'
LnkCap: Port #0, Speed 32GT/s, Width x8, ASPM not supported
LnkCtl: ASPM Disabled; RCB 64 bytes, Disabled- CommClk+

$ dmesg | grep -i 'FADT.*ASPM'
ACPI FADT declares the system doesn't support PCIe ASPM, so disable it

$ cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies
L1 RESIDENCY: 0            ← permanently zero
```

Idle power: **50.3 W**, broken down as:

| Domain | Power | Note |
|---|---|---|
| `pkg` | 34.7 W | GPU core + package; affected by ASPM and package power state |
| VRAM | 15.6 W | 24 GB GDDR6 self-refresh, **a physical floor you cannot push** |
| Total | 50.3 W | The gap versus review rigs (15–22 W) is mostly platform and firmware |

## 4. Why "0% utilization should mean low power" fails here

On a desktop or Windows, the usual behaviour is "utilization near 0 → clocks and power drop".
That intuition is only half right on this machine:

- The GPU internals do drop to a very low power state (`pkg` can reach the 1.1 W range — see
  [07 Measurement](07-measurement.md));
- but whether the **PCIe link layer** enters L1 depends on the upstream root port's ASPM
  configuration and on whether something touches it periodically;
- as long as the link sits in a high-power state, or gets woken by an MMIO read every few
  seconds, `pkg` keeps being pushed back up.

In other words: **the bottleneck is not the GPU, it's the link and the pollers.**

## 5. Where the ceiling is

- 50.3 W → 8.3 W is already close to this machine's practical ceiling.
- In shallow sleep (link in L1, package not deep-asleep), 24 GB of VRAM self-refresh holds a
  floor around 15.6 W; only deep sleep on the whole card brings the reading into the 8 W range.
  That is also why "L1 residency" and "whole-card power" must be read together — either alone
  will mislead you.
- Going lower requires hardware changes (different platform, different card). There is no
  software headroom left.

---

Next: [02 BIOS / ASPM settings](02-bios-aspm.md) →
