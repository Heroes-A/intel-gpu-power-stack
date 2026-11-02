# Idle Power Stack for Intel Discrete GPUs

**English** · [中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Heroes-A/intel-gpu-power-stack)](https://github.com/Heroes-A/intel-gpu-power-stack/releases)
[![Platform](https://img.shields.io/badge/platform-Linux%20%2F%20Debian%2012-informational)](docs/01-platform-baseline.md)

**An Intel Arc discrete GPU in a headless server sits at 0% utilization and still draws 50 W. Getting it to 8 W is not a matter of tweaking a setting — it takes four layers.**

> **Scope: discrete GPUs (dGPU).** Intel Arc A-series (DG2), B-series (Battlemage), and
> Pro-series (A40/A50/A60/B50/B60). **Integrated GPUs (iGPU) are out of scope** — see §11.
>
> Reference case: Arc Pro B60 (24 GB), 50.3 W → 8.3 W idle (−83%), with containers still
> getting the real device.

---

## 0. What this post solves

The scenario is narrow and concrete: a headless Linux box (NAS, inference node, transcoding
server) with an Intel Arc discrete GPU, serving only containerized inference / transcoding /
rendering workloads. Average utilization is near zero.

Conventional wisdom says an idle GPU should drop to single-digit watts. In practice it sat at
~50 W: fan off, zero load, power unchanged.

I spent a few days tracing this from "a configuration problem" to "a structural problem". The
conclusion: **it is not one switch left off. Each of four layers has its own independent
blocker.** Fixing one layer buys very little; fixing all four is what produces the order-of-
magnitude change.

This post turns the whole thing into a reusable stack for Intel discrete GPUs. Integrated
GPUs are addressed separately (§11), and the conclusion there is: don't do this to an iGPU.

---

## 1. Results first

Same machine, continuous measurements within a single boot cycle:

| Stage | Action | Whole-card idle | L1 link residency |
|---|---|---|---|
| Baseline | Factory state | 50.3 W | 0 % |
| ① Firmware | BIOS: PEG ASPM = L1, let firmware own the link | 42.1 W | Capable, but broken up by service polling |
| ② Nodes | `card0` / `renderD*` moved out of `/dev/dri` | 31.2 W * | Intermittent |
| ③ Settled | System services no longer hold stale fds or poll | **8.0 – 8.7 W** | **92 – 99.9 %** |

\* There is a transition period right after masking: services still hold the old fd and only
notice the device is gone on their next enumeration.

- **83%** below baseline, roughly **370 kWh/year** saved (at 42 W × 8760 h).
- The drop is not uniform: firmware buys 16%; the remaining 80% all comes from "stop letting
  system services touch it".

**One easy misread**: in shallow sleep (link in L1, package not deep-asleep), 24 GB of VRAM
self-refresh holds a floor of ~15.6 W. Only when the whole card reaches deep sleep does the
reading fall to the 8 W range. So **L1 residency and whole-card power must be read together** —
either one alone leads to the opposite conclusion.

---

## 2. Why Intel dGPU idle power gets stuck

It decomposes into three independent layers.

### 2.1 Firmware: the root port has no L1 capability at all

```
$ sudo lspci -s 00:01.0 -vv | grep LnkCap
LnkCap: Port #0, Speed 16GT/s, Width x16
        ASPM not supported, Exit Latency L0s <1us, L1 <16us
                ^^^^^^^^^^^^^^^^
```

The root port's `LnkCap` literally says `ASPM not supported`. Meanwhile `dmesg` shows:

```
ACPI FADT declares the system doesn't support PCIe ASPM, so disable it
```

Two signals stacked, and the kernel never takes over ASPM. On the motherboard side,
`PEG ASPM` defaults to `Auto` or `Disabled`, so the link stays in L0 forever — full-power
delivery every millisecond.

This is a **precondition**. Without it, everything downstream buys at most 1/5 of the total.

### 2.2 System services: periodic MMIO polling is the real killer

This is the most counter-intuitive part, and where I got it wrong first.

I assumed "the container holds the device, so the link can't sleep", and ran a control
experiment: `docker pause` freezes every thread in the container. Power was identical across
all three states (running / paused / stopped). **Holding the device does not block L1.**

What actually drags the link out is **periodic register reads**. NAS-dashboard-style services
(system info, resource monitor, media service) enumerate `/dev/dri` every few seconds, open the
node they find, and read an MMIO register to report temperature / VRAM / utilization. Every read
is a link wake-up; the link cannot stay in L1 for two full poll periods.

Measured: with those services running, L1 residency never climbs. Move the nodes they look for
out of the way, and residency jumps straight to 99%.

### 2.3 Architecture: VRAM self-refresh is a hard floor

A discrete GPU has dedicated VRAM. In shallow sleep the VRAM must keep self-refreshing, and
that power cannot be recovered.

- On a 24 GB card the shallow-sleep floor is about **15.6 W**.
- Only deep sleep on the whole card (D3cold / package state G2 or deeper) brings the reading
  down to the 8 W range.

This explains a common confusion: "why does the same card idle at 15 W for other people while
mine sits at 40 W?" — because 15 W *is* its shallow-sleep floor; it isn't better optimized. To
cross that line, the link has to genuinely sleep.

### 2.4 Access layer (an engineering problem, not a power problem)

Once the nodes are moved, newly installed containers and scripts can't find the card. That is
the built-in cost of node masking, and it has to be paid back with an access layer or the
approach doesn't survive.

---

## 3. The stack: six layers

```
┌──────────────────────────────────────────────────────────────────┐
│ L5 Observe   embedded dashboard · adaptive sampling · time series │
├──────────────────────────────────────────────────────────────────┤
│ L4 Access    container device passthrough · lease runner · check  │
├──────────────────────────────────────────────────────────────────┤
│ L3 Control   single-container control plane: policy · leases ·    │
│              pollution watchdog · consumer profiling              │
├──────────────────────────────────────────────────────────────────┤
│ L2 Nodes     /dev/dri ⇄ private dir rename (not a proxy, not a    │
│              symlink)                                             │
├──────────────────────────────────────────────────────────────────┤
│ L1 Kernel    xe / i915 · PCIe ASPM · runtime PM · debugfs · hwmon │
├──────────────────────────────────────────────────────────────────┤
│ L0 Firmware  BIOS: PEG ASPM = L1 · Native ASPM = Disabled         │
└──────────────────────────────────────────────────────────────────┘
```

| Layer | Component | Responsibility | New code? |
|---|---|---|---|
| L0 | Motherboard BIOS | Give the root port L1 capability; let firmware, not the kernel, configure the link | No |
| L1 | Kernel + driver | Real power management; expose residency / energy counters | No |
| L2 | `mv` (rename) | Decide whether system services can enumerate the device | No |
| L3 | FastAPI + SQLite in one container | Policy, leases, watchdog, sampling | **Yes**, a few hundred lines |
| L4 | Docker `devices:` + two bash scripts | Frictionless access, lease fallback | **Yes**, pure bash |
| L5 | Server-rendered HTML/JS | Curves, residency, who holds the card | **Yes**, no build step |

Nothing heavy is introduced: the only external dependencies are two pip packages
(`fastapi` + `uvicorn`); everything else reuses existing kernel capability. No Prometheus, no
Grafana, no frontend build chain.

---

## 4. L0 · Firmware: the general ASPM playbook

### 4.1 Target state

| Item | Target | Why |
|---|---|---|
| PCIe Native Power Management | Enabled | Unlocks the power features on this page |
| Native ASPM | **Disabled** | Lets BIOS own the link (FADT mode). The OS-takeover path never worked on the test platform |
| PEG ASPM (CPU-attached slot) | **L1** | L1 only; L0s just adds needless entry/exit overhead |
| PCH ASPM / DMI ASPM | Disabled | Irrelevant to the goal, adds risk |
| L1 Substates (L1.1 / L1.2) | **Disabled** | Measured to cause a black screen at POST plus loss of network; needs Clear CMOS to recover |

### 4.2 Vendor menu names

Names differ by vendor. Look for these keywords:

| Vendor | Path (Advanced Mode) | Key items |
|---|---|---|
| ASUS | Advanced → Platform Misc Configuration | `PEG ASPM` / `Native ASPM` / `PCI Express Native Power Management` |
| MSI | Settings → Advanced → PCI Subsystem Settings | `PCIe ASPM` / `PEG ASPM` |
| ASRock | Advanced → Chipset → PCI Express Config | `PCIe ASPM Support` / `PEG ASPM` |
| Gigabyte | Settings → Platform Power / Peripherals | `PCIe ASPM Mode` / `PEG ASPM` |

> Some boards hide PEG ASPM under `Advanced → System Agent (SA) → PCI Express Configuration`.
> If you can't find it, search the whole page for `ASPM`.

### 4.3 Operating discipline (more important than the settings)

1. **Set "Restore AC Power Loss = Power On" first.** Then if any change bricks the boot,
   a remote power cycle still gives you one self-recovery attempt.
2. **Change one item at a time**: change → save → cold boot → verify → next. Otherwise you
   can't attribute a failure.
3. **Never `reboot` from the OS.** After a warm reset, Battlemage (B-series) has too short a
   link retraining window; any soft reset hangs to a black screen. Standard procedure: F10 →
   black screen (**expected**) → cut power ≥10 s → power on. A-series (DG2) is usually less
   severe, but cold boot is still recommended.

### 4.4 The five-point acceptance check

```bash
# 1. The FADT line stays — that's by design, not a failure
sudo dmesg | grep -i 'FADT.*ASPM'
#    Native ASPM=Disabled means "BIOS owns ASPM"; FADT still declaring unsupported is normal

# 2. Root port capability now shows ASPM L1 (no longer "not supported")
sudo lspci -s 00:01.0 -vv | grep -E 'LnkCap|LnkCtl'

# 3. Upstream LnkCtl flips to Enabled
sudo lspci -s 01:00.0 -vv | grep LnkCtl

# 4. L1 residency starts growing (read twice, 10 s apart; second > first)
D=/sys/kernel/debug/dri/0000:03:00.0
sudo cat $D/*residenc* 2>/dev/null; sleep 10; sudo cat $D/*residenc* 2>/dev/null

# 5. Re-measure power (20 s energy delta)
E1=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1); sleep 20
E2=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1)
awk "BEGIN{printf \"card = %.1f W\n\", ($E2-$E1)/20e6}"
```

Check 2 failing = BIOS didn't take the setting. Check 2 passing but 3 failing = PEG ASPM is
still `Auto` rather than `L1`. Checks 1–4 passing but power unchanged = link is fine, package
state isn't entering; keep going to the next layers.

---

## 5. L1 · Kernel: trust only these four interfaces

Most online advice about "how to measure GPU power" is wrong. Only four interfaces give you
causality:

| Interface | Path | What it answers |
|---|---|---|
| Link residency | `debugfs/dri/<BDF>/*residenc*` | How much time the link actually spends in L1 — **the single most important metric** |
| Package state | `debugfs/dri/<BDF>/*pkg*residenc*` | Whether a deeper package state is reached (decides if you can beat the VRAM floor) |
| Energy counters | `hwmon*/energy{1,2}_input` (µJ) | Cumulative energy for card / package domains; difference = mean power |
| Client list | `debugfs/dri/<BDF>/clients` + `/proc/<pid>/fdinfo/*` | Who holds the device, and whether they merely *enumerate* or actually *work* |

Traps you must know:

- **`hwmon` numbering is not stable.** Find the one with a counter:
  `grep -l . /sys/class/hwmon/hwmon*/energy1_input`.
- **Energy counters update at ~100 ms granularity**, so a 100 ms delta jumps between 0 and
  5.4 J (equivalent to 0–54 W). Use deltas of 15 s or more for a stable reading.
- **debugfs filenames vary by driver/kernel version**: under `xe` they are
  `dgfx_pcie_link_residencies` / `dgfx_pkg_residencies`; `i915` or other versions may differ.
  **Glob for them, don't hardcode.**
- **`drm-engine-*` in `fdinfo` is the proof of real work.** A process that has the node open
  but shows zero engine time is merely enumerating.

---

## 6. L2 · Nodes: why moving `/dev` saves power

This looks like the least technical step, but it has a clear causal chain:

```
System service enumerates /dev/dri every N seconds
        │
        ├── card visible → open node → periodic MMIO register reads
        │                                └─ each read = a link wake-up → L1 can't hold
        │
        └── card invisible → does nothing → link sleeps into L1 on its own → 8 W
```

The implementation is two `mv`s (same-filesystem rename; inode, permissions and device number
are unchanged, already-open fds are unaffected):

```bash
sudo mkdir -p /dev/gpu
sudo mv /dev/dri/card0      /dev/gpu/card
sudo mv /dev/dri/renderD129 /dev/gpu/render
```

Containers then mount `/dev/gpu/render` directly and are unaffected — **same inode, same device
number, same real card.**

### 6.1 Applicability (important)

| Situation | Applies? | Note |
|---|---|---|
| NAS / dashboard services periodically enumerate the GPU | ✅ Biggest win | The primary scenario |
| Only containers use the card; system layer never touches it | ✅ Moderate win | You only get the firmware part |
| System-level tasks (photo AI, media transcoding) need the card | ⚠️ Needs leases | They only look in `/dev/dri`; expose on demand |
| Graphical session / monitor attached to the card | ❌ Don't | Removing the node removes the display |
| Integrated GPU (shared memory, no dedicated VRAM) | ❌ Not advised | Negligible gain, and `/dev/dri` is deeply depended on by the display stack |

### 6.2 A fact worth stating plainly: you cannot fake a node

I tried the "invent a fake device to fool the system layer" route. All three paths are closed:

1. **CUSE fake DRM node** — DRM requires `mmap`; CUSE doesn't support `mmap`; it dies on the
   first `mmap`.
2. **Forged `/sys/class/drm/` entries** — sysfs is kernel-generated; userspace can't write it.
3. **Forged `/dev` node** — `libdrm`'s `drmGetDevices2()` compares `stat()`'s `st_rdev` against
   the udev-recorded `devnum` and filters out mismatches.

So the correct shape is not deception but **path redirection plus explicit leases**: hide from
system services by default, and say "you have 30 minutes, starting now" when they need it.

---

## 7. L3 · Control plane: two states plus leases

### 7.1 State machine

| State | Node location | Visible to system services | Power | Who uses it |
|---|---|---|---|---|
| `SLEEP` | `/dev/gpu/{card,render}` | ✗ | ~8 W | Containers (direct path, always available) |
| `WORK` | `/dev/dri/{card,renderD*}` | ✓ | ~42 W | System-level tasks + containers |

Key design point: **containers can use the card in both states.** The only thing that ever
switches is whether system services can see it. So "I need the GPU" costs a container nothing
and needs no lease; only system-level tasks do.

### 7.2 Leases

```
POST   /api/lease  {"reason":"media transcode","ttl":1800}   → switch to WORK, return lease id
DELETE /api/lease/<id>                                        → switch back to SLEEP
```

- **TTL fallback**: auto-reclaim on expiry, so a forgotten release can't pin the box at 42 W.
- **Debounce**: a 15 s debounce window around transitions prevents flapping.
- **Orphan watchdog**: when the caller is `kill -9`'d, an independent process checks
  `/proc/<pid>/status` for liveness and releases on its behalf (with PID-reuse protection).

### 7.3 Pollution watchdog

Even with nodes moved, someone may move them back by hand, or a service may cache the path.
The watchdog tracks L1 residency continuously: three consecutive sample periods below 40% is
treated as pollution, triggering automatic re-masking plus a restart of the affected services,
and an event is recorded.

---

## 8. L4 · Access: making new installs transparent

This is what decides whether the approach survives long term. With nodes moved, there are only
two access paths:

**Containers (recommended, zero latency)**

```yaml
services:
  your-app:
    devices:
      - /dev/gpu/render:/dev/dri/renderD129
```

What gets mounted is still the real card; `vainfo`, `sycl-ls`, OpenVINO all work normally.
**No exposure needed, no waiting for a state switch.**

**Host commands (lease wrapper)**

```bash
gpu-run ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD129 -i in.mp4 out.mp4
```

Acquire lease → wait for the node to appear in `/dev/dri` → run → release. Exit codes pass
through unchanged, `INT/TERM/HUP` all release as a fallback, and `kill -9` is handled by the
watchdog process.

Pair it with `gpu-check.sh` for access self-checks: correct device mapping, device number
matching this card (not the iGPU), `DRM_IOCTL_VERSION` working, and dashboard visibility.
**The device number is the easiest step to get wrong** — inside a container `stat` prints hex
(`e2:81`) while `ls -l` on the host prints decimal (`226:129`); both forms must be accepted.

---

## 9. L5 · Observe: monitoring itself hurts power

This is the trap that hurt most.

When no one holds the card, the device sits in runtime suspend, and **frequent `hwmon` reads
keep waking it up**:

| Read pattern | Measured power |
|---|---|
| Only first and last read (60 s endpoint delta) | 12.0 W |
| One read every 0.5 s | **31 W** (and very stable, std 0.09) |

In other words, "monitoring added to observe power" pushed consumption from 12 W to 31 W, and
the result looked stable and convincing while doing it.

The fix is **adaptive sampling**: 2 s intervals while someone holds the card, 15 s when idle,
and store energy-interval means rather than instantaneous values. The dashboard curve must
state its sampling policy, or readers will conclude the device is oscillating.

Which answers a common question: "my power curve jumps between 0 and 43 W — is it broken?" No.
The true mean is stable; the sampling window is what jumps. With 15 s interval means, readings
sit between 8.0 and 8.6 W, variation under 0.6 W.

---

## 10. Diagnostic decision tree

Given a machine whose idle power won't come down, follow this order without skipping steps:

```
① lspci -vv, check root port LnkCap
   ├─ no ASPM L1  → firmware-layer problem, go to BIOS (§4)
   └─ ASPM L1 present
        ↓
② is upstream LnkCtl Enabled?
   ├─ no → BIOS still has PEG ASPM at Auto/Disabled
   └─ yes
        ↓
③ read *residenc*, does L1 residency grow within 10 s?
   ├─ no → something is touching it periodically, go to ④
   └─ grows but power doesn't drop → link sleeps, package doesn't; check pkg residency.
     This is the VRAM floor and is a normal ceiling
        ↓
④ list clients, per-pid check fdinfo's drm-engine-*
   ├─ engine time 0 → merely enumerating; this is what breaks L1
   └─ engine time > 0 → real work; high power is expected
        ↓
⑤ bisect
   - systemctl stop the suspicious service, see if residency recovers
   - or just mv the nodes and see if it improves immediately
```

In practice this identifies the specific service in about 20 minutes.

---

## 11. Fit across Intel GPUs

| Class | Examples | Driver | Dedicated VRAM | Shallow-sleep floor | Sensitive to polling | Gain from this stack |
|---|---|---|---|---|---|---|
| DG2 consumer | Arc A310 / A380 / A580 / A750 / A770 | `i915` (`xe` worth trying on newer kernels) | Yes | Medium | High | High |
| DG2 pro | Arc Pro A40 / A50 / A60 | same | Yes | Medium | High | High |
| BMG consumer | Arc B570 / B580 | `xe` | Yes | Medium–high | High | Very high |
| BMG pro | Arc Pro B50 / **B60** | `xe` | Yes (B60 = 24 GB) | **High** (~15.6 W) | High | **Very high (−83% measured)** |
| Xe2 iGPU | Lunar Lake / Arrow Lake integrated | `xe` | Shared memory | Very low | Medium | Low, not advised |
| Xe-LP iGPU | Tiger Lake / Alder Lake / Raptor Lake | `i915` | Shared memory | Very low | Medium | Low, not advised |

Three lessons:

1. **More VRAM means a higher floor.** A 24 GB card is pinned above 15 W by VRAM self-refresh in
   shallow sleep; going lower requires deep sleep — there is no software headroom left.
2. **`xe` exposes more observability than `i915`** (it has package-domain residency), but the
   names change; don't hardcode paths.
3. **Leave iGPUs alone.** There is no dedicated-VRAM self-refresh problem to solve, and
   `/dev/dri` is deeply depended on by the display stack — masking it breaks the display.

> For reference: the same B60 idles at 15–22 W in review rigs. The difference comes from the
> platform (motherboard / BIOS / how the CPU lane is wired), not from the card. Absolute numbers
> must be measured locally; the methodology transfers.

---

## 12. Rejected approaches (and why)

This section is for anyone looking for a more elegant solution — I have either tried or
calculated each of these, so you don't have to repeat them.

| Approach | Why rejected |
|---|---|
| CUSE fake DRM node | DRM depends on `mmap`; CUSE doesn't support it; dies on the first `mmap` |
| Forged `/sys/class/drm/` entries | sysfs is kernel-generated and not writable from userspace; `libdrm` rejects fake nodes anyway |
| udev rename rules | Only changes the `/dev` name, not the enumeration result, and conflicts with the `st_rdev ↔ devnum` check |
| `LD_PRELOAD` intercepting `open()` | Only affects injected processes; system services are out of reach; statically linked binaries bypass it |
| fanotify interception | Requires monitoring every mount point; conflicts with Docker mount namespaces |
| BPF LSM denying `file_open` | Theoretically possible, but needs `CONFIG_BPF_LSM` + BTF, and hook behaviour drifts across kernels |
| Patching the driver / adding a quirk | Requires rebuilding on every kernel upgrade; risk far exceeds reward |
| `rmmod xe` | All containers lose the GPU; reloading also triggers the Battlemage warm-reset problem |
| In-process "unload the model when idle" | Inference frameworks cache the GPU context at process level; `gc` can't reclaim it. Measured to crash the process too |
| Just stopping the system services | Breaks dashboard functionality and they come back after upgrade/reboot — unsustainable |
| Prometheus + Grafana | Two extra services to watch one 8 W curve; operational cost far exceeds the benefit |

---

## 13. Rollout order (an afternoon's work)

| Step | Action | Time | Risk |
|---|---|---|---|
| 1 | Record the baseline: power, L1 residency, clients | 10 min | None |
| 2 | BIOS: "Restore AC Power Loss = Power On", verify with a cold boot | 15 min | Low |
| 3 | BIOS: PEG ASPM = L1, cold boot, run the five-point check | 30 min | **Medium** (may black-screen; needs a power cycle) |
| 4 | Re-measure power, record the new baseline | 10 min | None |
| 5 | Manually `mv` the nodes, watch power for 10 minutes | 10 min | Low (instantly reversible) |
| 6 | If it works: repoint containers to the private path, deploy the control plane | 1 h | Low |
| 7 | Observe for 24 h: residency, curves, event stream | — | None |

The first five steps validate the gain without writing a single line of code. Step 6 is where
it becomes engineering.

---

## 14. Reproducing, and repo layout

```
docs/01-platform-baseline.md   platform topology, power breakdown
docs/02-bios-aspm.md           BIOS item-by-item, five-point check, rollback
docs/03-kernel-observation.md  correct reading of L1 residency / pkg state / hwmon / fdinfo
docs/04-node-masking.md        why masking works, why fake nodes can't be built
docs/05-lease-policy.md        state machine, leases, debounce, pollution watchdog
docs/06-integration.md         two-line container access + lease runner + troubleshooting
docs/07-measurement.md         the truth about "jumping" power and how to measure it
docs/08-tech-stack.md          component choices and rejected approaches
src/                           control plane (FastAPI + SQLite)
scripts/                       detect / self-check / lease runner / deploy
templates/                     compose snippet, host-side usage
```

Quick start:

```bash
bash scripts/gpu-detect.sh          # probe: BDF / node / device number / ASPM / residency / live power
bash scripts/deploy.sh              # start the control plane (default port 41966)
```

> The reference implementation still uses `B60_` / `/dev/b60` naming for env vars and paths,
> because that was the measured card. Switching cards only requires three values:
> `B60_BDF` (PCI address), `B60_DRI_NODE` / `B60_RENDER_DEVS` (node and device number), and
> `B60_SERVICES` (which services are polling). Behaviour is independent of the card model.

---

## 15. Boundaries and red lines

**What this cannot solve:**

- The VRAM self-refresh floor (~15.6 W for a 24 GB card in shallow sleep) — a physical limit;
  only deep sleep routes around it.
- Power under real load — that isn't the goal here.
- Platforms/BIOSes that don't offer ASPM capability — replace the platform.

**Red lines (violating these may require Clear CMOS to recover):**

1. Never enable L1 Substates / L1.1 / L1.2.
2. Never change more than one BIOS item at a time.
3. Never `reboot` from the OS (Battlemage warm-reset black screen). All restarts go
   power off → wait 10 s → power on.
4. Always set "Restore AC Power Loss = Power On" before touching BIOS.
5. Confirm no graphical session or monitor is attached to the card before masking nodes.

**Security posture:**

- The control plane has no authentication by default and should only listen on a trusted
  network. For public exposure, put it behind a reverse proxy with auth.
- The control plane container is `privileged` (it moves `/dev` entries and reads debugfs),
  which is equivalent to host root. Deploy only on machines you trust.
- This project stores no credentials.

---

## 16. Closing

The essence of this approach is not a power-saving trick but a judgement:

> **Holding the device does not block low power. Periodic disturbance does.**

Once you accept that, the shape of the solution becomes the only one available — not
intercepting `open()`, not forging devices, but turning "who may see it, and when" into an
explicit policy.

The measurements come from a real machine in production (Arc Pro B60 24 GB, headless,
container-only), not lab-tuned numbers. Every conclusion can be reproduced elsewhere using the
decision tree in §10.

---

*License: MIT · Issues with measurements from other platforms and cards are welcome.*
