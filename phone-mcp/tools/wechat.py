# -*- coding: utf-8 -*-
"""微信集成工具：联系人列表、会话列表、消息读写、搜索、发送。"""
import os, time, re, json

from adb import run_adb, log, with_retry, with_verification, resolve_device, DRYRUN
from utils import ok, fail, text_block
from tools._shared import SHOT_DIR, _ocr_debug, get_ocr_reader
from tools.vision import (smart_find, _get_ui_xml, _top_pkg, _screen_size,
                          ocr_boxes, ocr_match_contact, _ocr_sees, _ocr_tap)
from tools.ui import (t_launch_app, t_swipe_until_find, t_input_text,
                      _tap, _u2_device, wechat_tap_input_box,
                      wechat_clear_input, _input_region_has)
from tools.system import t_get_current_app


def _wechat_foreground(device):
    """微信是否当前前台 App（dumpsys 解析，无 OCR 开销）。"""
    return _top_pkg(device) == "com.tencent.mm"


# ─────────────────────────────────────────────────────────────
#  微信发送坐标缓存（秒级响应优化）
#  首次走完整 OCR 流程时记录：联系人条目坐标 / 发送按钮坐标 / 输入框坐标，
#  以及记录时的逻辑分辨率(w,h)。后续命中缓存直接用 input tap 点坐标，跳过 OCR，
#  把单次发送从 16~67s 压到秒级。坐标仅在「分辨率一致 + 微信已前台」时复用，
#  否则作废回退完整流程，避免界面变化导致点错联系人/按钮。
# ─────────────────────────────────────────────────────────────
_WX_COORD_CACHE = None
_WX_COORD_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "contact_coords.json")

def _wx_coord_load():
    global _WX_COORD_CACHE
    if _WX_COORD_CACHE is not None:
        return _WX_COORD_CACHE
    try:
        with open(_WX_COORD_CACHE_PATH, "r", encoding="utf-8") as f:
            _WX_COORD_CACHE = json.load(f)
    except Exception:
        _WX_COORD_CACHE = {}
    return _WX_COORD_CACHE

