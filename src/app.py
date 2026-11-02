#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B60 Power Guard —— 需求感知调度器（Demand-aware Dispatcher）
实测平台：Debian 12 系 NAS · Intel Arc Pro B60 · BDF 0000:03:00.0（可用 B60_BDF 覆盖）

设计前提（经实测确立，见 docs/07-measurement.md）
  · 掩藏节点后：整卡 8W / L1 驻留 99%，容器经 /dev/b60 仍可零延迟使用。
  · 暴露节点后：系统服务恢复轮询 → 42W（L1 被打断），但容器使用体验无差别。
  推论：**容器需求不需要切换设备状态**，切换只会白白抬高功耗；
        唯一需要切换的场景是「系统级任务确实要用 B60」（如影视转码指定独显）。

本版新增
  1) 消费者画像：DRM 客户端 → 归属（容器名 / systemd 单元 / 宿主进程）+ 角色（干活中 / 仅识别）。
  2) 工作时段记录：引擎占用 ≥1% 视为干活，自动开/闭 session 落库，可回看谁在何时真用过。
  3) 租约（Lease）：POST /api/lease 预约「暴露窗口」，到期或释放后自动回到睡眠态。
     这是「用则切、闲则睡」的可实现形态——切换点在任务开始前，而非 open 那一刻（后者内核层才做得到）。
  4) 策略机：auto（按租约自动）/ masked（强制睡眠）/ exposed（强制暴露），带迟滞与防抖。
  5) 污染看门狗：睡眠态下 L1 跌破阈值且出现持卡者 → WARN，可选自动重掩藏驱逐。
  6) v1.3 接入层：面板内置「接入指南」、租约列表显示 who。配套 b60-run / b60-check.sh /
     templates，把「新项目接入 B60」变成照抄即可。

采样策略
  无人持卡时降频到 IDLE_INTERVAL，避免监控自身高频读 hwmon 把设备唤醒（实测 12W → 31W）。
