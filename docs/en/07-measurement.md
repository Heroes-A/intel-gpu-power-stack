# 07 · Measurement: why readings "jump"

> 中文: [07 · 测量方法：为什么读数会"跳"](../07-measurement.md)

> This chapter answers a recurring question: "the power curve keeps jumping between 0 and 43 W —
> did I do something wrong?"
> No. **The true mean is stable; what jumps is the sampling window, not the device.**

## 1. Conclusion first

| Observation method | Result |
|---|---|
| 0.5 s window sampling (179 samples) | mean 8.3 W, std 5.0, min 0.0, max 19.5 W |
| Reading the energy counter at 0.1 s granularity | adjacent 100 ms deltas jump between 0 and 5.4 J (equivalent to 0–54 W) |
| **15 s interval energy mean (the stored metric)** | **8.0 – 8.6 W, variation < 0.6 W** |
| 60 s endpoint difference | 8.45 W |

That is: **the average sits at 8–8.6 W; the instantaneous value really does jump, but it is
meaningless** — the GPU spends most of its time in a very low power state and briefly rises by
tens of milliseconds every few seconds to ~10 W. Seen through a 2 s window, that looks like
"high-low flapping".

## 2. Suspects, ruled out one by one

| Suspect | Experiment | Result | Verdict |
|---|---|---|---|
| The dashboard's own sampling (reading hwmon every 2 s) | 60 s endpoint delta with the container stopped vs running | 12.00 W vs 12.51 W | Only 0.5 W; not the cause |
| Software activity in the inference process | `docker pause` freezing all threads (fd retained) | 8.34 / 8.41 / 8.35 W — identical in all three states | **Irrelevant** |
| The GPU engine has work | fdinfo engine-time deltas | 0.0% for every process | **Irrelevant (not compute load)** |
| Counter quantization (chunked updates) | Step behaviour at 0.1 s granularity | Every reading changes, ~0.101 s apart | **Not an artifact; real fluctuation** |
| System service polling | Already masked; client list contains only the container | L1 residency 88–99.9% | Already handled; not this problem |

**The physical picture**: the GPU cycles by itself between "deep power-save state" and "brief wake
state" (`pkg` domain 1.1 W ↔ 17 W). This is normal PCU power management on Battlemage cards; the
engine has no load, so there is no "who is working" to find.

## 3. Measurement itself costs power

When nobody holds the card (`runtime_usage == 0`) the device is suspended, and **frequent hwmon
reads keep waking it**:

| Read pattern while nobody holds the card | Measured |
|---|---|
| Only first and last read (60 s endpoint delta) | **12.0 W** |
| One read every 0.5 s | **31 W** (stable, std 0.09) |

0.5 s polling turned "12 W that could have stayed suspended" into "31 W continuously woken".
That's the cost of monitoring, not a degradation of the device.

> So `b60-guard` samples adaptively: every 2 s while someone holds the card, every 15 s otherwise,
> and the dashboard shows `sampling: idle-throttled`.

## 4. How to measure correctly

```bash
# 1) Interval difference on the energy counter, window ≥15 s
E1=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1); sleep 60
E2=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1)
awk "BEGIN{printf \"%.2f W\n\", ($E2-$E1)/60e6}"

# 2) Read L1 residency at the same time to confirm the link really sleeps
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies

# 3) Don't run a pile of monitors while measuring
systemctl stop <your-monitor>   # temporarily stop other samplers if needed
```

Suggested acceptance values:

| Metric | Healthy value |
|---|---|
| 15 s mean | 8.0 – 8.7 W |
| L1 residency | ≥90% |
| `runtime_usage` | 0 (while nobody holds the card) |
| Card temperature | Stable, no fan spinning up |

## 5. How the dashboard handles this

1. **Dual display**: the headline number is a 60 s moving mean; instantaneous value and the 60 s
   min–max range are listed separately, so spikes can't mislead.
2. **History storage**: one row per 15 s holding an "interval energy mean" (energy differences are
   inherently spike-resistant), retained 30 days in SQLite.
3. **Curve downsampling**: five ranges 1h / 6h / 24h / 7d / 30d, ≤360 points.
4. **Alerting rule**: means only — alert when it stays above 25 W for 5 minutes, and don't repeat
   within 30 minutes.

Instantaneous values are kept for diagnosis (you can see the spike shape); the headline uses the
mean for decision-making. The two don't conflict.

---

← [06 Integration guide](06-integration.md) · Next: [08 Tech stack and choices](08-tech-stack.md) →
