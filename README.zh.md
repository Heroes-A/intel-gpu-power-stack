# Intel 独立显卡空闲功耗技术栈

**中文** · [English](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Heroes-A/intel-gpu-power-stack)](https://github.com/Heroes-A/intel-gpu-power-stack/releases)
[![Platform](https://img.shields.io/badge/platform-Linux%20%2F%20Debian%2012-informational)](docs/01-platform-baseline.md)

**一块装在无头服务器上的英特尔独立显卡（Arc），利用率长期为 0，功耗却停在 50 W。把它压到 8 W，靠的不是调参，而是四层结构。**

> **适用对象：独立显卡（discrete GPU）** —— Intel Arc A 系列（DG2）、B 系列（Battlemage）、
> Pro 系列（A40/A50/A60/B50/B60）。**核显（iGPU）不适用**，原因见第 11 节。
>
> A layered idle-power stack for **Intel Arc discrete GPUs** on Linux: firmware ASPM L1,
> kernel residency observability, DRM node relocation, and explicit exposure leases.
> Reference case: Arc Pro B60 (24 GB), 50.3 W → 8.3 W idle (−83%) with containers still
> getting the real device.

---

## 0. 这篇帖子解决什么问题

场景很具体：一台 headless Linux 服务器（NAS / 推理机 / 转码机），插一张英特尔 Arc 独显，
只服务容器里的推理、转码、渲染任务。平均利用率接近 0。

按常理，显卡空载就该掉到个位数瓦。实际上它长期停在 50 W 上下，风扇不转、负载为零、功耗却不降。

我花了几天把这件事从"配置问题"追到"结构问题"，结论是：**这不是某一个开关没开，而是四层里每一层都有一个独立的阻塞点**。
只修其中一层，收益都很小；四层一起修，才有量级变化。

这篇文章把整套东西整理成一个可复用的技术栈，适用于**英特尔独立显卡**（Arc A 系列 / B 系列 / Pro 系列）。
核显（iGPU）在第 11 节单独说明，结论是：**不要对核显做这套**。

---

## 1. 先看结果

同一台机器、同一次开机周期内的连续测量：

| 阶段 | 动作 | 整卡空载 | L1 链路驻留 |
|---|---|---|---|
| 基线 | 出厂状态 | 50.3 W | 0 % |
| ① 固件 | BIOS 开 PEG ASPM = L1，交给 BIOS 管链路 | 42.1 W | 具备能力，但被服务轮询打断 |
| ② 节点 | `card0` / `renderD*` 移出 `/dev/dri` | 31.2 W * | 间歇 |
| ③ 稳定 | 系统服务不再持有旧 fd、不再轮询 | **8.0 – 8.7 W** | **92 – 99.9 %** |

\* 掩藏刚生效时有过渡读数：服务仍持有旧 fd，要等它下一次枚举才发现设备不见了。

- 相对基线降幅 **83%**，年化省电约 **370 kWh**（按 42 W × 8760 h 计）。
- 降幅不是均匀的：固件层只拿 16%，后 80% 全在"系统服务别再碰它"这一步。

**注意一个容易误判的地方**：浅睡（链路进 L1、封装未深睡）时，24 GB 显存自刷新会撑住约 15.6 W 的地板；
只有整卡进深睡，读数才落到 8 W 量级。所以"L1 驻留率"和"整卡功耗"必须一起看，
单看任何一个都会得出相反结论。

---

## 2. 为什么英特尔独显的待机功耗会卡住

拆开看是三层，彼此独立：

### 2.1 固件层：根端口根本没有 L1 能力

```
$ sudo lspci -s 00:01.0 -vv | grep LnkCap
LnkCap: Port #0, Speed 16GT/s, Width x16
        ASPM not supported, Exit Latency L0s <1us, L1 <16us
                ^^^^^^^^^^^^^^^^
```

根端口的 `LnkCap` 里直接写着 `ASPM not supported`。同时 `dmesg` 会有：

```
ACPI FADT declares the system doesn't support PCIe ASPM, so disable it
```

两个信号叠在一起，内核就完全不接管 ASPM 了。而主板 BIOS 里 `PEG ASPM` 这一项默认是 `Auto` 或 `Disabled`，
所以链路永远停在 L0——每一毫秒都在满速供电。

这一步是**前置条件**。不开它，后面所有工作最多拿到 1/5 的收益。

### 2.2 系统服务层：周期性 MMIO 轮询是真正的杀手

这是最反直觉的一点，也是我一开始判错的地方。

我以为是"容器开着设备所以链路睡不下去"，于是做了对照实验：`docker pause` 冻结容器全部线程，
功耗三态（跑 / 暂停 / 停止）完全一致。**持卡不阻塞 L1。**

真正把链路拽出来的是**周期性寄存器读**。NAS 面板类系统服务（系统信息、资源监控、媒体服务）
每隔几秒枚举一次 `/dev/dri`，发现设备就打开节点、读一次 MMIO 寄存器报告温度/显存/利用率。
每一次读都是一次链路唤醒，链路在 L1 里待不到两个周期就被叫醒。

实测：这类服务活着的时候，L1 驻留率始终上不去；把它们要看的节点挪走，驻留率直接跳到 99%。

### 2.3 架构层：显存自刷新构成硬地板

独显有独立 VRAM，进入浅睡时显存必须保持自刷新，这部分功耗收不回来。

- 24 GB 的卡，浅睡地板约 **15.6 W**；
- 只有整卡进深睡（D3cold / 封装 G2 以上），读数才落到 8 W 量级。

这解释了一个常见困惑："为什么别人同款卡空载只有 15 W，我这儿 40 W？"
——因为 15 W 就是它的浅睡地板，不是它省得好。要过这条线，必须让链路真的睡下去。

### 2.4 接入层（工程问题，非功耗问题）

节点挪走之后，新装的容器和脚本找不到卡了。这是"节点掩藏"方案自带的代价，
必须用接入层补回来，否则方案不可持续。

---

## 3. 技术栈：六层

```
┌──────────────────────────────────────────────────────────────────┐
│ L5 观测   内嵌面板 · 自适应采样 · 时序库 · 事件流                 │
├──────────────────────────────────────────────────────────────────┤
│ L4 接入   容器 devices 直通 · 租约启动器 · 接入自检               │
├──────────────────────────────────────────────────────────────────┤
│ L3 控制   单容器控制面：策略机 · 租约 · 污染看门狗 · 消费者画像    │
├──────────────────────────────────────────────────────────────────┤
│ L2 节点   /dev/dri ⇄ 私有目录 rename（不是代理、不是软链）         │
├──────────────────────────────────────────────────────────────────┤
│ L1 内核   xe / i915 · PCIe ASPM · runtime PM · debugfs · hwmon    │
├──────────────────────────────────────────────────────────────────┤
│ L0 固件   BIOS：PEG ASPM = L1 · Native ASPM = Disabled            │
└──────────────────────────────────────────────────────────────────┘
```

| 层 | 组件 | 职责 | 是否新写 |
|---|---|---|---|
| L0 | 主板 BIOS | 让根端口具备 L1 能力，且由 BIOS 而非内核配置链路 | 否 |
| L1 | 内核 + 驱动 | 真实电源管理；提供驻留/能耗观测接口 | 否 |
| L2 | `mv`（rename） | 决定系统服务能否枚举到设备 | 否 |
| L3 | FastAPI + SQLite 单容器 | 策略、租约、看门狗、采样 | **是**，几百行 |
| L4 | Docker `devices:` + 两个 bash 脚本 | 无感接入、租约兜底 | **是**，纯 bash |
| L5 | 服务端直出的 HTML/JS | 看曲线、看驻留、看谁在持卡 | **是**，无构建 |

整套东西没有引入任何重型组件：外部依赖只有 `fastapi` + `uvicorn` 两个 pip 包，
其余全部复用内核既有能力。没有 Prometheus、没有 Grafana、没有前端构建链。

---

## 4. L0 · 固件：ASPM 的通用打法

### 4.1 目标状态

| 项目 | 目标值 | 为什么 |
|---|---|---|
| PCIe Native Power Management | Enabled | 打开这一页的电源管理能力 |
| Native ASPM | **Disabled** | 让 BIOS 管链路（FADT 模式）。OS 接管路径在实测平台上从未走通 |
| PEG ASPM（CPU 直连槽） | **L1** | 只要 L1，L0s 只增加无谓的进出态开销 |
| PCH ASPM / DMI ASPM | Disabled | 与目标无关，只增风险 |
| L1 Substates（L1.1 / L1.2） | **Disabled** | 实测会导致 POST 阶段全黑 + 断网，须 Clear CMOS 才能救回 |

### 4.2 厂商菜单对照

不同主板叫法不一样，找这几个关键词：

| 厂商 | 路径（Advanced Mode） | 关键项名 |
|---|---|---|
| ASUS | Advanced → Platform Misc Configuration | `PEG ASPM` / `Native ASPM` / `PCI Express Native Power Management` |
| MSI | Settings → Advanced → PCI Subsystem Settings | `PCIe ASPM` / `PEG ASPM` |
| ASRock | Advanced → Chipset → PCI Express Config | `PCIe ASPM Support` / `PEG ASPM` |
| Gigabyte | Settings → Platform Power / Peripherals | `PCIe ASPM Mode` / `PEG ASPM` |

> 部分主板把 PEG ASPM 藏在 `Advanced → System Agent (SA) → PCI Express Configuration` 下。
> 找不到就全页搜 `ASPM`。

### 4.3 操作纪律（这几条比设置本身重要）

1. **先设"来电自启"**（Restore AC Power Loss = Power On）。之后任何设置导致起不来，远程断电再上电还有一次自恢复机会。
2. **一次只改一项**，改 → 存 → 冷启 → 验证 → 再改下一项。否则出问题无法归因。
3. **关掉 OS 内 `reboot`**。Battlemage（B 系列）热复位后链路重训练窗口不足，
   任何软复位都会黑屏挂死。标准流程：F10 → 黑屏（**预期**）→ 断电 ≥10 秒 → 上电。
   A 系列（DG2）一般没这么严重，但同样建议走冷启。

### 4.4 验收五连

```bash
# 1. FADT 那句会保留 —— 这是设计，不是失败
sudo dmesg | grep -i 'FADT.*ASPM'
#    Native ASPM=Disabled 就是"BIOS 管 ASPM"，FADT 继续声明 unsupported 属正常

# 2. 根端口能力位出现 ASPM L1（不再是 not supported）
sudo lspci -s 00:01.0 -vv | grep -E 'LnkCap|LnkCtl'

# 3. 上游 LnkCtl 变 Enabled
sudo lspci -s 01:00.0 -vv | grep LnkCtl

# 4. L1 驻留开始增长（间隔 10 s 读两次，第二次应大于第一次）
D=/sys/kernel/debug/dri/0000:03:00.0
sudo cat $D/*residenc* 2>/dev/null; sleep 10; sudo cat $D/*residenc* 2>/dev/null

# 5. 功耗复测（20 s 能量差分）
E1=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1); sleep 20
E2=$(cat /sys/class/hwmon/hwmon*/energy1_input | head -1)
awk "BEGIN{printf \"card = %.1f W\n\", ($E2-$E1)/20e6}"
```

判据 2 不过 = BIOS 没吃设置；判据 2 过、3 不过 = PEG ASPM 还是 `Auto` 而不是 `L1`；
1–4 过但功耗没降 = 链路通了、封装态没进，保留现状继续往下走。

---

## 5. L1 · 内核：只信这四个接口

网上关于"怎么测 GPU 功耗"的说法大半是错的。真正能给出因果关系的只有四个口：

| 接口 | 路径 | 它能回答什么 |
|---|---|---|
| 链路驻留 | `debugfs/dri/<BDF>/*residenc*` | 链路有多少时间真睡在 L1 —— **最重要的单一指标** |
| 封装态 | `debugfs/dri/<BDF>/*pkg*residenc*` | 是否进到更深封装态（决定能不能突破显存地板） |
| 能量计数器 | `hwmon*/energy{1,2}_input`（微焦） | 整卡 / 封装两个域的累计能耗，差分即均值 |
| 客户端列表 | `debugfs/dri/<BDF>/clients` + `/proc/<pid>/fdinfo/*` | 谁开着设备、是"仅识别"还是"在干活" |

几个必须知道的坑：

- **`hwmon` 编号不稳定**，用 `grep -l . /sys/class/hwmon/hwmon*/energy1_input` 找有计数器的那个。
- **能量计数器更新粒度约 100 ms**，所以 100 ms 差分会在 0 – 5.4 J 之间乱跳（等效 0 – 54 W）。
  用 15 s 以上的区间差分，读数才稳定。
- **debugfs 文件名随驱动/内核版本变化**：`xe` 下是 `dgfx_pcie_link_residencies` / `dgfx_pkg_residencies`；
  `i915` 或其他版本可能是别的名字。**用 glob 找，别写死**。
- **`fdinfo` 里的 `drm-engine-*` 才是"真在干活"的证据**。一个进程开着节点但引擎计数为 0，就是"仅识别"。

---

## 6. L2 · 节点：为什么搬 `/dev` 就能省电

这一步看起来最不像技术，但它有明确的因果链：

```
系统服务每 N 秒枚举 /dev/dri
        │
        ├── 看得到卡 → 打开节点 → 周期性 MMIO 读寄存器
        │                              └─ 每次读 = 一次链路唤醒 → L1 待不住
        │
        └── 看不到卡 → 什么也不做 → 链路自己睡进 L1 → 8 W
```

实现只是两个 `mv`（同分区 rename，不改变 inode / 权限 / 设备号，已打开的 fd 不受影响）：

```bash
sudo mkdir -p /dev/gpu
sudo mv /dev/dri/card0      /dev/gpu/card
sudo mv /dev/dri/renderD129 /dev/gpu/render
```

容器改成直挂 `/dev/gpu/render`，完全不受影响——**它拿到的是同一个 inode、同一个设备号、同一张真卡**。

### 6.1 适用边界（很重要）

| 情况 | 是否适用 | 说明 |
|---|---|---|
| NAS / 面板类服务周期性枚举 GPU | ✅ 收益最大 | 这是主要场景 |
| 只有容器用卡，系统层不碰 | ✅ 收益中等 | 只拿到固件层那部分 |
| 系统级任务（相册 AI、影视转码）必须用卡 | ⚠️ 需要租约 | 它们只认 `/dev/dri`，要用时得临时暴露 |
| 有图形会话 / 显示器接在卡上 | ❌ 不要做 | 拔掉节点等于拔掉显示 |
| 核显（共享内存，无独立 VRAM） | ❌ 不建议 | 收益极小，且 `/dev/dri` 被显示栈深度依赖 |

### 6.2 一个必须说清楚的事实：假节点做不出来

我试过"造一个假设备骗过系统层"的路子，三条路全堵死：

1. **CUSE 假 DRM 节点** —— DRM 离不开 `mmap`，CUSE 不支持 `mmap`，第一次 `mmap` 就崩。
2. **伪造 `/sys/class/drm/` 条目** —— sysfs 由内核生成，用户态写不进去。
3. **伪造 `/dev` 节点** —— `libdrm` 的 `drmGetDevices2()` 会拿 `stat()` 的 `st_rdev`
   和 udev 记录的 `devnum` 比对，对不上直接过滤掉。

所以正确形态不是"骗"，而是**路径重定向 + 显式租约**：平时让系统服务看不见，需要时明确地说"现在给你 30 分钟"。

---

## 7. L3 · 控制面：两个状态 + 租约

### 7.1 状态机

| 状态 | 节点位置 | 系统服务可见 | 功耗 | 谁在用 |
|---|---|---|---|---|
| `SLEEP` | `/dev/gpu/{card,render}` | ✗ | ~8 W | 容器（直通路径，随时可用） |
| `WORK` | `/dev/dri/{card,renderD*}` | ✓ | ~42 W | 系统级任务 + 容器 |

关键设计：**容器在两种状态下都能用卡**。切换的从来只有一件事——系统服务能不能看到它。
所以"要用 GPU"这个动作对容器是零成本的，不需要申请租约；只有系统级任务才需要。

### 7.2 租约

```
POST   /api/lease  {"reason":"影视转码","ttl":1800}   → 切 WORK，返回 lease id
DELETE /api/lease/<id>                                → 切回 SLEEP
```

- **TTL 兜底**：到期自动回收，防止"忘了释放"把机器钉在 42 W。
- **防抖**：切换有 15 s 防抖窗口，避免频繁抖动。
- **孤儿看门狗**：调用方被 `kill -9` 时，独立看护进程读 `/proc/<pid>/status` 判活并代释放（含 PID 复用保护）。

### 7.3 污染看门狗

节点虽然挪走了，但可能有人手工把它挪回来，或者某个服务缓存了路径。
看门狗持续看 L1 驻留率：连续 3 个采样周期低于 40% 就判定"被污染"，
自动重新掩藏并重启相关服务，同时记一条事件。

---

## 8. L4 · 接入：让新装的东西无感

这是整套方案能不能长期活下去的关键。节点挪走之后，接入只有两条路：

**容器（推荐，零延迟）**

```yaml
services:
  your-app:
    devices:
      - /dev/gpu/render:/dev/dri/renderD129
```

挂进去的仍然是真卡，`vainfo` / `sycl-ls` / OpenVINO 全部正常。**不需要暴露，不需要等切换。**

**宿主机命令（租约包装）**

```bash
gpu-run ffmpeg -hwaccel vaapi -vaapi_device /dev/dri/renderD129 -i in.mp4 out.mp4
```

申请租约 → 等节点出现在 `/dev/dri` → 执行 → 释放。退出码原样透传，
`INT/TERM/HUP` 都兜底释放，`kill -9` 交给看护进程。

配一个 `gpu-check.sh` 做接入自检：设备映射对不对、设备号是不是本卡（不是核显）、
`DRM_IOCTL_VERSION` 通不通、面板能不能看见。**设备号这一步最容易错**——容器里 `stat` 出来的是
十六进制（如 `e2:81`），宿主机 `ls -l` 看到的是十进制（`226:129`），两种写法都要认。

---

## 9. L5 · 观测：监控本身会伤功耗

这条是我踩得最疼的坑。

无人持卡时设备处于 runtime suspend，**高频读 `hwmon` 会把它反复唤醒**：

| 读法 | 实测功耗 |
|---|---|
| 只读首尾两次（60 s 端点差分） | 12.0 W |
| 每 0.5 s 读一次 | **31 W**（而且很稳定，std 0.09） |

也就是说，"为了看功耗而加的监控"把功耗从 12 W 抬到了 31 W，而且看起来还很稳定、很有说服力。

解法是**自适应采样**：有人持卡时 2 s 一次，无人持卡时 15 s 一次，落库用能量区间均值。
面板上那条曲线要标注采样策略，否则读者会以为设备在抖。

顺带回答一个常见疑问："功耗曲线在 0 – 43 W 之间跳，是不是没做好？"
——不是。真实均值很稳，跳的是采样窗。用 15 s 区间均值，读数在 8.0 – 8.6 W 之间，波动 < 0.6 W。

---

## 10. 诊断决策树

拿到一台"待机功耗降不下来"的机器，按这个顺序走，不要跳步：

```
① lspci -vv 看根端口 LnkCap
   ├─ 没有 ASPM L1  → 固件层问题，去 BIOS（第 4 节）
   └─ 有 ASPM L1
        ↓
② 看上游 LnkCtl 是否 Enabled
   ├─ 否 → BIOS 里 PEG ASPM 还是 Auto/Disabled
   └─ 是
        ↓
③ 读 *residenc*，10 s 内 L1 驻留是否增长
   ├─ 不增长 → 有东西在周期性碰它，进 ④
   └─ 增长但功耗不降 → 链路睡了、封装没睡，看 pkg 驻留；这是显存地板，属正常上限
        ↓
④ 列 clients，逐 pid 看 fdinfo 的 drm-engine-*
   ├─ 引擎计数为 0 → "仅识别"，是它打断了 L1
   └─ 引擎计数 > 0 → 真在干活，功耗高是应该的
        ↓
⑤ 二分验证
   - systemctl stop 掉可疑服务，看 L1 驻留是否恢复
   - 或直接 mv 节点，看是否立刻改善
```

实测这套走完一般 20 分钟能定位到具体是哪个服务。

---

## 11. 不同英特尔显卡的适配

| 类别 | 代表型号 | 驱动 | 独立 VRAM | 浅睡地板 | 对轮询敏感 | 本方案收益 |
|---|---|---|---|---|---|---|
| DG2 消费 | Arc A310 / A380 / A580 / A750 / A770 | `i915`（新内核可试 `xe`） | 有 | 中 | 高 | 高 |
| DG2 专业 | Arc Pro A40 / A50 / A60 | 同上 | 有 | 中 | 高 | 高 |
| BMG 消费 | Arc B570 / B580 | `xe` | 有 | 中–高 | 高 | 很高 |
| BMG 专业 | Arc Pro B50 / **B60** | `xe` | 有（B60 = 24 GB） | **高**（约 15.6 W） | 高 | **很高（实测 −83%）** |
| Xe2 核显 | Lunar Lake / Arrow Lake 集显 | `xe` | 共享内存 | 极低 | 中 | 低，不建议 |
| Xe-LP 核显 | Tiger Lake / Alder Lake / Raptor Lake | `i915` | 共享内存 | 极低 | 中 | 低，不建议 |

三条经验：

1. **显存越大，地板越高**。24 GB 的卡在浅睡时被显存自刷新钉在 15 W 以上，
   想再往下只能让它进深睡，软件层没有别的空间。
2. **`xe` 比 `i915` 的观测接口更完整**（有 pkg 域驻留），但名字会变，别写死路径。
3. **核显不要碰**。它没有独立显存的自刷新问题，且 `/dev/dri` 被显示栈深度依赖，掩藏等于砸显示。

> 参考：同款 B60 在评测机上空载 15 – 22 W。差异来自平台（主板/BIOS/CPU 直连方式），
> 不是卡本身的差别。绝对数字必须自测，方法论可以照搬。

---

## 12. 被否决的方案（以及为什么）

这一段是给"想找更优雅解法"的人看的——这些我都试过或算过，不必要再走一遍。

| 方案 | 否决原因 |
|---|---|
| CUSE 假 DRM 节点 | DRM 依赖 `mmap`，CUSE 不支持，第一次 `mmap` 就崩 |
| 伪造 `/sys/class/drm/` 条目 | sysfs 内核生成，用户态不可写；`libdrm` 也不认假节点 |
| udev 规则改名 | 只改 `/dev` 名字，改不了枚举结果，且与 `st_rdev ↔ devnum` 校验冲突 |
| `LD_PRELOAD` 拦截 `open()` | 只影响被注入的进程；系统服务不受控；静态链接程序绕过 |
| fanotify 拦截 | 需全盘挂载点监控，与 Docker mount namespace 冲突 |
| BPF LSM 拒绝 `file_open` | 理论可行，但要 `CONFIG_BPF_LSM` + BTF，且 hook 行为随内核漂移 |
| 驱动打补丁 / 加 quirk | 每次内核升级都要重编，风险与收益不成比例 |
| `rmmod xe` 卸载模块 | 容器全部失去 GPU；重载还会触发 Battlemage 热复位问题 |
| 进程内"闲置就卸载模型" | 推理框架的 GPU 上下文是进程级缓存，`gc` 收不回来；实测还导致进程崩溃 |
| 直接停掉系统服务 | 影响面板功能，升级/重启后会被拉起来，不可持续 |
| Prometheus + Grafana | 为看一条 8 W 的曲线引入两个服务，运维成本远超收益 |

---

## 13. 落地顺序（一个下午能做完）

| 顺序 | 动作 | 耗时 | 风险 |
|---|---|---|---|
| 1 | 记录当前基线：功耗、L1 驻留、clients | 10 min | 无 |
| 2 | BIOS 设"来电自启" + 冷启验证 | 15 min | 低 |
| 3 | BIOS 开 PEG ASPM = L1，冷启，跑验收五连 | 30 min | **中**（可能黑屏，需断电冷启） |
| 4 | 复测功耗，记录新基线 | 10 min | 无 |
| 5 | 手动 `mv` 节点，观察 10 分钟功耗 | 10 min | 低（可秒回滚） |
| 6 | 若有效：容器改挂私有路径，部署控制面 | 1 h | 低 |
| 7 | 观察 24 h：驻留率、曲线、事件流 | — | 无 |

前 5 步不用写一行代码就能验证收益。第 6 步才是工程化。

---

## 14. 复现与仓库结构

```
# docs/ = 中文原版，docs/en/ = 英文译本（结构完全一致）
docs/01-platform-baseline.md   平台拓扑、功耗构成
docs/02-bios-aspm.md           BIOS 逐项设置、验收五连、回退
docs/03-kernel-observation.md  L1 驻留 / pkg 态 / hwmon / fdinfo 的正确读法
docs/04-node-masking.md        掩藏为什么有效、假节点为什么做不出来
docs/05-lease-policy.md        状态机、租约、防抖、污染看门狗
docs/06-integration.md         容器两行接入 + 租约启动器 + 排障
docs/07-measurement.md         功耗"跳变"的真相与正确测法
docs/08-tech-stack.md          组件选型与被否决方案
src/                          控制面（FastAPI + SQLite）
scripts/                      检测 / 自检 / 租约启动器 / 部署
templates/                    compose 片段、宿主机用法
```

快速开始：

```bash
bash scripts/gpu-detect.sh          # 体检：BDF / 节点 / 设备号 / ASPM / 驻留 / 实测功耗
bash scripts/deploy.sh              # 起控制面（默认端口 41966）
```

> 参考实现里的环境变量与路径仍以 `B60_` / `/dev/b60` 命名，因为那是实测对象。
> 换卡只需改三个值：`B60_BDF`（PCI 地址）、`B60_DRI_NODE` / `B60_RENDER_DEVS`（节点与设备号）、
> `B60_SERVICES`（哪些服务在轮询）。行为和卡型号无关。

---

## 15. 边界与红线

**这套东西解决不了的事：**

- 显存自刷新地板（24 GB 卡浅睡时约 15.6 W）——物理限制，只能靠深睡绕过。
- 真有负载时的功耗——那不是本方案的目标。
- 平台/BIOS 不提供 ASPM 能力的机器——只能换平台。

**红线（违反可能要 Clear CMOS 才能救回）：**

1. 禁开 L1 Substates / L1.1 / L1.2。
2. 禁一次改多项 BIOS。
3. 禁 OS 内 `reboot`（Battlemage 热复位黑屏），一切重启走断电 → 等 10 s → 上电。
4. 改动 BIOS 前必先设"来电自启"。
5. 掩藏节点前确认没有图形会话/显示器挂在卡上。

**安全面：**

- 控制面默认无认证，只应监听内网。要暴露到公网，请套反向代理 + 认证。
- 控制面容器是 `privileged`（要搬 `/dev`、读 debugfs），等于宿主 root，只应部署在受信任的机器上。
- 本项目不保存任何凭据。

---

## 16. 最后

这套方案的本质不是"省电技巧"，而是一个判断：

> **持有设备不阻塞低功耗，周期性打扰才阻塞。**

一旦接受这个判断，方案形态就唯一了——不是拦截 `open()`，不是伪造设备，
而是把"谁在什么时候可以看见它"变成显式策略。

实测数据来自一台真实在跑的机器（Arc Pro B60 24 GB，无头，只跑容器），
不是实验室调出来的数字。所有结论都可以用第 10 节的决策树在别的机器上复现。

---

*License: MIT · 欢迎提 issue 补充其他平台/卡型的实测数据。*
