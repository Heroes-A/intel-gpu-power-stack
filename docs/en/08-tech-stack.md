# 08 · Tech stack and component choices

> 中文: [08 · 技术栈与选型](../08-tech-stack.md)

> This chapter is for people who want to modify or evaluate the project: what it uses, why, and
> why not something fancier.

## 1. The whole picture

```
┌──────────────────────────────────────────────────────────────────┐
│ L5 Observe   embedded HTML/JS dashboard (no build) · curve · CSV  │
│              · event stream                                       │
├──────────────────────────────────────────────────────────────────┤
│ L4 Access    container devices passthrough · b60-run (lease       │
│              runner) · b60-check                                  │
├──────────────────────────────────────────────────────────────────┤
│ L3 Control   b60-guard: FastAPI + uvicorn + SQLite (single         │
│              container) · policy machine · leases · pollution      │
│              watchdog · sampling · consumer profiling             │
├──────────────────────────────────────────────────────────────────┤
│ L2 Nodes     /dev/dri ⇄ /dev/b60 rename (not a proxy, not a        │
│              symlink)                                             │
├──────────────────────────────────────────────────────────────────┤
│ L1 Kernel    xe driver · PCIe ASPM · runtime PM · debugfs · hwmon  │
├──────────────────────────────────────────────────────────────────┤
│ L0 Firmware  BIOS: PEG ASPM=L1 · Native ASPM=Disabled · L1SS=Off   │
└──────────────────────────────────────────────────────────────────┘
```

**Not one layer is new code.** L0/L1 are existing firmware and kernel capability, L2 is two `mv`s,
L3 is a few hundred lines of Python, L4 is bash, and L5 is one HTML page assembled server-side.

## 2. Component inventory

| Component | Version / form | Role | Why this |
|---|---|---|---|
| BIOS ASPM | Motherboard firmware setting | Give the root port L1 | The kernel cannot conjure capability bits for the root port; this is the only entry point |
| `xe` driver | In-tree kernel | Real power management | The only upstream driver for B60; `i915` doesn't support BMG |
| debugfs residency counters | `dgfx_pcie_link_residencies` etc. | Verify the link really sleeps | The only kernel interface that exposes L1 residency directly |
| hwmon energy counters | `energy1_input` / `energy2_input` | Measure power | Cheaper than an external power meter, and splits by domain (card / pkg) |
| `mv` (rename) | coreutils | Move nodes | Same-filesystem rename doesn't change inode / permissions / device number; open fds are unaffected |
| **FastAPI + uvicorn** | Python 3.12 / `python:3.12-slim` | HTTP control plane + dashboard | Runs as a single file, JSON serialization built in; no frontend build chain |
| **SQLite** | stdlib `sqlite3` | Samples, events, leases, work sessions | Single-writer, zero ops; no extra service |
| **bash 5** | System-provided | `b60-run` / `b60-check.sh` / `b60-detect.sh` | Guaranteed present on the target; more reliable than adding another runtime |
| Docker | Already on the host | Packaging and isolation | Needs `privileged` + `pid=host` to move `/dev` entries and run `nsenter` |
| Embedded HTML/JS | Server-side strings | Dashboard | No npm, no build, no CDN; works offline |

External dependencies are only two pip packages: `fastapi` and `uvicorn[standard]`. The image
additionally installs `util-linux` (for `nsenter`).

## 3. Runtime shape

```bash
docker run -d --name b60-guard \
  --restart unless-stopped \
  --privileged --pid=host \
  -v /dev:/dev -v /sys:/sys:ro \
  -v <repo>/state.json:/app/state.json \
  -v <repo>/data:/app/data \
  -e RECORD_INTERVAL=15 -e RETENTION_DAYS=30 \
  -p 41966:8000 b60-guard:1.3
```

| Flag | Why it's needed |
|---|---|
| `--privileged` | Must `mv` nodes under `/dev` and read debugfs |
| `--pid=host` | Map holder PIDs back to container names via `/proc/<pid>/cgroup` |
| `-v /dev:/dev` | What is being moved is the host's `/dev` |
| `-v /sys:/sys:ro` | Read sysfs / hwmon (read-only is enough) |
| `state.json` | Desired state, applied automatically after a container restart → doubles as boot persistence |

## 4. Data model

