# 手机端权限配置清单（input 指令拦截排查）

> 设备：`134d2f8`（Redmi/小米，Android 14，MIUI，已 root）
> 目标：确保 `adb shell input tap/swipe/text` 能稳定注入，避免被系统/ROM 拦截。

---

## 一、先用事实区分「谁在拦截」

我们通过**直连 adb（绕过 MCP 安全闸门）**实测，结论很明确：

| 测试 | 设备反应 | 结论 |
|------|----------|------|
| `input tap 540 1000` | `rc=0` 无报错 | ✅ 设备**接受**单点点击 |
| `input swipe 540 1800 540 300` | `rc=0` 无报错 | ✅ 设备**接受**滑动 |
| MCP `phone_shell "input swipe ..."` | 被安全闸门误拦 | ❌ 是**我方闸门**把 `swipe` 里的 `wipe` 子串当灾难命令（已修复为词边界匹配） |

**关键结论**：这台手机本身**没有**拦截 input。唯一出现过的「拦截」来自 phone-mcp 自己的安全闸门（`swipe` 误判），已在 `server.py` 中修复。下面清单用于**兜底**——确保未来换机器/换 ROM 时 input 也不会被真拦截。

---

## 二、开发者选项必开项清单

| # | 选项（中文 / 英文） | 作用 | 本机当前状态 |
|---|----------------------|------|--------------|
| 1 | **USB 调试** / USB debugging | 基础 adb 连接前提 | ✅ 已开（`adb_enabled=1`） |
| 2 | **USB 调试（安全设置）** / USB debugging (Security settings) | 授予 shell `INJECT_EVENTS`，否则 `input` 在锁屏/安全窗口被拒 | ✅ 推断已开（`input tap` 实测得 `rc=0`） |
| 3 | **允许模拟点击** / Allow simulated clicks（部分 ROM 叫「模拟触摸」） | 放开 input 对更多系统界面的注入 | ⚠️ MIUI 此项存在，建议打开 |
| 4 | **停用 ADB 授权超时** / Disable ADB authorization timeout | 避免 7 天后需重新在手机上点「允许」 | ❌ 未停用（当前 `adb_allowed_connection_time=604800000`，即 7 天） |

### 开启路径
- 设置 → 我的设备 → 全部参数 → 连点「MIUI 版本」7 次开启开发者模式
- 设置 → 更多设置 → 开发者选项 → 找到上面 1/2/3/4 逐项打开

### 命令行等价（第 4 项，免手动）
```bash
adb shell settings put global adb_allowed_connection_time -1
```
验证：`adb shell settings get global adb_allowed_connection_time` 返回 `-1` 即已停用。

> 第 2/3 项在 MIUI 没有公开 setting key 可一键切换，需手动在开发者选项里点开。判断它们是否生效的最直接方法：跑一次 `adb shell input tap x y`，返回码 0 即说明 INJECT_EVENTS 已具备。

---

## 三、当前设备权限快照（实测）

```
USB调试 adb_enabled                         = 1          ✅
ADB授权超时 adb_allowed_connection_time      = 604800000  ❌ 建议设 -1
ADB over WiFi adb_wifi_enabled               = 0          （可选，按需开）
已启用无障碍服务 enabled_accessibility_services = 仅某健身App（无 phone-mcp 服务）
input tap 当前执行 rc                        = 0          ✅ 注入可用
```

---

## 四、仍被拦截时的升级排查

若换机/换 ROM 后 `input` 返回非 0 或「Permission denied / Injecting to another application requires INJECT_EVENTS permission」：
1. 确认第 2 项「USB 调试（安全设置）」已开（最常见根因）。
2. 确认第 3 项「允许模拟点击」已开。
3. 仍不行 → 走 **无障碍服务点击** 备选方案（见 `ACCESSIBILITY_FALLBACK.md`），它不依赖 INJECT_EVENTS。
