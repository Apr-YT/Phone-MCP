# -*- coding: utf-8 -*-
"""界面操作工具：点击、滑动、输入、按键、启动应用。"""
import os, time, base64, json, re, sys

from adb import run_adb, log, with_retry, resolve_device, list_devices
from adb import DRYRUN as adb_dryrun, ALLOW_SHELL as adb_allow_shell
from adb import DEFAULT_DEVICE as adb_default_device
from utils import ok, fail, text_block, image_block
from tools._shared import SHOT_DIR, FAST, _ocr_debug
from .vision import smart_find, _get_ui_xml, _top_pkg, _screen_size, ui_find

# 内核触摸全局状态
_MT_CACHE = {}

from adb import log as _log

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

