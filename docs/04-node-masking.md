# 04 · 节点掩藏：为什么搬 `/dev` 就能省电

> English: [04 · Node masking: why moving /dev saves power](en/04-node-masking.md)

> 这是整个项目里最"不像技术"的一步：把两个设备节点挪个位置，功耗从 42 W 掉到 8 W。
> 但它有明确的因果链，也有明确的边界。

## 1. 因果链

```
系统服务（sysinfo / resmon / 媒体服务）每 N 秒枚举 /dev/dri
        │
        ├── 能看到 B60 → 打开节点 → 周期性 MMIO 读寄存器
        │                              │
        │                              └─ 每次读 = 一次链路事务 → 链路回到 L0
        │                                 → L1 驻留被打断 → ~42 W
        │
        └── 看不到 B60 → 不打开、不轮询
                                       └─ 链路无人访问 → 进 L1 → ~8 W
```

关键点：**它们枚举的是 `/dev/dri` 目录**，不是 sysfs，也不是 PCI 总线。
所以只要节点不在这个目录里，轮询自然停止——不需要停服务、不需要改服务配置、不需要 hook。

## 2. 具体动作

```bash
mkdir -p /dev/b60
mv /dev/dri/card0        /dev/b60/card
mv /dev/dri/renderD129   /dev/b60/render
```

- **同一文件系统的 `rename`**，inode 不变、设备号不变、权限不变。
- 已经 `open()` 的进程**不受影响**（fd 指向 inode，不指向路径）——这正是掩藏刚生效时
  还会看到 31 W 过渡读数的原因：老 fd 还在被轮询，等它们松手才落到 8 W。
- 恢复就是反着 `mv` 回去。`b60-guard` 会同时校正 `root:render` 与 `0666`。

> `card0` 与 `renderD*` 的 kernel 名字不同机器可能不同。
> `b60-guard` 用 `/dev/dri/by-path/pci-<BDF>-{card,render}` 反查真实名字，不写死。

## 3. 为什么不做"假设备"

最直觉的方案是：造一个假节点放在 `/dev/dri`，让系统服务识别到它、但并不真的碰 B60。
这条路我们认真试过三次，三次都被堵死：

| 方案 | 否决原因 |
|---|---|
| **CUSE 用户态字符设备** 冒充 DRM 节点 | DRM 依赖 `mmap` 做缓冲与命令提交，**CUSE 不支持 `mmap`**。就算能 `open`，第一个 `mmap` 就失败 |
| **伪造 `/sys/class/drm/` 条目** | sysfs 由内核在设备注册时生成，用户态无法写入；`libdrm` 枚举走 sysfs + udev，不认纯 `/dev` 假节点 |
| **`libdrm` 前置校验** | `drmGetDevices2()` 会拿 `stat()` 的 `st_rdev` 与 udev 记录的 `devnum` 比对，不一致直接丢弃。要在隔离 mount namespace 里同时伪造 `/dev` 与 udev 数据库才可能骗过——而这已经超出"轻量中转层"的范畴 |

实测方式：在独立 mount namespace 里构造假节点 + 假 udev 记录，宿主零污染，
结果 `drmGetDevices2()` 仍返回空列表。

**结论：用户态做不出一个"系统可识别、又不落到真卡"的 DRM 设备。**
于是换思路——不伪造设备，只**重定向路径**，把"识别"和"调用"解耦到两个不同的路径上：

- 系统服务：只认 `/dev/dri` → 掩藏态下看不到 → 不轮询；
- 新项目：直接挂 `/dev/b60/render` → 永远能拿到真卡。

## 4. 边界与风险

| 风险 | 说明 | 处理 |
|---|---|---|
| 显卡兼作显示输出 | 掩藏 `card0` 会让控制台/桌面失去 GPU | 只在 headless 机器上用；带显示的机器别掩藏 `card0` |
| udev 重建节点 | 驱动重载/设备重新绑定会重新创建 `/dev/dri/*`，掩藏态被打破 | `b60-guard` 有污染看门狗，检测到 L1 掉 + 持卡者出现就重新掩藏 |
| 容器在掩藏前启动 | 挂载的是 `/dev/dri/renderD129`，掩藏后路径消失（fd 仍有效但重启即失效） | 一律挂 `/dev/b60/render`（见 [06](06-integration.md)） |
| 手工 `mv` | 与 `state.json` 不一致，下次调度错位 | 只通过 API / 面板操作 |
| 重启后节点回原位 | `/dev` 是 tmpfs，重启即重置 | `state.json` 持久化期望状态，容器启动时自动套用 |

## 5. 代码位置

- 掩藏/恢复：`src/app.py` → `apply_mask()` / `mask_status()`
- kernel 名字反查：`src/app.py` → `_b60_kernel_names()`
- 污染看门狗：`src/app.py` → `_watchdog_tick()` / `_evict()`
- 状态持久化：`src/app.py` → `_save_state_file()` / `_boot_apply()`

---

← [03 内核观测](03-kernel-observation.md) · 下一篇：[05 租约与策略机](05-lease-policy.md) →
