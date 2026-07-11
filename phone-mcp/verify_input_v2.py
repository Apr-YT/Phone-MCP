import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server
from server import (resolve_device, run_adb, _wechat_ensure_home, _screen_size,
                    ocr_boxes, _search_opened, ocr_match_contact, wechat_clear_input,
                    t_input_text, t_send_wechat_message, _ocr_screenshot,
                    _wechat_foreground, _input_region_has)

DEV = resolve_device(None)
W, H = _screen_size(DEV)
SHOTS = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"
os.makedirs(SHOTS, exist_ok=True)

def snap(name):
    p, _, _, _ = _ocr_screenshot(DEV)
    dst = os.path.join(SHOTS, name)
    if p and os.path.exists(p):
        import shutil
        shutil.copy(p, dst)
    return dst

def open_search():
    run_adb(["shell","input","tap",str(int(W*0.83)),str(int(H*0.07))],device=DEV,mutating=True)
    time.sleep(1.0)

def exit_to_home():
    for _ in range(3):
        if _wechat_foreground(DEV) and _ocr_sees_home():
            break
        run_adb(["shell","input","keyevent","4"],device=DEV,mutating=True)
        time.sleep(0.6)

def _ocr_sees_home():
    return server._ocr_sees(DEV, "微信", region=[0,0.9,1,1.0]) or \
           server._ocr_sees(DEV, "通讯录", region=[0,0.9,1,1.0])

CASES = [
    ("纯中文", "纯中文测试一二三"),
    ("中英文混合", "中英文Mix123测试"),
    ("emoji", "你好😀表情测试"),
    ("多行换行", "第一行内容\n第二行内容"),
]

print("=" * 64)
print("Part A：搜索框场景（4 类文本，不发送）")
print("=" * 64)
_wechat_ensure_home(DEV); time.sleep(0.8)
open_search()
for label, txt in CASES:
    wechat_clear_input(DEV, "search")
    r = t_input_text({"text": txt, "deviceSerial": DEV, "field": "search"})
    d = r.get("data") or {}
    # OCR 复验：搜索栏区域[0,0.06,1,0.16]（排除状态栏）是否有真实内容
    boxes = ocr_boxes(DEV, region=[0,0.06,1,0.16], min_conf=0.2)
    real = [b[0] for b in boxes if b[0].strip()
            and not any(p in b[0] for p in server._PLACEHOLDER)]
    ocr_ok = _input_region_has(DEV, "search", txt)
    print("  [%-6s] success=%-5s method=%-16s 内部OCR校验=%-4s 区域复验=%s"
          % (label, r["success"], d.get("method"), d.get("verified"), ocr_ok))
    print("           输入=%r  区域文字=%s" % (txt, real[:4]))
print("  -> 退出搜索回主页")
exit_to_home()

print("")
print("=" * 64)
print("Part B：聊天输入框场景（4 类文本，不发送）")
print("=" * 64)
_wechat_ensure_home(DEV); time.sleep(0.6)
open_search()
run_adb(["shell","input","tap",str(int(W*0.5)),str(int(H*0.07))],device=DEV,mutating=True)
time.sleep(0.3)
t_input_text({"text":"向远钦","deviceSerial":DEV,"field":"search"})
time.sleep(1.0)
hits = ocr_match_contact("向远钦", DEV, region=[0,0.12,1,0.6])
if not hits:
    print("  !! 未匹配到 向远钦，终止 Part B"); exit_to_home()
else:
    _, cx, cy, _ = hits[0]
    run_adb(["shell","input","tap",str(cx),str(cy)],device=DEV,mutating=True)
    time.sleep(1.2)
    # focus 输入框
    run_adb(["shell","input","tap","400",str(int(H*0.96))],device=DEV,mutating=True)
    time.sleep(0.5)
    for label, txt in CASES:
        wechat_clear_input(DEV, "chat")
        r = t_input_text({"text": txt, "deviceSerial": DEV, "field": "chat"})
        d = r.get("data") or {}
        boxes = ocr_boxes(DEV, region=[0,0.82,1,1.0], min_conf=0.2)
        real = [b[0] for b in boxes if b[0].strip()
                and not any(p in b[0] for p in server._PLACEHOLDER)]
        ocr_ok = _input_region_has(DEV, "chat", txt)
        print("  [%-6s] success=%-5s method=%-16s 内部OCR校验=%-4s 区域复验=%s"
              % (label, r["success"], d.get("method"), d.get("verified"), ocr_ok))
        print("           输入=%r  区域文字=%s" % (txt, real[:4]))
    # 清空，不发送，返回主页
    wechat_clear_input(DEV, "chat")
    print("  -> 清空输入框，不发送，返回主页")
    exit_to_home()

print("")
print("=" * 64)
print("Part C：全流程闭环发送（真实发送）")
print("  收件人=向远钦  消息=你好，这是自动化测试消息😀，来自 phone MCP")
print("=" * 64)
msg = "你好，这是自动化测试消息😀，来自 phone MCP"
# 发送前截图（主页）
before = snap("e2e_before.png")
t0 = time.time()
res = t_send_wechat_message({"contact_name":"向远钦","message":msg,"deviceSerial":DEV})
dt = time.time() - t0
after = snap("e2e_after.png")
data = res.get("data") or {}
print("success=%s  sent=%s  method=%s" % (res["success"], data.get("sent"), data.get("total_seconds")))
print("总耗时(脚本侧)=%.1fs  工具自报total_seconds=%s" % (dt, data.get("total_seconds")))
print("--- 每步执行日志 + 耗时 ---")
for s in data.get("steps", []):
    print("  " + s)
# 手机端确认：发送后聊天区出现该消息气泡且输入框清空
in_chat = any(msg in b[0] for b in ocr_boxes(DEV, region=[0,0.12,1,0.85], min_conf=0.25))
in_input = server._ocr_sees(DEV, msg, region=[0,0.9,1,1.0])
phone_confirm = in_chat and not in_input
print("--- 手机端实际发送确认 ---")
print("  聊天区出现消息气泡: %s" % in_chat)
print("  输入框已清空(不再含该消息): %s" % (not in_input))
print("  => 发送成功确认: %s" % phone_confirm)
print("  截图: before=%s  after=%s" % (before, after))
