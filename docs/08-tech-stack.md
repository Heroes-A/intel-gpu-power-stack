# 08 · 技术栈与选型

> 这一篇是给"要改它/要评估它"的人看的：用了什么、为什么用、为什么不用更炫的方案。

## 1. 全景

```
┌──────────────────────────────────────────────────────────────────┐
│ L5 观测   内嵌 HTML/JS 面板（无构建）· 曲线 · CSV · 事件流        │
├──────────────────────────────────────────────────────────────────┤
│ L4 接入   容器 devices 直通 · b60-run（租约启动器）· b60-check    │
├──────────────────────────────────────────────────────────────────┤
│ L3 控制   b60-guard：FastAPI + uvicorn + SQLite（单容器）         │
│           策略机 · 租约 · 污染看门狗 · 采样 · 消费者画像          │
├──────────────────────────────────────────────────────────────────┤
│ L2 节点   /dev/dri ⇄ /dev/b60  rename（不是代理、不是软链）       │
├──────────────────────────────────────────────────────────────────┤
│ L1 内核   xe 驱动 · PCIe ASPM · runtime PM · debugfs · hwmon      │
├──────────────────────────────────────────────────────────────────┤
│ L0 固件   BIOS：PEG ASPM=L1 · Native ASPM=Disabled · L1SS=Off      │
└──────────────────────────────────────────────────────────────────┘
```

**没有一层是新写的。** L0/L1 是既有固件与内核能力，L2 是两个 `mv`，
L3 才是一个几百行的 Python 进程，L4 是 bash，L5 是服务端拼出来的一个 HTML。

## 2. 组件清单

| 组件 | 版本 / 形态 | 作用 | 为什么是它 |
|---|---|---|---|
| BIOS ASPM | 主板固件设置 | 让根端口具备 L1 | 内核无法凭空给根端口加能力位；这是唯一入口 |
| `xe` 驱动 | 内核自带 | 真实电源管理 | B60 的唯一上游驱动；`i915` 不支持 BMG |
| debugfs 驻留计数 | `dgfx_pcie_link_residencies` 等 | 验证链路是否真睡 | 唯一能直接读到 L1 驻留的内核接口 |
| hwmon 能量计数器 | `energy1_input` / `energy2_input` | 测功耗 | 比外接功率计便宜，且能分域（card / pkg） |
| `mv`（rename） | coreutils | 搬节点 | 同分区 rename 不改变 inode/权限/设备号；已打开的 fd 不受影响 |
| **FastAPI + uvicorn** | Python 3.12 / `python:3.12-slim` | HTTP 控制面 + 面板 | 单文件即可跑，自带 JSON 序列化；不需要前端构建链 |
| **SQLite** | 标准库 `sqlite3` | 采样、事件、租约、工作时段 | 单进程写入，零运维；不需要额外服务 |
| **bash 5** | 系统自带 | `b60-run` / `b60-check.sh` / `b60-detect.sh` | 目标机一定有；比"再装一个运行时"更可靠 |
| Docker | 宿主已有 | 打包与隔离 | 需要 `privileged` + `pid=host` 才能搬 `/dev` 与 `nsenter` |
| 内嵌 HTML/JS | 服务端字符串 | 面板 | 无 npm、无构建、无 CDN；离线可用 |

外部依赖仅两个 pip 包：`fastapi`、`uvicorn[standard]`。镜像里额外装 `util-linux`（提供 `nsenter`）。

## 3. 运行时形态

```bash
docker run -d --name b60-guard \
  --restart unless-stopped \
  --privileged --pid=host \
  -v /dev:/dev -v /sys:/sys:ro \
  -v <repo>/state.json:/app/state.json \
  -v <repo>/data:/app/data \
  -e TZ=Asia/Shanghai -e RECORD_INTERVAL=15 -e RETENTION_DAYS=30 \
  -p 41966:8000 b60-guard:1.3
```

| 参数 | 必要性 |
|---|---|
| `--privileged` | 需要 `mv` `/dev` 下的节点、读 debugfs |
| `--pid=host` | 通过 `/proc/<pid>/cgroup` 把持卡进程映射回容器名 |
| `-v /dev:/dev` | 搬的就是宿主的 `/dev` |
| `-v /sys:/sys:ro` | 读 sysfs / hwmon（只读，足够） |
| `state.json` | 期望状态，容器重启后自动套用 → 兼作开机持久化 |

## 4. 数据模型

