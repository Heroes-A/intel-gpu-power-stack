# 03 · Kernel-side observation

> 中文: [03 · 内核侧观测](../03-kernel-observation.md)

> This chapter is only about "how to see the truth".
> Every judgement that follows rests on these few files; misreading them yields exactly the
> opposite conclusion.

## 1. Link residency: `dgfx_pcie_link_residencies`

```bash
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies
```

Output looks like:

```
L1 RESIDENCY: 1980ms   L1 ENTRY: 42   ...
```

- **L1 RESIDENCY is the single most important metric**: it tells you directly how much time the
  link actually spends asleep in L1.
- How to read it: sample twice 10 s apart and compare the delta. Healthy values are ≥90%.
- Permanently 0 means the link never entered L1 once — either the BIOS never granted the
  capability, or something is constantly touching the link.
- This is a `xe` driver debugfs node; `i915` uses different paths and was not verified here.

## 2. Package power state: `dgfx_pkg_residencies`

```bash
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pkg_residencies
```

Shows how long the GPU package spends in each power state (G2 / G8 and so on).
When the link is in L1 but power stays high, this is usually where to look — if the package
state doesn't follow, power won't come down.

## 3. Power: the hwmon energy counter

```bash
ls /sys/class/hwmon/hwmon*/energy1_input
# energy1 = whole card, energy2 = pkg (µJ, ~100 ms update granularity)

A=$(cat /sys/class/hwmon/hwmon4/energy1_input); sleep 20
B=$(cat /sys/class/hwmon/hwmon4/energy1_input)
awk "BEGIN{printf \"%.2f W\n\", ($B-$A)/20e6}"
```

- **You must use interval differences**; never treat a single reading as power.
- Sampling windows of ≥15 s are stable; a 0.5 s window shows noise (see [07](07-measurement.md)).
- ⚠️ **Don't read at high frequency while nobody holds the card**: one read every 0.5 s
  repeatedly wakes the device from suspend — measured 12 W → 31 W.

## 4. Who is using the GPU: DRM clients and fdinfo

```bash
# Processes that currently have this DRM device open
sudo ls -l /proc/*/fd 2>/dev/null | grep -i 'dri/'
# or read debugfs directly
sudo cat /sys/kernel/debug/dri/0000:03:00.0/clients
```

To decide "is it actually working", look at engine time in fdinfo:

```bash
sudo cat /proc/<pid>/fdinfo/<fd> | grep -E 'drm-engine|drm-client-id'
```

- All `drm-engine-*` deltas at 0 → the process merely holds an `open()` on the card, **no
  compute load**.
- This is the original evidence for the project's "holding ≠ waking" conclusion.

The consumer profiling in `b60-guard` follows this same chain: `pid` → `/proc/<pid>/cgroup` →
container name / systemd unit → engine time.

## 5. runtime PM state

```bash
cat /sys/bus/pci/devices/0000:03:00.0/power/runtime_status     # suspended / active
cat /sys/bus/pci/devices/0000:03:00.0/power/runtime_usage      # 0 = nobody holds it
```

- `runtime_usage == 0` and `runtime_status == suspended` → the card is suspended, and at that
  point **any** hwmon read wakes it.
- `b60-guard`'s adaptive sampling uses `runtime_usage` as the switch: 2 s while held, 15 s
  otherwise.

## 6. Common misreadings

| Misreading | What it actually means |
|---|---|
| Instantaneous power flipping between 0 W and 43 W = power saving failed | Sub-second self-oscillation of power states; the 15 s mean is the truth |
| L1 residency 99% but power 30 W | Link sleeps, package domain doesn't; or monitoring is waking it at high frequency |
| `FADT ... doesn't support PCIe ASPM` still present | Normal when Native ASPM=Disabled; judge by `LnkCap` instead |
| A process holds the card = it wakes the GPU | With zero engine time it doesn't affect L1 |

---

← [02 BIOS / ASPM](02-bios-aspm.md) · Next: [04 Node masking](04-node-masking.md) →
