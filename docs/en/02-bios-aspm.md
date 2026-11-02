# 02 · BIOS / ASPM settings

> 中文: [02 · BIOS / ASPM 设置](../02-bios-aspm.md)

> Goal: give the CPU-attached root port PCIe ASPM L1 capability and enable it, taking whole-card
> idle from 50.3 W to the 42 W range.
> This step is a **precondition** for the project — without it, node masking downstream only
> recovers a small fraction of the gain.

## 0. Three pitfalls that will ruin your day

1. **After F10 save-and-exit the machine black-screens and hangs — this is not a failure.**
   After a warm reset, Battlemage cards have too short a link retraining window; any **soft
   reset** (F10 save & exit, OS `reboot`) hangs to a black screen. The standard procedure is:
   F10 → screen goes black and unresponsive (**expected**) → cut power ≥10 s → power on (cold
   boot) → boots normally. With a KVM or smart plug: Power Off → wait 10 s → Power On.
2. **Change one item at a time.** Change one → save → cold boot → verify → next. Otherwise you
   cannot attribute a failure.
3. **Set "Restore AC Power Loss = Power On" before touching anything else.** Then even if a later
   setting prevents boot, a remote power cycle still gives you one self-recovery attempt.

### Do-not-touch list

Violating any of these may require a physical Clear CMOS to recover.

| Prohibited | Reason |
|---|---|
| L1 Substates / L1.1 / L1.2 = anything but Disabled | This platform's PEG side only supports L0s/L1; enabling L1SS measured to cause a black screen at POST plus loss of network |
| `PEG ASPM` = L0s or L0sL1 | L1 only; L0s just adds needless entry/exit transitions |
| `Native ASPM` = Enabled | You want BIOS to own the link (FADT mode). The OS-takeover path never worked with this card |
| `PCH ASPM` / `DMI ASPM` = Enabled | Irrelevant to the goal, adds risk |
| Changing several items at once | Impossible to attribute, very costly to roll back |

## 1. Entering the BIOS

Press **Del** repeatedly right after power-on; in EZ Mode press **F7** for Advanced Mode.

## 2. Target state, everything in one table

Execute in order; each row is an independent "change → save → cold boot → verify" cycle.
**Phase 1 and Phase 2 can be merged into one** (AC BACK is unrelated to ASPM and is a safety item).

| Phase | Menu location (Advanced Mode) | Item | Target | Forbidden |
|---|---|---|---|---|
| P0 | — | Screenshot every relevant page | — | — |
| P1 | Advanced → APM Configuration | Restore AC Power Loss | **Power On** | — |
| P2 | Advanced → Platform Misc Configuration | PCI Express Native Power Management | **Enabled** | — |
| P2 | same | Native ASPM | **Disabled** | Enabled |
| P2 | same | PEG ASPM | **L1** | Disabled / L0s / L0sL1 / L1SS |
| P2 | same | PCH ASPM | **Disabled** | any Enabled |
| P2 | same | DMI ASPM | **Disabled** (skip if absent) | any Enabled |
| P2 | same | L1 Substates (L1SS / L1.1 / L1.2) | **Disabled** | anything but Disabled |

> The P2 items are on one page, but split them over two rounds: in round one only set
> `PCI Express Native Power Management = Enabled` and `PEG ASPM = L1`; leave the rest alone if
> they are already Disabled, otherwise flip them back one at a time in round two.

## 3. Procedure

### Phase 0 · Record

1. Del into BIOS, F7 for Advanced Mode.
2. Advanced → Platform Misc Configuration, screenshot every current value.
3. Advanced → APM Configuration, same.

### Phase 1 · Restore on AC loss (safety item)

1. Advanced → APM Configuration → **Restore AC Power Loss = Power On**.
2. F10 → black screen hang (expected) → cut power 10 s → power on.
3. After boot, confirm you can log in remotely.

### Phase 2 · Enable PEG ASPM L1 (the core item)

1. Cold boot, then Del into BIOS again.
2. Advanced → Platform Misc Configuration:
   - `PCI Express Native Power Management` → **Enabled**
   - `PEG ASPM` → **L1**
   - confirm `Native ASPM` / `PCH ASPM` / `DMI ASPM` / `L1 Substates` are all **Disabled**
3. F10 → black screen hang (expected) → cut power 10 s → power on.
4. See whether it boots.

## 4. Acceptance (run each check)

```bash
# Check 1: the FADT line stays — that's by design, not a failure
sudo dmesg | grep -i 'FADT.*ASPM'
# Native ASPM=Disabled means "BIOS owns ASPM"; FADT keeps declaring unsupported and the
# BIOS configures the link itself. So this line remaining is normal — the real checks follow.

# Check 2: root port capability now shows ASPM L1
sudo lspci -s 00:01.0 -vv | grep -E 'LnkCap|LnkCtl'
# expect "ASPM L1" in LnkCap instead of "not supported"

# Check 3: upstream LnkCtl flips to Enabled
sudo lspci -s 01:00.0 -vv | grep LnkCtl
# expect ASPM L1 Enabled

# Check 4: L1 residency starts growing (read twice, 10 s apart; second > first)
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies; sleep 10
sudo cat /sys/kernel/debug/dri/0000:03:00.0/dgfx_pcie_link_residencies
# expect L1 RESIDENCY to grow (it was permanently zero)

# Check 5: re-measure power (20 s energy delta)
E1=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1); sleep 20
E2=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1)
awk "BEGIN{printf \"card = %.1f W\n\", ($E2-$E1)/20e6}"
# expect a fall from 50.3 W into the 42 W range
```

> `hwmon` numbering may not be `hwmon4` on your machine. Use
> `grep -l . /sys/class/hwmon/hwmon*/energy1_input` to find the one with an energy counter.

## 5. Verdict and rollback

| Situation | Verdict | Action |
|---|---|---|
| Boots, checks 2–5 all pass | Success | Done; record the new baseline |
| Check 2 fails (still not supported) | BIOS didn't take the setting | Re-enter BIOS and verify the actual P2 values |
| Check 2 passes, 3 fails | PEG ASPM not effective | Confirm PEG ASPM is `L1`, not `Auto` |
| 1–4 pass but power unchanged | Link works, package state doesn't enter | Keep it (harmless) and continue to node masking |
| **Two cold boots** still black screen + no network | The combination is incompatible | Back in BIOS set PEG ASPM back to Disabled; if you can't enter BIOS, physical Clear CMOS (CLRTC jumper) is the only way |

Rollback values: `PEG ASPM = Disabled`, `PCI Express Native Power Management = Disabled`.
Do **not** roll back `Restore AC Power Loss = Power On`.

## 6. Absolutely forbidden (summary)

1. No OS `reboot`; do not treat the post-F10 black screen as an incident. Every restart goes
   power off → wait 10 s → power on.
2. Do not enable L1 Substates / L0s / Native ASPM / PCH ASPM / DMI ASPM.
3. Do not change several items at once.
4. No `update-grub`, `apt upgrade`, `apt install grub*`.
5. Do not touch udev rules under `/etc/udev/rules.d/` that are unrelated to this project.
6. If a situation demands Clear CMOS, stop and get someone on site. Don't force it with software.

---

← [01 Platform and baseline](01-platform-baseline.md) · Next: [03 Kernel observation](03-kernel-observation.md) →
