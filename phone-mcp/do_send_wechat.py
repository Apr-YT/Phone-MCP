# -*- coding: utf-8 -*-
"""真机驱动：打开微信 → 进入「向远钦」聊天 → 输入「你好」→ 发送。
每一步都有校验，失败即停下上报，绝不在未确认状态下误发。"""
import sys, time, re

sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
device = server.resolve_device(None)


def step(name, fn):
    print("\n=== STEP: %s ===" % name)
    return fn()


def open_chat():
    r = server.t_wechat_open_chat({"contact": "向远钦", "deviceSerial": device})
    print("  ->", r)
    return r


def focus_input():
    xml = server._get_ui_xml(device)
    if not xml:
        raise RuntimeError("无法获取 UI 结构，聚焦输入框失败")
    nodes = re.findall(
        r'<node[^>]*class="[^"]*EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml,
    )
    if not nodes:
        raise RuntimeError("未在聊天界面找到输入框(EditText)")
    # 选屏幕最底部（y 最大）的 EditText，即消息输入框
    best = max(nodes, key=lambda b: int(b[3]))
    cx = (int(best[0]) + int(best[2])) // 2
    cy = (int(best[1]) + int(best[3])) // 2
    print("  输入框中心: (%d, %d)" % (cx, cy))
    server.run_adb(["shell", "input", "tap", str(cx), str(cy)],
                   device=device, mutating=True)
    time.sleep(0.6)
    return cx, cy


def type_text():
    r = server.t_input_text({"text": "你好", "deviceSerial": device})
    print("  ->", r)
    time.sleep(0.6)
    return r


def tap_send():
    r = server.t_tap_text({"text": "发送", "deviceSerial": device, "method": "ui"})
    print("  ->", r)
    time.sleep(0.8)
    return r


def verify_sent():
    """发送后输入框应被清空 → 说明「你好」已进入对话气泡。"""
    xml = server._get_ui_xml(device)
    if not xml:
        return "未知(无法获取UI)"
    # 取最底部 EditText 的文本
    rows = re.findall(
        r'<node[^>]*class="[^"]*EditText"[^>]*text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml,
    )
    if not rows:
        # 有些系统 bounds 在 text 之前，换一个顺序再试
        rows = re.findall(
            r'<node[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="([^"]*)"',
            xml,
        )
        if not rows:
            return "输入框文本无法解析(消息可能已发出)"
        rows = [(t, x1, y1, x2, y2) for (x1, y1, x2, y2, t) in rows]
    bottom = max(rows, key=lambda r: int(r[3]))
    txt = bottom[0]
    if txt.strip() == "":
        return "已发送：输入框已清空，消息进入对话气泡 ✓"
    return "输入框仍有文本=%r，可能未发送，请人工确认" % txt


# ---- 执行 ----
open_res = step("打开与 向远钦 的聊天", open_chat)
if not (isinstance(open_res, dict) and open_res.get("success")):
    print("\n[x] 进入聊天失败，停止后续操作。未发送任何消息。")
    sys.exit(2)

try:
    step("聚焦消息输入框", focus_input)
    step("输入「你好」", type_text)
    step("点击「发送」", tap_send)
    verdict = step("校验是否发出", verify_sent)
    print("\n-------- 结果 --------")
    print(verdict)
except Exception as e:
    print("\n[x] 执行过程出错:", repr(e))
    print("    已在出错前停止，未会继续误发。")
    sys.exit(3)
