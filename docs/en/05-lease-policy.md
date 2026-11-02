# 05 · Leases and the policy machine

> 中文: [05 · 租约与策略机](../05-lease-policy.md)

> The previous chapter solved "stay efficient at idle"; this one solves "how to get the card when
> you need it".
> The answer is not intercepting `open()` — it's **making the decision before `open()` happens**.

## 1. Two states

| State | Node location | Visible to system services | Power | Who uses it |
|---|---|---|---|---|
| **SLEEP** | `/dev/b60/{card,render}` | ✗ | ~8 W | Containers (direct path) |
| **WORK** | `/dev/dri/{card,renderD*}` | ✓ | ~42 W | System-level tasks + containers |

Two states only, and only one thing changes: **which directory the nodes live in.**
There is no third intermediate state and no "half masked".

## 2. Policy

| Policy | Behaviour | When to use |
|---|---|---|
| `auto` (default) | Lease → WORK; no lease → SLEEP | Everyday |
| `masked` | Force SLEEP, ignore leases | Troubleshooting / when absolute low power matters |
| `exposed` | Force WORK | Long system-level transcoding |

```bash
curl -s -X POST http://127.0.0.1:41966/api/policy \
     -H 'Content-Type: application/json' -d '{"policy":"auto"}'
```

## 3. Leases

A lease is a **time-boxed exposure window**, stored in SQLite so it survives process restarts.

```bash
curl -s -X POST http://127.0.0.1:41966/api/lease \
     -H 'Content-Type: application/json' \
     -d '{"ttl":900,"reason":"example: media transcode","who":"cli"}'
# → {"id":"a1b2c3d4", ...}

curl -s -X DELETE 'http://127.0.0.1:41966/api/lease?id=a1b2c3d4'
```

| Field | Constraint |
|---|---|
| `ttl` | Clamped server-side to `[30, 3600]` seconds |
| `reason` | Shown on the dashboard for humans |
| `who` | `b60-run` / `cli` / `api`, used for attribution |

**Why leases instead of "switch when a call is detected"?**
Because the "call" happens in `open()`, and `open()` happens in the kernel — userspace has no
reliable interception point (`inotify` can't see `open`, `fanotify` needs whole-filesystem mounts
and conflicts with Docker, `LD_PRELOAD` only affects injected processes).
So the decision point is **moved forward to task startup**: the launcher requests the window
first, then starts the task.

## 4. Debounce

Switching restarts system services (to release stale fds) and is expensive, so the policy machine
has two levels of debounce:

- The target state must **hold continuously for 15 seconds** (`SWITCH_DEBOUNCE_S`) before acting;
- At least 15 seconds between two switches;
- Decisions run every 3 seconds (`_dispatch_once`).

This prevents flapping when a lease is created and released immediately.

## 5. Pollution watchdog

While masked, if L1 residency falls below **40%** and a client exists, three consecutive samples
(~45 s) declare "pollution":

```
WARN  while masked, L1 is only 12% (threshold 40%), holders: container foo, systemd xxx
```

- By default it only warns; it does not act (`AUTO_EVICT=0` disables auto-eviction entirely).
- With auto-eviction on: re-mask (idempotent move + restart system services) and evict the newly
  appeared poller.
- Typical triggers: udev recreated `/dev/dri` nodes, or a newly installed service started
  scanning the GPU.

## 6. Consumer profiling

The "who is using it" on the dashboard is not a guess:

```
pid ──/proc/<pid>/cgroup──▶ container name / systemd unit / host process
  └──/proc/<pid>/fdinfo/*──▶ drm-engine-* delta ──▶ working / merely enumerating
```

| Role | Criterion | Effect on power |
|---|---|---|
| Idle (enumerating) | Node open, engine time 0 | None (link sleeps anyway) |
| Working | Engine time ≥1% | Yes (link leaves L1) |

Work sessions are accounted on that basis: start at ≥1% engine time, end after 30 s continuously
at zero. So "who actually used the GPU, and when" is reviewable, without digging through logs.

## 7. Boot persistence

`state.json` holds the **desired state** (masked or not, plus policy).
The container applies it once at startup, which is equivalent to a boot script — no extra systemd
unit, and no fighting with container restart policies.

```json
{"mask_enabled": true, "policy": "auto"}
```

---

← [04 Node masking](04-node-masking.md) · Next: [06 Integration guide](06-integration.md) →