| Table | Key fields | Write cadence | Retention |
|---|---|---|---|
| `samples` | `t, card_w, pkg_w, l1_pct, clients` | 15 s (interval energy mean) | 30 days |
| `events` | `t, level, msg` | Event-driven | 30 days |
| `leases` | `id, reason, created, until, who` | Acquire / release | Deleted on expiry |
| `sessions` | `owner, start, end, util` | From engine time ≥1% to end | 30 days |

At 15 s per row: roughly 5,760 rows/day, 170k rows per 30 days, `history.db` in the low tens of MB.

## 5. Rejected approaches (and why)

| Approach | Why rejected |
|---|---|
| **CUSE fake DRM node** | DRM depends on `mmap` and CUSE doesn't support it → dies on the first `mmap` |
| **Forging `/sys/class/drm/` entries** | sysfs is kernel-generated, not writable from userspace; `libdrm` enumeration ignores fake `/dev` nodes |
| **udev rename rules** | Only changes the `/dev` name, not the enumeration result; also conflicts with `libdrm`'s `st_rdev ↔ devnum` validation |
| **`LD_PRELOAD` intercepting `open()`** | Only affects injected processes; system services are out of reach; statically linked / non-glibc binaries bypass it |
| **fanotify interception** | Requires every mount point to be monitored; conflicts with Docker's mount namespace |
| **BPF LSM denying `file_open`** | Theoretically possible but needs `CONFIG_BPF_LSM` + BTF, and hook behaviour drifts across kernel versions; the target kernel doesn't qualify |
| **Patching the driver / adding a quirk** | Requires a rebuild on every kernel upgrade; risk out of proportion to reward |
| **`rmmod xe`** | All containers lose the GPU; unload/reload also triggers the Battlemage warm-reset problem |
| **In-process "unload the model when idle"** | OpenVINO caches the GPU context at process level; `gc` can't reclaim it. Measured to crash the process too |
| **Stopping the system services** | Breaks NAS dashboard functionality, and they come back after upgrade/reboot — unsustainable |
| **Prometheus + Grafana** | Two extra services to watch one 8 W curve; ops cost far exceeds the benefit. SQLite + an embedded page is enough |

## 6. Security and exposure surface

- The dashboard has **no authentication** and listens on the local network by default. To expose it
  publicly, put it behind a reverse proxy with auth.
- The container is `privileged`, equivalent to host root: deploy only on trusted machines.
- The project stores no credentials; `deploy.sh` accepts no password.
- The API has no rate limiting; `POST /api/mask` and `/api/policy` immediately change device
  visibility — never expose them to an untrusted network.

## 7. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `B60_BDF` | `0000:03:00.0` | GPU PCI address |
| `B60_SERVICES` | `sysinfo_service,resmon_service,mediasrv` | System services that poll the GPU |
| `B60_API` | `http://127.0.0.1:41966` | Address clients use to reach the control plane |
| `B60_DRI_NODE` | `/dev/dri/renderD129` | Node path to wait for in the exposed state |
| `B60_RENDER_DEVS` | `226:129` | Device number (find yours with `scripts/b60-detect.sh`) |
| `B60_REAL_NODE` | `/dev/b60/render` | Real card path in the masked state |
| `SAMPLE_INTERVAL` / `IDLE_INTERVAL` | `2` / `15` | Sampling interval while held / while idle (seconds) |
| `RECORD_INTERVAL` | `15` | Storage interval (seconds) |
| `RETENTION_DAYS` | `30` | History retention in days |
| `WATCHDOG_L1` / `WATCHDOG_HOLD` | `40` / `3` | Pollution threshold (%) / consecutive samples |
| `AUTO_EVICT` | `1` | Auto-evict on pollution |
| `LEASE_MAX_S` | `3600` | Maximum lease duration |
| `SWITCH_DEBOUNCE_S` | `15` | Switch debounce (seconds) |

## 8. If you want to take it further

Ordered by return on effort:

1. **Reverse proxy + auth**: right now it's bare on the internal network.
2. **Multi-GPU support**: the state machine assumes a single B60; multiple cards need a BDF
   dimension in state and API.
3. **udev event-driven detection**: pollution is found by polling today; a `netlink` socket would
   react faster.
4. **systemd unit instead of `state.json`**: more natural in non-Docker environments.
5. **Metrics export**: a Prometheus-compatible `/metrics` endpoint for existing monitoring stacks.

---

← [07 Measurement](07-measurement.md) · Back to [README](../../README.md)
