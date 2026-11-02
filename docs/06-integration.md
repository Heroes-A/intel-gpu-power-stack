# 06 · 接入指南

> 目标：新装一个要 GPU 的东西，不需要理解前面四章，照抄两行配置就能用。
> 用完也不需要"关掉"什么——它自己回睡。

## 1. 先确认控制面活着

```bash
curl -s http://127.0.0.1:41966/healthz
curl -s http://127.0.0.1:41966/api/status | jq '.mask, .dispatch, .power.card'
```

正常时：`mask.enabled=true`、`dispatch.state="SLEEP"`、`power.card ≈ 8`。

## 2. 容器接入（推荐）

### 2.1 两行配置

```yaml
services:
  my-gpu-app:
    image: my/app:latest
    devices:
      - /dev/b60/render:/dev/dri/renderD128
```

- 左边**永远是** `/dev/b60/render`（真卡常驻路径）。
- 右边随便，但映射成 `/dev/dri/renderD128` 最省事——很多镜像写死了这个路径。
- 不需要 `privileged`，不需要 `network host`，不需要任何 `B60_*` 环境变量。

### 2.2 自检

```bash
sudo bash scripts/b60-check.sh my-gpu-app
```

五项检查，退出码 0 = 全绿：

| # | 检查 | 失败的含义 |
|---|---|---|
| 1 | 宿主机 `/dev/b60/render` 存在且设备号与 `B60_RENDER_DEVS` 一致 | 控制面没起来，或拿到的不是 B60 |
| 2 | 容器 `HostConfig.Devices` 里有真卡路径 | 挂错了（挂成 `/dev/dri` 是最常见的错） |
| 3 | 容器内 `/dev/dri` 下有 render 节点，且设备号一致 | 挂到了核显 |
| 4 | 容器内 `DRM_IOCTL_VERSION` 能打开并读回 `xe` | 权限/驱动问题 |
| 5 | 面板 `/api/status` 里能看到该容器 | 仅提示：空闲进程本来就不持卡 |

### 2.3 常见错法

| 写法 | 后果 |
|---|---|
| `- /dev/dri:/dev/dri` | 掩藏态下容器里根本没有 B60，拿到的是核显 → 能跑但极慢 |
| `- /dev/dri/renderD129:/dev/dri/renderD129` | 掩藏后宿主机路径不存在，容器启动失败 |
| `privileged: true` 图省事 | 能用，但没必要；而且会把整机设备暴露给容器 |
| 以非 root 用户跑 | 需要 `group_add: ["<render gid>"]`（宿主机 `getent group render`） |

## 3. 宿主机命令（`b60-run`）

```bash
# 默认：申请 15 分钟租约 → 等节点出现 → 执行 → 释放
b60-run ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD129 -i in.mp4 out.mp4

# 显式租约时长（秒，30–3600）
b60-run --ttl 1800 vainfo

# 直通：不申请租约，命令自己用 /dev/b60/render
b60-run --direct my-app --render=/dev/b60/render
```

生命周期：

| 时刻 | 发生什么 |
|---|---|
| 启动 | `POST /api/lease`，`who=b60-run`，面板可见 |
| 0–~20 s | 等 `/dev/dri/renderD129` 出现（15 s 防抖 + 搬移），出现即启动 |
| 运行中 | 进程 ⇄ 内核直连，`b60-guard` 不代理任何 ioctl |
| 命令退出 | `DELETE /api/lease`，约 20 s 后面板回 SLEEP |
| Ctrl-C / `timeout` | `INT/TERM/HUP` trap 兜底释放 |
| **启动器被 `kill -9`** | 独立看护进程发现父进程消失（读 `/proc/<pid>/status` 的 State，僵尸也认），约 4 s 内代释放 |
| 看护进程也死了 | TTL 兜底（最长 60 分钟） |

退出码原样透传，可以直接用于 `if` / `set -e`。

> **它不会 fallback 到手工 `mv`。** 控制面不可达时直接报错退出——
> 因为手工搬节点会让状态文件与实际不一致，下一次调度就错位。

## 4. 不同框架的注意事项

| 框架 | 要点 |
|---|---|
| **VAAPI / ffmpeg** | `-vaapi_device /dev/dri/renderD128`（容器内路径）；`LIBVA_DRIVER_NAME` 一般留空 |
| **OpenVINO / OVMS** | 认 `/dev/dri` 下第一个 render 节点；容器内不需要额外变量 |
| **oneAPI / Level-Zero** | 需要 `render` 组权限；`sycl-ls` 能看到 B60 即可 |
| **PyTorch (xpu)** | 同上；首次编译 kernel 会长时间占用 GPU，建议 `--ttl 1800` |
| **Jellyfin/Emby 硬解** | 这类服务通常由系统服务拉起，只认 `/dev/dri` → 必须走 `b60-run` 或开租约 |

## 5. 排障速查

| 现象 | 排查 |
|---|---|
| 面板打不开 | `docker ps \| grep b60-guard`；宿主机防火墙；浏览器/系统代理是否把内网 IP 也代理走了（用 `curl --noproxy '*'` 对比） |
| 功耗停在 40 W+ | `curl -s .../api/events \| grep WARN`：暴露态没收回？有服务持卡轮询？看门狗报警？ |
| L1 只有个位数 | 看"持卡者"里是否有系统服务；确认 `/dev/dri` 下 B60 节点已收回 |
| 容器能跑但极慢 | 挂的是核显。`b60-check.sh <容器>` 会直接指出来 |
| `b60-run` 报控制面不可达 | 容器没起；`sudo bash scripts/deploy.sh` 重新拉起（掩藏状态不受影响） |
| 租约一直不消失 | 正常 20 s 内自释放；异常时 `DELETE /api/lease?id=<id>` 手工清，或等 TTL |
| 面板没有历史曲线 | 等 ≥2 个采样周期（15 s/条）；检查 `data/` 挂载 |

---

← [05 租约与策略机](05-lease-policy.md) · 下一篇：[07 测量方法](07-measurement.md) →
