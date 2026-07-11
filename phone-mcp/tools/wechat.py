# -*- coding: utf-8 -*-
"""微信集成工具：打开聊天、发送消息。"""
import os, time, re

from adb import run_adb, log, with_retry, resolve_device
from utils import ok, fail, text_block
from tools._shared import SHOT_DIR
from tools.vision import smart_find, _get_ui_xml, _top_pkg, _screen_size
from tools.ui import t_launch_app
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

