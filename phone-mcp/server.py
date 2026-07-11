#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phone-mcp —— 零依赖的 MCP (Model Context Protocol) stdio 服务。

通过 ADB 把 Android 手机的常见操作暴露成 MCP 工具，让 AI 助手（如 WorkBuddy）
在对话里直接操作手机：
  - 界面层：截图、点击、滑动、输入、启动 App、按键、读 UI 结构
  - 系统级/底层：自由 shell、管理服务/进程、系统属性、settings、文件读写、装/卸应用
  - 一键闭环：phone_auto_click 自动完成「截图/定位 → 点击 → 验证」，适合"点击 XX"类指令
  - 稳定性：所有 adb 操作带超时(PHONE_MCP_TIMEOUT，默认 30s)与失败重试(PHONE_MCP_RETRIES，默认 2 次)

协议：MCP over stdio，使用换行分隔的 JSON-RPC 2.0 消息。
依赖：Python 标准库 + 本机 adb；视觉定位(phone_tap_text/phone_find_text)可选依赖 rapidocr-onnxruntime。

环境变量：
  ADB_BIN                adb 可执行文件路径（默认自动探测，回退 D:\\ADB\\adb.exe）
  PHONE_MCP_DEVICE       默认设备序列号（默认 134d2f8）
  PHONE_MCP_DRYRUN       设为 1 → 所有"写"操作只打印命令不真正执行（安全预览）
  PHONE_MCP_ALLOW_SHELL  设为 1 → 开放底层/系统级命令(phone_shell 等)；默认关闭
  PHONE_MCP_SHOTDIR      截图/UI dump 的本地保存目录