def _wx_coord_save():
    try:
        os.makedirs(os.path.dirname(_WX_COORD_CACHE_PATH), exist_ok=True)
        with open(_WX_COORD_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_WX_COORD_CACHE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _ocr_debug("wx coord cache save 失败: %r" % e)

def _wx_coord_get(contact):
    return _wx_coord_load().get(contact)

def _wx_coord_put(contact, coords):
    _wx_coord_load()[contact] = coords
    _wx_coord_save()

def _wx_coord_invalidate(contact):
    c = _wx_coord_load()
    if contact in c:
        del c[contact]
        _wx_coord_save()

def _wx_find_coord(device, text, region):
    """OCR 找一个文字块中心坐标(逻辑分辨率空间)，用于缓存发送按钮等固定控件。"""
    boxes = ocr_boxes(device, region=region, min_conf=0.2)
    for b in boxes:
        if text in b[0]:
            return [b[1], b[2]]
    return None


def _req(args, key, kind="str"):
    """取必填参数；缺失或类型不符时抛 ValueError。"""
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
def _open_chat_by_search(device, contact):
    """经微信首页搜索打开与 contact 的聊天（已验证可靠路径，send 也走这条）。
    覆盖「文件传输助手」这类不在通讯录 Tab 的特殊会话。返回 True/False。"""
    w, h = _screen_size(device)
    _wechat_ensure_home(device)
    # 打开搜索框（已处于搜索页则跳过点击）
    if not _search_opened(device):
        def open_search():
            _tap(int(w * 0.83), int(h * 0.07), device)
            time.sleep(0.2)
        with_verification(open_search, lambda _: _search_opened(device),
                          max_retries=3, delay=0.3)
    # 输入联系人（先聚焦搜索框）
    def type_contact():
        _tap(int(w * 0.5), int(h * 0.07), device)
        time.sleep(0.15)
        t_input_text({"text": contact, "deviceSerial": device, "field": "search"})
        time.sleep(0.3)
    with_verification(type_contact,
                      lambda _: _ocr_sees(device, contact, region=[0, 0.10, 1, 0.6]),
                      max_retries=3, delay=0.3)
    # 点击搜索结果进聊天
    def click_contact():
        hits = ocr_match_contact(contact, device, region=[0, 0.12, 1, 0.6])
        if not hits:
            return False
        _, cx, cy, _ = hits[0]
        _tap(cx, cy, device)
        time.sleep(0.4)
        return True
    def verify_contact():
        if _chat_header_is(device, contact):
            return True
        if _ocr_tap(device, "发消息", region=[0, 0.2, 1, 0.9]):
            time.sleep(0.5)
            return _chat_header_is(device, contact)
        return False
    return with_verification(click_contact, verify_contact, max_retries=3, delay=0.4)


def t_wechat_open_chat(args):
    """进入微信某联系人的聊天界面：搜索优先（覆盖文件传输助手等特殊会话，
    已自动校验进入聊天），通讯录滑动查找作为兜底。手机需已登录微信且联系人存在。"""
    contact = _req(args, "contact")
    device = resolve_device(args.get("deviceSerial"))
    steps = []
    if DRYRUN:
        return ok("[DRYRUN] 将打开微信联系人 '%s' 的聊天。" % contact, dryrun=True, contact=contact)
    # 1) 搜索优先（send 已验证可靠路径）
    ok_open = _open_chat_by_search(device, contact)
    steps.append("搜索打开聊天: %s" % ("成功" if ok_open else "未找到，回退通讯录滑动"))
    if ok_open:
        return ok("已打开与 '%s' 的聊天（已自动校验进入聊天界面）。" % contact,
                  contact=contact, in_chat=True, steps=steps)
    # 2) 兜底：通讯录列表滑动查找并点击
    pkg = "com.tencent.mm"
    r = t_launch_app({"package": pkg, "deviceSerial": device})
    steps.append("启动微信: %s" % (r.get("message") if isinstance(r, dict) else r))
    time.sleep(1.5)

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

def t_send_wechat_message(args):
    """【完整闭环·性能优化版】给微信联系人发消息。
    优化：① 已在目标聊天 → 跳过启动/搜索/点击联系人(省6-10s)
          ② 所有 sleep 减半；③ input 广播成功则跳过 OCR 验证
          ④ 发送前先 BACK 关键盘，再点发送。
    contact_name=联系人名称(备注/昵称)，message=消息内容。"""
    contact = _req(args, "contact_name", "str")
    message = _req(args, "message", "str")
    device = resolve_device(args.get("deviceSerial"))
    steps = []
    clock = [time.time()]
    coord_buf = {}  # 本次流程捕获到的坐标，成功时写入缓存

    def mark():
        clock.append(time.time())
        return "  ⏱%.1fs" % (clock[-1] - clock[-2])

    if DRYRUN:
        return ok("[DRYRUN] 将给 '%s' 发送: %s" % (contact, message), dryrun=True,
                  contact_name=contact, message=message)

    w, h = _screen_size(device)

    # ═══ 坐标缓存快路径（秒级响应优化）═══
    # 命中条件：缓存存在 + 记录分辨率与当前一致 + 微信已在前台(免启动OCR)
    # 跳过搜索/联系人/发送按钮的 OCR 定位，直接 input tap 缓存坐标。
    _cache = _wx_coord_get(contact)
    if (_cache and _cache.get("w") == w and _cache.get("h") == h
            and _wechat_foreground(device)):
        _verify = os.environ.get("PHONE_MCP_WX_FAST") != "1"  # 默认做1次末校；=1 乐观免校验
        steps.append("⚡ 命中坐标缓存，进入快路径(末次OCR校验=%s)%s"
                     % ("开" if _verify else "关", mark()))
        # 确保回到微信主页（搜索入口在动作栏）；仅在非主页时做1次OCR判断
        if not _ocr_sees(device, "微信", region=[0, 0.0, 1, 0.12]):
            for _ in range(3):
                run_adb(["shell", "input", "keyevent", "4"], device=device, mutating=True)
                time.sleep(0.4)
                if _ocr_sees(device, "微信", region=[0, 0.0, 1, 0.12]):
                    break
        _tap(int(w * 0.83), int(h * 0.07), device); time.sleep(0.2)   # 打开搜索
        _tap(int(w * 0.5), int(h * 0.07), device); time.sleep(0.15)   # 点搜索框
        t_input_text({"text": contact, "deviceSerial": device, "field": "search"}); time.sleep(0.3)
        _tap(_cache["contact"][0], _cache["contact"][1], device); time.sleep(0.4)  # 点联系人条目
        _ix, _iy = _cache.get("input") or [int(w * 0.5), int(h * 0.96)]
        _tap(_ix, _iy, device); time.sleep(0.2)                        # 激活输入框
        t_input_text({"text": message, "deviceSerial": device, "field": "chat"}); time.sleep(0.2)
        _tap(_cache["send"][0], _cache["send"][1], device); time.sleep(0.3)        # 点发送
        if _verify:
            _sent = _msg_sent(device, message)
            if _sent:
                steps.append("⚡ 快路径发送成功(末次OCR校验通过)%s" % mark())
                return ok("已给「%s」发送消息：%s" % (contact, message),
                          contact_name=contact, content=message, sent=True, fast=True,
                          total_seconds=round(time.time() - clock[0], 1), steps=steps)
            _wx_coord_invalidate(contact)
            return fail("快路径发送未通过末次OCR校验（可能微信界面变化），已清除该联系人缓存，请重试。",
                        contact_name=contact, content=message, sent=False, fast=True,
                        total_seconds=round(time.time() - clock[0], 1), steps=steps)
        steps.append("⚡ 快路径发送(乐观免校验)%s" % mark())
        return ok("已给「%s」发送消息：%s" % (contact, message),
                  contact_name=contact, content=message, sent=True, fast=True,
                  total_seconds=round(time.time() - clock[0], 1), steps=steps)
    # ═══ 以下为原有完整流程（缓存未命中 / 不触发快路径时）═══

    # ═══ 快速短路：已在目标联系人聊天中 → 直接跳到输入+发送 ═══
    if _wechat_foreground(device):
        if _chat_header_is(device, contact):
            steps.append("⏩ 已在「%s」聊天中，跳过启动/搜索/点击" % contact)
            has_input = _ocr_sees(device, "发送", region=[0, 0.85, 1, 1.0])
            if not has_input:
                # 输入框可能未激活
                _tap(400, int(h * 0.96), device)
                time.sleep(0.2)
            # 直接跳到输入+发送
            inp = {}
            def type_msg():
                r = t_input_text({"text": message, "deviceSerial": device, "field": "chat"})
                inp["msg"] = (r.get("data") or {}).get("method")
                time.sleep(0.2)
            ok_m = with_verification(type_msg,
                                     lambda _: _ocr_sees(device, "发送", region=[0, 0.85, 1, 1.0]),
                                     max_retries=2, delay=0.3)
            steps.append("⑥ 输入消息(快速通道): %s%s" % ("成功" if ok_m else "未确认(继续)", mark()))
            def click_send():
                if _msg_sent(device, message):
                    return True
                run_adb(["shell", "input", "keyevent", "4"], device=device, mutating=True)
                time.sleep(0.2)
                return _ocr_tap(device, "发送", region=[0, 0.85, 1, 1.0])
            ok_send = with_verification(click_send, lambda _: _msg_sent(device, message),
                                        max_retries=2, delay=0.4)
            if ok_send:
                steps.append("⑦ 已发送%s" % mark())
                return ok("已给「%s」发送消息：%s" % (contact, message),
                          contact_name=contact, content=message, sent=True,
                          total_seconds=round(time.time() - clock[0], 1), steps=steps)
            steps.append("⑦ 发送未确认%s" % mark())
            return fail("消息已输入但发送未确认。", contact_name=contact, content=message,
                        sent=False, total_seconds=round(time.time() - clock[0], 1), steps=steps)

        # ═══ 微信已在前台但不在目标聊天 → 只跳到搜索 + 点击 ═══
        # 回到微信主页
        was_home = _ocr_sees(device, "微信", region=[0, 0.0, 1, 0.12])
        if not was_home:
            _wechat_ensure_home(device)
            steps.append("① 回到微信主页%s" % mark())
        else:
            steps.append("① 已在微信主页%s" % mark())
    else:
        # ═══ 微信不在前台 → 完整启动 ═══
        _wechat_ensure_home(device)
        steps.append("① 启动微信并回到主页%s" % mark())

    # ═══ 公用：搜索→点击→聊天→输入→发送 ═══
    w, h = _screen_size(device)
    # 2) 打开搜索框（前置判断：已处于搜索页则跳过）
    if _search_opened(device):
        steps.append("② 打开搜索框: 已处于搜索页，跳过点击%s" % mark())
    else:
        def open_search():
            _tap(int(w * 0.83), int(h * 0.07), device)
            time.sleep(0.2)
        ok_s = with_verification(open_search, lambda _: _search_opened(device),
                                 max_retries=3, delay=0.3)
        steps.append("② 打开搜索框: %s%s" % ("成功" if ok_s else "未自动确认(继续)", mark()))
    # 3) 输入联系人（先点搜索框聚焦）
    inp = {}
    def type_contact():
        _tap(int(w * 0.5), int(h * 0.07), device)
        time.sleep(0.15)
        r = t_input_text({"text": contact, "deviceSerial": device, "field": "search"})
        inp["contact"] = (r.get("data") or {}).get("method")
        time.sleep(0.3)
    ok_c = with_verification(type_contact,
                             lambda _: _ocr_sees(device, contact, region=[0, 0.10, 1, 0.6]),
                             max_retries=3, delay=0.3)
    steps.append("③ 搜索框输入联系人「%s」(输入方式=%s): %s%s"
                 % (contact, inp.get("contact"), "成功" if ok_c else "未确认(继续)", mark()))
    # 4) 点击联系人进聊天
    def click_contact():
        hits = ocr_match_contact(contact, device, region=[0, 0.12, 1, 0.6])
        if not hits:
            return False
        _, cx, cy, _ = hits[0]
        coord_buf["contact"] = [cx, cy]  # 捕获联系人条目坐标，用于写缓存
        _tap(cx, cy, device)
        time.sleep(0.4)
        return True
    def verify_contact():
        if _chat_header_is(device, contact):
            return True
        if _ocr_tap(device, "发消息", region=[0, 0.2, 1, 0.9]):
            time.sleep(0.5)
            return _chat_header_is(device, contact)
        return False
    ok_cc = with_verification(click_contact, verify_contact, max_retries=3, delay=0.4)
    if not ok_cc:
        steps.append("④ 点击联系人失败%s" % mark())
        return fail("未能找到/点击联系人 '%s'（可能在搜索结果中未出现，或匹配到聊天记录）。" % contact,
                    contact_name=contact, content=message, steps=steps)
    steps.append("④ 已进入与「%s」的聊天%s" % (contact, mark()))
    # 5) 激活输入框
    def focus_input():
        for q in ("发送消息", "按住 说话"):
            if _ocr_tap(device, q, region=[0, 0.85, 1, 1.0]):
                return True
        _tap(400, int(h * 0.96), device)
        return True
    ok_f = with_verification(focus_input,
                             lambda _: _ocr_sees(device, "发送", region=[0, 0.85, 1, 1.0]),
                             max_retries=2, delay=0.3)
    steps.append("⑤ 激活输入框: %s%s" % ("成功" if ok_f else "未确认(继续)", mark()))
    # 6) 输入消息
    def type_msg():
        r = t_input_text({"text": message, "deviceSerial": device, "field": "chat"})
        inp["msg"] = (r.get("data") or {}).get("method")
        # 透传输入校验细节（ADBKeyBoard 焦点/OCR 校验结果），便于排查与训练闭环观测
        inp["ak"] = (r.get("data") or {}).get("adbkeyboard_info")
        time.sleep(0.2)
    ok_m = with_verification(type_msg,
                             lambda _: _ocr_sees(device, "发送", region=[0, 0.85, 1, 1.0]),
                             max_retries=2, delay=0.3)
    steps.append("⑥ 输入消息「%s」(输入方式=%s): %s%s"
                 % (message, inp.get("msg"), "成功" if ok_m else "未确认(继续)", mark()))
    # 7) 点击发送
    def click_send():
        # 先捕获发送按钮坐标（无论是否已发送都抓，避免 _msg_sent 误判导致漏抓）
        _sc = _wx_find_coord(device, "发送", [0, 0.85, 1, 1.0])
        if _sc:
            coord_buf["send"] = _sc
        if _msg_sent(device, message):
            return True
        run_adb(["shell", "input", "keyevent", "4"], device=device, mutating=True)
        time.sleep(0.2)
        return _ocr_tap(device, "发送", region=[0, 0.85, 1, 1.0])
    ok_send = with_verification(click_send, lambda _: _msg_sent(device, message),
                                max_retries=2, delay=0.4)
    if ok_send:
        # 写入坐标缓存（下次同联系人直接 input tap，秒级响应）
        if coord_buf.get("contact") and coord_buf.get("send"):
            coord_buf["input"] = [int(w * 0.5), int(h * 0.96)]
            coord_buf["w"], coord_buf["h"] = w, h
            _wx_coord_put(contact, coord_buf)
        steps.append("⑦ 已发送%s" % mark())
        return ok("已给「%s」发送消息：%s" % (contact, message),
                  contact_name=contact, content=message, sent=True,
                  adbkeyboard_info=inp.get("ak"),
                  total_seconds=round(time.time() - clock[0], 1), steps=steps)
    steps.append("⑦ 发送未确认%s" % mark())
    return fail("已点击发送但未确认消息「%s」已出现在聊天中（可能发送失败）。" % message,
                contact_name=contact, content=message, sent=False,
                total_seconds=round(time.time() - clock[0], 1), steps=steps)


# ===========================================================================
# 通用联系人 & 消息工具（不限定特定联系人）
# ===========================================================================

def t_wechat_list_contacts(args):
    """列出微信通讯录中的联系人。
    流程：确保微信在首页 → 切到「通讯录」Tab → OCR 识别可见联系人 → 滚动加载更多。
    参数：maxScrolls(默认 5, 最多额外滚动次数)。"""
    device = resolve_device(args.get("deviceSerial"))
    max_scrolls = min(int(args.get("maxScrolls", 5)), 20)
    w, h = _screen_size(device)

    steps = []
    _wechat_ensure_home(device)
    steps.append("已回到微信主页")

    # 切到通讯录 Tab（微信底部: 「微信」「通讯录」「发现」「我」）
    for _ in range(2):
        _tap(int(w * 0.25), int(h * 0.965), device)
        time.sleep(0.6)
        if _ocr_sees(device, "通讯录", region=[0, 0.0, 1, 0.10]):
            break

    if not _ocr_sees(device, "通讯录", region=[0, 0.0, 1, 0.10]):
        return fail("未能切换到通讯录 Tab（可能微信版本差异）。", steps=steps)
    steps.append("已切到通讯录 Tab")

    contacts = _ocr_contact_list(device, region=[0, 0.10, 1, 0.90])
    seen = set(c[0] for c in contacts)

    for i in range(max_scrolls):
        if len(contacts) >= 200:
            break
        run_adb(["shell", "input", "swipe",
                 str(int(w * 0.5)), str(int(h * 0.75)),
                 str(int(w * 0.5)), str(int(h * 0.25)),
                 "200"], device=device, mutating=True)
        time.sleep(0.6)
        batch = _ocr_contact_list(device, region=[0, 0.10, 1, 0.90])
        new_count = 0
        for name, _, _, _ in batch:
            if name not in seen:
                seen.add(name)
                contacts.append((name, len(contacts) + new_count, 0, 1.0))
                new_count += 1
        if new_count == 0:
            break

    return ok(
        "通讯录共识别 %d 个联系人（滚动 %d 次）。" % (len(contacts), min(i + 1, max_scrolls)),
        total=len(contacts),
        contacts=[{"name": c[0]} for c in contacts],
        scrolls=min(i + 1, max_scrolls),
        steps=steps,
    )


def t_wechat_list_chats(args):
    """列出微信首页的聊天会话列表。
    参数：maxScrolls(默认 3)、minRecent(默认 10)。"""
    device = resolve_device(args.get("deviceSerial"))
    max_scrolls = min(int(args.get("maxScrolls", 3)), 10)
    min_recent = int(args.get("minRecent", 10))
    w, h = _screen_size(device)

    steps = []
    _wechat_ensure_home(device)
    steps.append("已回到微信主页")

    region = [0, 0.10, 1, 0.88]
    chats = _ocr_contact_list(device, region=region)
    seen = set(c[0] for c in chats)

    for i in range(max_scrolls):
        if len(chats) >= max(min_recent * 3, 60):
            break
        run_adb(["shell", "input", "swipe",
                 str(int(w * 0.5)), str(int(h * 0.80)),
                 str(int(w * 0.5)), str(int(h * 0.20)),
                 "200"], device=device, mutating=True)
        time.sleep(0.5)
        batch = _ocr_contact_list(device, region=region)
        new_count = 0
        for name, _, _, _ in batch:
            if name not in seen:
                seen.add(name)
                chats.append((name, len(chats) + new_count, 0, 1.0))
                new_count += 1
        if new_count == 0:
            break

    return ok(
        "首页共识别 %d 个聊天会话。" % len(chats),
        total=len(chats),
        chats=[{"name": c[0]} for c in chats],
        scrolls=min(i + 1, max_scrolls),
        steps=steps,
    )


def t_wechat_read_messages(args):
    """读取与某联系人的聊天记录。
    参数：contact(必填)、maxScrolls(默认 5)、maxMessages(默认 50)。"""
    contact = _req(args, "contact", "str")
    device = resolve_device(args.get("deviceSerial"))
    max_scrolls = min(int(args.get("maxScrolls", 5)), 20)
    max_msgs = min(int(args.get("maxMessages", 50)), 200)
    w, h = _screen_size(device)

    steps = []
    open_result = t_wechat_open_chat({
        "contact": contact, "deviceSerial": device,
    })
    if isinstance(open_result, dict) and not open_result.get("success"):
        return open_result
    steps.append("已进入与「%s」的聊天" % contact)

    msg_region = [0, 0.08, 1, 0.85]
    messages = _ocr_message_list(device, region=msg_region)
    seen = set(m[0] for m in messages)

    for i in range(max_scrolls):
        if len(messages) >= max_msgs:
            break
        run_adb(["shell", "input", "swipe",
                 str(int(w * 0.5)), str(int(h * 0.55)),
                 str(int(w * 0.5)), str(int(h * 0.18)),
                 "300"], device=device, mutating=True)
        time.sleep(0.8)
        batch = _ocr_message_list(device, region=msg_region, min_len=4)
        new_count = 0
        for text, _, _, _ in batch:
            if text not in seen:
                seen.add(text)
                messages.append((text, len(messages) + new_count, 0, 1.0))
                new_count += 1
        if new_count == 0:
            break

    return ok(
        "共读取 %d 条消息。" % len(messages),
        contact=contact,
        total=len(messages),
        messages=[{"content": m[0]} for m in messages],
        scrolls=min(i + 1, max_scrolls),
        steps=steps,
    )


def t_wechat_search_contact(args):
    """微信全局搜索联系人（首页搜索入口）。
    参数：query(必填)、openChat(默认 false)。"""
    query = _req(args, "query", "str")
    device = resolve_device(args.get("deviceSerial"))
    open_chat = bool(args.get("openChat", False))
    w, h = _screen_size(device)

    steps = []
    _wechat_ensure_home(device)
    steps.append("已回到微信主页")

    if not _search_opened(device):
        _tap(int(w * 0.83), int(h * 0.07), device)
        time.sleep(0.5)
    steps.append("已打开搜索框")

    t_input_text({"text": query, "deviceSerial": device, "field": "search"})
    time.sleep(0.6)

    hits = ocr_match_contact(query, device, region=[0, 0.10, 1, 0.55])
    if not hits:
        hits = ocr_match_contact(query, device, region=[0, 0.10, 1, 0.70])

    contacts_found = [
        {"name": h[0], "cx": h[1], "cy": h[2], "confidence": h[3]}
        for h in hits[:20]
    ]

    opened = False
    if open_chat and hits:
        lbl, cx, cy, _ = hits[0]
        _tap(cx, cy, device)
        time.sleep(0.8)
        opened = _chat_header_is(device, query)

    return ok(
        "搜索「%s」找到 %d 个匹配%s。" % (query, len(hits), "，已打开聊天" if opened else ""),
        query=query, found=len(hits), contacts=contacts_found,
        opened=opened, steps=steps,
    )


# ===========================================================================
# 内部辅助：OCR 联系人 / 消息列表
# ===========================================================================

def _ocr_contact_list(device, region=None, min_len=2, min_conf=0.35):
    """OCR 识别微信界面中的联系人名称列表。过滤掉系统标签和短文本。"""
    boxes = ocr_boxes(device, region=region, min_conf=min_conf)
    SKIP = [
        "新的朋友", "群聊", "标签", "公众号",
        "微信", "通讯录", "发现", "我",
        "搜索", "添加", "企业微信",
        "服务", "小程序", "视频号", "看一看",
        "搜一搜", "朋友圈", "收藏", "卡包",
        "设置", "表情", "拍一拍",
    ]
    results = []
    for text, cx, cy, conf in boxes:
        text = text.strip()
        if len(text) < min_len:
            continue
        if any(s in text for s in SKIP):
            continue
        if re.match(r'^[\d\s\W_]+$', text):
            continue
        results.append((text, cx, cy, conf))
    return results


def _ocr_message_list(device, region=None, min_len=2, min_conf=0.30):
    """OCR 识别微信聊天界面的消息气泡文本。过滤掉时间戳和系统提示。"""
    boxes = ocr_boxes(device, region=region, min_conf=min_conf)
    SKIP = [
        "发送消息", "按住 说话", "发送",
        "你已添加了", "以上是打招呼",
        "对方正在输入", "撤回了一条消息",
    ]
    results = []
    for text, cx, cy, conf in boxes:
        text = text.strip()
        if len(text) < min_len:
            continue
        if any(s in text for s in SKIP):
            continue
        if re.match(r'^[\d:/\-\s]+$', text):
            continue
        results.append((text, cx, cy, conf))
    return results
