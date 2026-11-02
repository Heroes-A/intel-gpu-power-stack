# 06 · Integration guide

> 中文: [06 · 接入指南](../06-integration.md)

> Goal: installing something new that needs the GPU should not require understanding the previous
> four chapters — copy two lines of config and it works.
> And when you're done you don't need to "turn anything off" — it goes back to sleep on its own.

## 1. First confirm the control plane is alive

```bash
curl -s http://127.0.0.1:41966/healthz
curl -s http://127.0.0.1:41966/api/status | jq '.mask, .dispatch, .power.card'
```

Healthy: `mask.enabled=true`, `dispatch.state="SLEEP"`, `power.card ≈ 8`.

## 2. Container access (recommended)

### 2.1 Two lines of config

```yaml
services:
  my-gpu-app:
    image: my/app:latest
    devices:
      - /dev/b60/render:/dev/dri/renderD128
```

- The left side is **always** `/dev/b60/render` (the permanent path to the real card).
- The right side is arbitrary, but mapping to `/dev/dri/renderD128` is easiest — many images
  hardcode that path.
- No `privileged`, no `network host`, no `B60_*` environment variables needed.

### 2.2 Self-check

```bash
sudo bash scripts/b60-check.sh my-gpu-app
```

Five checks; exit code 0 means all green:

| # | Check | What failure means |
|---|---|---|
| 1 | Host `/dev/b60/render` exists and its device number matches `B60_RENDER_DEVS` | Control plane didn't start, or this isn't the B60 |
| 2 | Container `HostConfig.Devices` contains the real card path | Wrong mount (mounting `/dev/dri` is the most common mistake) |
| 3 | Container `/dev/dri` has a render node with a matching device number | You mounted the iGPU |
| 4 | `DRM_IOCTL_VERSION` opens inside the container and reports `xe` | Permission / driver problem |
| 5 | The container appears in `/api/status` | Informational only: an idle process doesn't hold the card anyway |

### 2.3 Common mistakes

| Pattern | Consequence |
|---|---|
| `- /dev/dri:/dev/dri` | While masked there is no B60 inside the container; you get the iGPU → runs, but extremely slow |
| `- /dev/dri/renderD129:/dev/dri/renderD129` | The host path disappears after masking; container fails to start |
| `privileged: true` for convenience | Works, but unnecessary — and it exposes every device on the host to the container |
| Running as a non-root user | Needs `group_add: ["<render gid>"]` (host: `getent group render`) |

## 3. Host commands (`b60-run`)

```bash
# Default: acquire a 15-minute lease → wait for the node → run → release
b60-run ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD129 -i in.mp4 out.mp4

# Explicit lease duration (seconds, 30–3600)
b60-run --ttl 1800 vainfo

# Direct: no lease, the command uses /dev/b60/render itself
b60-run --direct my-app --render=/dev/b60/render
```

Lifecycle:

| Moment | What happens |
|---|---|
| Start | `POST /api/lease`, `who=b60-run`, visible on the dashboard |
| 0–~20 s | Wait for `/dev/dri/renderD129` to appear (15 s debounce + move); start as soon as it does |
| Running | Process ⇄ kernel directly; `b60-guard` proxies no ioctls |
| Command exits | `DELETE /api/lease`; dashboard returns to SLEEP after ~20 s |
| Ctrl-C / `timeout` | `INT/TERM/HUP` traps release as a fallback |
| **Launcher killed with `kill -9`** | The independent watchdog notices the parent is gone (reads State from `/proc/<pid>/status`, recognizes zombies too) and releases on its behalf within ~4 s |
| Watchdog also dies | TTL fallback (max 60 minutes) |

Exit codes pass through unchanged, so it composes with `if` / `set -e`.

> **It will not fall back to manual `mv`.** If the control plane is unreachable it exits with an
> error — because moving nodes by hand desynchronizes the state file and misaligns the next
> dispatch.

## 4. Notes per framework

| Framework | Key points |
|---|---|
| **VAAPI / ffmpeg** | `-vaapi_device /dev/dri/renderD128` (the in-container path); leave `LIBVA_DRIVER_NAME` empty in most cases |
| **OpenVINO / OVMS** | Picks the first render node under `/dev/dri`; no extra variables needed in the container |
| **oneAPI / Level-Zero** | Needs `render` group permission; `sycl-ls` should list the B60 |
| **PyTorch (xpu)** | Same as above; first kernel compilation holds the GPU for a long time — consider `--ttl 1800` |
| **Jellyfin/Emby hardware decoding** | These are usually launched by system services and only look in `/dev/dri` → must go through `b60-run` or an explicit lease |

## 5. Troubleshooting quick reference

| Symptom | Where to look |
|---|---|
| Dashboard won't load | `docker ps \| grep b60-guard`; host firewall; whether a browser/OS proxy is also proxying private IPs (compare with `curl --noproxy '*'`) |
| Power stuck at 40 W+ | `curl -s .../api/events \| grep WARN`: an exposure not released? A service holding and polling? A watchdog alarm? |
| L1 residency in single digits | Check whether a system service appears among holders; confirm the B60 nodes have been moved out of `/dev/dri` |
| Container runs but very slow | You mounted the iGPU. `b60-check.sh <container>` points it out directly |
| `b60-run` reports control plane unreachable | Container isn't running; `sudo bash scripts/deploy.sh` to bring it back (masked state is unaffected) |
| A lease never disappears | Normally self-releases within 20 s; otherwise force it with `DELETE /api/lease?id=<id>` or wait for the TTL |
| No history curve on the dashboard | Wait for ≥2 sampling periods (15 s each); check the `data/` mount |

---

← [05 Leases and policy](05-lease-policy.md) · Next: [07 Measurement](07-measurement.md) →
