# phone-mcp 代码审查报告

> **审查范围**：`phone-mcp/server.py`（4236 行，203KB，v0.10.0）  
> **审查日期**：2026-07-12  
> **审查方法**：静态分析 + 安全审查 + 架构分析

---

## 概览统计

| 维度 | 计数 |
|------|------|
| 代码行数 | 4236 |
| 注册工具数 | 65 |
| 严重问题 | 3 |
| 一般问题 | 8 |
| 优化建议 | 6 |
| 安全审查项 | 12 |

---

## 🔴 严重问题（需立即修复）

### 1. [P0] `phone_run_adb` / `phone_shell` 使用黑名单过滤，存在绕过风险

**位置**：`CATASTROPHIC_RE`(L87-94)、`forbid_catastrophic()`(L226-231)、`t_run_adb()`(L2038-2047)

**问题**：灾难命令防护采用黑名单正则匹配，存在已知绕过路径：
- `dd if=` 黑名单可通过 `dd  if=`(双空格) 绕过
- `rm -rf /` 不在黑名单中（尽管 `adb shell rm -rf /system` 可能被权限阻断，但非系统分区的数据仍会丢失）
- `mv /system/app/PackageInstaller/google.apk …` 等重命名攻击不在黑名单

**建议**：
```python
# 更安全的方案：白名单 + 正则多层防护
DANGEROUS_RE = [
    re.compile(r'\brm\b.*(-rf?|--recursive)'),
    re.compile(r'dd\s+.*if='),           # 宽松空格
    re.compile(r'>\s*/dev/'),             # 覆盖块设备
    re.compile(r'\bmount\b.*-o\s*remount'),  # 重新挂载
]
```

**优先级**：`phone_run_adb` 需要 `ALLOW_SHELL=1`，该环境变量默认关闭，风险已部分缓解。但一旦开启，绕过面很大。

---

### 2. [P0] 单文件 4200+ 行巨石架构，可维护性极差

**位置**：整个 `server.py`

**问题**：`server.py` 是一个 4200+ 行的单一文件，包含：
- MCP 协议层（initialize/list/call + JSON-RPC）
- ADB 控制层（截图/点击/滑动/输入/keyevent）
- 视觉定位层（UI XML 解析 + RapidOCR）
- 内核级输入（sendevent + evdev）
- 系统级操作（shell/proc/prop/settings/install）
- 微信集成（打开/输入/发送/验证）
- minicap 流式截图
- frida-rust 动态插桩
- 硬件控制（背光/CPU/振动/音频/防火墙）

全部混乱在一个文件中。

**建议**：拆分为以下模块结构：
```
phone-mcp/
├── server.py              # MCP 主入口（<200行）
├── adb/
│   ├── __init__.py
│   ├── executor.py        # run_adb, list_devices, resolve_device
│   └── retry.py           # with_retry, with_verification
├── tools/
│   ├── __init__.py
│   ├── ui.py              # tap, swipe, key_event, input
│   ├── system.py          # shell, ps, proc, prop, settings
│   ├── vision.py          # UI XML + OCR 定位
│   ├── wechat.py          # 微信集成
│   ├── stream.py          # minicap 流式截图
│   ├── frida.py           # frida-rust 集成
│   └── hardware.py        # 背光/CPU/音频/防火墙
├── protocol/
│   ├── __init__.py
│   ├── dispatch.py        # 工具调度
│   └── registry.py        # TOOLS 注册表
└── utils/
    ├── __init__.py
    ├── envelope.py         # ok/fail/text_block
    └── log.py              # 日志
```

---

### 3. [P0] `t_kill_process` 的 PID/包名判断不可靠

**位置**：`t_kill_process()` (L2106-2114)

```python
def t_kill_process(args):
    target = str(args["target"])
    if target.isdigit():
        run_adb(["shell", "kill", target], ...)
    else:
        run_adb(["shell", "am", "force-stop", target], ...)
```

**问题**：`str.isdigit()` 不能可靠区分 PID 和包名。如果调用者传入的是全数字的包名（虽然罕见，但 Android 允许包名含数字如 `com.example.123app`），会被错误地当作 PID 处理，执行 `kill 包名` 而不是 `force-stop`。

**建议**：
```python
def t_kill_process(args):
    target = str(args["target"])
    # 明确要求 type 参数，或改用 pid/package 分离参数
    if "type" in args and args["type"] == "pid":
        run_adb(["shell", "kill", target], ...)
    else:
        run_adb(["shell", "am", "force-stop", target], ...)
```

---

## 🟡 一般问题（建议修复）

### 4. 全局可变状态缺乏线程安全保护

**位置**：`_UI_EMPTY`(L652)、`_MT_CACHE`(L298)、`_CAP_STREAMS`(L2420)