注意：stdout 只能输出协议 JSON，其它日志一律写 stderr。
"""

import os
import sys
import json
import shlex
import shutil
import base64
import subprocess
import threading
import traceback
import time
import re
import collections
import xml.etree.ElementTree as ET

# MCP 框架以 UTF-8 收发 JSON。Windows 默认控制台编码为 GBK(cp936)，
# 会导致中文参数/返回值乱码（如 文件传输助手 → 鏂囦欢浼犺緭鍔╂墜），
# 进而使 phone_find_text/phone_tap_text 永远"找不到"。强制 stdin/stdout 走 UTF-8。
try:
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ADB = (
    os.environ.get("ADB_BIN")
    or shutil.which("adb")
    or r"D:\ADB\adb.exe"
)
DEFAULT_DEVICE = os.environ.get("PHONE_MCP_DEVICE") or "134d2f8"
DRYRUN = os.environ.get("PHONE_MCP_DRYRUN") == "1"
ALLOW_SHELL = os.environ.get("PHONE_MCP_ALLOW_SHELL") == "1"
# 极速模式：OCR 用更激进的缩放(720长边)进一步提速；对 UI 模式无影响(UI 本来就毫秒级)。
FAST = os.environ.get("PHONE_MCP_FAST") == "1"
SHOT_DIR = os.environ.get("PHONE_MCP_SHOTDIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "shots"
)
# 单次 adb 命令的超时(秒)与失败重试次数；可通过环境变量调整。
ADB_TIMEOUT = float(os.environ.get("PHONE_MCP_TIMEOUT", "30"))
ADB_RETRIES = int(os.environ.get("PHONE_MCP_RETRIES", "2"))
# 工具级整体重试（仅对瞬时/adb 异常重试，避免写入类操作重复执行导致副作用）。
# 默认 2 次；设 PHONE_MCP_TOOL_RETRIES=0 可关闭工具级重试。
_TOOL_RETRIES = int(os.environ.get("PHONE_MCP_TOOL_RETRIES", "2"))
PROTOCOL_VERSION = "2024-11-05"

SERVER_INFO = {"name": "phone-mcp", "version": "0.10.0"}

# ADBKeyBoard：行业标准的 Android 中文输入方案（ADB 输入法 + 广播注入文本）。
# 彻底替代不稳定的剪贴板方案：支持中文/英文/emoji/特殊符号/多行换行，且输入法无感知切换
# （用完即切回用户原输入法，不影响微信等 App 的正常输入法）。首次使用自动从本地 APK 安装启用。
ADB_KEYBOARD_PKG = "com.android.adbkeyboard"
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"
ADB_KEYBOARD_APK = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ADBKeyboard.apk"
)

# 灾难性命令黑名单（即使开启底层也禁止，防止变砖/丢数据）
# 用「词边界正则」匹配，避免 swipe 的子串 wipe 被误伤；同时仍拦得住真正的 wipe/reboot/format/dd if= 等。
CATASTROPHIC_RE = [
    re.compile(r"\b(reboot)\b"),
    re.compile(r"\b(wipe)\b"),
    re.compile(r"\b(format)\b"),
    re.compile(r"\b(mkfs)\b"),
    re.compile(r"\b(fastboot)\b"),
    re.compile(r"dd\s+if="),   # dd if=/dev/... 是销毁性写盘
]


def log(*args):
    """日志写 stderr，绝不污染 stdout。"""
    print("[phone-mcp]", *args, file=sys.stderr, flush=True)


def _req(args, key, kind="str"):
    """取必填参数；缺失或类型不符时抛 ValueError（handler 捕获后转友好提示）。

    kind='int' 尝试转 int；kind='str' 要求非空字符串（自动 strip）。
    """
    if key not in args or args[key] is None:
        raise ValueError("缺少必填参数: %s" % key)
    v = args[key]
    if kind == "int":
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError("参数 %s 必须为整数，收到: %r" % (key, v))
    s = str(v)
    if kind == "str" and not s.strip():
        raise ValueError("参数 %s 不能为空" % key)
    return s.strip() if kind == "str" else v


def with_retry(fn, retries=ADB_RETRIES, delay=0.4, what="操作"):
    """执行 fn（无参可调用）；失败自动重试 retries 次，间隔 delay 秒。

    返回 (ok, result)：成功时 ok=True，result 为 fn 的返回值；
    全部失败时 ok=False，result 为最后一次异常。仅捕获 Exception，
    异常会在 final 失败后原样抛出（由调用方决定如何呈现）。
    用于提升 adb 等易抖动操作的稳定性。
    """
    last = None
    for i in range(1, retries + 1):
        try:
            return True, fn()
        except Exception as e:  # noqa: BLE001 —— 统一重试任意异常
            last = e
            log("%s 第 %d/%d 次失败: %r" % (what, i, retries, e))
            if i < retries:
                time.sleep(delay)
    return False, last


def with_verification(action_fn, verify_fn, max_retries=2, delay=0.6):
    """通用操作后自动校验：执行 action_fn，再用 verify_fn 检查是否成功；失败则重试 action_fn。

    action_fn 无参可调用；verify_fn 接收 action_fn 的返回值，返回 True/False（或抛异常视为未通过）。
    成功返回 (True, action_result)；全部失败返回 (False, last_result)。
    用于实现'操作失败自动重试'的闭环思想（如点击后校验页面是否切换）。
    """
    last = None
    for i in range(1, max_retries + 1):
        try:
            last = action_fn()
        except Exception as e:  # noqa: BLE001
            last = e
        try:
            if verify_fn(last):
                return True, last
        except Exception:
            pass
        if i < max_retries:
            time.sleep(delay)
    return False, last


def _poll(verify_fn, tries=5, interval=0.4):
    """轮询检测：每 interval 秒调用 verify_fn()，一旦返回真立即返回 True；超时返回 False。
    用于替代固定长等待——目标状态一出现就继续，无需等满整段延时。"""
    for _ in range(tries):
        try:
            if verify_fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _wechat_foreground(device):
    """微信是否当前前台 App（dumpsys 解析，无 OCR 开销）。"""
    return _top_pkg(device) == "com.tencent.mm"


# ---------------------------------------------------------------------------
# ADB 执行
# ---------------------------------------------------------------------------

def list_devices():
    out = subprocess.run(
        [ADB, "devices"], capture_output=True, text=True
    ).stdout
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def resolve_device(explicit):
    """确定要操作的设备序列号。"""
    if explicit:
        return explicit
    if DEFAULT_DEVICE and DEFAULT_DEVICE in list_devices():
        return DEFAULT_DEVICE
    devs = list_devices()
    if len(devs) == 1:
        return devs[0]
    if len(devs) == 0:
        raise RuntimeError("未检测到已连接的 adb 设备，请先 `adb devices` 确认。")
    raise RuntimeError(
        "检测到多台设备 %s，请通过 deviceSerial 参数明确指定。" % devs
    )


def require_shell():
    """底层/系统级命令的闸门。"""
    if not ALLOW_SHELL:
        raise PermissionError(
            "底层(系统级)命令已禁用。如需启用，请在启动服务时设置环境变量 "
            "PHONE_MCP_ALLOW_SHELL=1（建议在 mcp.json 的 env 中设置）。"
        )


def forbid_catastrophic(text):
    low = (text or "").lower()
    for rx in CATASTROPHIC_RE:
        m = rx.search(low)
        if m:
            raise PermissionError("出于安全考虑，禁止执行灾难性命令: %s" % m.group(0))


def run_adb(args, device=None, mutating=False, capture=True, binary=False,
            timeout=ADB_TIMEOUT, retries=ADB_RETRIES, delay=0.4, what="adb"):
    """执行一条 adb 命令，带超时与失败重试（提升稳定性）。

    mutating 且在 DRYRUN 模式下只打印不执行（不真正连接设备）。
    binary=True 时用字节模式捕获（适用于截图等二进制输出）。
    失败(异常或超时)会自动重试 retries 次，全部失败则抛 RuntimeError。
    """
    cmd = [ADB]
    if device:
        cmd += ["-s", device]
    cmd += args

    if mutating and DRYRUN:
        log("[DRYRUN] 将执行:", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"" if binary else "", "")

    log("执行:", " ".join(cmd))

    def _run():
        return subprocess.run(
            cmd, capture_output=capture, text=(capture and not binary), timeout=timeout
        )

    ok, proc = with_retry(_run, retries=retries, delay=delay,
                          what="%s %s" % (what, " ".join(args[:2])))
    if not ok:
        raise RuntimeError("adb 命令执行失败: %s（%s）" % (" ".join(cmd), proc))
    return proc


# ---------------------------------------------------------------------------
# 界面层工具实现
# ---------------------------------------------------------------------------

def t_get_devices(args):
    devs = list_devices()
    if not devs:
        return [text_block("未发现已连接设备。请确认：\n1) 手机已开启 USB 调试\n2) 已授权此电脑\n3) 数据线正常")]
    lines = ["已连接设备："]
    for d in devs:
        mark = " (默认)" if d == DEFAULT_DEVICE else ""
        lines.append("  - %s%s" % (d, mark))
    return [text_block("\n".join(lines))]


def t_screenshot(args):
    device = resolve_device(args.get("deviceSerial"))
    os.makedirs(SHOT_DIR, exist_ok=True)
    dest = os.path.join(SHOT_DIR, "screen.png")
    try:
        _, w, h, _, _ = _cap_frame_root(device, dest)
    except Exception as e:
        return fail("截图失败: %s" % e)
    with open(dest, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return ok("已截图，保存至: %s\n图片字节数: %d" % (dest, os.path.getsize(dest)),
              path=dest, bytes=os.path.getsize(dest), width=w, height=h, image_b64=b64)


# ---- 内核级触摸：直接写 /dev/input/eventX 的 evdev 事件，绕过安卓 InputManager ----
# 与 minitouch / pyminitouch 等价的能力（无需额外的 daemon 二进制）：写入 ABS_MT 原生触摸事件，
# 不受 MIUI / 鸿蒙「禁止模拟点击」限制，事件与真人手指硬件信号一致。
# 已真机验证（小米 arm64 + root）：内核单击能可靠激活微信输入框焦点，使 ADBKeyBoard 文本成功写入。
_MT_CACHE = {}  # device -> {"event":..., "max_x":..., "max_y":...}

def _mt_detect(device):
    """探测多点触控 event 节点及其坐标上限，缓存。返回 dict 或 None。"""
    if device in _MT_CACHE:
        return _MT_CACHE[device]
    info = None
    try:
        cp = run_adb(["shell", "su -c 'getevent -p'"], device=device, capture=True)
        out = getattr(cp, "stdout", "") or ""
        if not isinstance(out, str):
            out = out.decode("utf-8", "ignore")
    except Exception:
        out = ""
    cur = None
    for line in out.splitlines():
        if line.strip().startswith("add device"):
            try:
                ev = line.split(":")[1].strip()
            except Exception:
                ev = None
            cur = {"event": ev, "max_x": None, "max_y": None}
            continue
        if cur and cur["event"]:
            # getevent -p 输出用 code 数字(0035=ABS_MT_POSITION_X, 0036=ABS_MT_POSITION_Y)，非符号名
            if "0035" in line and "max" in line:
                try:
                    cur["max_x"] = int(line.split("max")[-1].split(",")[0].strip())
                except Exception:
                    pass
            if "0036" in line and "max" in line:
                try:
                    cur["max_y"] = int(line.split("max")[-1].split(",")[0].strip())
                except Exception:
                    pass
            if cur["max_x"] and cur["max_y"]:
                info = cur
                break
    if info and info["event"]:
        _MT_CACHE[device] = info
    return info

def _mt_tap(x, y, device, hold=0.08):
    """内核级单击：写 event 设备 ABS_MT 事件序列（按下→抬起）。"""
    info = _mt_detect(device)
    if not info:
        raise RuntimeError("未探测到多点触控 event 节点，内核点击不可用。")
    w, h = _screen_size(device)
    ev = info["event"]
    dx = int(round(x * info["max_x"] / w))
    dy = int(round(y * info["max_y"] / h))
    press = [
        "sendevent %s 3 57 0" % ev,           # ABS_MT_TRACKING_ID = 0
        "sendevent %s 3 53 %d" % (ev, dx),    # ABS_MT_POSITION_X
        "sendevent %s 3 54 %d" % (ev, dy),    # ABS_MT_POSITION_Y
        "sendevent %s 3 48 8" % ev,           # ABS_MT_TOUCH_MAJOR
        "sendevent %s 3 49 8" % ev,           # ABS_MT_WIDTH_MAJOR
        "sendevent %s 3 51 100" % ev,         # ABS_MT_PRESSURE
        "sendevent %s 1 330 1" % ev,          # BTN_TOUCH down
        "sendevent %s 0 0 0" % ev,            # SYN_REPORT
    ]
    run_adb(["shell", "su -c '%s'" % "; ".join(press)], device=device, mutating=True)
    time.sleep(hold)
    release = [
        "sendevent %s 1 330 0" % ev,          # BTN_TOUCH up
        "sendevent %s 3 57 4294967295" % ev,  # ABS_MT_TRACKING_ID = -1 (lift)
        "sendevent %s 0 0 0" % ev,            # SYN_REPORT
    ]
    run_adb(["shell", "su -c '%s'" % "; ".join(release)], device=device, mutating=True)
    time.sleep(0.05)
    return True

def _mt_swipe(x1, y1, x2, y2, device, duration_ms=300, steps=12):
    """内核级滑动：首点按下后沿直线分步 move 到终点再抬起。"""
    info = _mt_detect(device)
    if not info:
        raise RuntimeError("未探测到多点触控 event 节点，内核滑动不可用。")
    w, h = _screen_size(device)
    ev = info["event"]
    def to_dev(x, y):
        return int(round(x * info["max_x"] / w)), int(round(y * info["max_y"] / h))
    ax, ay = to_dev(x1, y1)
    bx, by = to_dev(x2, y2)
    seg = max(1, int(duration_ms / steps))
    down = [
        "sendevent %s 3 57 0" % ev,
        "sendevent %s 3 53 %d" % (ev, ax),
        "sendevent %s 3 54 %d" % (ev, ay),
        "sendevent %s 3 48 8" % ev,
        "sendevent %s 3 49 8" % ev,
        "sendevent %s 3 51 100" % ev,
        "sendevent %s 1 330 1" % ev,
        "sendevent %s 0 0 0" % ev,
    ]
    run_adb(["shell", "su -c '%s'" % "; ".join(down)], device=device, mutating=True)
    time.sleep(0.02)
    for i in range(1, steps + 1):
        ix = int(ax + (bx - ax) * i / steps)
        iy = int(ay + (by - ay) * i / steps)
        mv = ["sendevent %s 3 53 %d" % (ev, ix), "sendevent %s 3 54 %d" % (ev, iy),
              "sendevent %s 3 51 100" % ev, "sendevent %s 0 0 0" % ev]
        run_adb(["shell", "su -c '%s'" % "; ".join(mv)], device=device, mutating=True)
        time.sleep(seg / 1000.0)
    rel = ["sendevent %s 1 330 0" % ev, "sendevent %s 3 57 4294967295" % ev, "sendevent %s 0 0 0" % ev]
    run_adb(["shell", "su -c '%s'" % "; ".join(rel)], device=device, mutating=True)
    time.sleep(0.05)
    return True

def _tap(x, y, device=None, hold=0.08, force_source=None):
    """统一点击：优先内核级（绕过 InputManager），失败自动降级 input tap。返回 'kernel'/'input'。"""
    dev = resolve_device(device)
    if force_source == "input":
        run_adb(["shell", "input", "tap", str(x), str(y)], device=dev, mutating=True)
        return "input"
    try:
        _mt_tap(x, y, dev, hold=hold)
        return "kernel"
    except Exception as e:
        _ocr_debug("内核点击失败，降级 input tap: %r" % e)
        try:
            run_adb(["shell", "input", "tap", str(x), str(y)], device=dev, mutating=True)
        except Exception:
            pass
        return "input"

def _swipe(x1, y1, x2, y2, device=None, duration_ms=300, force_source=None):
    """统一滑动：优先内核级，失败降级 input swipe。返回 'kernel'/'input'。"""
    dev = resolve_device(device)
    if force_source == "input":
        run_adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
                device=dev, mutating=True)
        return "input"
    try:
        _mt_swipe(x1, y1, x2, y2, dev, duration_ms=duration_ms)
        return "kernel"
    except Exception as e:
        _ocr_debug("内核滑动失败，降级 input swipe: %r" % e)
        try:
            run_adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
                    device=dev, mutating=True)
        except Exception:
            pass
        return "input"


def t_tap(args):
    x = _req(args, "x", "int")
    y = _req(args, "y", "int")
    device = resolve_device(args.get("deviceSerial"))
    src = _tap(x, y, device)
    return ok("已在 (%d, %d) 点击（输入方式=%s，绕过 InputManager 模拟点击限制）。" % (x, y, src),
              x=x, y=y, source=src)


def t_swipe(args):
    x1 = _req(args, "x1", "int")
    y1 = _req(args, "y1", "int")
    x2 = _req(args, "x2", "int")
    y2 = _req(args, "y2", "int")
    if "durationMs" in args and args.get("durationMs") is not None:
        dur = _req(args, "durationMs", "int")
    else:
        dur = 300
    device = resolve_device(args.get("deviceSerial"))
    src = _swipe(x1, y1, x2, y2, device, duration_ms=dur)
    return ok("已从 (%d,%d) 滑动到 (%d,%d)，时长 %dms（输入方式=%s，绕过 InputManager 模拟点击限制）。"
              % (x1, y1, x2, y2, dur, src),
              x1=x1, y1=y1, x2=x2, y2=y2, durationMs=dur, source=src)


# 无障碍服务点击：当 input 注入被系统/ROM 拦截时的备选路径。
# 需先在手机上安装并启用 phone-mcp 无障碍服务（设计见 ACCESSIBILITY_FALLBACK.md）。
A11Y_PKG = "com.phonemcp.a11y"
A11Y_TAP_ACTION = A11Y_PKG + ".TAP"


def a11y_tap(x, y, device, duration_ms=80):
    """经无障碍服务广播触发坐标点击，绕过 input 注入的 INJECT_EVENTS 权限限制。"""
    run_adb(
        ["shell", "am", "broadcast", "-a", A11Y_TAP_ACTION,
         "--ei", "x", str(x), "--ei", "y", str(y), "--ei", "d", str(duration_ms)],
        device=device, mutating=True,
    )
    return ok(
        "已通过无障碍服务请求在 (%d, %d) 点击（需手机端已安装并启用 %s 无障碍服务）。" % (x, y, A11Y_PKG),
        x=x, y=y,
    )


def t_a11y_tap(args):
    device = resolve_device(args.get("deviceSerial"))
    x, y = int(args["x"]), int(args["y"])
    return a11y_tap(x, y, device, int(args.get("durationMs", 80)))


def t_input_text(args):
    text = _req(args, "text")
    device = resolve_device(args.get("deviceSerial"))
    try:
        text.encode("ascii")
        ascii_only = True
    except UnicodeEncodeError:
        ascii_only = False
    if ascii_only:
        # adb input text 仅可靠支持 ASCII；空格用 %s 表示
        run_adb(["shell", "input", "text", text.replace(" ", "%s")], device=device, mutating=True)
        return ok("已输入文本（ASCII input）。", text=text, method="input")
    # 含非 ASCII（中文/emoji 等）→ 自动走剪贴板 + 粘贴键方案
    run_adb(["shell", "cmd", "clipboard", "set", text], device=device, mutating=True)
    run_adb(["shell", "input", "keyevent", "279"], device=device, mutating=True)  # KEYCODE_PASTE
    return ok("已通过剪贴板粘贴文本（含中文等非 ASCII 字符）。", text=text, method="clipboard")


def t_paste_text(args):
    """通过剪贴板 + 粘贴键实现任意 Unicode（含中文）输入。"""
    text = _req(args, "text")
    device = resolve_device(args.get("deviceSerial"))
    run_adb(["shell", "cmd", "clipboard", "set", text], device=device, mutating=True)
    run_adb(["shell", "input", "keyevent", "279"], device=device, mutating=True)  # KEYCODE_PASTE
    return ok("已通过剪贴板粘贴文本（支持中文）。", text=text)


def t_launch_app(args):
    pkg = _req(args, "package")
    act = args.get("activity")
    device = resolve_device(args.get("deviceSerial"))
    if act:
        run_adb(["shell", "am", "start", "-n", "%s/%s" % (pkg, act)], device=device, mutating=True)
    else:
        run_adb(
            ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
            device=device,
            mutating=True,
        )
    return ok("已尝试启动 %s。" % pkg, package=pkg, activity=act)


KEYCODES = {
    "HOME": "3", "BACK": "4", "MENU": "82", "VOLUME_UP": "24",
    "VOLUME_DOWN": "25", "POWER": "26", "ENTER": "66", "DELETE": "67",
    "RECENT": "187", "CAMERA": "27", "PASTE": "279",
}


def t_key_event(args):
    k = _req(args, "keycode")
    device = resolve_device(args.get("deviceSerial"))
    code = KEYCODES.get(k.upper(), k)  # 允许传名称(HOME/BACK/POWER)或数字
    run_adb(["shell", "input", "keyevent", code], device=device, mutating=True)
    return ok("已发送按键: %s (code=%s)。" % (k, code), keycode=k, code=code)


def t_press_back(args):
    """返回键(BACK)。便捷别名，等价于 phone_key_event 传 BACK。"""
    a = dict(args)
    a["keycode"] = "BACK"
    return t_key_event(a)


def t_press_home(args):
    """主页键(HOME)。便捷别名，等价于 phone_key_event 传 HOME。"""
    a = dict(args)
    a["keycode"] = "HOME"
    return t_key_event(a)


def t_dump_ui(args):
    device = resolve_device(args.get("deviceSerial"))
    run_adb(
        ["shell", "uiautomator", "dump", "/sdcard/ui_dump.xml"],
        device=device,
        mutating=False,
    )
    os.makedirs(SHOT_DIR, exist_ok=True)
    local = os.path.join(SHOT_DIR, "ui_dump.xml")
    run_adb(["pull", "/sdcard/ui_dump.xml", local], device=device, mutating=False)
    try:
        with open(local, "r", encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception as e:
        return [text_block("读取 UI 结构失败: %s" % e)], True
    if len(xml) > 200000:
        xml = xml[:200000] + "\n... (已截断，完整文件见 %s)" % local
    return ok("当前界面 UI 结构（来自 %s）：\n%s" % (local, xml), path=local)


# ---------------------------------------------------------------------------
# 模式 1：无障碍 / UI 模式 —— 解析 uiautomator dump 的 XML，直接拿控件文字+坐标。
# 毫秒级、零 OCR、坐标精准。适用于系统界面/桌面/大多数 App。
# 缺点：微信/QQ 等关闭无障碍导出时会得到空树 → auto 模式会自动回退到 OCR。
# ---------------------------------------------------------------------------

# bounds 形如 [x1,y1][x2,y2]
_BOUNDS_RE = re.compile(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]")
# 逐个 node 标签
_NODE_RE = re.compile(r"<node\b[^>]*/?>")
_ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')


def _get_ui_xml(device):
    """dump 当前界面 UI 结构并拉取到本地，返回 XML 文本；失败返回 None（带超时+重试）。"""
    try:
        run_adb(["shell", "uiautomator", "dump", "/sdcard/ui_dump.xml"],
                device=device, mutating=False, what="uiautomator dump")
    except Exception as e:
        log("uiautomator dump 失败:", e)
        return None
    os.makedirs(SHOT_DIR, exist_ok=True)
    local = os.path.join(SHOT_DIR, "ui_dump.xml")
    try:
        run_adb(["pull", "/sdcard/ui_dump.xml", local], device=device, mutating=False, what="pull ui_dump")
    except Exception as e:
        log("pull ui_dump 失败:", e)
        return None
    try:
        with open(local, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        log("读取 UI dump 失败:", e)
        return None


def ui_find(query, xml, exact=False):
    """从 uiautomator XML 中查找 text / content-desc 含 query 的控件。
    返回 [(text, cx, cy, 1.0)]（原图像素坐标，置信度恒为 1.0 表示精确来源）。"""
    hits = []
    if not xml:
        return hits
    for node in _NODE_RE.findall(xml):
        attrs = dict(_ATTR_RE.findall(node))
        label = attrs.get("text") or ""
        desc = attrs.get("content-desc") or ""
        candidates = [c for c in (label, desc) if c]
        if not candidates:
            continue
        matched = None
        for c in candidates:
            if (c == query) if exact else (query in c):
                matched = c
                break
        if matched is None:
            continue
        m = _BOUNDS_RE.search(attrs.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        hits.append((matched, cx, cy, 1.0))
    return hits


# 空树缓存：某 App 的 UI 导出全空(如微信/QQ)时，auto 模式在 TTL 内跳过 UI 探测，
# 直接走 OCR，省掉每次白白浪费的 dump 时间(约 2s)。按 "device:pkg" 精准标记，
# 桌面/系统界面与微信/QQ 互不干扰。
_UI_EMPTY = {}
_UI_EMPTY_TTL = 30
_TOP_PKG_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([\w.]+)/")
# 解析当前前台应用（包名 + Activity），用于 get_current_app
_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\b([\w.\-/$]+)/([\w.\-/$]+)")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=AppWindowToken\{[^}]*?\b([\w.\-/$]+)/([\w.\-/$]+)")


def _top_pkg(device):
    """取当前顶层 App 包名，用于精准标记空树 App。失败返回 None（走统一 run_adb，带超时+重试）。"""
    try:
        r = run_adb(["shell", "dumpsys", "window"], device=device,
                    mutating=False, what="dumpsys window")
        m = _TOP_PKG_RE.search(r.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def _ui_is_empty(xml):
    """UI dump 是否完全无文字（text 与 content-desc 全空）。"""
    if not xml:
        return True
    for node in _NODE_RE.findall(xml):
        a = dict(_ATTR_RE.findall(node))
        if (a.get("text") or "").strip() or (a.get("content-desc") or "").strip():
            return False
    return True


def _ocr_only(query, device, exact, region):
    get_ocr_reader()
    shot = _ocr_screenshot(device, region)
    if not shot:
        raise RuntimeError("截图失败，无法 OCR。")
    path, scale, off_x, off_y = shot
    return ocr_find(query, path, scale, off_x=off_x, off_y=off_y, exact=exact)


def smart_find(query, device, exact=False, region=None, method="auto"):
    """统一查找入口，返回 (hits, used_method)。
      method="ui"   → 只用 UI 模式（毫秒级，系统界面/桌面/计算器可用）
      method="ocr"  → 只用 OCR 模式（微信/QQ 等空树 App 用）
      method="auto" → 先 UI；未命中或空树再回退 OCR（默认，兼顾速度与兼容）
                      空树 App 会被缓存 TTL 秒，期间直接走 OCR，避免重复 dump 拖慢。
    hits = [(text, cx, cy, conf)]，原图像素坐标。
    """
    if method == "ui":
        return ui_find(query, _get_ui_xml(device), exact=exact), "ui"
    if method == "ocr":
        return _ocr_only(query, device, exact, region), "ocr"
    # auto
    pkg = _top_pkg(device)
    key = "%s:%s" % (device, pkg)
    if pkg and time.time() < _UI_EMPTY.get(key, 0):
        return _ocr_only(query, device, exact, region), "ocr"
    xml = _get_ui_xml(device)
    hits = ui_find(query, xml, exact=exact)
    if hits:
        return hits, "ui"
    if _ui_is_empty(xml) and pkg:
        _UI_EMPTY[key] = time.time() + _UI_EMPTY_TTL
    return _ocr_only(query, device, exact, region), "ocr"


# ---------------------------------------------------------------------------
# 控件级定位（element）：把 uiautomator dump 解析成结构化控件树，
# 支持按 文字 / resource-id / content-desc 精准查找并直接点击。
# 比 OCR 更稳定（无需图像、坐标来自系统、毫秒级），是主力定位方案；
# 微信/QQ 等空树 App 由 tap_element 自动回退 OCR。
# ---------------------------------------------------------------------------

def _node_from_attrs(a):
    """把一个 node 的属性 dict 转成结构化控件信息（含中心像素坐标）。"""
    a = a or {}
    rid = a.get("resource-id", "") or ""
    text = a.get("text", "") or ""
    desc = a.get("content-desc", "") or ""
    bounds = a.get("bounds", "") or ""
    cls = a.get("class", "") or ""
    pkg = rid.split("/", 1)[0] if "/" in rid else ""
    cx = cy = None
    m = _BOUNDS_RE.search(bounds)
    if m:
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    return {
        "text": text,
        "resourceId": rid,
        "contentDesc": desc,
        "className": cls,
        "package": pkg,
        "clickable": (a.get("clickable", "false") == "true"),
        "bounds": bounds,
        "cx": cx,
        "cy": cy,
    }


def parse_ui_xml(xml):
    """解析 uiautomator dump XML，返回所有 node 的结构化 dict 列表。

    每个 dict: text, resourceId, contentDesc, className, clickable, package,
               bounds, cx, cy(中心像素坐标, 无 bounds 则为 None)。
    ElementTree 解析失败时回退到正则逐 node 提取，保证健壮性。
    """
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except Exception as e:
        log("ElementTree 解析 UI XML 失败，回退正则:", e)
        nodes = []
        for node in _NODE_RE.findall(xml):
            nodes.append(_node_from_attrs(dict(_ATTR_RE.findall(node))))
        return nodes
    return [_node_from_attrs(el.attrib) for el in root.iter("node")]


def element_find(query, xml, match_by="any", exact=False):
    """在 UI 树中按 text / resource-id / content-desc 查找控件。

    match_by: any(默认) | text | resource-id | content-desc
      - any  : 在 text / resource-id / content-desc 任一字段匹配
      - 其余 : 仅在指定字段匹配
    exact=True 要求完全相等；否则子串包含即匹配。
    仅返回带有效坐标的命中。返回 [(label, cx, cy, node)]，node 为结构化 dict。
    """
    if not xml or not query:
        return []
    hits = []
    for n in parse_ui_xml(xml):
        if match_by in ("any", "text"):
            fields = [n["text"], n["resourceId"], n["contentDesc"]]
        elif match_by == "resource-id":
            fields = [n["resourceId"]]
        elif match_by == "content-desc":
            fields = [n["contentDesc"]]
        else:  # text
            fields = [n["text"]]
        matched = None
        for fld in fields:
            if not fld:
                continue
            if (fld == query) if exact else (query in fld):
                matched = fld
                break
        if matched is None:
            continue
        if n["cx"] is None:
            continue
        hits.append((matched, n["cx"], n["cy"], n))
    return hits


def t_ui_dump(args):
    """解析当前界面控件树（底层复用 uiautomator2 dump_hierarchy，不再自己解析 XML 字节）。
    返回所有具名控件(含文字/resource-id/content-desc)的中心坐标；完整树另存为 JSON。"""
    device = resolve_device(args.get("deviceSerial"))
    xml = None
    try:
        d = _u2_device(device)
        xml = d.dump_hierarchy()
    except Exception as e:
        _ocr_debug("uiautomator2 dump_hierarchy 失败，回退 adb uiautomator dump: %r" % e)
        xml = _get_ui_xml(device)
    if not xml:
        return [text_block("UI 结构获取失败（可能设备未就绪或 uiautomator 不可用）。")], True
    nodes = parse_ui_xml(xml)
    total = len(nodes)
    named = [n for n in nodes if (n["text"] or n["contentDesc"] or n["resourceId"])]
    os.makedirs(SHOT_DIR, exist_ok=True)
    json_path = os.path.join(SHOT_DIR, "ui_dump.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(nodes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("保存 ui_dump.json 失败:", e)
    lines = ["已解析界面控件树（via uiautomator2）：共 %d 个节点，其中含文字/描述/ID 的有 %d 个（完整 JSON 见 %s）。"
             % (total, len(named), json_path)]
    MAX = 200
    shown = named[:MAX]
    for i, n in enumerate(shown, 1):
        label = n["text"] or n["contentDesc"] or n["resourceId"]
        coord = "@ (%d, %d)" % (n["cx"], n["cy"]) if n["cx"] is not None else "(无坐标)"
        rid = (' id="%s"' % n["resourceId"]) if n["resourceId"] else ""
        cls = (" class=%s" % n["className"].split(".")[-1]) if n["className"] else ""
        click = " [可点]" if n["clickable"] else ""
        lines.append("  %d. %s%s | %s%s%s" % (i, label, rid, coord, cls, click))
    if len(named) > MAX:
        lines.append("  ... 其余 %d 个具名控件见 JSON 文件" % (len(named) - MAX))
    return ok("\n".join(lines), total=total, named=len(named), json_path=json_path)


def t_find_element(args):
    """按 文字 / resource-id / content-desc 查找控件并返回坐标。
    底层复用 uiautomator2 原生选择器(d(text=)/d(resourceId=)/d(description=)，自带超时重试)，
    不再自己解析 XML；微信/QQ 等空树 App 自动回退 OCR。matchBy 指定字段(默认 any 全字段)。"""
    device = resolve_device(args.get("deviceSerial"))
    query = str(args["query"])
    match_by = str(args.get("matchBy", "any")).lower()
    exact = bool(args.get("exact", False))
    try:
        d = _u2_device(device)
        sel = _u2_selector(d, query, match_by, exact)
        hits = []
        for el in sel:
            info = el.info or {}
            bx = info.get("bounds") or [0, 0, 0, 0]
            cx = (bx[0] + bx[2]) // 2
            cy = (bx[1] + bx[3]) // 2
            label = info.get("text") or info.get("contentDescription") or query
            hits.append({"label": label, "cx": cx, "cy": cy,
                         "resourceId": info.get("resourceName") or ""})
        if hits:
            lines = ["找到 %d 个匹配 '%s' 的控件（matchBy=%s，via uiautomator2）："
                     % (len(hits), query, match_by)]
            for i, h in enumerate(hits, 1):
                rid = (' id="%s"' % h["resourceId"]) if h["resourceId"] else ""
                lines.append("  %d. %s%s @ (%d, %d)" % (i, h["label"], rid, h["cx"], h["cy"]))
            return ok("\n".join(lines), count=len(hits), hits=hits,
                      query=query, matchBy=match_by, method="uiautomator2")
    except Exception as e:
        _ocr_debug("uiautomator2 查找失败，回退 OCR: %r" % e)
    # 回退 OCR（微信/QQ 等空树 App 用；仅文字类查找有效）
    if match_by in ("any", "text"):
        try:
            ocr_hits, _ = smart_find(query, device, exact=exact, method="ocr")
        except Exception:
            ocr_hits = []
        if ocr_hits:
            hit_list = [{"label": t, "cx": cx, "cy": cy, "resourceId": ""}
                        for (t, cx, cy, _c) in ocr_hits]
            lines = ["找到 %d 个匹配 '%s'（via OCR 回退）：" % (len(hit_list), query)]
            for i, h in enumerate(hit_list, 1):
                lines.append("  %d. %s @ (%d, %d)" % (i, h["label"], h["cx"], h["cy"]))
            return ok("\n".join(lines), count=len(hit_list), hits=hit_list,
                      query=query, matchBy=match_by, method="ocr")
    return fail("未找到匹配 '%s' 的控件（matchBy=%s）。" % (query, match_by),
                query=query, matchBy=match_by)


def _u2_selector(d, query, match_by, exact):
    """按 match_by 构造 uiautomator2 选择器（any=text→resourceId→description 依次尝试）。"""
    if match_by == "resource-id":
        return d(resourceId=query) if exact else d(resourceIdMatches=".*%s.*" % re.escape(query))
    if match_by == "content-desc":
        return d(description=query) if exact else d(descriptionContains=query)
    if match_by == "text":
        return d(text=query) if exact else d(textContains=query)
    # any：text 优先，未命中再试 resourceId / description
    sel = d(text=query) if exact else d(textContains=query)
    if sel.count > 0:
        return sel
    sel = d(resourceId=query) if exact else d(resourceIdMatches=".*%s.*" % re.escape(query))
    if sel.count > 0:
        return sel
    return d(description=query) if exact else d(descriptionContains=query)


def t_tap_element(args):
    """按 文字 / resource-id / content-desc 直接点击控件。底层复用 uiautomator2 原生选择器
    （毫秒级、系统坐标、自带超时重试）；微信/QQ 等空树 App 自动回退 OCR。index 指定第几个匹配。"""
    device = resolve_device(args.get("deviceSerial"))
    query = str(args["query"])
    match_by = str(args.get("matchBy", "any")).lower()
    exact = bool(args.get("exact", False))
    index = int(args.get("index", 1))
    fallback = bool(args.get("fallback", True))
    cx = cy = None
    used = None
    try:
        d = _u2_device(device)
        sel = _u2_selector(d, query, match_by, exact)
        if sel.count > 0:
            idx = index - 1
            if 0 <= idx < sel.count:
                info = sel[idx].info or {}
                bx = info.get("bounds") or [0, 0, 0, 0]
                cx = (bx[0] + bx[2]) // 2
                cy = (bx[1] + bx[3]) // 2
                used = "uiautomator2"
    except Exception as e:
        _ocr_debug("uiautomator2 点击查找失败，回退 OCR: %r" % e)
    # OCR 回退（微信/QQ 等空树 App；仅文字类查找生效）
    if cx is None and match_by in ("any", "text") and fallback:
        try:
            ocr_hits, _ = smart_find(query, device, exact=exact, method="ocr")
        except Exception:
            ocr_hits = []
        if ocr_hits and 1 <= index <= len(ocr_hits):
            _t, cx, cy, _c = ocr_hits[index - 1]
            used = "ocr"
    if cx is None:
        return fail("未找到匹配 '%s' 的控件，未点击（matchBy=%s）。" % (query, match_by),
                    query=query, matchBy=match_by)
    if DRYRUN:
        return ok("[DRYRUN] 将点击 '%s' @ (%d, %d)（%s），未真正执行。" % (query, cx, cy, used),
                  dryrun=True, label=query, cx=cx, cy=cy, method=used)
    src = _tap(cx, cy, device)
    return ok("已点击控件 '%s' @ (%d, %d)（via %s 元素定位，输入方式=%s，绕过 InputManager 模拟点击限制）。"
              % (query, cx, cy, used, src),
              label=query, cx=cx, cy=cy, method=used, source=src)


# ---------------------------------------------------------------------------
# 模式 2：OCR 视觉定位（可选依赖：rapidocr-onnxruntime）。未安装时工具返回友好提示，不影响其他工具。
# ---------------------------------------------------------------------------

def _ocr_debug(msg):
    """OCR 诊断信息追加写入 SHOT_DIR/ocr_debug.txt，用于排查 MCP 进程内 OCR 异常。"""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        with open(os.path.join(SHOT_DIR, "ocr_debug.txt"), "a", encoding="utf-8") as f:
            f.write("[%.3f] %s\n" % (time.time(), msg))
    except Exception:
        pass


_OCR_READER = None
_OCR_READY = threading.Event()

# 注意：不做坐标记忆——手机界面是动态的（新消息/置顶/滑动都会改位置），
# 缓存坐标会点错。每次操作都实时截图 + 实时 OCR，拿到当前坐标再点。



_OCR_LOCK = threading.Lock()


def get_ocr_reader():
    """获取 RapidOCR 引擎单例。

    必须在调用线程(serve 主线程)内创建 InferenceSession，否则 onnxruntime 会话跨线程
    失效会让 reader(image) 返回 None（表现为 ocr_find 报"两次调用均失败 None"）。
    用锁保证只构建一次，且构建发生在真正调用的线程中。
    """
    global _OCR_READER
    if _OCR_READER is not None:
        return _OCR_READER
    with _OCR_LOCK:
        if _OCR_READER is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_READER = RapidOCR()
    return _OCR_READER


# 启动时在【主线程】同步预加载 OCR 引擎。
# 关键点：InferenceSession 必须在 serve 主线程内创建，跨线程创建会导致推理返回 None。
# 因此这里直接在当前(主)线程构建，不再用后台线程预热。
try:
    get_ocr_reader()
    log("OCR 引擎预加载完成(RapidOCR)")
except Exception as e:
    log("OCR 预加载失败（首次调用时将重试）:", e)


def _ocr_screenshot(device, region=None):
    """截图到 OCR 专用文件。

    region: 可选 [x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以大幅提速。
    返回 (path, scale, off_x, off_y)：scale 为缩放比，off_* 为裁剪区在全图的左上角(原图像素)。
    不做坐标记忆——每次都实时截图+实时OCR，界面动态也不怕点错。
    """
    import cv2
    try:
        ok = run_adb(["exec-out", "su -c 'screencap -p'"], device=device, capture=True,
                     binary=True, what="screencap(root)")
    except Exception as e:
        _ocr_debug("截图失败: %s" % e)
        return None
    png = ok.stdout
    if not png or len(png) < 100:
        return None
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, "ocr_shot.png")
    with open(path, "wb") as f:
        f.write(png)
    img = cv2.imread(path)
    if img is None:
        _ocr_debug("截图 cv2.imread 失败 path=%s" % path)
        return None
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        cv2.imwrite(path, img)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region:
        x1, y1, x2, y2 = [float(v) for v in region]
        cxa, cya = int(x1 * w), int(y1 * h)
        cxb, cyb = int(x2 * w), int(y2 * h)
        if cxb > cxa and cyb > cya:
            img = img[cya:cyb, cxa:cxb]
            off_x, off_y = cxa, cya
            cv2.imwrite(path, img)
            h, w = img.shape[:2]
    # 极速模式用 720 长边(更快但精度略降)；默认 1080(精度更稳)。
    max_side = 720 if FAST else 1080
    scale = 1.0
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        small = cv2.resize(img, (int(w * scale), int(h * scale)))
        cv2.imwrite(path, small)
    _ocr_debug("截图 OK path=%s size=%dx%d scale=%.3f" % (path, w, h, scale))
    return path, scale, off_x, off_y


def ocr_find(query, image_path, scale, off_x=0, off_y=0, exact=False, min_conf=0.25):
    """在截图中查找包含 query 的文字块，返回 [(text, cx, cy, conf)]（原图像素坐标）。"""
    reader = get_ocr_reader()
    result = None
    last_err = None
    for _attempt in range(2):
        try:
            result, _ = reader(image_path)   # RapidOCR: list of [bbox, text, score]
            break
        except Exception as e:
            last_err = e
            _ocr_debug("ocr_find: reader 异常(尝试%d) %s:\n%s" % (
                _attempt + 1, type(e).__name__, traceback.format_exc()))
    if result is None:
        _ocr_debug("ocr_find: 两次调用均失败 last_err=%r path=%s" % (last_err, image_path))
        return []
    hits = []
    if not result:
        _ocr_debug("ocr_find: 空结果 path=%s" % image_path)
        return hits
    for bbox, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        match = (txt == query) if exact else (query in txt)
        if match:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx = int((min(xs) + max(xs)) / 2 / scale) + off_x
            cy = int((min(ys) + max(ys)) / 2 / scale) + off_y
            hits.append((txt, cx, cy, conf))
    if not hits:
        _ocr_debug("ocr_find: 无匹配 query=%r 总数=%d 样例=%s" % (
            query, len(result), [(t, round(float(c), 2)) for _, t, c in result[:15]]))
    return hits


def ocr_boxes(device, region=None, min_conf=0.25):
    """全屏(或 region 归一化区域)OCR，返回 [(text, cx, cy, conf)]（原图像素坐标）。
    region: 可选 [x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以提速并避免误匹配。"""
    shot = _ocr_screenshot(device, region)
    if not shot:
        return []
    path, scale, off_x, off_y = shot
    try:
        result, _ = get_ocr_reader()(path)
    except Exception as e:  # noqa: BLE001
        _ocr_debug("ocr_boxes reader 异常: %r" % e)
        return []
    if not result:
        return []
    boxes = []
    for bbox, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2 / scale) + off_x
        cy = int((min(ys) + max(ys)) / 2 / scale) + off_y
        boxes.append((txt, cx, cy, conf))
    return boxes


def ocr_match_contact(query, device, region=None, min_conf=0.3):
    """OCR 精准匹配联系人条目：返回按 y 升序(最顶部优先)的 [(text, cx, cy, conf)]。

    针对搜索结果多匹配问题：
      - 区域过滤(region)避开底部聊天记录与输入框；
      - 优先精确匹配(整块文字 == query)，其次包含匹配，排除聊天记录里出现的同名片段；
      - 结果按 y 升序，调用方取 [0] 即最顶部的联系人条目，避免误点到聊天记录。
    """
    boxes = ocr_boxes(device, region=region, min_conf=min_conf)
    hits = [b for b in boxes if query in b[0]]
    if not hits:
        return []
    exact = [b for b in hits if b[0].strip() == query]
    pool = exact if exact else hits
    return sorted(pool, key=lambda b: b[2])


def _ocr_sees(device, query, region=None, min_conf=0.25):
    """屏幕(或 region)上是否存在含 query 的文字块。"""
    return any(query in b[0] for b in ocr_boxes(device, region=region, min_conf=min_conf))


def _ocr_tap(device, query, region=None, min_conf=0.25, strategy="top"):
    """OCR 找到含 query 的文字块并点击；strategy=top 点最靠上的，lowest 点最靠下的。
    命中返回 True，未找到返回 False。"""
    hits = [b for b in ocr_boxes(device, region=region, min_conf=min_conf) if query in b[0]]
    if not hits:
        return False
    sel = sorted(hits, key=lambda b: b[2])[0 if strategy == "top" else -1]
    cx, cy = sel[1], sel[2]
    run_adb(["shell", "input", "tap", str(cx), str(cy)], device=device, mutating=True)
    return True


def t_find_text(args):
    device = resolve_device(args.get("deviceSerial"))
    query = str(args["text"])
    _ocr_debug("收到 t_find_text query=%r method=%s" % (query, args.get("method")))
    exact = bool(args.get("exact", False))
    region = args.get("region")
    method = str(args.get("method", "auto")).lower()
    try:
        hits, used = smart_find(query, device, exact=exact, region=region, method=method)
    except RuntimeError as e:
        return fail(str(e))
    mode_label = {"ui": "无障碍/UI(毫秒级)", "ocr": "OCR(视觉)"}.get(used, used)
    if not hits:
        return ok("未找到包含 '%s' 的文字（模式: %s）。" % (query, mode_label),
                  found=False, query=query, method=used)
    hit_list = [{"text": txt, "cx": cx, "cy": cy, "conf": round(float(conf), 2)}
                for (txt, cx, cy, conf) in hits]
    lines = ["在屏幕上找到 %d 处匹配 '%s'（模式: %s）：" % (len(hits), query, mode_label)]
    for i, h in enumerate(hit_list, 1):
        lines.append("  %d. '%s' @ (%d, %d) 置信度 %.2f" % (i, h["text"], h["cx"], h["cy"], h["conf"]))
    return ok("\n".join(lines), found=True, count=len(hits), hits=hit_list,
              query=query, method=used)


def t_tap_text(args):
    device = resolve_device(args.get("deviceSerial"))
    query = str(args["text"])
    _ocr_debug("收到 t_tap_text query=%r method=%s" % (query, args.get("method")))
    exact = bool(args.get("exact", False))
    index = int(args.get("index", 1))
    region = args.get("region")
    method = str(args.get("method", "auto")).lower()
    max_retries = max(1, int(args.get("maxRetries", 2)))
    # 自动重试：界面可能未渲染完导致一时找不到，稍后重定位通常即可点到。
    for attempt in range(1, max_retries + 1):
        try:
            hits, used = smart_find(query, device, exact=exact, region=region, method=method)
        except RuntimeError as e:
            if attempt >= max_retries:
                return fail(str(e))
            time.sleep(0.5)
            continue
        mode_label = {"ui": "无障碍/UI(毫秒级)", "ocr": "OCR(视觉)"}.get(used, used)
        if not hits:
            if attempt >= max_retries:
                return fail("未找到包含 '%s' 的文字，未点击（模式: %s）。" % (query, mode_label),
                            query=query, method=used, found=False)
            time.sleep(0.6)
            continue
        if index < 1 or index > len(hits):
            return fail("index 超出范围（共 %d 处匹配，请用 index 指定）。" % len(hits),
                        query=query, method=used, count=len(hits))
        txt, cx, cy, conf = hits[index - 1]
        if DRYRUN:
            return ok("[DRYRUN] 将点击 '%s' @ (%d, %d)（模式: %s），未真正执行。" % (txt, cx, cy, mode_label),
                      dryrun=True, label=txt, cx=cx, cy=cy, method=used, conf=round(float(conf), 2),
                      attempts=attempt)
        run_adb(["shell", "input", "tap", str(cx), str(cy)], device=device, mutating=True)
        return ok("已点击 '%s' @ (%d, %d)（模式: %s，置信度 %.2f）。" % (txt, cx, cy, mode_label, conf),
                  label=txt, cx=cx, cy=cy, method=used, conf=round(float(conf), 2), attempts=attempt)
    return fail("未点击 '%s'（已重试 %d 次）。" % (query, max_retries),
                query=query, attempts=max_retries)


def t_auto_click(args):
    """【一键闭环】截图/定位 → 点击 → 验证：用户说"点击 XX"时调用。

    流程：
      1) 定位：smart_find(query) 自动选 UI(毫秒级) 或 OCR(微信/QQ 等空树 App 回退)，
         拿不到坐标就重试（界面可能还没渲染完）。
      2) 点击：input tap 命中坐标（带重试）。
      3) 验证：再次 smart_find；若目标已离开屏幕 → 说明页面已切换/操作生效，成功。
         目标仍在 → 可能是开关类控件或页面未切换，进入下一轮重试。
    失败自动重试 maxRetries 轮；DRYRUN 仅打印意图不真正点击。
    """
    query = _req(args, "query")
    device = resolve_device(args.get("deviceSerial"))
    method = str(args.get("method", "auto")).lower()
    match_by = str(args.get("matchBy", "any")).lower()
    exact = bool(args.get("exact", False))
    index = int(args.get("index", 1))
    max_retries = max(1, int(args.get("maxRetries", 3)))
    verify = str(args.get("verify", "gone")).lower()  # gone | any

    steps = []
    for attempt in range(1, max_retries + 1):
        # 1) 定位
        try:
            hits, used = smart_find(query, device, exact=exact, method=method)
        except Exception as e:
            steps.append("第 %d 次：定位异常 %r" % (attempt, e))
            time.sleep(0.6)
            continue
        if not hits:
            steps.append("第 %d 次：未找到 '%s'（界面可能未渲染，稍后重试）" % (attempt, query))
            time.sleep(0.8)
            continue
        if index < 1 or index > len(hits):
            steps.append("第 %d 次：index 超出范围（共 %d 个匹配），改点第 1 个" % (attempt, len(hits)))
        lbl, cx, cy, conf = hits[min(max(index, 1), len(hits)) - 1]
        # 2) 点击
        if DRYRUN:
            return ok("[DRYRUN] 将自动点击 '%s' @ (%d, %d)（%s 定位）。" % (lbl, cx, cy, used),
                      dryrun=True, label=lbl, cx=cx, cy=cy, method=used)
        tap_ok, _ = with_retry(
            lambda: run_adb(["shell", "input", "tap", str(cx), str(cy)],
                            device=device, mutating=True),
            retries=2, delay=0.4, what="点击(%d,%d)" % (cx, cy),
        )
        if not tap_ok:
            steps.append("第 %d 次：点击 '%s' @ (%d, %d) 执行失败" % (attempt, lbl, cx, cy))
            time.sleep(0.4)
            continue
        time.sleep(0.7)  # 等界面响应
        # 3) 验证
        try:
            after, _ = smart_find(query, device, exact=exact, method=method)
        except Exception:
            after = []
        if verify == "any" or not after:
            conf_s = "%.2f" % conf if isinstance(conf, float) else "1.00"
            mode = ("已验证：点击后目标已离开屏幕（页面已切换/操作生效）"
                    if not after else "已点击（verify=any，不检查去留）")
            return ok(
                "✅ 已点击 '%s' @ (%d, %d)（%s 定位，置信度 %s）。%s。（共尝试 %d 次）"
                % (lbl, cx, cy, used, conf_s, mode, attempt),
                label=lbl, cx=cx, cy=cy, method=used, conf=conf_s, attempts=attempt, verified=True,
            )
        steps.append("第 %d 次：点击 '%s' @ (%d, %d) 已执行，但目标仍在屏幕"
                     "（可能是开关类/页面未切换）。" % (attempt, lbl, cx, cy))
        time.sleep(0.5)
    return fail(
        "⚠️ 已尝试 %d 次，仍未确认「点击 %s」成功：\n%s"
        % (max_retries, query, "\n".join(steps)),
        steps=steps, query=query, attempts=max_retries,
    )


def _screen_size(device):
    """取屏幕像素尺寸 (w, h)；失败回退 1080x2340。"""
    try:
        r = run_adb(["shell", "wm", "size"], device=device, mutating=False, what="wm size")
        m = re.search(r"(\d+)x(\d+)", r.stdout or "")
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1080, 2340


def t_swipe_until_find(args):
    """自动滑动屏幕直到找到目标文字：每滑一次就重新定位，找到即停（可顺带点击）。

    适用于'滚动长列表找某一条'场景。direction=up 表示向上滑(内容下滚，找下方项)；
    down 相反；left/right 为横向滑动。swipeStep 为单次滑动占屏比例(默认 0.6)。
    最多滑动 maxSwipes 次；找到返回坐标(method=ui/ocr)，可设 tapOnFind=true 顺手点击。
    """
    query = _req(args, "query")
    device = resolve_device(args.get("deviceSerial"))
    direction = str(args.get("direction", "up")).lower()
    max_swipes = max(1, int(args.get("maxSwipes", 8)))
    exact = bool(args.get("exact", False))
    tap_on_find = bool(args.get("tapOnFind", False))
    method = str(args.get("method", "auto")).lower()
    swipe_step = max(0.1, min(0.9, float(args.get("swipeStep", 0.6))))
    w, h = _screen_size(device)
    cx0, cy0 = w // 2, h // 2
    if direction == "up":
        y1, y2 = int(h * (0.5 + swipe_step / 2)), int(h * (0.5 - swipe_step / 2))
        horiz = False
    elif direction == "down":
        y1, y2 = int(h * (0.5 - swipe_step / 2)), int(h * (0.5 + swipe_step / 2))
        horiz = False
    elif direction == "left":
        x1, x2 = int(w * (0.5 + swipe_step / 2)), int(w * (0.5 - swipe_step / 2))
        horiz = True
    else:  # right
        x1, x2 = int(w * (0.5 - swipe_step / 2)), int(w * (0.5 + swipe_step / 2))
        horiz = True
    steps_log = []
    for i in range(1, max_swipes + 1):
        try:
            hits, used = smart_find(query, device, exact=exact, method=method)
        except RuntimeError as e:
            steps_log.append("第 %d 次定位异常: %s" % (i, e))
            time.sleep(0.5)
            continue
        if hits:
            lbl, hx, hy, conf = hits[0]
            mode_label = {"ui": "无障碍/UI", "ocr": "OCR"}.get(used, used)
            if tap_on_find and not DRYRUN:
                run_adb(["shell", "input", "tap", str(hx), str(hy)], device=device, mutating=True)
                return ok("滑动 %d 次后找到 '%s' 并已点击 @ (%d, %d)（%s）。"
                          % (i, lbl, hx, hy, mode_label),
                          found=True, label=lbl, cx=hx, cy=hy, method=used,
                          conf=round(float(conf), 2), swipes=i, tapped=True)
            return ok("滑动 %d 次后找到 '%s' @ (%d, %d)（%s，置信度 %.2f）。"
                      % (i, lbl, hx, hy, mode_label, conf),
                      found=True, label=lbl, cx=hx, cy=hy, method=used,
                      conf=round(float(conf), 2), swipes=i, tapped=False)
        if horiz:
            run_adb(["shell", "input", "swipe", str(x1), str(cy0), str(x2), str(cy0), "300"],
                    device=device, mutating=True)
        else:
            run_adb(["shell", "input", "swipe", str(cx0), str(y1), str(cx0), str(y2), "300"],
                    device=device, mutating=True)
        steps_log.append("第 %d 次滑动(%s)后仍未找到 '%s'" % (i, direction, query))
        time.sleep(0.5)
    return fail("滑动 %d 次仍未找到 '%s'。" % (max_swipes, query),
                found=False, query=query, swipes=max_swipes, steps=steps_log)


def t_wechat_open_chat(args):
    """【全链路示例】进入微信某联系人的聊天界面：
       启动微信 → 切到通讯录 → (自动校验)在联系人列表滑动找到并点击该联系人 → 校验进入聊天。
    演示'操作后自动校验 + 失败自动重试'的通用闭环思想（基于 with_verification）。
    微信版本/界面差异可能需微调；手机需已登录微信且联系人存在。"""
    contact = _req(args, "contact")
    device = resolve_device(args.get("deviceSerial"))
    pkg = "com.tencent.mm"
    steps = []
    if DRYRUN:
        return ok("[DRYRUN] 将打开微信联系人 '%s' 的聊天。" % contact, dryrun=True, contact=contact)
    # 1) 启动微信
    r = t_launch_app({"package": pkg, "deviceSerial": device})
    steps.append("启动微信: %s" % (r.get("message") if isinstance(r, dict) else r))
    time.sleep(1.5)
    # 2) 切到通讯录（with_verification：点完校验通讯录标签仍可见）
    def _tap_contacts():
        try:
            hits, _ = smart_find("通讯录", device, method="ui")
        except Exception:
            return False
        if not hits:
            return False
        cx, cy = hits[0][2], hits[0][3]
        run_adb(["shell", "input", "tap", str(cx), str(cy)], device=device, mutating=True)
        return True

    def _contacts_visible():
        try:
            h, _ = smart_find("通讯录", device, method="ui")
            return bool(h)
        except Exception:
            return False

    ok_c, _ = with_verification(_tap_contacts, lambda _r: _contacts_visible(),
                                max_retries=2, delay=0.8)
    steps.append("切换到通讯录: %s" % ("成功" if ok_c else "未确认(可能已在通讯录)"))
    time.sleep(0.8)
    # 3) (自动校验+失败重试) 在联系人列表滑动找到并点击联系人，并校验进入聊天
    def _open():
        return t_swipe_until_find({"query": contact, "tapOnFind": True,
                                   "maxSwipes": int(args.get("maxSwipes", 12)),
                                   "deviceSerial": device})

    def _chat_ok(_r):
        try:
            cur = t_get_current_app({"deviceSerial": device})
            if isinstance(cur, dict) and cur["data"].get("package") != pkg:
                return False
            h, _ = smart_find(contact, device, method="ui")
            return bool(h)
        except Exception:
            return False

    ok_chat, res = with_verification(_open, _chat_ok, max_retries=2, delay=1.0)
    if isinstance(res, dict):
        steps.append("查找并点击联系人: %s" % res.get("message", ""))
    if ok_chat:
        return ok("已打开与 '%s' 的聊天（已自动校验进入聊天界面）。" % contact,
                  contact=contact, in_chat=True, steps=steps)
    if isinstance(res, dict) and res.get("data", {}).get("found"):
        return ok("已点击联系人 '%s' 并尝试进入聊天，但未能自动确认进入聊天界面（可能微信版本差异/动画）。"
                  % contact, contact=contact, in_chat=False, verified=False, steps=steps)
    return fail("未能在联系人列表中找到并点击 '%s'。" % contact,
                contact=contact, in_chat=False, steps=steps)


# ---------------------------------------------------------------------------
# 微信发消息完整闭环（基于 OCR，每步校验 + 失败重试）
# ---------------------------------------------------------------------------

def _wechat_ensure_home(device):
    """启动微信并确保处于「微信」Tab 主页(聊天列表)。带前置状态判断，避免无谓耗时：
       - 已在微信主页 → 直接返回(0 额外操作)；
       - 已在微信但停在聊天/其它 Tab → 仅用返回键回到主页；
       - 否则(后台/其它 App) → 冷启动后回主页。
    微信常驻在通讯录/发现/我等其它 Tab 或某聊天内，必须显式回到微信主页，搜索入口才在动作栏上。"""
    w, h = _screen_size(device)
    home_region = [0, 0.0, 1, 0.12]
    # 前置判断 ①：已在微信主页（顶部动作栏有「微信」标题），直接跳过
    if _wechat_foreground(device) and _ocr_sees(device, "微信", region=home_region):
        return True
    # 前置判断 ②：已在微信前台但不在主页 → 仅返回键退出子页面
    if _wechat_foreground(device):
        for _ in range(3):
            if _ocr_sees(device, "微信", region=home_region):
                return True
            run_adb(["shell", "input", "keyevent", "4"], device=device, mutating=True)
            time.sleep(0.4)
    # 否则冷启动微信
    t_launch_app({"package": "com.tencent.mm", "deviceSerial": device})
    time.sleep(1.2)
    for _ in range(3):
        if _ocr_sees(device, "微信", region=home_region):
            return True
        run_adb(["shell", "input", "keyevent", "4"], device=device, mutating=True)
        time.sleep(0.4)
    # 兜底：显式点「微信」Tab(左下角)确保停在微信主页
    run_adb(["shell", "input", "tap", "150", str(int(h * 0.965))], device=device, mutating=True)
    time.sleep(0.6)
    if not _ocr_sees(device, "微信", region=home_region):
        run_adb(["shell", "input", "keyevent", "4"], device=device, mutating=True)
        time.sleep(0.4)
        _tap(150, int(h * 0.965), device)
        time.sleep(0.6)
    return _ocr_sees(device, "微信", region=home_region)


def _chat_header_is(device, contact):
    """双条件判定『真进入聊天窗口』，杜绝搜索页/资料页假阳性：
       ① 顶部标题区显示该联系人名称；
       ② 底部出现聊天输入框(发送消息/按住 说话/发送按钮)。
       两者同时满足才认定已进入聊天。"""
    top_ok = _ocr_sees(device, contact, region=[0, 0.0, 1, 0.12])
    if not top_ok:
        return False
    bottom_ok = (_ocr_sees(device, "发送消息", region=[0, 0.85, 1, 1.0]) or
                 _ocr_sees(device, "按住 说话", region=[0, 0.85, 1, 1.0]) or
                 _ocr_sees(device, "发送", region=[0, 0.85, 1, 1.0]))
    return bottom_ok


def _search_opened(device):
    """微信搜索页是否已打开：顶部出现「搜索」占位符，或动作栏「微信」标题已消失(进入搜索子页)。"""
    if _ocr_sees(device, "搜索", region=[0, 0.0, 1, 0.22]):
        return True
    # 顶部动作栏不再含「微信」标题，也视为已离开主页进入搜索页
    if not _ocr_sees(device, "微信", region=[0, 0.0, 1, 0.12]):
        return True
    return False


def _msg_sent(device, message):
    """发送结果双重校验（防假成功）：① 底部输入框已清空(不再含该消息) ② 聊天区出现该消息气泡。
    对含 emoji/特殊符号等 OCR 无法识别的内容，退化为『输入框已清空』判定（发送键已点击）。
    _msg_sent 只在已进入聊天后调用，无需再判搜索页。"""
    in_input = _ocr_sees(device, message, region=[0, 0.9, 1, 1.0])
    if in_input:
        return False  # 还在输入框，没发出去
    # 输入框已清空：再确认聊天区出现气泡（普通文本用子串；含 emoji 退化为输入框清空判定）
    plain = re.sub(
        r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF"
        r"\u3000-\u303F\uff00-\uffef]", "", message
    ).strip()
    if not plain:
        return True  # 纯 emoji/符号，OCR 无法确认气泡，输入框已清空即视为发送成功
    boxes = ocr_boxes(device, region=[0, 0.12, 1, 0.85], min_conf=0.25)
    head = plain[:6]  # 取前 6 字符做宽松子串匹配，兼容 OCR 长句切分误差
    return any(head in b[0] for b in boxes)


def _clipboard_set(device, text):
    """写入剪贴板。本机 134d2f8(Android 10+) 实测：service call clipboard 被系统剪贴板
    沙箱拦截、写入无效；cmd clipboard set 实测可靠。故以 cmd 为主、service call 作兼容性兜底。
    返回 (method, output, ok)。"""
    # 1) cmd clipboard set（主，已验证可用）
    try:
        r = run_adb(["shell", "cmd", "clipboard", "set", text], device=device,
                    mutating=True, capture=True)
        out = (r.stdout or "")
        if "Unknown command" not in out and "No shell command" not in out:
            return "cmd clipboard set", out, True
    except Exception as e:  # noqa: BLE001
        _ocr_debug("clipboard cmd 失败: %r" % e)
    # 2) service call 兜底（部分老机型/ROM 可用；本机被沙箱拦截，仅作兼容保留）
    try:
        hx = text.encode("utf-16-le").hex() + "0000"
        r = run_adb(["shell", "service", "call", "clipboard", "2", "s16", hx],
                    device=device, mutating=True, capture=True)
        return "service call", (r.stdout or ""), True
    except Exception as e:  # noqa: BLE001
        _ocr_debug("clipboard service call 失败: %r" % e)
    return "none", "", False


def _clipboard_get(device):
    """读取剪贴板内容（Android 10+ 可能因隐私限制返回空/被拒）。成功返回字符串，否则 None。"""
    for cmd in ("am get-clipboard", "cmd clipboard get"):
        try:
            r = run_adb(["shell", cmd], device=device, mutating=False, capture=True)
            val = (r.stdout or "").strip()
            if val and "Unknown command" not in val and "No shell command" not in val:
                return val
        except Exception:  # noqa: BLE001
            continue
    return None


# ---- ADBKeyBoard 输入法（行业标准中文输入方案）----
def _ime_current(device):
    """读取当前默认输入法（如 com.tencent.wetype）。"""
    r = run_adb(["shell", "settings", "get", "secure", "default_input_method"],
               device=device, capture=True)
    return (r.stdout or "").strip()


def _ime_enabled_list(device):
    r = run_adb(["shell", "settings", "get", "secure", "enabled_input_methods"],
               device=device, capture=True)
    return (r.stdout or "").strip()


def _adbkeyboard_installed(device):
    """ADBKeyBoard IME 是否已安装（出现在 ime list 中）。"""
    try:
        out = run_adb(["shell", "ime", "list", "-s"], device=device, capture=True).stdout or ""
    except Exception:
        return False
    return ADB_KEYBOARD_IME in out


def _adbkeyboard_enable(device):
    """启用 ADBKeyBoard IME：先 ime enable，再写入 enabled_input_methods 白名单。"""
    run_adb(["shell", "ime", "enable", ADB_KEYBOARD_IME], device=device,
            mutating=True, capture=True)
    cur = _ime_enabled_list(device)
    if ADB_KEYBOARD_IME not in cur:
        new = (cur.strip().rstrip(":") + ":" + ADB_KEYBOARD_IME).strip(":")
        if new:
            run_adb(["shell", "settings", "put", "secure", "enabled_input_methods", new],
                    device=device, mutating=True, capture=True)


def _adbkeyboard_install(device):
    """确保 ADBKeyBoard 已安装并启用；未安装则尝试从本地 ADBKeyboard.apk 安装。
    成功返回 True；APK 缺失或安装失败返回 False（调用方自动降级剪贴板）。"""
    if _adbkeyboard_installed(device):
        _adbkeyboard_enable(device)
        return True
    if not os.path.exists(ADB_KEYBOARD_APK):
        _ocr_debug("ADBKeyboard.apk 不存在: %s（将降级剪贴板，可手动放入该 APK 启用 ADBKeyBoard）"
                   % ADB_KEYBOARD_APK)
        return False
    try:
        run_adb(["install", "-r", "-g", ADB_KEYBOARD_APK], device=device,
                mutating=True, capture=True, timeout=180, what="adb install ADBKeyBoard")
    except Exception as e:
        _ocr_debug("ADBKeyBoard 安装失败: %r" % e)
        return False
    _adbkeyboard_enable(device)
    return _adbkeyboard_installed(device)


def _adbkeyboard_input(device, text):
    """通过 ADBKeyBoard 广播注入文本（base64 编码，支持中文/emoji/特殊符号/多行换行）。

    加固项（对齐需求步骤2）：
      - 文本经 base64 编码（天然 shell 安全），仍用 shlex.quote 包裹做防御性转义；
      - 广播加 --user 0，兼容高版本 Android 多用户；
      - 解析广播返回码（result=0 表示接收器已处理），非 0 抛出结构化异常，
        由上层捕获并降级剪贴板，避免“广播被静默丢弃却报成功”。
    返回广播的返回码(int)。"""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # shlex.quote 防御：base64 仅含 [A-Za-z0-9+/=]，正常无需转义；此处仍包裹以满足
    # “文本统一转义”要求，并兼容未来若改用 ADB_INPUT_TEXT(明文) 的场景。
    b64_arg = shlex.quote(b64)
    out = run_adb(
        ["shell", "am", "broadcast", "--user", "0", "-a", "ADB_INPUT_B64",
         "--es", "msg", b64_arg],
        device=device, mutating=True, capture=True).stdout or ""
    # 解析广播返回码：Broadcast completed: result=0
    m = re.search(r"result=(-?\d+)", out)
    code = int(m.group(1)) if m else None
    if code is None or code != 0:
        raise RuntimeError(
            "ADBKeyBoard 广播未成功(返回码=%s)。可能输入法未真正激活或广播被丢弃。out=%s"
            % (code, out[:200]))
    return code


def _ime_set(device, ime):
    run_adb(["shell", "ime", "set", ime], device=device, mutating=True, capture=True)


def _input_focus(device, field):
    """激活输入框焦点：微信用固定坐标点击(自研控件无标准 EditText)；
    非微信尝试 UiAutomator 聚焦首个 EditText，失败回退坐标点击。"""
    if _wechat_foreground(device):
        wechat_tap_input_box(device, field)
        return
    try:
        d = _u2_device(device)
        els = d(className="android.widget.EditText")
        if els.count > 0:
            els[0].click()
            return
    except Exception as e:
        _ocr_debug("非微信 UiAutomator 聚焦失败，回退坐标点击: %r" % e)
    w, h = _screen_size(device)
    _tap(int(w * 0.5), int(h * 0.85), device)


def _input_via_adbkeyboard(device, text, field):
    """ADBKeyBoard 主路径（全链路校验，对齐需求步骤2/3）：
      1) 记录原输入法 -> 启用并 ime set 切到 ADBKeyBoard
      2) 切换结果校验：读 default_input_method，未切成 adbkeyboard 则抛异常降级
      3) 点击输入框区域激活焦点，等待 0.3s 让 IME 与输入框绑定（先聚焦再广播，避免丢字）
      4) 广播注入文本 + 广播返回码校验（result=0 才认为注入被接收）
      5) 微信场景 OCR 校验内容写入；非微信返回 None(无 OCR 依据)
      6) finally 无感切回原输入法（失败仅告警，不阻断上层）
    任意环节失败抛出 RuntimeError(含诊断信息)，由 t_input_text 捕获并降级剪贴板。
    返回 (verified, info)：verified=微信场景 OCR 结果(True/False/None)；info=链路诊断 dict。"""
    original = _ime_current(device)
    info = {"original_ime": original, "after_switch": None,
            "switched": False, "broadcast_code": None, "verified": None}
    try:
        _adbkeyboard_enable(device)
        # 关键顺序：先聚焦输入框（用当前输入法即可），焦点绑定到 EditText 视图，
        # 不受后续切换输入法影响。若先切输入法再聚焦，ADBKeyBoard 为无键盘隐形输入法，
        # 微信布局变化会导致“点坐标聚焦”落空、广播时丢焦点（表现为消息写不进框）。
        _input_focus(device, field)
        time.sleep(0.2)
        _ime_set(device, ADB_KEYBOARD_IME)
        # 切换结果校验
        cur = _ime_current(device)
        info["after_switch"] = cur
        if cur != ADB_KEYBOARD_IME:
            raise RuntimeError("切换输入法到 ADBKeyBoard 失败(当前仍为 %s，可能未 enabled)。" % cur)
        info["switched"] = True
        time.sleep(0.3)  # 等 IME 与输入框绑定（先聚焦再广播，避免 IME 未绑定时丢字）
        # 广播注入 + 返回码校验
        info["broadcast_code"] = _adbkeyboard_input(device, text)
        time.sleep(0.4)
        # 微信场景 OCR 校验内容确实写入
        if _wechat_foreground(device):
            verified = _input_region_has(device, field, text)
            info["verified"] = verified
            # 焦点丢失兜底：首轮 OCR 未确认则重新聚焦 + 广播一次
            if verified is not True:
                _ocr_debug("ADBKeyBoard 首轮 OCR 未确认，重新聚焦+广播重试")
                _input_focus(device, field)
                time.sleep(0.3)
                _adbkeyboard_input(device, text)
                time.sleep(0.4)
                verified = _input_region_has(device, field, text)
                info["verified_retry"] = verified
            return verified, info
        return None, info
    finally:
        # 无感切换：无论如何切回用户原输入法，避免影响微信正常输入法
        if original and original != ADB_KEYBOARD_IME:
            try:
                _ime_set(device, original)
                if _ime_current(device) != original:
                    _ocr_debug("输入法切回 %s 后仍非该值(下次操作会再尝试)。" % original)
            except Exception as e:
                _ocr_debug("恢复输入法失败(下次操作会再尝试): %r" % e)


def _input_via_clipboard(device, text, field):
    """剪贴板兜底方案：写入(cmd/service call) + 粘贴(KEYCODE_PASTE=279) + OCR 校验 + 清空重试。"""
    method, _out, written = _clipboard_set(device, text)
    if not written:
        return fail("剪贴板写入失败：cmd / service call 两种方式均不可用。",
                    text=text, written=False)
    if _wechat_foreground(device):
        wechat_tap_input_box(device, field)
    else:
        _input_focus(device, field)
    run_adb(["shell", "input", "keyevent", "279"], device=device, mutating=True)
    time.sleep(0.5)
    verified = _input_region_has(device, field, text) if _wechat_foreground(device) else None
    if not verified and _wechat_foreground(device):
        wechat_clear_input(device, field)
        _clipboard_set(device, text)
        wechat_tap_input_box(device, field)
        run_adb(["shell", "input", "keyevent", "279"], device=device, mutating=True)
        time.sleep(0.5)
        verified = _input_region_has(device, field, text)
    return ok("已通过剪贴板写入并粘贴文本(输入方案=clipboard, OCR校验=%s): %s"
              % ("通过" if verified else "未通过(内容应已写入，OCR可能未识别)", text),
              text=text, method="clipboard", written=True, verified=verified, field=field)


# ---- 微信输入专用辅助（微信输入框为自研控件，无标准 EditText，必须用剪贴板 + 焦点点击）----
def wechat_tap_input_box(device, field="chat"):
    """精准点击微信输入框以激活焦点（内核级 sendevent，绕过 MIUI 模拟点击限制）：
    field='chat' 点底部固定区；field='search' 点顶部搜索栏。"""
    w, h = _screen_size(device)
    if field == "search":
        _tap(int(w * 0.5), int(h * 0.07), device)
    else:
        _tap(400, int(h * 0.96), device)
    time.sleep(0.3)
    return True


def wechat_clear_input(device, field="chat"):
    """清空微信输入框：激活→移到行尾→连续 DEL（适配自定义控件，无标准 EditText 可用）。"""
    wechat_tap_input_box(device, field)
    run_adb(["shell", "input", "keyevent", "123"], device=device, mutating=True)  # MOVE_END
    time.sleep(0.1)
    run_adb(["shell", "input", "keyevent"] + ["67"] * 50, device=device, mutating=True)  # 50×DEL
    time.sleep(0.1)
    return True


_PLACEHOLDER = ("搜索", "搜索本地或网络结果", "深度思考", "○深度思考",
                "发送消息", "按住 说话")


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    "\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\u200d\u20e3]+", flags=re.UNICODE)


def _norm_ocr(s):
    """OCR 校验归一化：去掉 emoji/空白/常见标点，仅留 CJK 与字母数字，便于子串比对。"""
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"[\s\W_]+", "", s)
    return s


def _input_region_has(device, field, text):
    """校验输入区域是否已出现目标文本（兼容中文 OCR 误差，但避免过松误判）：
    搜索框看 [0,0.06,1,0.16]（排除顶部状态栏）；聊天输入框看底部 [0,0.82,1,1.0]。
    过滤占位符文字；归一化(去 emoji/空白/标点)后要求期望文本有效前缀出现，
    才判定已输入；若期望文本全是 emoji/符号(归一化后为空)则 OCR 无法验证，返回 None。"""
    if field == "search":
        boxes = ocr_boxes(device, region=[0, 0.06, 1, 0.16], min_conf=0.2)
    else:
        boxes = ocr_boxes(device, region=[0, 0.82, 1, 1.0], min_conf=0.2)
    real = [b[0] for b in boxes
            if b[0].strip() and not any(p in b[0] for p in _PLACEHOLDER)]
    if not real:
        return False
    flat = " ".join(real)
    norm_flat = _norm_ocr(flat)
    norm_exp = _norm_ocr(text)
    if not norm_exp:
        return None  # 全 emoji/符号，OCR 无法验证，交由上层(发送后气泡校验)确认
    for L in (min(len(norm_exp), 6), min(len(norm_exp), 4), min(len(norm_exp), 2)):
        if L >= 1 and norm_exp[:L] in norm_flat:
            return True
    return False


def t_input_chinese(args):
    """【剪贴板·中文/任意文本输入】写剪贴板 + 触发粘贴(KEYCODE_PASTE=279)。
    解决 adb input text 不支持中文/特殊符号；适用于任意可粘贴焦点。
    优先 cmd clipboard set（本机可靠），service call 兜底；全程异常捕获返回结构化错误。"""
    text = _req(args, "text")
    device = resolve_device(args.get("deviceSerial"))
    try:
        method, _out, written = _clipboard_set(device, text)
    except Exception as e:  # noqa: BLE001
        return fail("剪贴板写入异常（%s）：%r" % (type(e).__name__, e),
                    text=text, written=False)
    if not written:
        return fail("剪贴板写入失败：cmd / service call 两种方式均不可用。",
                    text=text, written=False)
    try:
        run_adb(["shell", "input", "keyevent", "279"], device=device, mutating=True)
        pasted = True
    except Exception as e:  # noqa: BLE001
        return fail("剪贴板已写入(%s)但粘贴失败：%r" % (method, e),
                    text=text, written=True, method=method, pasted=False)
    return ok("已写入并粘贴文本(写入方案=%s): %s" % (method, text),
              text=text, method=method, written=True, verified=None, pasted=True)


_U2_CACHE = {}


def _u2_device(device):
    """惰性连接并缓存 uiautomator2 Device 对象（同序列号复用，避免反复重连开销）。"""
    if device not in _U2_CACHE:
        import uiautomator2 as u2
        _U2_CACHE[device] = u2.connect(device)
    return _U2_CACHE[device]


def t_input_text(args):
    """【统一中文/任意文本输入】自动选择最优输入方式（对齐 open-mobile-mcp / wechat-mcp-server 行业标准）：
      - 优先 ADBKeyBoard（ADB 输入法 + 广播注入）：支持中文/英文/emoji/特殊符号/多行换行，
        且输入法无感知切换（输入前记录用户原输入法→切到 ADBKeyBoard→广播提交→切回原输入法）；
        首次使用自动从本地 ADBKeyboard.apk 安装并启用。
      - ADBKeyBoard 不可用/异常 → 自动降级剪贴板(cmd/service call + 粘贴键)，
        保留 service call 剪贴板方案作为兜底，确保中文始终可输入。
    微信(com.tencent.mm)场景自动判定 search/chat 区域；非微信聚焦首个 EditText。
    返回结构化信封，data.method 标注实际输入方式('adbkeyboard'|'clipboard')，
    data.verified 标注 OCR 校验结果（微信场景）。"""
    text = _req(args, "text")
    device = resolve_device(args.get("deviceSerial"))
    field = (args.get("field") or "auto")
    if field == "auto":
        # 仅在真正处于搜索页(顶部出现「搜索」占位符)时按搜索框处理，避免把聊天页误判为搜索页
        field = "search" if _ocr_sees(device, "搜索", region=[0, 0.0, 1, 0.22]) else "chat"
    # 1) 优先 ADBKeyBoard（首次使用自动安装并启用）
    ak_err = None
    if _adbkeyboard_install(device):
        try:
            verified, info = _input_via_adbkeyboard(device, text, field)
            vlabel = ("通过" if verified is True
                      else ("未确认(内容应已写入)" if verified is None else "未通过"))
            return ok("已通过 ADBKeyBoard 输入法输入文本(输入方案=adbkeyboard, OCR校验=%s): %s"
                      % (vlabel, text),
                      text=text, method="adbkeyboard", verified=verified, field=field,
                      adbkeyboard_info=info)
        except Exception as e:
            ak_err = "%s: %s" % (type(e).__name__, e)
            _ocr_debug("ADBKeyBoard 输入失败，降级剪贴板: %s" % ak_err)
    else:
        ak_err = "ADBKeyBoard 不可用(未安装/未启用)"
    # 2) 兜底：剪贴板方案（附上 ADBKeyBoard 失败原因，便于排查）
    try:
        res = _input_via_clipboard(device, text, field)
        if ak_err and isinstance(res, dict):
            res.setdefault("data", {})
            res["data"]["adbkeyboard_error"] = ak_err
        return res
    except Exception as e:
        return fail("文本输入失败（ADBKeyBoard 与剪贴板均不可用）：%r" % e, text=text,
                    adbkeyboard_error=ak_err)


def t_setup_adbkeyboard(args):
    """安装并启用 ADBKeyBoard 输入法，返回输入法状态，供显式预置与排障。
    若本地 ADBKeyboard.apk 缺失，会提示放入路径（不影响 phone_input_text 的剪贴板兜底）。"""
    device = resolve_device(args.get("deviceSerial"))
    installed = _adbkeyboard_install(device)
    cur = _ime_current(device)
    enabled = _ime_enabled_list(device)
    return ok("ADBKeyBoard 安装/启用结果: %s" % ("成功" if installed else "失败(APK缺失或安装出错)"),
              installed=installed,
              current_ime=cur,
              adbkeyboard_enabled=(ADB_KEYBOARD_IME in enabled),
              apk_present=os.path.exists(ADB_KEYBOARD_APK),
              apk_path=ADB_KEYBOARD_APK)


def t_send_wechat_message(args):
    """【完整闭环】给微信联系人发消息：启动微信→回主页→打开搜索→输入联系人→
    精准点击最顶部联系人条目进入聊天→激活输入框→粘贴消息→点发送；每步 OCR 校验、失败重试 2 次。
    contact_name=联系人名称(备注/昵称)，message=消息内容。需手机已登录微信且该联系人存在。"""
    contact = _req(args, "contact_name", "str")
    message = _req(args, "message", "str")
    device = resolve_device(args.get("deviceSerial"))
    steps = []
    clock = [time.time()]

    def mark():
        clock.append(time.time())
        return "  ⏱%.1fs" % (clock[-1] - clock[-2])

    if DRYRUN:
        return ok("[DRYRUN] 将给 '%s' 发送: %s" % (contact, message), dryrun=True,
                  contact_name=contact, message=message)
    # 1) 启动微信并回到主页（带前置判断：已在主页则跳过，省 3~6s）
    was_home = _wechat_foreground(device) and _ocr_sees(device, "微信", region=[0, 0.0, 1, 0.12])
    _wechat_ensure_home(device)
    steps.append("① 启动微信并回到主页%s%s" % ("(已在主页，跳过启动/返回)" if was_home else "", mark()))
    w, h = _screen_size(device)
    # 2) 打开搜索框（前置判断：已处于搜索页则跳过；否则点动作栏搜索图标≈(w*0.83,h*0.07)）
    if _search_opened(device):
        steps.append("② 打开搜索框: 已处于搜索页，跳过点击%s" % mark())
    else:
        def open_search():
            # 真机实测：微信动作栏搜索图标位于 (int(w*0.83), int(h*0.07))≈(996,182)，
            # 旧坐标 (w*0.91, h*0.03)≈(1092,78) 落在状态栏、点击无效。
            _tap(int(w * 0.83), int(h * 0.07), device)
            time.sleep(0.4)

        ok_s = with_verification(open_search, lambda _: _search_opened(device),
                                 max_retries=3, delay=0.6)
        steps.append("② 打开搜索框: %s%s" % ("成功" if ok_s else "未自动确认(继续)", mark()))
    # 3) 输入联系人（先点搜索框聚焦，再控件级直写/降级剪贴板粘贴）
    inp = {}
    def type_contact():
        _tap(int(w * 0.5), int(h * 0.07), device)
        time.sleep(0.2)
        r = t_input_text({"text": contact, "deviceSerial": device, "field": "search"})
        inp["contact"] = (r.get("data") or {}).get("method")
        time.sleep(0.5)

    ok_c = with_verification(type_contact,
                             lambda _: _ocr_sees(device, contact, region=[0, 0.10, 1, 0.6]),
                             max_retries=3, delay=0.6)
    steps.append("③ 搜索框输入联系人「%s」(输入方式=%s): %s%s"
                 % (contact, inp.get("contact"), "成功" if ok_c else "未确认(继续)", mark()))
    # 4) 精准点击最顶部联系人条目进入聊天（失败自动重试2次；若停在资料页则点「发消息」）
    def click_contact():
        hits = ocr_match_contact(contact, device, region=[0, 0.12, 1, 0.6])
        if not hits:
            return False
        _, cx, cy, _ = hits[0]
        _tap(cx, cy, device)
        time.sleep(0.8)  # 页面跳转过渡，避免取到过渡画面就误判
        return True

    def verify_contact():
        # 双条件判定真进聊天：顶部标题=联系人 且 底部出现对话框(发消息/按住说话/发送)
        if _chat_header_is(device, contact):
            return True
        # 停在资料页：点「发消息」进聊天，再校验双条件
        if _ocr_tap(device, "发消息", region=[0, 0.2, 1, 0.9]):
            time.sleep(1.0)
            return _chat_header_is(device, contact)
        return False

    ok_cc = with_verification(click_contact, verify_contact, max_retries=3, delay=0.8)
    if not ok_cc:
        steps.append("④ 点击联系人失败%s" % mark())
        return fail("未能找到/点击联系人 '%s'（可能在搜索结果中未出现，或匹配到聊天记录）。" % contact,
                    contact_name=contact, content=message, steps=steps)
    steps.append("④ 已进入与「%s」的聊天(双条件校验通过)%s" % (contact, mark()))
    # 5) 激活输入框（重试2次）
    def focus_input():
        for q in ("发送消息", "按住 说话"):
            if _ocr_tap(device, q, region=[0, 0.85, 1, 1.0]):
                return True
        _tap(400, int(h * 0.96), device)
        return True

    ok_f = with_verification(focus_input,
                             lambda _: _ocr_sees(device, "发送", region=[0, 0.85, 1, 1.0]),
                             max_retries=3, delay=0.5)
    steps.append("⑤ 激活输入框: %s%s" % ("成功" if ok_f else "未确认(继续)", mark()))
    # 6) 输入消息（控件级直写/降级剪贴板粘贴，重试2次）
    def type_msg():
        r = t_input_text({"text": message, "deviceSerial": device, "field": "chat"})
        inp["msg"] = (r.get("data") or {}).get("method")
        time.sleep(0.4)

    ok_m = with_verification(type_msg,
                             lambda _: _ocr_sees(device, message, region=[0, 0.85, 1, 1.0]),
                             max_retries=3, delay=0.5)
    steps.append("⑥ 输入消息「%s」(输入方式=%s): %s%s"
                 % (message, inp.get("msg"), "成功" if ok_m else "未确认(继续)", mark()))
    # 7) 点击发送（重试2次；已发出则直接返回，避免重复发送）
    def click_send():
        if _msg_sent(device, message):
            return True
        return _ocr_tap(device, "发送", region=[0, 0.85, 1, 1.0])

    ok_send = with_verification(click_send, lambda _: _msg_sent(device, message),
                                max_retries=3, delay=0.8)
    if ok_send:
        steps.append("⑦ 已发送%s" % mark())
        return ok("已给「%s」发送消息：%s" % (contact, message),
                  contact_name=contact, content=message, sent=True,
                  total_seconds=round(time.time() - clock[0], 1), steps=steps)
    steps.append("⑦ 发送未确认%s" % mark())
    return fail("已点击发送但未确认消息「%s」已出现在聊天中（可能发送失败）。" % message,
                contact_name=contact, content=message, sent=False,
                total_seconds=round(time.time() - clock[0], 1), steps=steps)


# ---------------------------------------------------------------------------
# 系统级/底层工具实现（phone_shell 等需要 PHONE_MCP_ALLOW_SHELL=1）
# ---------------------------------------------------------------------------

def t_shell(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    cmd = args["command"]
    forbid_catastrophic(cmd)
    r = run_adb(["shell", cmd], device=device, mutating=True)
    out = (r.stdout or "") + (r.stderr or "")
    return [text_block(out or "(无输出)")]


def t_run_adb(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    raw = args["args"]
    if isinstance(raw, str):
        raw = raw.split()
    forbid_catastrophic(" ".join(raw))
    r = run_adb(raw, device=device, mutating=True)
    out = (r.stdout or "") + (r.stderr or "")
    return [text_block(out or "(无输出)")]


def t_list_packages(args):
    device = resolve_device(args.get("deviceSerial"))
    base = ["shell", "pm", "list", "packages"]
    filt = args.get("filter")
    if filt:
        base.append(filt)
    r = run_adb(base, device=device, mutating=False)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > 60000:
        out = out[:60000] + "\n... (已截断)"
    return [text_block(out or "(无输出)")]


def t_list_processes(args):
    device = resolve_device(args.get("deviceSerial"))
    r = run_adb(["shell", "ps", "-A"], device=device, mutating=False)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > 60000:
        out = out[:60000] + "\n... (已截断)"
    return [text_block(out or "(无输出)")]


def t_start_service(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    pkg, svc = args["package"], args["service"]
    run_adb(["shell", "am", "startservice", "-n", "%s/%s" % (pkg, svc)],
            device=device, mutating=True)
    return [text_block("已尝试启动服务 %s/%s。" % (pkg, svc))]


def t_force_stop(args):
    require_shell()
    pkg = _req(args, "package")
    device = resolve_device(args.get("deviceSerial"))
    run_adb(["shell", "am", "force-stop", pkg], device=device, mutating=True)
    return [text_block("已强制停止 %s。" % pkg)]


def t_get_current_app(args):
    """返回当前前台应用包名与 Activity（dumpsys window 解析 mCurrentFocus/mFocusedApp）。只读。"""
    device = resolve_device(args.get("deviceSerial"))
    try:
        r = run_adb(["shell", "dumpsys", "window"], device=device,
                    mutating=False, what="dumpsys window")
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return fail("获取当前应用失败: %s" % e)
    m = _FOCUS_RE.search(out) or _FOCUSED_APP_RE.search(out)
    if not m:
        return fail("未能解析当前前台应用（可能无前台界面或 dumpsys 无输出）。")
    pkg, act = m.group(1), m.group(2)
    return ok("当前前台应用：\n  包名(package): %s\n  Activity: %s" % (pkg, act),
              package=pkg, activity=act)


def t_kill_process(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    target = str(args["target"])
    if target.isdigit():
        run_adb(["shell", "kill", target], device=device, mutating=True)
    else:
        run_adb(["shell", "am", "force-stop", target], device=device, mutating=True)
    return [text_block("已结束进程: %s。" % target)]


def t_getprop(args):
    device = resolve_device(args.get("deviceSerial"))
    key = args.get("key")
    cmd = ["shell", "getprop"] + ([key] if key else [])
    r = run_adb(cmd, device=device, mutating=False)
    return [text_block((r.stdout or r.stderr or "(无输出)").strip())]


def t_setprop(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    forbid_catastrophic(args.get("key", ""))
    run_adb(["shell", "setprop", args["key"], args["value"]],
            device=device, mutating=True)
    return [text_block("已设置属性 %s=%s。" % (args["key"], args["value"]))]


def t_settings_get(args):
    device = resolve_device(args.get("deviceSerial"))
    r = run_adb(["shell", "settings", "get", args["namespace"], args["key"]],
                device=device, mutating=False)
    return [text_block((r.stdout or r.stderr or "(无输出)").strip())]


def t_settings_put(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    run_adb(["shell", "settings", "put", args["namespace"], args["key"], args["value"]],
            device=device, mutating=True)
    return [text_block("已设置 settings %s/%s=%s。" % (args["namespace"], args["key"], args["value"]))]


def t_file_read(args):
    device = resolve_device(args.get("deviceSerial"))
    path = args["path"]
    r = run_adb(["shell", "cat", path], device=device, mutating=False)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > 200000:
        out = out[:200000] + "\n... (已截断)"
    return [text_block("读取 %s:\n%s" % (path, out or "(空或无权读取)"))]


def t_file_write(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    path = args["path"]
    content = args["content"]
    os.makedirs(SHOT_DIR, exist_ok=True)
    tmp = os.path.join(SHOT_DIR, "_write_tmp.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    run_adb(["push", tmp, path], device=device, mutating=True)
    return [text_block("已通过 push 写入 %s。" % path)]


def t_install_apk(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    local = args["localPath"]
    if not os.path.isfile(local):
        return [text_block("本地 APK 不存在: %s" % local)], True
    run_adb(["install", local], device=device, mutating=True)
    return [text_block("已安装 %s。" % local)]


def t_uninstall(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    forbid_catastrophic(args.get("package", ""))
    run_adb(["uninstall", args["package"]], device=device, mutating=True)
    return [text_block("已卸载 %s（应用数据一并清除）。" % args["package"])]


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

def text_block(text):
    return {"type": "text", "text": text}


def image_block(b64, mime):
    return {"type": "image", "data": b64, "mimeType": mime}


# ---------------------------------------------------------------------------
# 统一结果信封 / 日志 / 调度（阶段一：工程化加固）
# ---------------------------------------------------------------------------

def ok(message, **data):
    """构造统一成功信封 {success,message,data}。data 传结构化字段。"""
    return {"__envelope__": True, "success": True, "message": message, "data": data}


def fail(message, **data):
    """构造统一失败信封。"""
    return {"__envelope__": True, "success": False, "message": message, "data": data}


def _is_transient(e):
    """判断异常是否'瞬时/可重试'（adb 抖动、设备断开、超时等）。
    入参校验(ValueError)与权限(PermissionError)不属于可重试。"""
    if isinstance(e, (ValueError, PermissionError)):
        return False
    if isinstance(e, (subprocess.TimeoutExpired, OSError, BrokenPipeError)):
        return True
    msg = str(e).lower()
    return any(k in msg for k in (
        "device not found", "closed", "broken pipe", "timed out",
        "connection", "no such device", "transport",
    ))


def _normalize_result(res):
    """把 handler 返回值统一成 (envelope_dict, image_block_or_None)。
    兼容：① 新式信封 dict(ok/fail) ② 旧式 (content_list, is_error) ③ 旧式 content_list。"""
    if isinstance(res, dict) and res.get("__envelope__"):
        env = {"success": res["success"], "message": res["message"],
               "data": res.get("data") or {}}
        image = None
        if env["data"].get("image_b64"):
            image = image_block(env["data"]["image_b64"],
                                env["data"].get("image_mime", "image/png"))
            env["data"].pop("image_b64", None)
            env["data"].pop("image_mime", None)
        return env, image
    if isinstance(res, tuple):
        content, is_error = res
    else:
        content, is_error = res, False
    texts = []
    image = None
    for blk in content:
        t = blk.get("type")
        if t == "text":
            texts.append(blk.get("text", ""))
        elif t == "image":
            image = blk
    message = "\n".join(x for x in texts if x).strip()
    return {"success": not is_error, "message": message, "data": {}}, image


def log_tool(name, args, success, message, dt_ms, attempts=1):
    """统一工具日志：入参(脱敏预览) + 结果 + 耗时。"""
    try:
        preview = {k: (v if not isinstance(v, str) or len(v) < 80 else v[:80] + "…")
                   for k, v in (args or {}).items()}
        arg_s = json.dumps(preview, ensure_ascii=False)
    except Exception:
        arg_s = str(args)
    tag = "OK " if success else "ERR"
    log("[%s] %s 入参=%s 耗时=%.1fms 重试=%d 结果=%s"
        % (tag, name, arg_s, dt_ms, attempts, (message or "")[:200]))


def _env_content(env, image):
    content = [text_block(json.dumps(env, ensure_ascii=False))]
    if image is not None:
        content.insert(0, image)
    return content


def dispatch_tool(name, arguments, req_id):
    """统一调度：异常捕获(绝不崩溃) + 瞬时异常重试 + 统一信封 + 日志。"""
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if not tool:
        env = fail("未知工具: %s" % name)
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"isError": True, "content": _env_content(env, None)}}
    attempts = 0
    while attempts < _TOOL_RETRIES + 1:
        attempts += 1
        t0 = time.time()
        try:
            res = tool["handler"](arguments)
            env, image = _normalize_result(res)
            dt = (time.time() - t0) * 1000
            log_tool(name, arguments, env["success"], env["message"], dt, attempts)
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": _env_content(env, image),
                               "isError": (not env["success"])}}
        except (ValueError, PermissionError) as e:
            dt = (time.time() - t0) * 1000
            msg = str(e)
            if isinstance(e, ValueError):
                msg = "参数错误: " + msg
            env = fail(msg)
            log_tool(name, arguments, False, env["message"], dt, attempts)
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": _env_content(env, None), "isError": True}}
        except Exception as e:
            dt = (time.time() - t0) * 1000
            if not _is_transient(e) or attempts >= _TOOL_RETRIES + 1:
                env = fail("执行失败: %s" % e)
                log_tool(name, arguments, False, env["message"], dt, attempts)
                return {"jsonrpc": "2.0", "id": req_id,
                        "result": {"content": _env_content(env, None), "isError": True}}
            log("[RETRY] %s 第 %d/%d 次瞬时异常: %r"
                % (name, attempts, _TOOL_RETRIES, e))
            time.sleep(0.4)
    env = fail("执行失败: 未知错误")
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": _env_content(env, None), "isError": True}}


# ===========================================================================
# 工具注册表（三层架构 · 对齐 open-mobile-mcp 工程规范）
#   原子工具层  : phone_get_devices / screenshot / tap / swipe / keyevent /
#                launch_app / ui_dump / find_element / tap_element / find_text /
#                tap_text / shell / run_adb ... —— 单一原子操作，无业务组合。
#   封装组合层  : phone_auto_click / swipe_until_find / wechat_open_chat /
#                input_text / input_chinese / input_method_setup ...
#                —— 多个原子操作 + 校验/重试 组合成可靠能力。
#   场景闭环层  : phone_send_wechat_message ... —— 面向具体业务场景的完整闭环
#                （启动→定位→输入→校验→发送），自带每步 steps 与耗时统计。
# 统一返回格式 : {success, message, data, steps?}（由 ok/fail 构造，dispatch_tool 统一归一）。
# 通用装饰器   : dispatch_tool 已对所有工具统一提供「全局异常捕获 + 超时控制 +
#                瞬时异常自动重试」，无需每个工具重复编写（见上方 dispatch_tool）。
# ===========================================================================
# ===========================================================================
# minicap 等价流式截图子系统（root 直连底层截图，无弹窗/无权限拦截）
# ---------------------------------------------------------------------------
# 本机为 Android 16(SDK36)，已无 /dev/graphics/fb0 帧缓冲，且 minicap 版本相关的
# minicap.so 最高只到 Android 10，无法在本机运行。故以 root `screencap` 等价实现
# minicap 的「握手 banner(分辨率/旋转/刷新率) + socket 持续图像流」流程：
#   phone_cap_sync          -> 同步屏幕参数(banner)
#   phone_screenshot_stream -> 单帧 root 截图(等价 minicap 一帧)
#   phone_stream_start/stop -> 后台持续截帧写本地(等价 minicap 图像流)
#   phone_ocr_stream        -> 对流中最新帧跑 RapidOCR(文字识别/页面状态校验)
# 全程走 `adb exec-out su -c 'screencap -p'`（root），绕过应用层截图 API，
# 实测无 MIUI 截图弹窗、无权限拦截。


def _cap_sync(device):
    """同步设备屏幕参数（minicap banner 等价）：物理分辨率、旋转、刷新率。"""
    w = h = rotation = None
    fps = None
    try:
        so = run_adb(["shell", "wm", "size"], device=device, capture=True, timeout=10)
        txt = getattr(so, "stdout", "") or ""
        for line in txt.splitlines():
            if "Physical size" in line:
                m = re.search(r"(\d+)x(\d+)", line)
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
    except Exception as e:
        _ocr_debug("cap_sync wm size 失败: %r" % e)
    try:
        dsp = run_adb(["shell", "dumpsys", "display"], device=device, capture=True, timeout=15)
        txt = getattr(dsp, "stdout", "") or ""
        m = re.search(r"rotation\s+(\d)", txt)
        if m:
            rotation = int(m.group(1))
        m2 = re.search(r"renderFrameRate\s+([\d.]+)", txt)
        if m2:
            fps = float(m2.group(1))
    except Exception as e:
        _ocr_debug("cap_sync dumpsys 失败: %r" % e)
    return {"width": w, "height": h, "rotation": rotation or 0, "fps": fps}


def _cap_frame_root(device, dest_local, region=None, max_side=None):
    """root 直连截图（无弹窗/无权限拦截），写入 dest_local，返回 (path, w, h, off_x, off_y)。

    region: 可选 [x1,y1,x2,y2] 归一化裁剪；max_side: 长边像素上限，超则缩放(提速)。
    """
    import cv2
    ok = run_adb(["exec-out", "su -c 'screencap -p'"], device=device,
                 capture=True, binary=True, what="screencap(root)")
    png = ok.stdout
    if not png or len(png) < 100:
        raise RuntimeError("root 截图返回空（设备未就绪或 su 不可用）")
    d = os.path.dirname(dest_local)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(dest_local, "wb") as f:
        f.write(png)
    img = cv2.imread(dest_local)
    if img is None:
        raise RuntimeError("截图解码失败 path=%s" % dest_local)
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        cv2.imwrite(dest_local, img)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region:
        x1, y1, x2, y2 = [float(v) for v in region]
        cxa, cya = int(x1 * w), int(y1 * h)
        cxb, cyb = int(x2 * w), int(y2 * h)
        if cxb > cxa and cyb > cya:
            img = img[cya:cyb, cxa:cxb]
            off_x, off_y = cxa, cya
            cv2.imwrite(dest_local, img)
            h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        scale = max_side / max(h, w)
        small = cv2.resize(img, (int(w * scale), int(h * scale)))
        cv2.imwrite(dest_local, small)
        h, w = small.shape[:2]
    return dest_local, w, h, off_x, off_y


# ---- 持续截帧流（等价 minicap 的 socket 持续图像流）----
_CAP_STREAMS = {}
_CAP_STREAMS_LOCK = threading.Lock()


def _cap_stream_start(device, fps=4, max_frames=12):
    with _CAP_STREAMS_LOCK:
        cur = _CAP_STREAMS.get(device)
        if cur and not cur["stop"].is_set():
            cur["fps"] = fps
            return cur
        stop = threading.Event()
        entry = {"stop": stop, "latest": None,
                 "frames": collections.deque(maxlen=max_frames),
                 "lock": threading.Lock(), "seq": 0, "fps": fps}
        _CAP_STREAMS[device] = entry
    th = threading.Thread(target=_cap_stream_loop, args=(device, fps, stop, entry),
                          name="capstream-%s" % device, daemon=True)
    th.start()
    return entry


def _cap_stream_loop(device, fps, stop, entry):
    interval = max(0.05, 1.0 / max(1, fps))
    basedir = os.path.join(SHOT_DIR, "stream")
    try:
        os.makedirs(basedir, exist_ok=True)
    except Exception:
        pass
    while not stop.is_set():
        try:
            seq = entry["seq"]
            dest = os.path.join(basedir, "frame_%06d.png" % seq)
            _cap_frame_root(device, dest)
            with entry["lock"]:
                entry["seq"] = seq + 1
                entry["latest"] = dest
                entry["frames"].append(dest)
        except Exception as e:
            _ocr_debug("stream 帧捕获失败(设备=%s): %r" % (device, e))
            if stop.wait(interval):
                break
            continue
        if stop.wait(interval):
            break


def _cap_stream_stop(device):
    with _CAP_STREAMS_LOCK:
        entry = _CAP_STREAMS.pop(device, None)
    if entry:
        entry["stop"].set()
    return entry


def _cap_stream_latest(device):
    with _CAP_STREAMS_LOCK:
        entry = _CAP_STREAMS.get(device)
    if not entry:
        return None
    with entry["lock"]:
        return entry["latest"]


def _ocr_boxes_from_image(src_path, region=None, min_conf=0.25):
    """对一张已存在的截图跑 RapidOCR，返回 [(text, cx, cy, conf)]（原图像素坐标）。"""
    import cv2
    img = cv2.imread(src_path)
    if img is None:
        return []
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region:
        x1, y1, x2, y2 = [float(v) for v in region]
        cxa, cya = int(x1 * w), int(y1 * h)
        cxb, cyb = int(x2 * w), int(y2 * h)
        if cxb > cxa and cyb > cya:
            img = img[cya:cyb, cxa:cxb]
            off_x, off_y = cxa, cya
            h, w = img.shape[:2]
    max_side = 720 if FAST else 1080
    scale = 1.0
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    tmp = src_path + ".ocr.png"
    cv2.imwrite(tmp, img)
    try:
        result, _ = get_ocr_reader()(tmp)
    except Exception as e:
        _ocr_debug("ocr_boxes_from_image reader 异常: %r" % e)
        return []
    if not result:
        return []
    boxes = []
    for bbox, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2 / scale) + off_x
        cy = int((min(ys) + max(ys)) / 2 / scale) + off_y
        boxes.append((txt, cx, cy, conf))
    return boxes


def _box_to_dict(b):
    t, x, y, c = b
    return {"text": t, "x": x, "y": y, "conf": round(c, 3)}


def t_cap_sync(args):
    device = resolve_device(args.get("deviceSerial"))
    info = _cap_sync(device)
    return ok("已同步设备屏幕参数（minicap banner 等价）。",
              width=info["width"], height=info["height"],
              rotation=info["rotation"], fps=info["fps"])


def t_screenshot_stream(args):
    device = resolve_device(args.get("deviceSerial"))
    os.makedirs(SHOT_DIR, exist_ok=True)
    dest = os.path.join(SHOT_DIR, "screen_stream.png")
    try:
        _, w, h, _, _ = _cap_frame_root(device, dest)
    except Exception as e:
        return fail("root 截图失败: %s" % e)
    with open(dest, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return ok("已 root 截图（无弹窗/无权限拦截），保存至: %s" % dest,
              path=dest, width=w, height=h, bytes=os.path.getsize(dest),
              image_b64=b64)


def t_stream_start(args):
    device = resolve_device(args.get("deviceSerial"))
    fps = 4
    if "fps" in args and args["fps"] is not None:
        fps = _req(args, "fps", "int")
    fps = max(1, min(30, fps))
    _cap_stream_start(device, fps=fps)
    return ok("已启动持续截帧流（root, 无弹窗）。fps≈%d，帧写入 %s/stream/。"
              % (fps, SHOT_DIR), fps=fps, streamDir=os.path.join(SHOT_DIR, "stream"))


def t_stream_stop(args):
    device = resolve_device(args.get("deviceSerial"))
    entry = _cap_stream_stop(device)
    return ok("已停止截帧流。" if entry else "截帧流本就未运行。", stopped=bool(entry))


def t_ocr_stream(args):
    device = resolve_device(args.get("deviceSerial"))
    region = args.get("region")
    query = args.get("query")
    exact = bool(args.get("exact", False))
    min_conf = 0.3
    if "minConf" in args and args["minConf"] is not None:
        try:
            min_conf = float(args["minConf"])
        except (TypeError, ValueError):
            pass
    latest = _cap_stream_latest(device)
    src = latest if (latest and os.path.exists(latest)) else None
    if not src:
        os.makedirs(SHOT_DIR, exist_ok=True)
        dest = os.path.join(SHOT_DIR, "ocr_stream_snap.png")
        try:
            _cap_frame_root(device, dest)
        except Exception as e:
            return fail("截图失败: %s" % e)
        src = dest
    boxes = _ocr_boxes_from_image(src, region=region, min_conf=min_conf)
    if query:
        if exact:
            hits = [b for b in boxes if b[0].strip() == query]
        else:
            hits = [b for b in boxes if query in b[0]]
        return ok("OCR 命中 %d 处含 %r 的文字块（共识别 %d 块）。"
                  % (len(hits), query, len(boxes)),
                  query=query, hitCount=len(hits),
                  hits=[_box_to_dict(b) for b in hits],
                  boxes=[_box_to_dict(b) for b in boxes])
    return ok("OCR 完成，共识别 %d 个文字块。" % len(boxes),
              count=len(boxes), boxes=[_box_to_dict(b) for b in boxes])


# ---------------------------------------------------------------------------
# 内核态进程 / 微信数据库直读（原生 Linux 底层）
# ---------------------------------------------------------------------------

def _proc_list(device, flt=None, limit=3000):
    """枚举全部进程(PID/PPID/UID/RSS/CMD)。
    等价于遍历 /proc/[0-9]*/cmdline+status；ps 本身就是 /proc 读取器，
    走 root 以看到其它用户(u0_a*)的进程。"""
    proc = run_adb(["shell", "su", "-c", "ps -A -o PID,PPID,UID,RSS,CMD"],
                   device=device, capture=True, what="ps")
    out = proc.stdout or ""
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, uid, rss, cmd = parts
        if flt and flt not in cmd and flt not in pid:
            continue
        try:
            rows.append({"pid": int(pid), "ppid": int(ppid), "uid": uid,
                         "rssKb": int(rss), "cmd": cmd.strip()})
        except ValueError:
            continue
        if len(rows) >= limit:
            break
    return rows


def _foreground_activity(device):
    """取前台 Activity（package + activity）。
    dumpsys activity 解析 mResumedActivity，交叉校验 window mCurrentFocus。"""
    out = (run_adb(["shell", "dumpsys", "activity", "activities"],
                   device=device, capture=True, what="dumpsys").stdout or "")
    pkg = act = None
    m = re.search(r"mResumedActivity.*?(\S+)/(\S+)", out)
    if not m:
        m = re.search(r"ResumedActivity.*?(\S+)/(\S+)", out)
    if m:
        pkg, act = m.group(1), m.group(2)
    try:
        w = (run_adb(["shell", "dumpsys", "window", "windows"],
                     device=device, capture=True, what="dumpsys").stdout or "")
        fm = re.search(r"mCurrentFocus=.*?(\S+)/(\S+)", w)
        if fm and not pkg:
            pkg, act = fm.group(1), fm.group(2)
    except Exception:
        pass
    return {"package": pkg, "activity": act}


def _proc_read(device, pid):
    """直读 /proc/<pid>/cmdline 与 /proc/<pid>/status（内核态原始信息）。"""
    pid = int(pid)
    cmd = (run_adb(["shell", "su", "-c", "cat /proc/%d/cmdline" % pid],
                   device=device, capture=True, binary=True, what="proc-cmdline").stdout or b"").replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    status = (run_adb(["shell", "su", "-c", "cat /proc/%d/status" % pid],
                      device=device, capture=True, binary=True, what="proc-status").stdout or b"").decode("utf-8", "replace")
    info = {}
    for ln in status.splitlines():
        if ln.startswith(("Name:", "State:", "PPid:", "Uid:", "VmRSS:", "VmSize:")):
            k, _, v = ln.partition(":")
            info[k.strip()] = v.strip()
    return {"pid": pid, "cmdline": cmd, "status": info}


def _wechat_db_path(device):
    out = (run_adb(["shell", "su", "-c",
                    "ls /data/data/com.tencent.mm/MicroMsg/*/EnMicroMsg.db"],
                   device=device, capture=True, what="ls-db").stdout or "")
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.endswith("EnMicroMsg.db"):
            return ln
    return None


def _wechat_pulled_db_local():
    return os.path.join(SHOT_DIR, "wechat", "EnMicroMsg.db")


def t_ps(args):
    device = resolve_device(args.get("deviceSerial"))
    flt = args.get("filter")
    rows = _proc_list(device, flt=flt)
    fg = _foreground_activity(device)
    fpkg = fg.get("package")
    for r in rows:
        r["foreground"] = bool(fpkg) and r["cmd"].startswith(fpkg)
    n_wechat = sum(1 for r in rows if "com.tencent.mm" in r["cmd"])
    summary = "进程数: %d（含微信相关 %d）\n前台: %s/%s" % (
        len(rows), n_wechat, fg.get("package"), fg.get("activity"))
    return ok(summary, count=len(rows), foreground=fg,
              processes=rows[:200])


def t_proc_read(args):
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    return ok("已读取 /proc/%d 原始信息。" % pid, **_proc_read(device, pid))


def t_kill(args):
    device = resolve_device(args.get("deviceSerial"))
    pid = args.get("pid")
    package = args.get("package")
    if pid is None and not package:
        return fail("请提供 pid 或 package 之一。")
    targets = []
    if pid is not None:
        pid = int(pid)
        run_adb(["shell", "su", "-c", "kill -9 %d" % pid],
                device=device, mutating=True, what="kill")
        targets.append("pid=%d" % pid)
    if package:
        run_adb(["shell", "su", "-c", "pkill -9 -f %s" % shlex.quote(package)],
                device=device, mutating=True, what="pkill")
        targets.append("package=%s" % package)
    time.sleep(0.5)
    remain = []
    if pid is not None:
        try:
            remain = _proc_list(device, flt=str(pid))
        except Exception:
            pass
    gone = (pid is None) or (not any(r["pid"] == pid for r in remain))
    return ok("已发送 SIGKILL: %s。%s" % (
        ", ".join(targets),
        "进程已退出。" if gone else "进程仍在(可能已被同类保活机制重新拉起)。"),
        targets=targets, killed=gone)


def t_wechat_db_pull(args):
    device = resolve_device(args.get("deviceSerial"))
    src = _wechat_db_path(device)
    if not src:
        return fail("未找到 EnMicroMsg.db（微信未安装或未初始化？）")
    base = src[:-3]  # 去掉 .db
    dest_dir = os.path.join(SHOT_DIR, "wechat")
    os.makedirs(dest_dir, exist_ok=True)
    listing = (run_adb(["shell", "su", "-c", "ls -1 %s*" % shlex.quote(base)],
                       device=device, capture=True, what="ls-db").stdout or "").split()
    pulled = []
    for s in listing:
        s = s.strip()
        if not s:
            continue
        remote = "/data/local/tmp/wxdb_%s" % os.path.basename(s).replace(".", "_")
        local = os.path.join(dest_dir, os.path.basename(s))
        try:
            run_adb(["shell", "su", "-c", "cp %s %s" % (shlex.quote(s), remote)],
                    device=device, mutating=True, what="cp-db")
            run_adb(["pull", remote, local], device=device, what="pull-db")
            pulled.append(local)
        except Exception:
            pass
    return ok("已 root 拉取微信加密数据库(EnMicroMsg.db + 配套文件)到本地。\n"
              "注意：这是 SQLCipher 加密库，需用密钥解密才能读内容(见 phone_wechat_db_decrypt)。",
              dbPath=src, pulled=pulled,
              localDb=_wechat_pulled_db_local() if os.path.exists(_wechat_pulled_db_local()) else None)


def t_wechat_db_decrypt(args):
    """尝试解密微信 EnMicroMsg.db。
    注：微信 8.x 的密钥是随机 256-bit，存于微信进程内存，本机需 SQLCipher 库 +
    frida 取出的密钥才能真正解密。本函数做：①legacy 候选密钥(md5(imei+uin)[:7])计算；
    ②若本机装了 SQLCipher 且提供了 key，则直接解密读联系人/消息。"""
    device = resolve_device(args.get("deviceSerial"))
    key = args.get("key")
    use_legacy = bool(args.get("legacy", True))
    candidate = None
    if use_legacy and not key:
        try:
            import hashlib
            prefs = (run_adb(["shell", "su", "-c",
                              "cat /data/data/com.tencent.mm/shared_prefs/system_config_prefs.xml"],
                             device=device, capture=True, what="prefs").stdout or "")
            m = re.search(r'name="(?:_uin|default_uin)"\s+value="(-?\d+)"', prefs)
            uin = m.group(1) if m else None
            if uin:
                cand = hashlib.md5(("1234567890ABCDEF" + uin).encode()).hexdigest()[:7].lower()
                candidate = cand
        except Exception:
            candidate = None
    sqlcipher = None
    try:
        import pysqlcipher3.dbapi2 as sqlcipher
    except Exception:
        try:
            import sqlcipher3.dbapi2 as sqlcipher
        except Exception:
            sqlcipher = None
    if sqlcipher is None:
        msg = ("本机未安装 SQLCipher 库(pysqlcipher3/sqlcipher3)，无法在此解密。\n"
               "可行路径：\n"
               "1) 安装: pip install pysqlcipher3（需本机有 libsqlcipher 开发文件/编译链，Windows 常失败）\n"
               "2) 用 frida 挂钩微信 sqlite3_key 取出 8.0.x 的 256-bit 密钥，再作为 key 参数传入\n"
               "3) 把已拉取的 EnMicroMsg.db 用 sqlcipher CLI / DB Browser 离线解密")
        if candidate:
            msg += ("\n\nlegacy 候选密钥 md5(imei+uin)[:7] = %s\n"
                    "（仅对微信 <7.x 有效，对 8.0.74 大概率无效）" % candidate)
        return fail(msg)
    db_local = args.get("dbPath") or _wechat_pulled_db_local()
    if not os.path.exists(db_local):
        return fail("本地未找到数据库文件: %s（请先调用 phone_wechat_db_pull）" % db_local)
    if not key:
        return fail("已检测到 SQLCipher 库，但缺密钥(key 参数)。\n"
                    "8.0.x 需要 frida 取出的 256-bit 密钥；legacy 候选=%s(可能无效)。" % candidate)
    try:
        conn = sqlcipher.connect(db_local)
        conn.execute("PRAGMA key = \"%s\"" % key)
        n = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        contacts = []
        try:
            for row in conn.execute(
                    "SELECT username, nickname FROM rcontact "
                    "WHERE username LIKE '%@chatroom' OR username LIKE 'wxid%' LIMIT 50"):
                contacts.append({"username": row[0], "nickname": row[1]})
        except Exception:
            pass
        conn.close()
        return ok("解密成功！sqlite_master 表数=%d。" % n,
                  tableCount=n, contacts=contacts[:50], legacyCandidate=candidate)
    except Exception as e:
        return fail("解密失败(密钥错误或无法打开): %s" % e)


# ---------------------------------------------------------------------------
# /sys 硬件节点调控（背光、震动、CPU）
# ---------------------------------------------------------------------------

def _backlight_node(device):
    """返回 (brightness_path, max_brightness) 或 (None, 0)。"""
    # 先用已知路径快匹配（避免 su -c 多命令引号问题）
    known = [
        "/sys/class/backlight/panel0-backlight",
        "/sys/class/backlight/backlight",
        "/sys/class/backlight/wled",
        "/sys/class/backlight/lcd-backlight",
    ]
    for p in known:
        try:
            mx = run_adb(["shell", "su", "-c", "cat %s" % shlex.quote(p + "/max_brightness")],
                        device=device, capture=True, what="bl-max").stdout or ""
            if mx.strip() and mx.strip() != "0":
                return "%s/brightness" % p, int(mx.strip())
        except Exception:
            continue
    # 回退：动态扫描（作为单引号包裹的完整 shell 命令）
    try:
        out = run_adb(["shell",
                       "su -c 'for d in /sys/class/backlight/*/; do b=$d/brightness; m=$d/max_brightness; "
                       "if [ -e $b ]; then echo $b $(cat $m 2>/dev/null); break; fi; done'"],
                      device=device, capture=True, what="bl-find").stdout or ""
        parts = out.split()
        if len(parts) >= 2:
            return parts[0], int(parts[1])
    except Exception:
        pass
    return None, 0


def t_brightness(args):
    """Root 直写背光滑块 sysfs 节点：读写亮度，支持百分比/原始值。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "get")
    node, mx = _backlight_node(device)
    if not node:
        return fail("未找到背光滑块 sysfs 节点(/sys/class/backlight/*/brightness)。可能不是受支持的设备。")
    cur = (run_adb(["shell", "su", "-c", "cat %s" % shlex.quote(node)],
                   device=device, capture=True, what="bright-get").stdout or "").strip()
    try:
        cur_v = int(cur)
    except Exception:
        cur_v = None
    if action == "get":
        return ok("当前亮度: %s / max %d%s" % (
            cur_v, mx, (" (%d%%)" % round(cur_v * 100.0 / mx)) if (cur_v is not None and mx) else ""),
            brightness=cur_v, max=mx,
            percent=(round(cur_v * 100.0 / mx) if (cur_v is not None and mx) else None))
    # set
    raw = args.get("raw", False)
    level = args.get("level")
    if level is None:
        return fail("set 动作需要 level 参数(0-100 百分比，或 raw=True 时 0-max 原始值)。")
    level = int(level)
    if raw:
        target = level
    else:
        if not (0 <= level <= 100):
            return fail("level 百分比需在 0-100 之间，或用 raw=True 传原始值。")
        target = int(round(level * mx / 100.0))
    target = max(0, min(target, mx))
    run_adb(["shell", "su", "-c", "echo %d > %s" % (target, node)],
            device=device, mutating=True, what="bright-set")
    new_v = (run_adb(["shell", "su", "-c", "cat %s" % shlex.quote(node)],
                     device=device, capture=True, what="bright-get2").stdout or "").strip()
    try:
        new_v = int(new_v)
    except Exception:
        new_v = target
    return ok("亮度已设置: %s -> %s (max %d)" % (cur_v, new_v, mx),
              old=cur_v, new=new_v, max=mx,
              percent=(round(new_v * 100.0 / mx) if mx else None))


