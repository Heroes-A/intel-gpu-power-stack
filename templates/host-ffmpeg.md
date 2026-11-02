# 宿主机直通 B60：`b60-run` 用法

> 下文用 `/dev/dri/renderD129` 作为示例节点名。你的机器可能是 `renderD128`/`renderD130`，
> 用 `bash scripts/b60-detect.sh` 查，然后 `export B60_DRI_NODE=/dev/dri/renderD1xx`。

宿主机上的普通命令（ffmpeg、vainfo、Python 脚本、oneAPI 工具）有两种用法。
**先看第一种**：大多数情况下你要的就是它。

## 一、默认模式：自动开窗，跑完自动回睡

```bash
# 申请 15 分钟租约 → 等 B60 出现在 /dev/dri/renderD129 → 执行 → 释放租约
b60-run ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD129 \
               -i input.mp4 -c:v hevc_vaapi -rc_mode CQP -global_quality 24 output.mp4

# 显式指定租约时长（秒，服务端夹在 30–3600）
b60-run --ttl 1800 ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD129 -i in.mp4 out.mp4

# 在面板上能看出是谁在用
b60-run --reason "影视转码：旅行纪录片" ffmpeg ...

# 延长等待节点出现的超时（默认 60s，够用；系统服务重启才需要更久）
b60-run --wait 90 vainfo
```

行为约定：

| 时刻 | 发生什么 |
|---|---|
| 启动 | `POST /api/lease` 建租约，`who=b60-run`，面板可见 |
| 0–~20s | 等 `/dev/dri/renderD129` 出现（15s 防抖 + 搬移），出现即启动命令 |
| 运行中 | 你的进程 ⇄ 内核 xe 驱动直连，b60-guard 不代理、不拦截任何 ioctl |
| 命令退出 | `DELETE /api/lease?id=…`，约 20s 后面板回 SLEEP |
| 命令被 Ctrl-C / `timeout` | INT/TERM/HUP trap 兜底释放租约 |
| **启动器被 `kill -9`** | 独立看护进程检测到父进程消失（读 `/proc/<pid>/status` 的 State，僵尸也认），2s 内代释放 |
| 启动器被 `kill -9` 且看护进程也死了 | TTL 兜底（最长 60 分钟），面板上会看到租约自然到期 |

退出码是命令的退出码，可以直接用于 `if` / `set -e`。

## 二、直通模式：命令自己接受设备路径

```bash
b60-run --direct my-app --render=/dev/b60/render
b60-run --direct vainfo --display drm --device /dev/b60/render
```

`--direct` 不申请租约、不动 `/dev/dri`，只确认 `/dev/b60/render` 存在然后把命令跑起来。
适合：长期跑的常驻任务、以及**不需要系统自带服务看得到卡**的任何场景。

> 判据很简单：**只有系统自带服务（影视转码、相册 AI、资源监控等）必须走第一种**，
> 因为它们只认 `/dev/dri`。你自己的程序、ffmpeg、Python 推理，两种都行，直通更省一次切换。

## 三、常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `b60-guard 不可达` | 面板容器没跑 | `docker ps \| grep b60-guard`；`bash deploy-b60mask.sh` 重新拉起 |
| `策略当前为「强制睡眠」` | 面板策略被切成 masked | 面板改回「自动（按租约）」 |
| 等 60s 节点没出现 | 防抖中，或搬移失败 | 看 `curl -s http://127.0.0.1:41966/api/events \| tail` |
| 用了 `/dev/dri/renderD129` 却报 no device | 命令没经 b60-run，节点在掩藏态下不在 `/dev/dri` | 套 b60-run，或改用 `--direct` + `/dev/b60/render` |
| 转码成功但很慢 | 挂/指到了核显 | `b60-check.sh --host` 确认设备号 226:129 |

## 四、不要在宿主机上做的事

- 不要手工 `mv /dev/dri/renderD129 /dev/b60/render`——搬节点是 b60-guard 的唯一职责，
  手工搬会让 state.json 与实际不符，下一次调度就会错位。
- 不要 `systemctl restart sysinfo_service` 之类的操作去"让它看到卡"；需要它看到就开租约。
- 不要把 `/dev/b60/render` 写进任何开机自启脚本的硬编码路径里——它是 b60-guard 启动后
  才建立的；容器重启策略天然满足这个次序，自启脚本不满足。