| 表 | 关键字段 | 写入节奏 | 保留 |
|---|---|---|---|
| `samples` | `t, card_w, pkg_w, l1_pct, clients` | 15 s（能量区间均值） | 30 天 |
| `events` | `t, level, msg` | 事件驱动 | 30 天 |
| `leases` | `id, reason, created, until, who` | 申请/释放 | 到期即删 |
| `sessions` | `owner, start, end, util` | 引擎占用 ≥1% 起止 | 30 天 |

按 15 s/条估算：约 5 760 行/天、17 万行/30 天，`history.db` 在十几 MB 量级。

## 5. 被否决的方案（及原因）

| 方案 | 否决原因 |
|---|---|
| **CUSE 假 DRM 节点** | DRM 依赖 `mmap`，CUSE 不支持 → 第一个 `mmap` 就崩 |
| **伪造 `/sys/class/drm/` 条目** | sysfs 由内核生成，用户态不可写；`libdrm` 枚举不认假 `/dev` 节点 |
| **udev 规则改名** | 只改 `/dev` 名字，改不了枚举结果；且与 `libdrm` 的 `st_rdev ↔ devnum` 校验冲突 |
| **`LD_PRELOAD` 拦截 `open()`** | 只影响被注入的进程；系统服务不受控；静态链接/非 glibc 程序绕过 |
| **fanotify 拦截** | 需全盘挂载点监控，与 Docker 的 mount namespace 冲突 |
| **BPF LSM 拒绝 `file_open`** | 理论可行，但要求内核启用 `CONFIG_BPF_LSM` + BTF，且 hook 行为随内核版本漂移；目标机的内核不满足 |
| **驱动内打补丁/加 quirk** | 每次内核升级都要重编；风险与收益不成比例 |
| **卸载模块（`rmmod xe`）** | 容器全部失去 GPU；且卸载/重载会触发 Battlemage 热复位问题 |
| **进程内"闲置就卸载模型"** | OpenVINO 的 GPU 上下文在进程级缓存，`gc` 收不回来；实测还导致进程崩溃 |
| **停掉系统服务** | 影响 NAS 面板功能；且升级/重启后会被拉起，不可持续 |
| **Prometheus + Grafana** | 为了看一条 8 W 的曲线引入两个服务，运维成本远超收益；SQLite + 内嵌页面足够 |

## 6. 安全与暴露面

- 面板**没有认证**，默认只监听内网。要暴露到外网，请套反向代理 + 认证。
- 容器是 `privileged`，等于宿主 root：只应部署在受信任的机器上。
- 本项目不保存任何凭据；`deploy.sh` 不接收密码。
- API 无速率限制；`POST /api/mask`、`/api/policy` 会立刻改变设备可见性，别暴露给不可信网络。

## 7. 可配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `B60_BDF` | `0000:03:00.0` | 显卡 PCI 地址 |
| `B60_SERVICES` | `sysinfo_service,resmon_service,mediasrv` | 会轮询 GPU 的系统服务 |
| `B60_API` | `http://127.0.0.1:41966` | 客户端访问控制面的地址 |
| `B60_DRI_NODE` | `/dev/dri/renderD129` | 暴露态下等待的节点路径 |
| `B60_RENDER_DEVS` | `226:129` | 设备号（`scripts/b60-detect.sh` 可查） |
| `B60_REAL_NODE` | `/dev/b60/render` | 掩藏态下的真卡路径 |
| `SAMPLE_INTERVAL` / `IDLE_INTERVAL` | `2` / `15` | 有人持卡 / 无人持卡的采样间隔（秒） |
| `RECORD_INTERVAL` | `15` | 落库间隔（秒） |
| `RETENTION_DAYS` | `30` | 历史保留天数 |
| `WATCHDOG_L1` / `WATCHDOG_HOLD` | `40` / `3` | 污染判定阈值（%）/ 连续次数 |
| `AUTO_EVICT` | `1` | 污染时是否自动驱逐 |
| `LEASE_MAX_S` | `3600` | 单次租约上限 |
| `SWITCH_DEBOUNCE_S` | `15` | 切换防抖（秒） |

## 8. 想继续往下做的话

按投入产出排序：

1. **反代 + 认证**：现在是内网裸奔。
2. **多卡支持**：状态机目前假设单张 B60；多卡需要把 BDF 维度加进 state 与 API。
3. **udev 事件驱动**：现在靠轮询发现污染，接 `netlink` 可以更快。
4. **systemd 单元替代 `state.json`**：非 Docker 环境更自然。
5. **指标导出**：`/metrics` 出一个 Prometheus 兼容端点，方便已有监控栈接入。

---

← [07 测量方法](07-measurement.md) · 回到 [README](../README.md)