三个全局字典在无锁保护下被读写：
- `_UI_EMPTY`：`smart_find` 读写（多线程竞争可能读脏）
- `_MT_CACHE`：`_mt_detect` 读写（竞争写入可能覆盖）
- `_CAP_STREAMS`：有 `_CAP_STREAMS_LOCK` 保护 ≈ 基本安全

**建议**：对 `_UI_EMPTY` 和 `_MT_CACHE` 添加 `threading.Lock()` 保护。

---

### 5. `_ocr_debug` 每调用一次打开/关闭一次文件

**位置**：`_ocr_debug()` (L961-968)

```python
def _ocr_debug(msg):
    with open(os.path.join(SHOT_DIR, "ocr_debug.txt"), "a", ...) as f:
        f.write(...)
```

在 OCR 重试循环中可能每秒被调用多次，每次 open/close 开销很小但堆积后不可忽视。

**建议**：使用 `logging` 模块的 FileHandler 或至少 cache 文件句柄：
```python
_OCR_DEBUG_FH = None
def _ocr_debug(msg):
    global _OCR_DEBUG_FH
    if _OCR_DEBUG_FH is None:
        _OCR_DEBUG_FH = open(..., "a")
    _OCR_DEBUG_FH.write(...); _OCR_DEBUG_FH.flush()
```

---

### 6. 硬编码的设备默认值暴露内部配置

**位置**：`DEFAULT_DEVICE`(L58)、`ADB`(L53-57)

```python
DEFAULT_DEVICE = os.environ.get("PHONE_MCP_DEVICE") or "134d2f8"
ADB = os.environ.get("ADB_BIN") or shutil.which("adb") or r"D:\ADB\adb.exe"
```

- 设备序列号 `134d2f8` 硬编码在源码中，泄露到 GitHub 后有隐私风险
- `D:\ADB\adb.exe` 是 Windows 绝对路径，其他平台不可用

**建议**：
- `PHONE_MCP_DEVICE` 设为**必须配置**（不提供默认值），未配置时自动选单设备
- `ADB_BIN` 保留自动探测回退，但移除硬编码 Windows 路径

---

### 7. `_screen_size` 的回退尺寸不准确

**位置**：`_screen_size()` (L1301-1311)

```python
def _screen_size(device):
    try:
        ...
    except Exception:
        return 1080, 2340  # 硬编码回退
```

回退值 `1080x2340` 仅匹配特定小米机型。其他设备（三星/华为/Pixel/AOSP）比例完全不同，会导致内核点击坐标完全错位。

**建议**：
```python
def _screen_size(device):
    try:
        ...
    except Exception:
        raise RuntimeError("无法获取屏幕尺寸，内核点击不可用。请确认设备已连接且 adb 正常。")
```

---

### 8. `_clipboard_set` / `_clipboard_get` 无 API 级别检查

**位置**：`_clipboard_set()`(L1526)、`_clipboard_get()`(L1550)

使用 `cmd clipboard` 命令，需要 **Android 8.0 (API 26+)**。在更低版本设备上静默失败。

**建议**：启动时通过 `getprop ro.build.version.sdk` 检查 API 级别，低于 26 时禁用剪贴板方案并打印警告。

---

### 9. `phone_file_write` 的临时文件无清理

**位置**：`t_file_write()` (L2159-2169)

```python
tmp = os.path.join(SHOT_DIR, "_write_tmp.txt")
with open(tmp, "w") as f: f.write(content)
run_adb(["push", tmp, path], ...)
# tmp 未删除！
```

每次调用写入的临时文件从不删除，`SHOT_DIR` 可能无限增长。

**建议**：在 push 完成后 `os.remove(tmp)`。

---

### 10. frida-rust 脚本注入缺乏输入长度限制

**位置**：`t_frida_script()` (L3160-3190)

脚本内容 `script_content` 无最大长度限制，可能上传巨大文件到设备。

**建议**：添加最大脚本大小限制（如 64KB）并检查：
```python
if len(script_content) > 65536:
    return fail("脚本超过 64KB 限制")
```

---

### 11. OCR 预加载异常被静默吞掉

**位置**：`get_ocr_reader()` 启动预热 (L1002-1006)

```python
try:
    get_ocr_reader()
except Exception as e:
    log("OCR 预加载失败（首次调用时将重试）:", e)
```

如果 RapidOCR 安装有问题，首次调用才会报错，用户失去提前发现问题的机会。

**建议**：预加载失败时应记录完整 traceback 并设置全局标志，在 `tools/list` 中标记 OCR 工具不可用。

---

## 🔵 优化建议

### 12. 改用 `argparse` 或 `dataclass` 统一参数校验

当前所有工具的入参校验分散在 65 个 `t_*` 函数中，各自通过 `_req()` + 手动 `args.get()` 取值，重复代码多且易出错。

**建议**：使用 `dataclass` + 类型注解定义工具参数：
```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TapParams:
    x: int
    y: int
    deviceSerial: Optional[str] = None
    hold: float = 0.08
    
    @classmethod
    def from_args(cls, args):
        return cls(
            x=_req(args, "x", "int"),
            y=_req(args, "y", "int"),
            deviceSerial=args.get("deviceSerial"),
        )
```