"""
import glob
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
import uvicorn

VERSION = "1.3"

BDF = os.environ.get("B60_BDF", "0000:03:00.0")
DEV_DRI = os.environ.get("DEV_DRI", "/dev/dri")
DEV_B60 = os.environ.get("DEV_B60", "/dev/b60")
STATE_FILE = os.environ.get("STATE_FILE", "/app/state.json")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DB_FILE = os.path.join(DATA_DIR, "history.db")
# 会周期轮询 DRM 设备的系统服务；不同发行版/厂商不同，可用 B60_SERVICES 覆盖
SERVICES = [x.strip() for x in os.environ.get(
    "B60_SERVICES", "sysinfo_service,resmon_service,mediasrv").split(",") if x.strip()]

SAMPLE_INTERVAL = float(os.environ.get("SAMPLE_INTERVAL", "2"))
IDLE_INTERVAL = float(os.environ.get("IDLE_INTERVAL", "15"))
RECORD_INTERVAL = float(os.environ.get("RECORD_INTERVAL", "15"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
ALERT_W = float(os.environ.get("ALERT_W", "25"))
ALERT_HOLD_S = 300

WORK_UTIL = float(os.environ.get("WORK_UTIL", "1.0"))     # 引擎占用 ≥1% 视为真正干活
SESSION_GAP = float(os.environ.get("SESSION_GAP", "30"))  # 超过 30s 无占用 → 结束工作时段
LEASE_MAX_S = int(os.environ.get("LEASE_MAX_S", "3600"))
SWITCH_DEBOUNCE_S = float(os.environ.get("SWITCH_DEBOUNCE_S", "15"))
WATCHDOG_L1 = float(os.environ.get("WATCHDOG_L1", "40"))  # 睡眠态 L1 占比低于此值视为被污染
WATCHDOG_HOLD = int(os.environ.get("WATCHDOG_HOLD", "3")) # 连续 N 次判定（约 45s）
AUTO_EVICT = os.environ.get("AUTO_EVICT", "1") == "1"

_lock = threading.RLock()
_now = {"vals": deque(maxlen=90)}

_state = {
    "version": VERSION,
    "power": {"card": None, "pkg": None, "vram": None,
              "avg60": None, "min60": None, "max60": None},
    "temp_c": None,
    "l1": {"pct": None, "residency": None, "residency_l12": None},
    "runtime": {"status": None, "control": None, "usage": None},
    "clients": [],
    "consumers": [],
    "sessions_active": [],
    "services": {},
    "mask": {"enabled": None, "detail": ""},
    "dispatch": {"policy": "auto", "state": "SLEEP", "reason": "初始化",
                 "since": None, "last_switch": None, "pending": None},
    "leases": [],
    "sampling": "normal",
    "db": {"samples": None, "size_kb": None},
    "updated": None,
    "error": None,
}

app = FastAPI(title="b60-power-guard", version=VERSION)


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def db_init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db_connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS samples(
            ts INTEGER PRIMARY KEY, card REAL, pkg REAL, vram REAL,
            l1 REAL, temp REAL, clients INTEGER, mask INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            ts INTEGER, level TEXT, msg TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts INTEGER, end_ts INTEGER,
            consumer TEXT, owner TEXT, klass TEXT, seconds INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS leases(
            id TEXT PRIMARY KEY, reason TEXT, created INTEGER, until INTEGER, who TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts)")


def log_event(level, msg):
    ts = int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    print("[%s] %s: %s" % (stamp, level, msg), flush=True)
    try:
        with db_connect() as c:
            c.execute("INSERT INTO events VALUES(?,?,?)", (ts, level, msg))
    except sqlite3.Error as e:
        print("事件写库失败: %s" % e, flush=True)


def db_housekeeping():
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    try:
        with db_connect() as c:
            c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM sessions WHERE end_ts < ?", (cutoff,))
            c.execute("DELETE FROM leases WHERE until < ?", (int(time.time()) - 86400,))
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# 宿主命令
# ---------------------------------------------------------------------------
def host_run(args, timeout=20):
    last = None
    for prefix in (["nsenter", "-t", "1", "-m"], []):
        try:
            return subprocess.run(prefix + args, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            last = e
            continue

    class _Fake:
        returncode = 127
        stdout = ""
        stderr = str(last)
    return _Fake()


def service_states():
    out = {}
    for s in SERVICES:
        try:
            p = host_run(["systemctl", "is-active", s], timeout=8)
            out[s] = (p.stdout or "").strip() or "unknown"
        except Exception:
            out[s] = "unknown"
    return out


# ---------------------------------------------------------------------------
# 设备节点与掩藏
# ---------------------------------------------------------------------------
def _b60_kernel_names():
    card = render = None
    try:
        card = os.path.basename(os.readlink(os.path.join(DEV_DRI, "by-path", "pci-%s-card" % BDF)))
    except OSError:
        pass
    try:
        render = os.path.basename(os.readlink(os.path.join(DEV_DRI, "by-path", "pci-%s-render" % BDF)))
    except OSError:
        pass
    return card or os.environ.get("B60_CARD", "card0"), render or os.environ.get("B60_RENDER", "renderD129")


def mask_status():
    card, render = _b60_kernel_names()
    in_dri = os.path.exists(os.path.join(DEV_DRI, card)) or os.path.exists(os.path.join(DEV_DRI, render))
    in_b60 = os.path.exists(os.path.join(DEV_B60, "card")) or os.path.exists(os.path.join(DEV_B60, "render"))
    if in_b60 and not in_dri:
        return True, "节点位于 /dev/b60（系统服务不可见）"
    if in_dri:
        return False, "节点位于 /dev/dri（系统服务可见）"
    return None, "节点缺失（%s/%s 均未找到）" % (card, render)


def _save_state_file():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"mask_enabled": _state["mask"]["enabled"] is True,
                       "policy": _state["dispatch"]["policy"]}, f)
    except OSError as e:
        log_event("WARN", "状态文件写入失败: %s" % e)


def apply_mask(enable, reason=""):
    """搬移节点 + 重启三服务。返回 (ok, log_lines)。"""
    logs = []
    card, render = _b60_kernel_names()
    os.makedirs(DEV_B60, exist_ok=True)
    moved = False
    pairs = [(card, "card"), (render, "render")]

    if enable:
        for src_name, dst_name in pairs:
            src, dst = os.path.join(DEV_DRI, src_name), os.path.join(DEV_B60, dst_name)
            if os.path.lexists(src):
                if os.path.lexists(dst):
                    os.remove(dst)
                os.rename(src, dst)
                moved = True
                logs.append("mv %s → %s" % (src, dst))
    else:
        for src_name, dst_name in pairs:
            src, dst = os.path.join(DEV_B60, dst_name), os.path.join(DEV_DRI, src_name)
            if os.path.lexists(src):
                if os.path.lexists(dst):
                    os.remove(dst)
                os.rename(src, dst)
                moved = True
                logs.append("mv %s → %s" % (src, dst))

    if not moved:
        logs.append("节点已在目标位置（幂等跳过搬移）")

    for s in SERVICES:
        p = host_run(["systemctl", "restart", s], timeout=30)
        logs.append("systemctl restart %s → rc=%d" % (s, p.returncode))
    time.sleep(3)

    ok, detail = mask_status()
    logs.append("当前状态: %s" % detail)
    log_event("INFO", "%s%s → %s（搬移=%s）"
              % ("掩藏开启" if enable else "掩藏关闭",
                 ("｜%s" % reason) if reason else "", detail, moved))

    with _lock:
        _state["mask"] = {"enabled": ok, "detail": detail}
    _save_state_file()
    return (ok is enable), logs


# ---------------------------------------------------------------------------
# 消费者画像：DRM 客户端 → 容器 / systemd / 宿主进程
# ---------------------------------------------------------------------------
_docker = {"ts": 0, "map": {}}


def _docker_map():
    now = time.time()
    if now - _docker["ts"] < 60 and _docker["map"]:
        return _docker["map"]
    p = host_run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"], timeout=12)
    m = {}
    for ln in (p.stdout or "").splitlines():
        parts = ln.split()
        if len(parts) >= 2:
            m[parts[0].strip()] = parts[1].strip()
    if m or now - _docker["ts"] > 300:
        _docker.update({"ts": now, "map": m})
    return _docker["map"]


def _owner_of(pid):
    """返回 (klass, name)：klass ∈ docker / system / other。"""
    try:
        with open("/proc/%s/cgroup" % pid) as f:
            txt = f.read()
    except OSError:
        return "other", "已退出"
    m = re.search(r"docker[-/]([0-9a-f]{12,64})", txt)
    if m:
        cid = m.group(1)
        dmap = _docker_map()
        name = dmap.get(cid)
        if not name:
            for k, v in dmap.items():
                if k.startswith(cid[:12]) or cid.startswith(k[:12]):
                    name = v
                    break
        return "docker", name or ("容器 " + cid[:8])
    m = re.search(r"/([\w@.\-]+)\.service", txt)
    if m:
        return "system", m.group(1)
    return "other", "宿主进程"


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------
def _hwmon_dir():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if BDF in os.readlink(os.path.join(d, "device")):
                return d
        except OSError:
            continue
    return None


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _clients():
    path = "/sys/kernel/debug/dri/%s/clients" % BDF
    try:
        with open(path) as f:
            lines = f.read().strip().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) >= 3:
            out.append({"cmd": parts[0], "pid": parts[1], "dev": parts[2]})
    return out


def _fdinfo_engine_ns(pid):
    total = 0
    try:
        for fd in glob.glob("/proc/%s/fdinfo/*" % pid):
            try:
                with open(fd) as f:
                    for line in f:
                        if line.startswith("drm-engine-"):
                            parts = line.split()
                            if len(parts) >= 2:
                                total += int(parts[1])
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    return total


def _l1_residency():
    path = "/sys/kernel/debug/dri/%s/dgfx_pcie_link_residencies" % BDF
    try:
        with open(path) as f:
            txt = f.read()
    except OSError:
        return None, None, None
    def grab(k):
        m = re.search(k + r" RESIDENCY\s*:\s*(\d+)", txt)
        return int(m.group(1)) if m else None
    return grab("L0"), grab("L1"), grab("L1\\.2")


_prev = {}
_rec = {"t": None, "e1": None, "e2": None, "l0": None, "l1": None, "clients": 0}
_alert = {"since": None, "last_log": 0}
_sess = {}          # key -> {start,last,owner,klass,cmd}
_wd = {"bad": 0, "grace": time.time() + 120}


def _session_tick(consumers, now):
    """引擎占用 ≥ WORK_UTIL 视为干活：开/闭工作时段并落库。"""
    seen = set()
    for c in consumers:
        key = c["key"]
        if (c.get("util") or 0) >= WORK_UTIL:
            seen.add(key)
            s = _sess.get(key)
            if s is None:
                _sess[key] = {"start": now, "last": now, "owner": c["owner"],
                              "klass": c["klass"], "cmd": c["cmd"]}
            else:
                s["last"] = now
                s["owner"] = c["owner"]
    for key, s in list(_sess.items()):
        if key in seen:
            continue
        if now - s["last"] < SESSION_GAP:
            continue
        dur = int(s["last"] - s["start"])
        del _sess[key]
        if dur < 5:
            continue
        try:
            with db_connect() as c:
                c.execute("INSERT INTO sessions(start_ts,end_ts,consumer,owner,klass,seconds)"
                          " VALUES(?,?,?,?,?,?)",
                          (int(s["start"]), int(s["last"]), s["cmd"], s["owner"], s["klass"], dur))
        except sqlite3.Error as e:
            print("时段写库失败: %s" % e, flush=True)
        if dur >= 30:
            log_event("INFO", "工作时段：%s（%s）%s–%s 共 %d 分 %d 秒"
                      % (s["owner"], s["cmd"],
                         time.strftime("%H:%M:%S", time.localtime(s["start"])),
                         time.strftime("%H:%M:%S", time.localtime(s["last"])),
                         dur // 60, dur % 60))


def _sample_once():
    global _prev
    hw = _hwmon_dir()
    now = time.time()

    power = {"card": None, "pkg": None, "vram": None}
    temp_c = None
    e1 = e2 = None
    if hw:
        e1 = _read_int(os.path.join(hw, "energy1_input"))
        e2 = _read_int(os.path.join(hw, "energy2_input"))
        for t in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            v = _read_int(t)
            if v:
                temp_c = round(v / 1000.0, 1)
                break
        if e1 is not None and "e1" in _prev:
            dt = now - _prev["t"]
            if dt > 0.1:
                card = (e1 - _prev["e1"]) / dt / 1e6
                power["card"] = round(card, 1)
                if e2 is not None and "e2" in _prev:
                    pkg = (e2 - _prev["e2"]) / dt / 1e6
                    power["pkg"] = round(pkg, 1)
                    power["vram"] = round(card - pkg, 1)
                if card >= 0:
                    _now["vals"].append((now, card))
        if e1 is not None:
            _prev["e1"], _prev["e2"], _prev["t"] = e1, e2, now

    win = [(t, v) for t, v in _now["vals"] if now - t <= 60]
    if win:
        vs = [v for _, v in win]
        power["avg60"] = round(sum(vs) / len(vs), 1)
        power["min60"] = round(min(vs), 1)
        power["max60"] = round(max(vs), 1)

    l0, l1, l12 = _l1_residency()
    l1_pct = None
    if l0 is not None and l1 is not None and "l0" in _prev:
        d0, d1 = l0 - _prev["l0"], l1 - _prev["l1"]
        if d0 + d1 > 0:
            l1_pct = round(d1 * 100.0 / (d0 + d1), 1)
    if l0 is not None:
        _prev["l0"], _prev["l1"] = l0, l1

    base = "/sys/bus/pci/devices/%s/power" % BDF
    usage = _read_str(os.path.join(base, "runtime_usage"))
    enabled, detail = mask_status()

    consumers = []
    for c in _clients():
        ns = _fdinfo_engine_ns(c["pid"])
        util = None
        key = "eng_%s" % c["pid"]
        if ns is not None:
            if key in _prev and now - _prev.get(key + "_t", 0) > 0.1:
                dt = now - _prev[key + "_t"]
                util = round(max(0.0, (ns - _prev[key]) / dt / 1e7), 1)
            _prev[key] = ns
            _prev[key + "_t"] = now
        klass, name = _owner_of(c["pid"])
        consumers.append({
            "key": "%s|%s" % (name, c["pid"]), "cmd": c["cmd"], "pid": c["pid"],
            "dev": ("card%s" % c["dev"]) if c["dev"] == "0" else ("renderD%s" % c["dev"]),
            "util": util,
            "role": "干活中" if (util or 0) >= WORK_UTIL else "仅识别",
            "klass": klass, "owner": name,
            "since": int(now - _sess["%s|%s" % (name, c["pid"])]["start"])
                     if ("%s|%s" % (name, c["pid"])) in _sess else None,
        })

    _session_tick(consumers, now)
    _record_if_due(e1, e2, l0, l1, temp_c, len(consumers), enabled)
    _watchdog_tick(enabled, l1_pct, consumers)

    with _lock:
        _state["power"] = power
        _state["temp_c"] = temp_c
        _state["l1"] = {"pct": l1_pct, "residency": l1, "residency_l12": l12}
        _state["runtime"] = {
            "status": _read_str(os.path.join(base, "runtime_status")),
            "control": _read_str(os.path.join(base, "control")),
            "usage": usage,
        }
        _state["clients"] = consumers
        _state["consumers"] = consumers
        _state["sessions_active"] = [
            {"consumer": s["cmd"], "owner": s["owner"], "klass": s["klass"],
             "seconds": int(now - s["start"])} for s in _sess.values()]
        _state["mask"] = {"enabled": enabled, "detail": detail}
        _state["sampling"] = "idle-slow" if usage == "0" else "normal"
        _state["updated"] = time.strftime("%H:%M:%S")
        _state["error"] = None if hw else "未找到 B60 的 hwmon（%s）" % BDF


def _record_if_due(e1, e2, l0, l1, temp_c, nclients, enabled):
    now = time.time()
    if e1 is None:
        return
    if _rec["t"] is None:
        _rec.update({"t": now, "e1": e1, "e2": e2, "l0": l0, "l1": l1})
        return
    span = now - _rec["t"]
    if span < RECORD_INTERVAL:
        _rec["clients"] = max(_rec["clients"], nclients)
        return
    card = (e1 - _rec["e1"]) / span / 1e6
    pkg = (e2 - _rec["e2"]) / span / 1e6 if (e2 is not None and _rec["e2"] is not None) else None
    vram = (card - pkg) if pkg is not None else None
    l1p = None
    if l0 is not None and l1 is not None and _rec["l0"] is not None:
        d0, d1 = l0 - _rec["l0"], l1 - _rec["l1"]
        if d0 + d1 > 0:
            l1p = round(d1 * 100.0 / (d0 + d1), 1)
    try:
        with db_connect() as c:
            c.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)",
                      (int(now), round(card, 2), None if pkg is None else round(pkg, 2),
                       None if vram is None else round(vram, 2), l1p, temp_c,
                       _rec["clients"], 1 if enabled else 0))
        _dbstat["n"] += 1
        if _dbstat["n"] % 20 == 0:
            try:
                _dbstat["size_kb"] = os.path.getsize(DB_FILE) // 1024
            except OSError:
                pass
            with _lock:
                _state["db"] = {"samples": _dbstat["n"], "size_kb": _dbstat["size_kb"]}
    except sqlite3.Error as e:
        print("采样写库失败: %s" % e, flush=True)

    if enabled and card >= ALERT_W:
        if _alert["since"] is None:
            _alert["since"] = now
        elif now - _alert["since"] >= ALERT_HOLD_S and now - _alert["last_log"] > 1800:
            _alert["last_log"] = now
            log_event("WARN", "整卡功耗持续 %.1fW（>%.0fW 超过 %d 分钟），持卡 %d 个进程"
                      % (card, ALERT_W, ALERT_HOLD_S // 60, _rec["clients"]))
    else:
        _alert["since"] = None

    _rec.update({"t": now, "e1": e1, "e2": e2, "l0": l0, "l1": l1, "clients": nclients})


def _watchdog_tick(enabled, l1_pct, consumers):
    """睡眠态下 L1 被打断且有人持卡 → 污染告警，可选自动驱逐（重掩藏 + 重启服务）。"""
    if not enabled:
        _wd["bad"] = 0
        return
    if time.time() < _wd["grace"]:
        return
    if l1_pct is None or l1_pct >= WATCHDOG_L1:
        _wd["bad"] = 0
        return
    _wd["bad"] += 1
    if _wd["bad"] != WATCHDOG_HOLD:
        return
    who = "、".join(sorted({c["owner"] for c in consumers})) or "未知"
    log_event("WARN", "掩藏态下 L1 仅 %.0f%%（阈值 %.0f%%），持卡者：%s"
              % (l1_pct, WATCHDOG_L1, who))
    if not AUTO_EVICT:
        return
    with _lock:
        _state["dispatch"]["last_switch"] = time.time()
    # 重新掩藏（幂等搬移 + 重启三服务）→ 驱逐新出现的轮询者
    threading.Thread(target=_evict, args=(who,), daemon=True).start()


def _evict(who):
    if not _lock.acquire(blocking=False):
        return
    try:
        log_event("WARN", "自动驱逐：重掩藏并重启系统服务（触发者 %s）" % who)
        apply_mask(True, reason="污染自动驱逐")
    finally:
        _lock.release()


def _sampler():
    n = 0
    while True:
        try:
            _sample_once()
            n += 1
            if n % 8 == 1:
                with _lock:
                    _state["services"] = service_states()
                _housekeeping_tick()
        except Exception as e:
            with _lock:
                _state["error"] = "采样异常: %s" % e
            log_event("ERROR", "采样异常: %s" % e)
        with _lock:
            slow = _state["sampling"] == "idle-slow"
        time.sleep(IDLE_INTERVAL if slow else SAMPLE_INTERVAL)


_hk = {"last": 0}
_dbstat = {"n": 0, "size_kb": 0}


def _dbstat_init():
    try:
        with db_connect() as c:
            _dbstat["n"] = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        _dbstat["size_kb"] = os.path.getsize(DB_FILE) // 1024
    except (sqlite3.Error, OSError):
        pass
    with _lock:
        _state["db"] = {"samples": _dbstat["n"], "size_kb": _dbstat["size_kb"]}


def _housekeeping_tick():
    now = time.time()
    if now - _hk["last"] < 3600:
        return
    _hk["last"] = now
    db_housekeeping()
    _dbstat_init()


# ---------------------------------------------------------------------------
# 租约与调度器
# ---------------------------------------------------------------------------
def load_leases():
    try:
        with db_connect() as c:
            rows = c.execute("SELECT id,reason,created,until,who FROM leases ORDER BY until").fetchall()
    except sqlite3.Error:
        return []
    now = int(time.time())
    return [{"id": r[0], "reason": r[1], "created": r[2], "until": r[3],
             "who": r[4], "left": max(0, r[3] - now)} for r in rows if r[3] > now]


def add_lease(ttl, reason="", who="api"):
    ttl = max(30, min(int(ttl), LEASE_MAX_S))
    lid = uuid.uuid4().hex[:8]
    now = int(time.time())
    with db_connect() as c:
        c.execute("INSERT INTO leases VALUES(?,?,?,?,?)", (lid, reason, now, now + ttl, who))
    log_event("INFO", "新增租约 %s：%s（%d 分钟）" % (lid, reason or "未注明", ttl // 60))
    return lid


def drop_lease(lid=None):
    with db_connect() as c:
        if lid:
            c.execute("DELETE FROM leases WHERE id=?", (lid,))
        else:
            c.execute("DELETE FROM leases")
    if lid:
        log_event("INFO", "释放租约 %s" % lid)


def _dispatch_once():
    """策略机：决定目标态并执行；带防抖，避免服务被反复重启。"""
    now = time.time()
    leases = load_leases()
    with _lock:
        pol = _state["dispatch"]["policy"]
        cur = _state["dispatch"]["state"]
    if pol == "masked":
        target, reason = "SLEEP", "强制睡眠策略"
    elif pol == "exposed":
        target, reason = "WORK", "强制暴露策略"
    else:
        if leases:
            target = "WORK"
            reason = "租约生效（%s，剩 %d 分）" % (leases[0]["reason"] or leases[0]["id"],
                                              max(1, leases[0]["left"] // 60))
        else:
            target, reason = "SLEEP", "无租约，保持睡眠"

    with _lock:
        _state["leases"] = leases
        pend = _state["dispatch"].get("pending")
        if target == cur:
            _state["dispatch"]["pending"] = None
            return
        if not pend or pend.get("target") != target:
            _state["dispatch"]["pending"] = {"target": target, "since": now, "reason": reason}
            return
        if now - pend["since"] < SWITCH_DEBOUNCE_S:
            return
        if now - (_state["dispatch"].get("last_switch") or 0) < SWITCH_DEBOUNCE_S:
            return
        _state["dispatch"]["pending"] = None
        _state["dispatch"]["last_switch"] = now

    if not _lock.acquire(blocking=False):
        return
    try:
        ok, logs = apply_mask(target == "SLEEP", reason=reason)
        for ln in logs:
            print("[dispatch] " + ln, flush=True)
        with _lock:
            _state["dispatch"].update({
                "state": target, "reason": reason, "since": now,
                "pending": None, "last_switch": time.time()})
        log_event("INFO" if ok else "WARN",
                  "调度器：%s → %s（%s）" % (cur, target, reason))
    except Exception as e:
        log_event("ERROR", "调度器切换异常: %s" % e)
    finally:
        _lock.release()


def _dispatcher():
    time.sleep(5)
    while True:
        try:
            _dispatch_once()
        except Exception as e:
            log_event("ERROR", "调度循环异常: %s" % e)
        time.sleep(3)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status():
    with _lock:
        snap = dict(_state)
    snap["leases"] = load_leases()
    return JSONResponse(snap)


RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}


@app.get("/api/history")
def api_history(range: str = "1h"):
    span = RANGES.get(range, 3600)
    since = int(time.time()) - span
    bucket = max(15, span // 360)
    try:
        with db_connect() as c:
            rows = c.execute(
                "SELECT ts - (ts % ?) AS tb, AVG(card), MAX(card), MIN(card), "
                "AVG(pkg), AVG(l1), AVG(temp), MAX(clients) "
                "FROM samples WHERE ts >= ? GROUP BY tb ORDER BY tb",
                (bucket, since)).fetchall()
    except sqlite3.Error as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {
        "range": range, "bucket": bucket, "span": span,
        "points": [{"t": r[0], "card": None if r[1] is None else round(r[1], 2),
                    "max": None if r[2] is None else round(r[2], 2),
                    "min": None if r[3] is None else round(r[3], 2),
                    "pkg": None if r[4] is None else round(r[4], 2),
                    "l1": None if r[5] is None else round(r[5], 1),
                    "temp": r[6], "clients": r[7]} for r in rows],
    }


@app.get("/api/history.csv")
def api_history_csv(range: str = "24h"):
    span = RANGES.get(range, 86400)
    since = int(time.time()) - span
    try:
        with db_connect() as c:
            rows = c.execute("SELECT ts,card,pkg,vram,l1,temp,clients,mask FROM samples "
                             "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    except sqlite3.Error as e:
        return PlainTextResponse("error: %s" % e, status_code=500)
    out = ["time,card_w,pkg_w,vram_w,l1_pct,temp_c,clients,masked"]
    for r in rows:
        out.append("%s,%s,%s,%s,%s,%s,%s,%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0])),
            *["" if v is None else v for v in r[1:]]))
    return PlainTextResponse("\n".join(out), headers={
        "Content-Disposition": 'attachment; filename="b60-power-%s.csv"' % range})


@app.get("/api/events")
def api_events(limit: int = 100):
    limit = max(1, min(limit, 1000))
    try:
        with db_connect() as c:
            rows = c.execute("SELECT ts,level,msg FROM events ORDER BY ts DESC LIMIT ?",
                             (limit,)).fetchall()
    except sqlite3.Error as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"events": [{"ts": r[0], "time": time.strftime("%m-%d %H:%M:%S", time.localtime(r[0])),
                        "level": r[1], "msg": r[2]} for r in rows]}


@app.get("/api/sessions")
def api_sessions(limit: int = 50):
    limit = max(1, min(limit, 500))
    try:
        with db_connect() as c:
            rows = c.execute("SELECT start_ts,end_ts,consumer,owner,klass,seconds FROM sessions "
                             "ORDER BY start_ts DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"sessions": [{"start": time.strftime("%m-%d %H:%M", time.localtime(r[0])),
                          "end": time.strftime("%H:%M", time.localtime(r[1])),
                          "consumer": r[2], "owner": r[3], "klass": r[4],
                          "seconds": r[5]} for r in rows]}


@app.post("/api/mask")
def api_mask(req: dict):
    enable = bool(req.get("enable"))
    reason = str(req.get("reason") or "手动操作")
    if not _lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "logs": ["另一个操作进行中"]}, status_code=409)
    try:
        ok, logs = apply_mask(enable, reason=reason)
        with _lock:
            _state["dispatch"].update({"state": "SLEEP" if enable else "WORK",
                                       "reason": reason, "since": time.time(),
                                       "last_switch": time.time()})
        _sample_once()
        with _lock:
            _state["services"] = service_states()
        return {"ok": ok, "logs": logs}
    except Exception as e:
        log_event("ERROR", "掩藏操作异常: %s" % e)
        return JSONResponse({"ok": False, "logs": ["异常: %s" % e]}, status_code=500)
    finally:
        _lock.release()


@app.post("/api/policy")
def api_policy(req: dict):
    pol = str(req.get("policy") or "").strip()
    if pol not in ("auto", "masked", "exposed"):
        return JSONResponse({"ok": False, "error": "policy 必须是 auto/masked/exposed"},
                            status_code=400)
    with _lock:
        _state["dispatch"]["policy"] = pol
        _state["dispatch"]["pending"] = None
    _save_state_file()
    log_event("INFO", "策略切换为 %s" % pol)
    return {"ok": True, "policy": pol}


@app.post("/api/lease")
def api_lease(req: dict):
    ttl = int(req.get("ttl") or 900)
    reason = str(req.get("reason") or "")
    who = str(req.get("who") or "ui")
    lid = add_lease(ttl, reason, who)
    with _lock:
        _state["leases"] = load_leases()
    return {"ok": True, "id": lid, "ttl": ttl}


@app.delete("/api/lease")
def api_lease_release(id: str = ""):
    drop_lease(id or None)
    with _lock:
        _state["leases"] = load_leases()
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebUI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML


_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>B60 Power Guard</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --fg:#e6edf3; --dim:#8b949e;
          --green:#3fb950; --red:#f85149; --blue:#58a6ff; --amber:#d29922; --purple:#bc8cff; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         max-width:1080px; margin:0 auto; padding:20px 16px 40px; }
  h1 { font-size:18px; font-weight:600; }
  .sub { color:var(--dim); font-size:12px; margin-top:4px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:12px; margin-top:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .card h2 { font-size:12px; color:var(--dim); font-weight:500; margin-bottom:8px; }
  .big { font-size:34px; font-weight:700; font-variant-numeric:tabular-nums; }
  .unit { font-size:14px; color:var(--dim); font-weight:400; margin-left:2px; }
  .badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:13px; font-weight:600; }
  .b-green { background:rgba(63,185,80,.15); color:var(--green); }
  .b-red { background:rgba(248,81,73,.15); color:var(--red); }
  .b-amber { background:rgba(210,153,34,.15); color:var(--amber); }
  .b-blue { background:rgba(88,166,255,.15); color:var(--blue); }
  .b-gray { background:rgba(139,148,158,.15); color:var(--dim); }
  .row { display:flex; justify-content:space-between; padding:5px 0; font-size:13px; border-top:1px solid var(--border); }
  .row:first-of-type { border-top:none; }
  .row .k { color:var(--dim); }
  button { background:var(--blue); color:#fff; border:none; border-radius:8px; padding:9px 18px;
           font-size:14px; font-weight:600; cursor:pointer; width:100%; margin-top:10px; }
  button.off { background:var(--red); }
  button:disabled { opacity:.5; cursor:wait; }
  .tabs { display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap; }
  .tabs button { width:auto; margin:0; padding:5px 12px; font-size:12px; background:#21262d; color:var(--dim); }
  .tabs button.on { background:rgba(88,166,255,.2); color:var(--blue); }
  table { width:100%; font-size:12px; border-collapse:collapse; }
  td,th { padding:4px 6px; text-align:left; border-bottom:1px solid var(--border); }
  th { color:var(--dim); font-weight:500; }
  .warn { background:rgba(210,153,34,.1); border:1px solid var(--amber); border-radius:8px;
          padding:10px 12px; font-size:12px; color:var(--amber); margin-top:12px; display:none; }
  #logs { background:#010409; border:1px solid var(--border); border-radius:8px; padding:10px;
          font-family:monospace; font-size:11px; color:var(--dim); white-space:pre-wrap;
          max-height:180px; overflow:auto; margin-top:12px; display:none; }
  #chart { width:100%; height:220px; display:block; }
  .legend { display:flex; gap:16px; font-size:11px; color:var(--dim); margin-top:8px; flex-wrap:wrap; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:4px; }
  .ev { max-height:230px; overflow:auto; font-size:12px; }
  .ev-row { display:flex; gap:8px; padding:4px 0; border-bottom:1px solid var(--border); }
  .ev-t { color:var(--dim); font-family:monospace; font-size:11px; white-space:nowrap; }
  .foot { color:var(--dim); font-size:11px; margin-top:16px; text-align:center; }
  a { color:var(--blue); }
  input,select { background:#010409; color:var(--fg); border:1px solid var(--border);
                 border-radius:6px; padding:6px 8px; font-size:12px; }
  .lease { display:flex; align-items:center; gap:8px; font-size:12px; padding:4px 0;
           border-top:1px solid var(--border); }
  .lease button { width:auto; margin:0; padding:2px 8px; font-size:11px; background:#21262d; color:var(--red); }
  .flow { font-family:monospace; font-size:11px; color:var(--dim); line-height:1.7;
          background:#010409; border-radius:8px; padding:10px; margin-top:10px; }
</style>
</head>
<body>
<h1>🛡️ B60 Power Guard <span class="sub" style="display:inline">v1.3 调度器 + 接入层</span></h1>
<div class="sub">Intel Arc Pro B60 · 0000:03:00.0 · 需求感知调度 + 实时功耗 + 历史曲线 + 无感接入</div>

<div class="grid">
  <div class="card">
    <h2>整卡功耗（60s 均值 / 瞬时）</h2>
    <div class="big"><span id="pavg">--</span><span class="unit">W</span></div>
    <div class="row"><span class="k">瞬时</span><span id="pnow">--</span></div>
    <div class="row"><span class="k">60s 区间</span><span id="prange">--</span></div>
    <div class="row"><span class="k">GPU 核心 (pkg)</span><span id="pkg">--</span></div>
    <div class="row"><span class="k">显存 (VRAM)</span><span id="vram">--</span></div>
    <div class="row"><span class="k">温度</span><span id="temp">--</span></div>
  </div>

  <div class="card">
    <h2>链路状态 (ASPM)</h2>
    <div class="big"><span class="badge b-blue" id="l1badge">--</span></div>
    <div class="row"><span class="k">L1 驻留占比（采样窗）</span><span id="l1pct">--</span></div>
    <div class="row"><span class="k">runtime_status</span><span id="rtstatus">--</span></div>
    <div class="row"><span class="k">power/control · usage</span><span id="rtctrl">--</span></div>
    <div class="row"><span class="k">采样模式</span><span id="sampling">--</span></div>
  </div>

  <div class="card">
    <h2>调度状态</h2>
    <div class="big"><span class="badge b-green" id="dsbadge">--</span></div>
    <div class="sub" id="dsreason" style="margin-top:6px">--</div>
    <div class="row"><span class="k">掩藏</span><span id="maskbadge">--</span></div>
    <div class="row"><span class="k">时长</span><span id="dssince">--</span></div>
  </div>
</div>

<div class="card" style="margin-top:12px">
  <h2>策略与租约（切换的只有一件事：系统服务能不能看到 B60）</h2>
  <div class="tabs" style="margin-bottom:0">
    <button data-p="auto" onclick="setPolicy('auto',this)">自动（按租约）</button>
    <button data-p="masked" onclick="setPolicy('masked',this)">强制睡眠</button>
    <button data-p="exposed" onclick="setPolicy('exposed',this)">强制暴露</button>
  </div>
  <div class="flow">容器（推理服务 / 转写服务 / 任意映射 /dev/b60 的服务）—— 无论哪种策略，<b>始终可零延迟使用 B60</b>，无需切换。<br>
睡眠：B60 节点在 /dev/b60，系统服务看不到 → 不轮询 → 链路进 L1 → 约 8 W。<br>
暴露：节点回 /dev/dri，系统服务恢复轮询 → 约 42 W，仅当系统级任务确实要用独显时才有意义。</div>
  <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <input id="lreason" placeholder="租约用途（如：影视转码走独显）" style="flex:1;min-width:180px">
    <select id="lttl"><option value="300">5 分钟</option><option value="900" selected>15 分钟</option>
      <option value="1800">30 分钟</option><option value="3600">60 分钟</option></select>
    <button style="width:auto;margin:0" onclick="newLease()">申请暴露租约</button>
  </div>
  <div id="leaselist" style="margin-top:8px"></div>
</div>

<div class="card" style="margin-top:12px">
  <h2>接入指南（新项目照抄即可，不需要理解这套机制）</h2>
  <div class="flow"><b>① 容器（推荐：永久直通真卡，零调度，空闲自动 L1）</b><br>
&nbsp;&nbsp;devices:<br>
&nbsp;&nbsp;&nbsp;&nbsp;- /dev/b60/render:/dev/dri/renderD128&nbsp;&nbsp;&nbsp;# 左=宿主机真卡，右=容器内路径（叫什么都行）<br>
&nbsp;&nbsp;自检：<b>b60-check.sh &lt;容器名&gt;</b> —— 全绿即接对<br>
<br>
<b>② 宿主机命令（需要 /dev/dri 里的节点时）</b><br>
&nbsp;&nbsp;b60-run ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/&lt;render 节点&gt; -i in.mp4 out.mp4<br>
&nbsp;&nbsp;b60-run --ttl 1800 vainfo&nbsp;&nbsp;&nbsp;# 指定租约秒数<br>
&nbsp;&nbsp;b60-run --direct my-app --render=/dev/b60/render&nbsp;&nbsp;&nbsp;# 直通，不开租约<br>
<br>
<b>判据</b>：只有系统自带服务（影视转码 / 相册 AI / 资源监控等）必须开租约，因为它们只认 /dev/dri。<br>
你自己的程序或容器两种都行 —— 容器挂 /dev/b60/render 最省，连一次切换都不花。<br>
<b>细节</b>：templates/docker-compose.snippet.yml · templates/host-ffmpeg.md · README.md</div>
</div>

<div class="warn" id="warnbox"></div>

<div class="card" style="margin-top:12px">
  <h2>功耗曲线 <span id="hint" class="sub" style="display:inline;font-weight:400"></span></h2>
  <div class="tabs">
    <button class="on" data-r="1h" onclick="setRange('1h',this)">1 小时</button>
    <button data-r="6h" onclick="setRange('6h',this)">6 小时</button>
    <button data-r="24h" onclick="setRange('24h',this)">24 小时</button>
    <button data-r="7d" onclick="setRange('7d',this)">7 天</button>
    <button data-r="30d" onclick="setRange('30d',this)">30 天</button>
  </div>
  <canvas id="chart"></canvas>
  <div class="legend">
    <span><i class="dot" style="background:#58a6ff"></i>整卡均值</span>
    <span><i class="dot" style="background:rgba(88,166,255,.25)"></i>区间峰值</span>
    <span><i class="dot" style="background:#3fb950"></i>GPU 核心 (pkg)</span>
    <span id="csvlink"></span>
  </div>
</div>

<div class="card" style="margin-top:12px">
  <h2>B60 持有者（角色由引擎占用差分判定：≥1% 为干活）</h2>
  <table id="clients"><tr><th>进程</th><th>归属</th><th>PID</th><th>节点</th><th>引擎</th><th>角色</th><th>连续</th></tr></table>
</div>

<div class="grid" style="margin-top:12px">
  <div class="card">
    <h2>系统服务</h2>
    <div id="services"></div>
  </div>
  <div class="card">
    <h2>最近工作时段（谁真用过）</h2>
    <div class="ev" id="sessions" style="max-height:150px"><span class="sub">读取中…</span></div>
  </div>
</div>

<div class="card" style="margin-top:12px">
  <h2>运行日志（调度切换 / 服务重启 / 异常告警）</h2>
  <div class="ev" id="events"></div>
</div>

<div id="logs"></div>
<div class="foot">v1.0 掩藏 → v1.1 记录 → v1.2 调度 → v1.3 接入层（b60-run / 容器模板 / b60-check.sh）</div>
<div class="foot">采样 2s（待机降频 15s）· 落库 15s · 保留 30 天 · 更新于 <span id="updated">--</span> · <span id="err" style="color:var(--red)"></span></div>

<script>
let busy=false, range='1h', hist=null, policy='auto';
function fmtW(v){ return v==null?'--':v.toFixed(1)+' W'; }
function fmtDur(s){ if(s==null) return '--'; s=Math.max(0,Math.round(s));
  if(s<60) return s+' 秒'; if(s<3600) return Math.floor(s/60)+' 分'; return Math.floor(s/3600)+' 时 '+Math.floor(s%3600/60)+' 分'; }

async function refresh(){
  try{
    const r=await fetch('/api/status'); const s=await r.json(); const p=s.power||{};
    document.getElementById('pavg').textContent = p.avg60==null?'--':p.avg60.toFixed(1);
    document.getElementById('pnow').textContent = fmtW(p.card);
    document.getElementById('prange').textContent = p.min60==null?'--':p.min60.toFixed(1)+' – '+p.max60.toFixed(1)+' W';
    document.getElementById('pkg').textContent = fmtW(p.pkg);
    document.getElementById('vram').textContent = fmtW(p.vram);
    document.getElementById('temp').textContent = s.temp_c==null?'--':s.temp_c+' °C';

    const pct=s.l1.pct, lb=document.getElementById('l1badge');
    if(pct==null){lb.textContent='--';lb.className='badge b-blue';}
    else if(pct>=80){lb.textContent='L1 深睡中';lb.className='badge b-green';}
    else if(pct>=5){lb.textContent='L1 间歇进入';lb.className='badge b-amber';}
    else {lb.textContent='L0 清醒';lb.className='badge b-red';}
    document.getElementById('l1pct').textContent = pct==null?'--':pct.toFixed(1)+' %';
    document.getElementById('rtstatus').textContent = s.runtime.status||'--';
    document.getElementById('rtctrl').textContent = (s.runtime.control||'?')+' · '+(s.runtime.usage||'?');
    document.getElementById('sampling').textContent = s.sampling==='idle-slow'?'待机降频（15s，避免唤醒）':'常规（2s）';

    const d=s.dispatch||{}, db=document.getElementById('dsbadge');
    if(d.state==='SLEEP'){db.textContent='睡眠 · 低功耗';db.className='badge b-green';}
    else if(d.state==='WORK'){db.textContent='工作 · 已暴露';db.className='badge b-amber';}
    else {db.textContent=d.state||'--';db.className='badge b-gray';}
    document.getElementById('dsreason').textContent=(d.reason||'')+(d.pending?('（待切换 → '+d.pending.target+'）'):'');
    document.getElementById('dssince').textContent = d.since? fmtDur(Date.now()/1000-d.since):'--';

    const m=s.mask.enabled, mb=document.getElementById('maskbadge');
    if(m===true){mb.textContent='已掩藏';mb.className='badge b-green';}
    else if(m===false){mb.textContent='已暴露';mb.className='badge b-amber';}
    else {mb.textContent='未知';mb.className='badge b-amber';}

    if(d.policy){ policy=d.policy;
      document.querySelectorAll('[data-p]').forEach(b=>b.classList.toggle('on', b.dataset.p===policy)); }

    const wb=document.getElementById('warnbox');
    if(m===false){ wb.style.display='block';
      wb.innerHTML='⚠️ 当前处于暴露态：系统服务会重新轮询 B60，功耗约 42 W。容器无需暴露即可使用，建议结束后回到自动/睡眠。'; }
    else if((s.l1.pct||100)<40 && (s.clients||[]).length){ wb.style.display='block';
      wb.innerHTML='⚠️ 掩藏态下 L1 仅 '+(s.l1.pct||0).toFixed(0)+'%，有 '+(s.clients||[]).length+' 个持卡者，可能被轮询污染。'; }
    else wb.style.display='none';

    const ct=document.getElementById('clients');
    const kl={docker:'容器',system:'系统服务',other:'宿主'};
    ct.innerHTML='<tr><th>进程</th><th>归属</th><th>PID</th><th>节点</th><th>引擎</th><th>角色</th><th>连续</th></tr>'+
      ((s.clients||[]).length?s.clients.map(c=>{
        const u=c.util==null?'--':c.util.toFixed(1)+'%';
        const rb=c.role==='干活中'?'<span class="badge b-amber">干活中</span>':'<span class="badge b-blue">仅识别</span>';
        const own=(kl[c.klass]||'?')+': '+(c.owner||'?');
        return '<tr><td>'+c.cmd+'</td><td>'+own+'</td><td>'+c.pid+'</td><td>'+c.dev+'</td><td>'+u+'</td><td>'+rb+'</td><td>'+(c.since==null?'--':fmtDur(c.since))+'</td></tr>';
      }).join(''):'<tr><td colspan="7" style="color:var(--green)">无进程持卡 ✓</td></tr>');

    const sv=document.getElementById('services');
    sv.innerHTML=Object.entries(s.services||{}).map(([k,v])=>{
      const cls=v==='active'?'b-green':(v==='unknown'?'b-amber':'b-red');
      return '<div class="row"><span class="k">'+k+'</span><span class="badge '+cls+'">'+v+'</span></div>';
    }).join('')||'<span class="sub">读取中…</span>';

    const ll=document.getElementById('leaselist');
    ll.innerHTML=((s.leases||[]).length?s.leases.map(l=>
      '<div class="lease"><span class="badge b-blue" style="font-size:10px">'+l.id+'</span>'+
      '<span class="badge b-gray" style="font-size:10px">'+(l.who||'api')+'</span>'+
      '<span style="flex:1">'+(l.reason||'未注明')+' · 剩 '+fmtDur(l.left)+'</span>'+
      '<button data-lid="'+l.id+'" onclick="dropLease(this.dataset.lid)">释放</button></div>').join('')
      :'<div class="sub">无租约（睡眠中）</div>');

    document.getElementById('updated').textContent=s.updated||'--';
    document.getElementById('err').textContent=s.error||'';
  }catch(e){}
}

async function loadHistory(){
  try{
    const r=await fetch('/api/history?range='+range); hist=await r.json();
    document.getElementById('csvlink').innerHTML='<a href="/api/history.csv?range='+range+'">导出 CSV</a>';
    drawChart();
  }catch(e){}
}
function setRange(r,el){ range=r; document.querySelectorAll('[data-r]').forEach(b=>b.classList.remove('on'));
  el.classList.add('on'); hist=null; loadHistory(); }

function drawChart(){
  const cv=document.getElementById('chart'); const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth,H=cv.clientHeight; cv.width=W*dpr; cv.height=H*dpr;
  const g=cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,W,H);
  const pad={l:38,r:8,t:10,b:20}; const iw=W-pad.l-pad.r, ih=H-pad.t-pad.b;
  if(!hist||!hist.points||hist.points.length<2){
    g.fillStyle='#8b949e'; g.font='12px -apple-system,sans-serif';
    g.fillText('暂无足够数据（每 15s 落库一条，稍候）', pad.l, H/2); return;
  }
  const pts=hist.points, vals=[];
  pts.forEach(p=>{ if(p.card!=null)vals.push(p.card); if(p.max!=null)vals.push(p.max); if(p.pkg!=null)vals.push(p.pkg); });
  let maxY=Math.max(15,Math.ceil(Math.max(...vals)/5)*5);
  const t0=pts[0].t, t1=pts[pts.length-1].t||t0+1;
  const X=t=>pad.l+(t-t0)/(t1-t0||1)*iw, Y=v=>pad.t+ih-(v/maxY)*ih;
  g.strokeStyle='#21262d'; g.lineWidth=1; g.fillStyle='#8b949e'; g.font='10px monospace';
  for(let i=0;i<=4;i++){ const v=maxY*i/4,y=Y(v);
    g.beginPath(); g.moveTo(pad.l,y); g.lineTo(W-pad.r,y); g.stroke();
    g.fillText(v.toFixed(0)+'W',4,y+3); }
  for(let i=0;i<=5;i++){ const t=t0+(t1-t0)*i/5,d=new Date(t*1000);
    const lab=hist.span>86400?(d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':00'
                             :String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
    g.fillText(lab,Math.min(W-pad.r-34,Math.max(pad.l,X(t)-16)),H-6); }
  g.beginPath(); let first=true;
  pts.forEach(p=>{ if(p.max==null)return; const x=X(p.t),y=Y(p.max); first?g.moveTo(x,y):g.lineTo(x,y); first=false; });
  for(let i=pts.length-1;i>=0;i--){ const p=pts[i]; if(p.min==null)continue; g.lineTo(X(p.t),Y(p.min)); }
  g.closePath(); g.fillStyle='rgba(88,166,255,.10)'; g.fill();
  function line(key,color,width,fill){
    const valid=pts.filter(p=>p[key]!=null); if(!valid.length)return;
    if(fill){ g.beginPath(); g.moveTo(X(valid[0].t),Y(valid[0][key]));
      valid.forEach(p=>g.lineTo(X(p.t),Y(p[key])));
      g.lineTo(X(valid[valid.length-1].t),Y(0)); g.lineTo(X(valid[0].t),Y(0)); g.closePath();
      const gr=g.createLinearGradient(0,pad.t,0,pad.t+ih);
      gr.addColorStop(0,'rgba(88,166,255,.28)'); gr.addColorStop(1,'rgba(88,166,255,0)');
      g.fillStyle=gr; g.fill(); }
    g.beginPath(); g.strokeStyle=color; g.lineWidth=width; g.lineJoin='round';
    valid.forEach((p,i)=>{ const x=X(p.t),y=Y(p[key]); i?g.lineTo(x,y):g.moveTo(x,y); });
    g.stroke(); }
  line('pkg','#3fb950',1.2,false); line('card','#58a6ff',1.8,true);
  const last=pts[pts.length-1];
  if(last.card!=null){ g.fillStyle='#e6edf3'; g.font='11px monospace';
    g.fillText(last.card.toFixed(1)+'W',Math.min(W-46,X(last.t)+4),Math.max(12,Y(last.card)-4)); }
}

async function loadEvents(){
  try{
    const r=await fetch('/api/events?limit=60'); const j=await r.json();
    const cls={INFO:'b-blue',WARN:'b-amber',ERROR:'b-red'};
    document.getElementById('events').innerHTML=(j.events||[]).map(e=>
      '<div class="ev-row"><span class="ev-t">'+e.time+'</span>'+
      '<span class="badge '+(cls[e.level]||'b-gray')+'" style="font-size:10px;padding:1px 6px">'+e.level+'</span>'+
      '<span>'+e.msg+'</span></div>').join('')||'<span class="sub">暂无事件</span>';
  }catch(e){}
}

async function loadSessions(){
  try{
    const r=await fetch('/api/sessions?limit=12'); const j=await r.json();
    document.getElementById('sessions').innerHTML=(j.sessions||[]).map(s=>
      '<div class="ev-row"><span class="ev-t">'+s.start+'</span>'+
      '<span style="flex:1">'+(s.owner||'?')+' · '+fmtDur(s.seconds)+'</span>'+
      '<span class="badge b-gray" style="font-size:10px;padding:1px 6px">'+(s.klass==='docker'?'容器':'系统')+'</span></div>'
      ).join('')||'<span class="sub">暂无记录（引擎占用 ≥1% 才算干活）</span>';
  }catch(e){}
}

async function setPolicy(pol){
  if(busy) return; busy=true;
  if(pol==='exposed' && !confirm('强制暴露：B60 节点回 /dev/dri 并重启三服务，功耗将升至约 42W。确认？')){busy=false;return;}
  try{
    const r=await fetch('/api/policy',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({policy:pol})}); const j=await r.json();
    if(!j.ok) alert(j.error||'失败');
  }catch(e){alert('请求失败: '+e);}
  busy=false; refresh(); loadEvents();
}

async function newLease(){
  if(busy) return; busy=true;
  const ttl=parseInt(document.getElementById('lttl').value,10);
  const reason=document.getElementById('lreason').value||'未注明';
  try{
    const r=await fetch('/api/lease',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ttl:ttl,reason:reason,who:'ui'})});
    const j=await r.json();
    const logs=document.getElementById('logs');
    logs.style.display='block';
    logs.textContent = j.ok ? ('✅ 租约 '+j.id+' 已创建：'+(policy==='masked'?'当前是强制睡眠策略，租约不会触发切换':'调度器将在数秒内切到工作态'))
                            : '❌ 创建失败';
  }catch(e){}
  busy=false; refresh();
}

async function dropLease(id){
  try{ await fetch('/api/lease?id='+id,{method:'DELETE'}); }catch(e){}
  refresh();
}

window.addEventListener('resize',()=>{ if(hist) drawChart(); });
document.querySelectorAll('[data-p]').forEach(b=>b.classList.toggle('on', b.dataset.p==='auto'));
refresh(); loadHistory(); loadEvents(); loadSessions();
setInterval(refresh,3000); setInterval(loadHistory,30000);
setInterval(loadEvents,15000); setInterval(loadSessions,20000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def _boot_apply():
    time.sleep(2)
    pol = "auto"
    want_mask = True
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        pol = st.get("policy") or "auto"
        want_mask = bool(st.get("mask_enabled", True))
    except (OSError, ValueError):
        pass
    with _lock:
        _state["dispatch"]["policy"] = pol
    if pol == "exposed":
        want_mask = False
    cur, _ = mask_status()
    if cur is not want_mask:
        with _lock:
            ok, logs = apply_mask(want_mask, reason="开机应用期望状态")
        for ln in logs:
            print("[boot] " + ln, flush=True)
    else:
        print("[boot] 掩藏状态已符合期望（%s），跳过" % want_mask, flush=True)
        log_event("INFO", "容器启动：掩藏状态已符合期望（%s），策略 %s"
                  % ("开启" if want_mask else "关闭", pol))
    with _lock:
        _state["dispatch"].update({"state": "SLEEP" if want_mask else "WORK",
                                   "since": time.time(), "reason": "开机初始化（策略 %s）" % pol})


@app.on_event("startup")
def _on_startup():
    db_init()
    _dbstat_init()
    log_event("INFO", "b60-guard v%s 启动（记录 %.0fs，保留 %d 天）"
              % (VERSION, RECORD_INTERVAL, RETENTION_DAYS))
    threading.Thread(target=_boot_apply, daemon=True).start()
    threading.Thread(target=_sampler, daemon=True).start()
    threading.Thread(target=_dispatcher, daemon=True).start()


if __name__ == "__main__":
    db_init()
    _dbstat_init()
    threading.Thread(target=_boot_apply, daemon=True).start()
    threading.Thread(target=_sampler, daemon=True).start()
    threading.Thread(target=_dispatcher, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