def t_vibrate(args):
    """触发手机震动：sysfs 节点 -> cmd vibrator -> AIDL HAL service call 三级回退。"""
    device = resolve_device(args.get("deviceSerial"))
    ms = int(args.get("durationMs", 200))
    ms = max(10, min(ms, 60000))
    # 1) 经典 sysfs 节点
    sysfs = "/sys/class/timed_output/vibrator/enable"
    try:
        test = (run_adb(["shell", "su", "-c", "test -w %s && echo Y" % sysfs],
                        device=device, capture=True, what="vib-sysfs").stdout or "").strip()
        if test == "Y":
            run_adb(["shell", "su", "-c", "echo %d > %s" % (ms, sysfs)],
                    device=device, mutating=True, what="vib-set")
            return ok("已通过 sysfs 节点触发震动(%dms)。" % ms, method="sysfs", durationMs=ms)
    except Exception:
        pass
    # 2) cmd vibrator
    try:
        r = run_adb(["shell", "cmd", "vibrator", "vibrate", str(ms)],
                    device=device, capture=True, what="vib-cmd")
        if r.returncode == 0:
            return ok("已通过 cmd vibrator 触发震动(%dms)。" % ms, method="cmd", durationMs=ms)
    except Exception:
        pass
    # 3) AIDL HAL (best effort)
    try:
        r = run_adb(["shell", "service", "call",
                     "android.hardware.vibrator.IVibrator/default", "1", "i32", str(ms)],
                    device=device, capture=True, what="vib-hal")
        combined = (r.stdout or "") + (r.stderr or "")
        if "does not exist" not in combined and "error" not in combined.lower():
            return ok("已通过 AIDL HAL 触发震动(%dms)。" % ms, method="hal", durationMs=ms)
    except Exception:
        pass
    return fail("本设备无法通过 shell/sysfs 触发震动。\n"
                "原因: Android 16 / HyperOS 已移除 /sys/class/timed_output/vibrator/enable，"
                "且无 `cmd vibrator` 命令；vibrator 仅暴露为 binder HAL 无 shell 入口。\n"
                "可行替代: 安装调用 VibratorManager 的小 App，或用通知(带 vibrate)实现提醒。",
                method=None)