### 13. 日志系统改用标准 `logging` 模块

当前所有日志通过自定义 `log()` 函数写 stderr，无级别控制、无格式化、无文件轮转。

**建议**：
```python
import logging
logger = logging.getLogger("phone-mcp")
logger.addHandler(logging.StreamHandler(sys.stderr))
logger.setLevel(os.environ.get("PHONE_MCP_LOG_LEVEL", "INFO"))
```

### 14. `TOOLS` 注册表从 Python 列表改为 JSON 文件

当前 `TOOLS` 列表硬编码 65 个工具定义在 `server.py` 中（~1200 行），影响可读性。

**建议**：抽出为独立的 `tools.json` 或 `tools.yaml` 文件，启动时加载。这样也能让非 Python 用户/MCP 客户端直接阅读工具列表。

### 15. 添加单元测试

当前无任何自动化测试，所有验证依赖手动 `verify_*.py` 脚本。

**建议**：为核心模块添加 pytest 单元测试：
- `test_run_adb.py` — mock subprocess，测试重试/超时/DRYRUN
- `test_vision.py` — 用 fixture XML 测试 ui_find/element_find
- `test_dispatch.py` — 测试工具调度/异常处理/信封归一化
- `test_forbid.py` — 测试灾难命令黑名单匹配各种绕过

### 16. `_tap` 降级逻辑有资源泄漏风险

**位置**：`_tap()` (L406-421)

```python
try:
    _mt_tap(x, y, dev, hold=hold)
    return "kernel"
except Exception as e:
    _ocr_debug("内核点击失败，降级 input tap: %r" % e)
    try:
        run_adb(["shell", "input", "tap", ...])
    except Exception:
        pass  # ← 静默忽略降级失败
    return "input"
```

第二次降级失败时 `pass` 掉了异常但仍返回 `"input"`，调用方无法区分"内核成功"和"两次全失败"。

### 17. `_wechat_ensure_home` 轮询逻辑可以简化

**位置**：`_wechat_ensure_home()` (L1446-1481)

内部使用手动 `for i in range(40)` + `time.sleep(0.2)` = 最长 8 秒等待，已有 `_poll()` 工具函数但未复用。

---

## 🛡️ 安全审查

| 检查项 | 状态 | 说明 |
|--------|------|------|
| 灾难命令黑名单 | ⚠️ | 有 `CATASTROPHIC_RE`，但正则可被额外空格绕过 |
| shell 命令闸门 | ✅ | `require_shell()` + `ALLOW_SHELL` 环境变量，默认关闭 |
| DRYRUN 模式 | ✅ | 所有 mutating 操作支持 DRYRUN 只打印不执行 |
| 输入注入风险 | ⚠️ | `t_shell` 直接将用户输入传给 `subprocess.run` |
| 文件路径遍历 | ⚠️ | `t_file_read` 允许读任意路径（包括 `/data/data`），无路径白名单 |
| APK 安装 | ⚠️ | `t_install_apk` 允许安装任意本地 APK，无签名校验 |
| frida 脚本注入 | ⚠️ | `t_frida_script` 允许注入任意 Rhai 脚本到目标进程 |
| 内存写入 | ⚠️ | `t_frida_write_mem` 允许写任意地址内存 |
| 键盘注入 | ✅ | `_adbkeyboard_input` 使用 base64 编码文本，无注入风险 |
| 敏感信息泄漏 | ⚠️ | 设备序列号硬编码在源码中（已推 GitHub） |
| PID/进程操作 | ⚠️ | `t_kill` 可杀任意进程（需 ALLOW_SHELL） |
| 去卸载保护 | ❌ | 无保护—`t_uninstall` 可卸载任意应用（需 ALLOW_SHELL） |

---

## 📊 总结

| 维度 | 评分 | 说明 |
|------|------|------|
| 功能完整性 | ⭐⭐⭐⭐⭐ | 65 个工具覆盖界面、系统、硬件、插桩全场景 |
| 正确性 | ⭐⭐⭐⭐ | 已真机验证，核心功能稳定 |
| 安全性 | ⭐⭐⭐ | 有闸门和黑名单，但存在绕过路径 |
| 可维护性 | ⭐⭐ | 4200+ 行单文件，无模块拆分无自动化测试 |
| 可移植性 | ⭐⭐⭐ | 强依赖 root + adb，部分路径/设备硬编码 |
| 可观测性 | ⭐⭐⭐ | 有工具级日志和耗时统计，缺结构化日志和 metrics |

**优先修复顺序**：
1. 🔴 拆分单文件架构（否则任何改进都举步维艰）
2. 🔴 加固灾难命令黑名单
3. 🟡 修复 `t_kill_process` PID/包名判断
4. 🟡 移除硬编码设备序列号
5. 🔵 添加单元测试覆盖核心路径
