# phone-mcp —— 让 AI 直接操作你的 Android 手机（界面层 + 系统级/底层）

一个轻依赖的 MCP (Model Context Protocol) stdio 服务，把手机常用操作封装成工具（视觉点击依赖 RapidOCR，已隔离在独立 venv 中，不影响系统环境），
接入 WorkBuddy 后，我就能在对话里真正去操作你的手机：先看屏 → 决策 → 用 ADB 执行 → 再看结果，循环。
不仅支持界面点击，还支持**系统级/底层**操作（shell、服务、进程、系统属性、文件、装卸应用）。

## 已注册位置
- 服务脚本：`C:\Users\AprYT\.workbuddy\phone-mcp\server.py`
- 配置：`C:\Users\AprYT\.workbuddy\mcp.json`（mcpServers.phone-mcp）
- 截图/UI dump 落盘：`C:\Users\AprYT\.workbuddy\phone-mcp\shots\`

## 工作原理
截图 → 多模态视觉理解 → 决策动作 → 通过 ADB 执行 → 再看结果，循环。

## 两种定位模式（phone_find_text / phone_tap_text）
这两个工具默认 `method="auto"`，自动在两种模式间择优：

- **模式 1 · 无障碍 / UI 模式**（毫秒级，~2s）：解析 `uiautomator dump` 的 XML，直接从控件 `text` / `content-desc` 拿文字 + `bounds` 算坐标。**零 OCR、坐标精准**，适用于系统界面 / 桌面 / 计算器 / 文件管理等标准视图。
- **模式 2 · 极速 OCR 模式**（~2-2.5s）：截图后用 RapidOCR 识别文字并点击。适用于**微信 / QQ 等关闭无障碍导出、UI 树为空**的 App。

**auto 策略**：先试 UI（秒回）；若 UI 全空或未命中，自动回退 OCR。且会对"UI 全空"的 App（如微信）按包名缓存 TTL（默认 30s），缓存期内直接走 OCR，**省掉每次白白浪费的 UI dump（约 2s）**——连续在微信里操作第二次起即可提速到 ~2s。

**引擎**：RapidOCR（ONNX 版，中英文，CPU 友好），运行在独立 venv：`C:\Users\AprYT\.workbuddy\phone-mcp\venv\`
**原则**：每次操作都实时截图 + 实时识别，**不缓存坐标**（界面动态变化也不会点错）。
**提速选项**：
  - `region: [x1,y1,x2,y2]`（归一化 0~1）只识别屏幕局部，截图更小、OCR 更快
  - `method: "ui" | "ocr" | "auto"` 可手动强制单一模式
  - 环境变量 `PHONE_MCP_FAST=1` 让 OCR 用更激进缩放（720 长边，再快约 0.3s，精度略降）
**首次加载**：服务启动时后台预热 OCR 模型，避免首次调用超时。

## 可用工具（共 26 个）
### 界面层（只读）
| 工具 | 作用 |
|------|------|
| `phone_get_devices` | 列出已连接设备 |
| `phone_screenshot` | 截图，返回图片+路径 |
| `phone_dump_ui` | dump 当前界面 UI 结构(XML) |

### 界面层（写）
| 工具 | 作用 |
|------|------|
| `phone_tap` | 坐标点击 (x, y) |
| `phone_swipe` | 滑动 (x1,y1)→(x2,y2) |
| `phone_input_text` | 输入 ASCII 文本 |
| `phone_paste_text` | 剪贴板+粘贴，支持中文 |
| `phone_launch_app` | 启动 App(包名) |
| `phone_key_event` | 发送按键(HOME/BACK/…) |

### 视觉定位（双模式：无障碍 UI / OCR，依赖 RapidOCR）
| 工具 | 作用 |
|------|------|
| `phone_find_text` | 按文字定位坐标（只读、不点击）。默认 auto：先 UI(毫秒级)后 OCR；支持 region 区域限定、method 强制模式 |
| `phone_tap_text` | 按文字自动点击。默认 auto：先 UI(毫秒级)后 OCR；支持 region、index 多匹配、method 强制模式 |

> 说明：OCR 每次都**实时截图 + 实时识别**，不缓存坐标——手机界面动态变化（新消息 / 置顶 / 滑动）也不会点错。

### 系统级 / 底层（只读，常开）
| 工具 | 作用 |
|------|------|
| `phone_list_packages` | 列出已安装应用（可过滤） |
| `phone_list_processes` | 列出运行中的进程 (ps -A) |
| `phone_getprop` | 读系统属性 getprop |
| `phone_settings_get` | 读系统设置 settings get |
| `phone_file_read` | 读设备文本文件 cat |

### 系统级 / 底层（写，需 `PHONE_MCP_ALLOW_SHELL=1`）
| 工具 | 作用 |
|------|------|
| `phone_shell` | 设备内执行任意 shell 命令（支持管道/重定向） |
| `phone_run_adb` | 执行原始 adb 命令（host 侧） |
| `phone_start_service` | 启动 Android 服务 (am startservice) |
| `phone_force_stop` | 强制停止应用 (am force-stop) |
| `phone_kill_process` | 结束进程（PID 或包名） |
| `phone_setprop` | 设置系统属性 setprop |
| `phone_settings_put` | 修改系统设置 settings put |
| `phone_file_write` | 向设备写文本文件（push） |
| `phone_install_apk` | 安装本地 APK |
| `phone_uninstall` | 卸载应用（会清数据！） |

## 在 WorkBuddy 中启用
1. 保存 `mcp.json` 后，打开 WorkBuddy **连接器管理页**（右上角）。
2. 在自定义连接器里找到 `phone-mcp`，点击 **Trust** 信任它。
3. 重启/刷新对话，之后我就能直接调用上述工具。

## 首次对话建议先跑
```
phone_get_devices      # 确认设备在线
phone_screenshot       # 让我"看到"手机当前界面
phone_dump_ui          # 拿到按钮坐标，便于精准点击
phone_list_processes   # 看看手机里在跑什么（底层只读）
```

## 环境变量（在 mcp.json 的 env 里设置）
| 变量 | 默认 | 说明 |
|------|------|------|
| `PHONE_MCP_DEVICE` | `134d2f8` | 默认设备序列号 |
| `ADB_BIN` | 自动探测/D:\ADB\adb.exe | adb 路径 |
| `PHONE_MCP_DRYRUN` | 未设(关) | 设为 `1` → 所有写操作只打印命令不执行（安全预览模式）|
| `PHONE_MCP_ALLOW_SHELL` | 未设(关) | 设为 `1` → 开放底层/系统级命令(phone_shell 等) |
| `PHONE_MCP_SHOTDIR` | 脚本同级的 shots/ | 截图/文件保存目录 |
| `PHONE_MCP_FAST` | 未设(关) | 设为 `1` → OCR 用更激进的 720 长边缩放，再快约 0.3s（精度略降）|

## 安全提示
- 写操作会真实改变手机状态（误触/误删/变砖）。建议先用 `PHONE_MCP_DRYRUN=1` 跑通，确认无误再放开。
- **灾难命令黑名单**（始终禁止，即使开启底层）：`reboot` `wipe` `format` `mkfs` `dd if=` `fastboot`。
- `phone_uninstall` 会清除应用数据，执行前请确认。
- 中文输入请用 `phone_paste_text`（剪贴板方案），`phone_input_text` 仅支持 ASCII。
- 底层 shell 权限很大，执行前我会向你确认破坏性操作。