def t_cpu(args):
    """Root 调控 CPU：查看状态、切换 governor、上线/下线核心、设最大频率。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "list")
    if action == "list":
        online = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/online"],
                         device=device, capture=True, what="cpu-online").stdout or "").strip()
        gov = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"],
                       device=device, capture=True, what="cpu-gov").stdout or "").strip()
        avail = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"],
                         device=device, capture=True, what="cpu-avail").stdout or "").strip()
        avail_f = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies"],
                           device=device, capture=True, what="cpu-freq").stdout or "").strip()
        maxf = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq"],
                        device=device, capture=True, what="cpu-maxf").stdout or "").strip()
        return ok("CPU 状态", online=online, governor=gov,
                  availableGovernors=avail.split(), availableFrequenciesKHz=avail_f.split(),
                  maxFreqKHz=maxf)
    if action == "set_governor":
        gov = args.get("governor")
        if not gov:
            return fail("set_governor 需要 governor 参数。")
        run_adb(["shell", "su", "-c",
                 "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo %s > $f; done"
                 % shlex.quote(gov)], device=device, mutating=True, what="cpu-gov-set")
        return ok("已设置所有 CPU 核心 governor=%s。" % gov, governor=gov)
    if action in ("online_core", "offline_core"):
        core = args.get("core")
        if core is None:
            return fail("%s 需要 core 参数(如 7 表示 cpu7)。" % action)
        core = int(core)
        val = 1 if action == "online_core" else 0
        run_adb(["shell", "su", "-c", "echo %d > /sys/devices/system/cpu/cpu%d/online" % (val, core)],
                device=device, mutating=True, what="cpu-online-set")
        now = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu%d/online" % core],
                       device=device, capture=True, what="cpu-online-get").stdout or "").strip()
        return ok("已%s cpu%d，当前 online=%s。" % (
            "上线" if val else "下线", core, now), core=core, online=now)
    if action == "set_max_freq":
        freq = args.get("freqKHz")
        if freq is None:
            return fail("set_max_freq 需要 freqKHz 参数(单位 kHz，需是 availableFrequencies 之一)。")
        run_adb(["shell", "su", "-c",
                 "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo %d > $f; done"
                 % int(freq)], device=device, mutating=True, what="cpu-freq-set")
        return ok("已设置所有核心 max_freq=%d kHz。" % int(freq), freqKHz=int(freq))
    return fail("未知 action: %s" % action)


# ---------------------------------------------------------------------------
# 音频底层操控（cmd audio，绕过系统设置 UI）
# ---------------------------------------------------------------------------

_AUDIO_STREAMS = {
    "voice_call": 0, "system": 1, "ring": 2, "music": 3, "media": 3,
    "alarm": 4, "notification": 5,
}


def _audio_stream_idx(stream):
    s = str(stream or "3")
    if s.isdigit():
        return int(s)
    return _AUDIO_STREAMS.get(s.lower(), 3)


def t_audio(args):
    """Root 操控音频：获取/设置媒体音量、静音/取消静音。走 cmd audio(AudioService)，绕过系统设置 UI。
    注：/dev/snd 原始 PCM 读写改变的是采样数据，不是音量；音量通过 cmd audio 控制才是正确的底层 CLI。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "get")
    stream = _audio_stream_idx(args.get("stream", "music"))
    if action == "get":
        cur = (run_adb(["shell", "cmd", "audio", "get-stream-volume", str(stream)],
                       device=device, capture=True, what="aud-get").stdout or "").strip()
        mx = (run_adb(["shell", "cmd", "audio", "get-max-volume", str(stream)],
                      device=device, capture=True, what="aud-max").stdout or "").strip()
        m1 = re.search(r"->\s*(\d+)", cur)
        m2 = re.search(r"->\s*(\d+)", mx)
        cv = int(m1.group(1)) if m1 else None
        mv = int(m2.group(1)) if m2 else None
        return ok("音量(流%d): %s / %s" % (stream, cv, mv), stream=stream,
                  volume=cv, max=mv,
                  percent=(round(cv * 100.0 / mv) if (cv is not None and mv) else None))
    if action == "set_volume":
        level = args.get("level")
        if level is None:
            return fail("set_volume 需要 level 参数(0-max 整数)。")
        run_adb(["shell", "cmd", "audio", "set-volume", str(stream), str(int(level))],
                device=device, mutating=True, what="aud-set")
        return ok("已设置流%d音量为 %d。" % (stream, int(level)), stream=stream, volume=int(level))
    if action == "mute":
        run_adb(["shell", "cmd", "audio", "adj-mute", str(stream)],
                device=device, mutating=True, what="aud-mute")
        return ok("已静音流%d。" % stream, stream=stream, muted=True)
    if action == "unmute":
        run_adb(["shell", "cmd", "audio", "adj-unmute", str(stream)],
                device=device, mutating=True, what="aud-unmute")
        return ok("已取消静音流%d。" % stream, stream=stream, muted=False)
    return fail("未知 action: %s" % action)


# ---------------------------------------------------------------------------
# iptables Root 防火墙
# ---------------------------------------------------------------------------

def _app_uid(device, package):
    """由包名解析 Android uid: 先查 /data/system/packages.list(root)，再回退 dumpsys。"""
    try:
        out = (run_adb(["shell", "su", "-c",
                        "grep ^%s /data/system/packages.list 2>/dev/null | awk '{print $2}'"
                        % shlex.quote(package)],
                       device=device, capture=True, what="pkg-uid").stdout or "").strip()
        if out and out.isdigit():
            return int(out)
    except Exception:
        pass
    out2 = (run_adb(["shell", "dumpsys", "package", package],
                    device=device, capture=True, what="pkg-dumpsys").stdout or "")
    m = re.search(r"userId=(\d+)", out2)
    if m:
        return int(m.group(1))
    return None


def t_net_firewall(args):
    """Root iptables 防火墙：按 App uid/包名拦截网络(OUTPUT DROP)、解封、查看规则、清空。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "list")
    if action == "list":
        out = (run_adb(["shell", "su", "-c",
                        "iptables -L OUTPUT -n -v 2>&1; echo ---V6---; ip6tables -L OUTPUT -n -v 2>&1"],
                       device=device, capture=True, what="fw-list").stdout or "")
        rules = [l.strip() for l in out.splitlines() if l.strip()]
        return ok("当前 OUTPUT 链规则(含 owner 拦截): %d 条" % len(rules),
                  rules=rules, raw=out)
    if action in ("block_app", "unblock_app"):
        package = args.get("package")
        uid = args.get("uid")
        if uid is None and package:
            uid = _app_uid(device, package)
        if uid is None:
            return fail("需要 uid 或有效的 package(以解析 uid)。")
        uid = int(uid)
        if action == "block_app":
            run_adb(["shell", "su", "-c",
                     "iptables -C OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null || "
                     "iptables -A OUTPUT -m owner --uid-owner %d -j DROP" % (uid, uid)],
                    device=device, mutating=True, what="fw-block")
            run_adb(["shell", "su", "-c",
                     "ip6tables -C OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null || "
                     "ip6tables -A OUTPUT -m owner --uid-owner %d -j DROP" % (uid, uid)],
                    device=device, mutating=True, what="fw-block6")
            return ok("已拦截 uid=%d 的所有网络(IPv4+IPv6 OUTPUT DROP)。" % uid,
                      uid=uid, package=package, blocked=True)
        else:
            run_adb(["shell", "su", "-c",
                     "iptables -D OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null; "
                     "ip6tables -D OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null" % (uid, uid)],
                    device=device, mutating=True, what="fw-unblock")
            return ok("已解除对 uid=%d 的网络拦截。" % uid, uid=uid, package=package, blocked=False)
    if action == "clear_all":
        run_adb(["shell", "su", "-c", "iptables -F OUTPUT; ip6tables -F OUTPUT"],
                device=device, mutating=True, what="fw-clear")
        return ok("已清空 OUTPUT 链全部规则（含非本工具添加的规则，请慎用）。", cleared=True)
    return fail("未知 action: %s" % action)



# ======================== frida-rust 集成 ========================
_FRIDA_BIN = "/data/local/tmp/frida-rust"

def _frida_run(device, subcmd, extra_args=None, timeout=60):
    """在设备上执行 frida-rust 子命令，返回 (stdout, stderr, returncode)。"""
    parts = [_FRIDA_BIN, subcmd] + [shlex.quote(a) for a in (extra_args or [])]
    cmd = ["shell", "su", "-c", " ".join(parts)]
    r = run_adb(cmd, device=device, capture=True, timeout=timeout, what="frida-" + subcmd)
    return r.stdout or "", r.stderr or "", r.returncode


def t_frida_inject(args):
    """使用 frida-rust 注入共享库到目标进程（ptrace + dlopen）。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    lib = args.get("libPath", "/data/local/tmp/libfrida_agent.so")
    out, err, rc = _frida_run(device, "inject", [str(pid), lib])
    if rc == 0:
        return ok("已注入 '%s' -> PID=%d" % (lib, pid), stdout=out.strip())
    return fail("注入失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_attach(args):
    """使用 frida-rust ptrace 附着到目标进程。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    name = _req(args, "processName")
    out, err, rc = _frida_run(device, "attach", [name])
    if rc == 0:
        return ok("已附着 '%s'" % name, stdout=out.strip())
    return fail("附着失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_script(args):
    """在目标进程上执行 Rhai 脚本（可选反检测）。脚本内容直接传入。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    script_content = _req(args, "script")
    pid = args.get("pid")
    anti_detect = args.get("antiDetect", False)

    # 写脚本到设备临时文件
    local_tmp = os.path.join(SHOT_DIR, "_frida_tmp.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script_content)
    device_tmp = "/data/local/tmp/_frida_tmp.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    cmd_args = [device_tmp]
    if pid is not None:
        cmd_args += ["--pid", str(pid)]
    if anti_detect:
        cmd_args += ["--anti-detect"]

    out, err, rc = _frida_run(device, "script", cmd_args, timeout=120)
    # 清理
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        return ok("脚本执行完成", stdout=out.strip(), stderr=err.strip())
    return fail("脚本执行失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_read_mem(args):
    """跨进程读取目标内存（返回十六进制）。frida-rust MemoryScanner。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    address = _req(args, "address")
    size = _req(args, "size", "int")
    if size > 0x100000:
        return fail("最大读取 1MB")

    # 用 Rhai 脚本读内存 (确保地址带 0x 前缀)
    if not address.startswith("0x"):
        address = "0x" + address
    script = 'let data = read_memory(%s, %d); log_info(hex(data));' % (address, size)
    local_tmp = os.path.join(SHOT_DIR, "_frida_read.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_read.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--pid", str(pid)],
                               timeout=30)
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        return ok("已读取 %d 字节 @ %s (PID=%d)" % (size, address, pid),
                  hex_data=out.strip())
    return fail("内存读取失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_write_mem(args):
    """跨进程写入目标内存（hex_data 为十六进制字符串）。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    address = _req(args, "address")
    hex_data = _req(args, "hexData")
    data_bytes = bytes.fromhex(hex_data.replace(" ", ""))

    if not address.startswith("0x"):
        address = "0x" + address
    script = 'write_memory(%s, blob([%s]));' % (
        address, ",".join(str(b) for b in data_bytes))
    local_tmp = os.path.join(SHOT_DIR, "_frida_write.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_write.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--pid", str(pid)],
                               timeout=30)
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        return ok("已写入 %d 字节 -> %s (PID=%d)" % (len(data_bytes), address, pid))
    return fail("内存写入失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_scan_mem(args):
    """在目标进程内存中搜索字节模式（返回匹配地址列表）。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    pattern = _req(args, "pattern")
    data_bytes = bytes.fromhex(pattern.replace(" ", ""))

    script = 'let results = search_bytes(blob([%s])); for addr in results { log_info("0x" + to_string(addr)); }' % (
        ",".join(str(b) for b in data_bytes))
    local_tmp = os.path.join(SHOT_DIR, "_frida_scan.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_scan.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--pid", str(pid)],
                               timeout=60)
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        addresses = [l.strip() for l in out.splitlines() if l.strip().startswith("0x")]
        return ok("找到 %d 个匹配" % len(addresses), addresses=addresses, raw=out.strip())
    return fail("内存扫描失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_stealth(args):
    """对目标进程应用 frida-rust 全部反检测措施(TracerPid/maps/特征擦除)。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    out, err, rc = _frida_run(device, "script",
                               ["--anti-detect", "--pid", str(args.get("pid", 0))])
    if rc == 0:
        return ok("反检测措施已应用", stdout=out.strip())
    return fail("反检测失败 (rc=%d): %s" % (rc, (err or out).strip()))

TOOLS = [
    # ---- 内核态 / 系统级（需 root）----
    {
        "name": "phone_ps",
        "description": "枚举设备全部进程(/proc 等价: PID/PPID/UID/RSS/CMD)，并解析当前前台 Activity。可直接看到微信等进程 PID。可选 filter 按包名/PID 过滤。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "可选，按包名或 PID 子串过滤"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_ps,
    },
    {
        "name": "phone_proc_read",
        "description": "直读单个进程的 /proc/<pid>/cmdline 与 /proc/<pid>/status 原始内核信息(Name/State/PPid/Uid/VmRSS/VmSize)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "进程 PID"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_proc_read,
    },
    {
        "name": "phone_kill",
        "description": "强制杀进程(无视应用保活): 给指定 pid 或 package 发 SIGKILL(kill -9 / pkill -9)。用于重启微信、干掉卡死进程。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "可选，要杀的进程 PID"},
                "package": {"type": "string", "description": "可选，按包名杀全部相关进程，如 com.tencent.mm"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_kill,
    },
    {
        "name": "phone_wechat_db_pull",
        "description": "root 直拉微信加密数据库 EnMicroMsg.db(+wal/shm/ini)到本机。绕过应用层，无需 OCR。注意文件是 SQLCipher 加密，需 phone_wechat_db_decrypt 解密。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_wechat_db_pull,
    },
    {
        "name": "phone_wechat_db_decrypt",
        "description": "尝试解密微信 EnMicroMsg.db。计算 legacy 候选密钥(md5(imei+uin)[:7])；若本机装了 SQLCipher 且提供 key(8.x 需 frida 取出的 256-bit 密钥)则直接解密读联系人/消息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "可选，SQLCipher 密钥；8.x 需 frida 取出的 256-bit 密钥"},
                "dbPath": {"type": "string", "description": "可选，本地 db 路径；缺省用 phone_wechat_db_pull 拉取的"},
                "legacy": {"type": "boolean", "description": "是否计算 legacy 候选密钥(默认 true)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_wechat_db_decrypt,
    },
    {
        "name": "phone_brightness",
        "description": "Root 直写背光滑块 /sys 节点：获取/设置屏幕亮度。action: get 返回当前+max；set 设百分比(0-100)或 raw=True 传原始值。自动化时先调暗省电、完成后恢复。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "get 或 set"},
                "level": {"type": "integer", "description": "set 时: 0-100 百分比，或 raw=True 时 0-max 原始值"},
                "raw": {"type": "boolean", "description": "set 时: True 传原始值而非百分比"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_brightness,
    },
    {
        "name": "phone_vibrate",
        "description": "触发手机震动指定毫秒(10-60000)。三级回退: sysfs 节点 -> cmd vibrator -> AIDL HAL service call。用于任务完成提醒。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "durationMs": {"type": "integer", "description": "震动时长(毫秒)，默认 200"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_vibrate,
    },
    {
        "name": "phone_cpu",
        "description": "Root 调控 CPU：list(查看在线核心/governor/可用频率)、set_governor(切换调度器，如 walt/schedutil)、online_core/offline_core(上线/下线指定核心)、set_max_freq(限制最大频率 kHz)。用于自动化时降低功耗。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list / set_governor / online_core / offline_core / set_max_freq"},
                "governor": {"type": "string", "description": "set_governor 时: 目标调度器名"},
                "core": {"type": "integer", "description": "online_core/offline_core 时: 核心编号(如 7 表示 cpu7)"},
                "freqKHz": {"type": "integer", "description": "set_max_freq 时: 频率上限(kHz，需是 availableFrequencies 之一)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_cpu,
    },
    {
        "name": "phone_audio",
        "description": "Root 操控音频：获取/设置音量(stream: music/system/ring/alarm/notification 或数字)、静音/取消静音。走 cmd audio(AudioService CLI)，绕过系统设置 UI。注: /dev/snd 原始 PCM 写的是采样数据非音量，正确音量由 cmd audio 控制。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "get / set_volume / mute / unmute"},
                "stream": {"type": "string", "description": "音频流: music(默认)/system/ring/alarm/notification 或数字 0-5"},
                "level": {"type": "integer", "description": "set_volume 时: 音量值(0-max 整数)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_audio,
    },
    {
        "name": "phone_net_firewall",
        "description": "Root iptables 防火墙：按 App uid/包名拦截所有网络(IPv4+IPv6 OUTPUT DROP)、解封、查看规则、清空全部。用于断网调试自动化 App 离线行为。⚠️ clear_all 会清空全部 OUTPUT 规则！",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list / block_app / unblock_app / clear_all"},
                "package": {"type": "string", "description": "block/unblock 时: 包名(如 com.tencent.mm)，自动解析 uid"},
                "uid": {"type": "integer", "description": "block/unblock 时: 直接指定 uid"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_net_firewall,
    },
    # ---- 界面层（只读）----
    {
        "name": "phone_get_devices",
        "description": "列出当前通过 adb 连接的设备。只读，建议先调用确认设备在线。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_get_devices,
    },
    {
        "name": "phone_screenshot",
        "description": "截取手机当前屏幕，返回图片与本地保存路径。AI 可据此'看到'手机界面。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则用默认设备"}
            },
        },
        "handler": t_screenshot,
    },
    {
        "name": "phone_cap_sync",
        "description": "同步设备屏幕参数(分辨率/旋转/刷新率)，等价于 minicap 的握手 banner。启动持续截帧流前调用，让 AI 获知当前屏幕宽高与朝向。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则用默认设备"}},
        },
        "handler": t_cap_sync,
    },
    {
        "name": "phone_screenshot_stream",
        "description": "root 直连截图(绕过应用层截图 API，无系统弹窗/无权限拦截)，保存本地 PNG 并返回路径与 base64。等价于 minicap 单帧抓取，比 phone_screenshot 更稳。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号"}},
        },
        "handler": t_screenshot_stream,
    },
    {
        "name": "phone_stream_start",
        "description": "启动持续截帧流(root, 无弹窗)：后台以 fps 频率持续截图写入本地目录，供 phone_ocr_stream 低延迟取最新帧做文字识别/页面状态校验。等价于 minicap 的 socket 持续图像流。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fps": {"type": "integer", "description": "截帧频率(1~30，默认4)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_stream_start,
    },
    {
        "name": "phone_stream_stop",
        "description": "停止持续截帧流，释放后台线程。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号"}},
        },
        "handler": t_stream_stop,
    },
    {
        "name": "phone_ocr_stream",
        "description": "对截帧流最新帧(或现截一帧)运行 RapidOCR 文字识别。可传 query 精确/包含匹配返回命中坐标，用于'页面是否显示某文字/某状态'的低延迟校验。无弹窗、无权限拦截。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选，要查找的文字；不传则返回全部文字块"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含(默认)"},
                "region": {"type": "array", "items": {"type": "number"}, "description": "可选归一化裁剪 [x1,y1,x2,y2](0~1)，只识别该区域提速"},
                "minConf": {"type": "number", "description": "最小置信度(默认0.3)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_ocr_stream,
    },
    {
        "name": "phone_dump_ui",
        "description": "dump 当前界面 UI 结构(XML)，含各控件文本与坐标边界，便于 AI 定位按钮。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_dump_ui,
    },
    # ---- 控件级定位（element）：比 OCR 更稳定，主力方案 ----
    {
        "name": "phone_ui_dump",
        "description": "解析当前界面控件树，返回所有具名控件(含文字/resource-id/content-desc)的中心坐标；完整树另存为 JSON。比 OCR 更稳定，毫秒级。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_ui_dump,
    },
    {
        "name": "phone_find_element",
        "description": "按 文字 / resource-id / content-desc 查找控件并返回坐标。matchBy 可指定字段(默认 any 全字段匹配)，exact 控制完全/包含匹配。只读、不点击。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的值，如 '文件传输助手' 或 'com.tencent.mm:id/xxx'"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_find_element,
    },
    {
        "name": "phone_find_ui_element",
        "description": "解析 uiautomator 控件树，按 文字 / resource-id / content-desc 查找控件坐标（比 OCR 更稳、毫秒级）。matchBy 指定字段(默认 any 全字段)；exact 控制完全/包含匹配。只读、不点击。即 phone_find_element 的同功能别名，命名更直白。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的值，如 '文件传输助手' 或 'com.tencent.mm:id/xxx'"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_find_element,
    },
    {
        "name": "phone_tap_element",
        "description": "按 文字 / resource-id / content-desc 直接点击控件，作为比 OCR 更稳定的主力定位方案。UI 树为空(微信/QQ等)时自动回退 OCR。多个匹配用 index(从1)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要点击的控件值，如 '文件传输助手' 或 resource-id"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段：any=三字段任一(默认)；text；resource-id；content-desc"},
                "exact": {"type": "boolean", "description": "true=完全匹配"},
                "index": {"type": "integer", "description": "多个匹配时点的第几个，默认 1"},
                "fallback": {"type": "boolean", "description": "UI 未命中时是否回退 OCR(默认 true)，仅文字查找生效"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_tap_element,
    },
    {
        "name": "phone_find_text",
        "description": "按文字定位坐标(只读、不点击)。默认 auto：先用无障碍/UI(解析uiautomator dump，毫秒级、精准)，拿不到再回退 OCR(视觉)。可用 method 强制单一模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要查找的文字，如 '爸爸'"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"},
                "region": {"type": "array", "items": {"type": "number"}, "description": "仅OCR模式生效：[x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以提速"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_find_text,
    },
    {
        "name": "phone_tap_text",
        "description": "按文字自动点击。默认 auto：先用无障碍/UI(毫秒级、精准)，拿不到再回退 OCR(视觉)。多个匹配用 index(从1)。可用 method 强制单一模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要点击的文字，如 '爸爸'"},
                "exact": {"type": "boolean", "description": "true=完全匹配"},
                "index": {"type": "integer", "description": "多个匹配时点的第几个，默认 1"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"},
                "region": {"type": "array", "items": {"type": "number"}, "description": "仅OCR模式生效：[x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以提速"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_tap_text,
    },
    # ---- 一键闭环：截图→定位→点击→验证（用户说"点击 XX"优先用本工具）----
    {
        "name": "phone_auto_click",
        "description": "【一键闭环】自动完成『截图/定位 → 点击 → 验证』。先按文字/ID/描述定位控件(自动选 UI 无障碍或 OCR 视觉，微信/QQ 等空树 App 自动回退 OCR)，点击后再次定位确认目标已离开屏幕(说明页面已切换、操作生效)。用户说『点击 XX』时优先用本工具，比单独调 phone_tap_text 更稳、自带重试。query 为要点的目标文字或控件值；method 同 phone_find_text(auto/ui/ocr)；verify=gone(默认，要求点后目标消失)或 any(只确认点击已执行)。整体失败会自动重试 maxRetries 轮。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要点击的目标文字或控件值，如 '文件传输助手' / 'WLAN' / '设置' / '返回'"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段(仅 UI 模式生效)：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "定位方式：auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"},
                "index": {"type": "integer", "description": "多个匹配时点的第几个，默认 1"},
                "maxRetries": {"type": "integer", "description": "最多尝试轮数(每轮=定位+点击+验证)，默认 3"},
                "verify": {"type": "string", "enum": ["gone", "any"], "description": "验证方式：gone=要求点击后目标离开屏幕(默认，强确认)；any=只确认点击已执行"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_auto_click,
    },
    {
        "name": "phone_swipe_until_find",
        "description": "自动滑动屏幕直到找到目标文字：每滑一次就重新定位，找到即停（可顺带点击）。适合'滚动长列表找某条'。direction=up(默认，内容下滚找下方项)/down/left/right；maxSwipes 最大滑动次数(默认8)；exact 严格匹配；tapOnFind=true 找到后顺手点击；method 同 phone_find_text(auto/ui/ocr)；swipeStep 单次滑动占屏比例(默认0.6)。返回是否找到、坐标与所用滑动次数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的目标文字"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "滑动方向：up=向上滑(默认，找下方项)；down=向下滑；left/right=横向滑动"},
                "maxSwipes": {"type": "integer", "description": "最多滑动次数，默认 8"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "tapOnFind": {"type": "boolean", "description": "找到后是否顺手点击，默认 false(只定位不点)"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "定位方式：auto=先UI后OCR(默认)；ui=只用无障碍；ocr=只用视觉"},
                "swipeStep": {"type": "number", "description": "单次滑动占屏比例(0.1~0.9)，默认 0.6"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_swipe_until_find,
    },
    {
        "name": "phone_wechat_open_chat",
        "description": "【全链路示例】进入微信某联系人的聊天界面：启动微信→切到通讯录→(自动校验)在联系人列表滑动找到并点击该联系人→校验进入聊天。演示'操作后自动校验+失败自动重试'闭环。需手机已登录微信且该联系人存在；微信版本/界面差异可能需微调。contact 为联系人备注/昵称。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "要打开聊天的联系人备注或昵称，如 '爸爸' / '文件传输助手'"},
                "maxSwipes": {"type": "integer", "description": "联系人列表最多滑动次数(默认12)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["contact"],
        },
        "handler": t_wechat_open_chat,
    },
    {
        "name": "phone_send_wechat_message",
        "description": "【完整闭环】给微信联系人发消息：启动微信→回主页→打开搜索→输入联系人→精准点击最顶部联系人条目进入聊天→激活输入框→粘贴消息→点发送。每步都做 OCR 校验、失败自动重试 2 次，返回结构化结果(含每步steps)。contact_name=联系人名称(备注/昵称)，message=消息内容。需手机已登录微信且该联系人存在。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_name": {"type": "string", "description": "联系人名称（备注或昵称），如 '向远钦' / '文件传输助手'"},
                "message": {"type": "string", "description": "要发送的消息内容，如 '你好'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["contact_name", "message"],
        },
        "handler": t_send_wechat_message,
    },
    # ---- 界面层（写）----
    {
        "name": "phone_tap",
        "description": "在屏幕坐标 (x, y) 点击。坐标为像素，需先截图确认尺寸。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "横坐标像素"},
                "y": {"type": "integer", "description": "纵坐标像素"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["x", "y"],
        },
        "handler": t_tap,
    },
    {
        "name": "phone_swipe",
        "description": "从 (x1,y1) 滑动到 (x2,y2)，可指定时长(ms)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer", "description": "起点横坐标像素"},
                "y1": {"type": "integer", "description": "起点纵坐标像素"},
                "x2": {"type": "integer", "description": "终点横坐标像素"},
                "y2": {"type": "integer", "description": "终点纵坐标像素"},
                "durationMs": {"type": "integer", "description": "滑动时长，默认 300"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
        "handler": t_swipe,
    },
    {
        "name": "phone_a11y_tap",
        "description": "无障碍服务坐标点击（input 注入被系统拦截时的备选路径）。发送广播给已安装的 phone-mcp 无障碍服务，由它用 dispatchGesture 点击。需手机端先安装并启用该无障碍服务，否则无效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "横坐标像素"},
                "y": {"type": "integer", "description": "纵坐标像素"},
                "durationMs": {"type": "integer", "description": "按下时长，默认 80ms"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["x", "y"],
        },
        "handler": t_a11y_tap,
    },
    {
        "name": "phone_input_text",
        "description": "【统一文本输入·行业标准】自动选择最优输入方式：优先 ADBKeyBoard（ADB 输入法+广播注入，支持中文/英文/emoji/特殊符号/多行，且输入法无感知切换，用完自动切回用户原输入法，首次使用自动从本地 ADBKeyboard.apk 安装启用）；ADBKeyBoard 不可用/异常时自动降级剪贴板(cmd/service call+粘贴键)兜底。微信场景自动判定 search/chat 区域；也可用 field 显式指定('search'|'chat'|'auto')。返回 data.method 标注实际方式('adbkeyboard'|'clipboard')。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本（支持中文/英文/emoji/特殊符号/多行换行）"},
                "field": {"type": "string", "enum": ["auto", "search", "chat"], "description": "输入区域：auto=自动判定(默认)；search=搜索框；chat=聊天输入框"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_input_text,
    },
    {
        "name": "phone_paste_text",
        "description": "通过剪贴板+粘贴键输入任意 Unicode 文本（含中文）。先把文本写入剪贴板，再发送 PASTE 键(279)。当需要显式用粘贴而非 input text 时用本工具；一般中文输入用 phone_input_text 即可自动路由。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要粘贴的文本（支持中文等任意 Unicode）"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_paste_text,
    },
    {
        "name": "phone_input_chinese",
        "description": "中文输入专用工具：把文本写入手机剪贴板并触发粘贴键(PASTE=279)，解决 adb input text 不支持中文的问题。适用于搜索框、聊天输入框等任意可粘贴焦点。优先用本工具输入中文；英文也可使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本（中文走剪贴板粘贴，英文同样支持）"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_input_chinese,
    },
    {
        "name": "phone_input_method_setup",
        "description": "安装并启用 ADBKeyBoard 输入法（行业标准中文输入方案），返回当前/可用输入法状态，供显式预置与排障。需要本地 ADBKeyboard.apk 存在（放至 phone-mcp 目录）；缺失时会提示路径且不影响 phone_input_text 的剪贴板兜底。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_setup_adbkeyboard,
    },
    {
        "name": "phone_launch_app",
        "description": "启动应用。给 package(如 com.tencent.mm)；省略 activity 时用 monkey 启动主 Activity。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名"},
                "activity": {"type": "string", "description": "可选，完整 Activity 名"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_launch_app,
    },
    {
        "name": "phone_key_event",
        "description": "发送按键事件。支持名称(HOME/BACK/VOLUME_UP/RECENT 等)或数字 keycode。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keycode": {"type": "string", "description": "如 HOME / BACK / 3 / 187"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["keycode"],
        },
        "handler": t_key_event,
    },
    {
        "name": "phone_press_key",
        "description": "发送按键(返回/主页/电源等)。keycode 支持名称(HOME/BACK/POWER/VOLUME_UP/RECENT...)或数字(如 26=电源, 4=返回)。即 phone_key_event 的别名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keycode": {"type": "string", "description": "如 HOME / BACK / POWER / 26 / 4"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["keycode"],
        },
        "handler": t_key_event,
    },
    {
        "name": "phone_press_back",
        "description": "发送返回键(BACK=4)。快捷别名，等价于 phone_key_event 传 BACK。进入子页面后回退常用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_press_back,
    },
    {
        "name": "phone_press_home",
        "description": "发送主页键(HOME=3)。快捷别名，等价于 phone_key_event 传 HOME。一键回桌面常用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_press_home,
    },
    # ---- 系统级 / 底层（只读，常开）----
    {
        "name": "phone_list_packages",
        "description": "列出已安装应用包名，可选 filter 关键字过滤。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "可选，包名关键字过滤"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_list_packages,
    },
    {
        "name": "phone_list_processes",
        "description": "列出设备上正在运行的进程(ps -A)。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_list_processes,
    },
    {
        "name": "phone_getprop",
        "description": "读取 Android 系统属性(getprop)。可指定 key，省略则列出全部。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "可选属性名"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_getprop,
    },
    {
        "name": "phone_settings_get",
        "description": "读取系统设置(settings get)。namespace 如 global/system/secure。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "global / system / secure"},
                "key": {"type": "string", "description": "属性名 / 设置项键名，如 bluetooth_on / wifi_on"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["namespace", "key"],
        },
        "handler": t_settings_get,
    },
    {
        "name": "phone_get_current_app",
        "description": "返回当前前台应用的包名与 Activity（dumpsys window 解析 mCurrentFocus/mFocusedApp）。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_get_current_app,
    },
    {
        "name": "phone_file_read",
        "description": "读取设备上的文本文件内容(cat)。只读。受权限限制的路径可能读不到。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "设备内文件绝对路径"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["path"],
        },
        "handler": t_file_read,
    },
    # ---- 系统级 / 底层（写，需 PHONE_MCP_ALLOW_SHELL=1）----
    {
        "name": "phone_shell",
        "description": "在设备上执行任意 shell 命令(单条，支持管道/重定向)。需 ALLOW_SHELL=1；禁止 reboot/wipe 等灾难命令。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "如 'ps -A | grep tencent'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["command"],
        },
        "handler": t_shell,
    },
    {
        "name": "phone_run_shell",
        "description": "安全透传 adb shell 命令(单条，支持管道/重定向)。需 ALLOW_SHELL=1；禁止 reboot/wipe/format/dd if= 等灾难命令。即 phone_shell 的别名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "如 'ps -A | grep tencent'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["command"],
        },
        "handler": t_shell,
    },
    {
        "name": "phone_run_adb",
        "description": "执行原始 adb 命令(host 侧,数组或字符串)。需 ALLOW_SHELL=1；拦截 reboot/wipe/rm 等危险指令。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "adb 参数，如 'shell pm list packages'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["args"],
        },
        "handler": t_run_adb,
    },
    {
        "name": "phone_start_service",
        "description": "启动一个 Android 服务(am startservice -n pkg/Service)。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "service": {"type": "string", "description": "服务类名，如 .MyService"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package", "service"],
        },
        "handler": t_start_service,
    },
    {
        "name": "phone_force_stop",
        "description": "强制停止某应用(am force-stop pkg)，会结束其所有进程与后台服务。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_force_stop,
    },
    {
        "name": "phone_stop_app",
        "description": "停止应用(am force-stop pkg)：结束其所有进程与后台服务，回到桌面。需 ALLOW_SHELL=1。即 phone_force_stop 的别名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_force_stop,
    },
    {
        "name": "phone_kill_process",
        "description": "结束进程。target 为数字 PID 用 kill；为包名则用 force-stop。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "PID 或包名"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["target"],
        },
        "handler": t_kill_process,
    },
    {
        "name": "phone_setprop",
        "description": "设置 Android 系统属性(setprop)。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "属性名 / 设置项键名，如 bluetooth_on / wifi_on"},
                "value": {"type": "string", "description": "要写入的值"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["key", "value"],
        },
        "handler": t_setprop,
    },
    {
        "name": "phone_settings_put",
        "description": "修改系统设置(settings put)。namespace 如 global/system/secure。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "设置命名空间：global / system / secure"},
                "key": {"type": "string", "description": "属性名 / 设置项键名，如 bluetooth_on / wifi_on"},
                "value": {"type": "string", "description": "要写入的值"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["namespace", "key", "value"],
        },
        "handler": t_settings_put,
    },
    {
        "name": "phone_file_write",
        "description": "向设备写文本文件(push)。需 ALLOW_SHELL=1。写系统分区可能需 root/remount。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "设备内目标路径"},
                "content": {"type": "string", "description": "要写入的文本"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["path", "content"],
        },
        "handler": t_file_write,
    },
    {
        "name": "phone_install_apk",
        "description": "安装本地 APK 到设备(adb install)。需 ALLOW_SHELL=1。localPath 为电脑上的 apk 路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "localPath": {"type": "string", "description": "本机 apk 绝对路径"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["localPath"],
        },
        "handler": t_install_apk,
    },
    {
        "name": "phone_uninstall",
        "description": "卸载应用并清除数据(adb uninstall)。需 ALLOW_SHELL=1。会丢失应用数据！",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_uninstall,
    },
    # ---- frida-rust 动态插桩（需 root + frida-rust 部署到设备）----
    {
        "name": "phone_frida_inject",
        "description": "使用 frida-rust 将共享库注入到目标进程(ptrace+dlopen)。需 root，设备上需有 frida-rust 二进制。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "libPath": {"type": "string", "description": "可选，共享库路径(默认 /data/local/tmp/libfrida_agent.so)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid"],
        },
        "handler": t_frida_inject,
    },
    {
        "name": "phone_frida_attach",
        "description": "使用 frida-rust ptrace 附着到目标进程（按进程名查找）。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "processName": {"type": "string", "description": "目标进程名称(如 com.tencent.mm)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["processName"],
        },
        "handler": t_frida_attach,
    },
    {
        "name": "phone_frida_script",
        "description": "在目标进程上执行 Rhai 脚本（frida-rust 脚本引擎）。支持内存读写、Hook、搜索等 API。可选 --anti-detect。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Rhai 脚本内容（支持 find_module_base/read_memory/write_memory/search_bytes/hook_function 等 API）"},
                "pid": {"type": "integer", "description": "可选，目标进程 PID"},
                "antiDetect": {"type": "boolean", "description": "可选，是否启用反检测(默认 false)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["script"],
        },
        "handler": t_frida_script,
    },
    {
        "name": "phone_frida_read_mem",
        "description": "跨进程读取目标内存，返回十六进制数据。通过 frida-rust Rhai 脚本的 read_memory API。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "address": {"type": "string", "description": "起始地址(十六进制，如 0x7f12345000)"},
                "size": {"type": "integer", "description": "读取字节数(最大 1MB)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid", "address", "size"],
        },
        "handler": t_frida_read_mem,
    },
    {
        "name": "phone_frida_write_mem",
        "description": "跨进程写入目标内存(hexData 为十六进制字符串)。通过 frida-rust Rhai 脚本的 write_memory API。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "address": {"type": "string", "description": "目标地址(十六进制)"},
                "hexData": {"type": "string", "description": "要写入的十六进制数据(如 90909090)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid", "address", "hexData"],
        },
        "handler": t_frida_write_mem,
    },
    {
        "name": "phone_frida_scan_mem",
        "description": "在目标进程内存中搜索字节模式，返回所有匹配地址。通过 frida-rust search_bytes API。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "pattern": {"type": "string", "description": "十六进制字节模式(如 48895C24)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid", "pattern"],
        },
        "handler": t_frida_scan_mem,
    },
    {
        "name": "phone_frida_stealth",
        "description": "对目标进程应用 frida-rust 全部反检测措施：TracerPid 清零、/proc/maps 隐藏、Frida 特征擦除。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "可选，目标进程 PID(默认 0 表示自身)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
        },
        "handler": t_frida_stealth,
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC / MCP 协议层
# ---------------------------------------------------------------------------

def handle_request(req):
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "notifications/initialized":
        return None  # 通知，无需回复

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        tools = []
        for t in TOOLS:
            tools.append({
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            })
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return dispatch_tool(name, arguments, req_id)

    # 未知方法
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": "Method not found: %s" % method},
        }
    return None


def main():
    log("phone-mcp 启动, adb=%s, 默认设备=%s, DRYRUN=%s, ALLOW_SHELL=%s"
        % (ADB, DEFAULT_DEVICE, DRYRUN, ALLOW_SHELL))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("无法解析的行:", line[:200])
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()



