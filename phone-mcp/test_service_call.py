import sys, time
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server
from server import resolve_device, run_adb, _wechat_ensure_home, _screen_size, ocr_boxes, _search_opened

DEV = resolve_device(None)
W, H = _screen_size(DEV)

def enc16(text):
    return text.encode("utf-16-le").hex() + "0000"

def service_set(text):
    hx = enc16(text)
    run_adb(["shell", "service", "call", "clipboard", "2", "s16", hx],
            device=DEV, mutating=True)
    return hx

def cmd_set(text):
    run_adb(["shell", "cmd", "clipboard", "set", text], device=DEV, mutating=True)

def paste():
    run_adb(["shell", "input", "keyevent", "279"], device=DEV, mutating=True)

def top_texts():
    return [b[0] for b in ocr_boxes(DEV, region=[0, 0.0, 1, 0.20])]

print("=== foreground:", server._top_pkg(DEV))
server._wechat_ensure_home(DEV)
time.sleep(0.8)

# ---- 搜索框场景：纯中文 / 中英文混合 / emoji ----
run_adb(["shell", "input", "tap", str(int(W*0.83)), str(int(H*0.07))], device=DEV, mutating=True)
time.sleep(1.0)
print("search_opened:", _search_opened(DEV))

cases_search = [
    ("纯中文", "纯中文测试一二三"),
    ("中英文混合", "中英文Mix123测试"),
    ("emoji", "测试😀表情"),
]
for label, txt in cases_search:
    # 先清空搜索框（如果上次有残留）
    run_adb(["shell", "input", "tap", str(int(W*0.5)), str(int(H*0.07))], device=DEV, mutating=True)
    time.sleep(0.2)
    service_set(txt)
    time.sleep(0.3)
    paste()
    time.sleep(0.6)
    seen = top_texts()
    flat = " ".join(seen)
    ok = txt in flat or any(c in flat for c in txt if c.strip())
    # 检查是否至少出现了部分文字（OCR 不完全）
    hit = any(txt[:3] in s for s in seen)
    print(f"  [service] {label}: paste_seen_hit={hit}  top_texts={seen[:6]}")

# ---- 聊天框场景：多行换行 ----
print("\n=== 进入与 向远钦 的聊天测试多行 ===")
server._wechat_ensure_home(DEV)
time.sleep(0.6)
run_adb(["shell", "input", "tap", str(int(W*0.83)), str(int(H*0.07))], device=DEV, mutating=True)
time.sleep(1.0)
run_adb(["shell", "input", "tap", str(int(W*0.5)), str(int(H*0.07))], device=DEV, mutating=True)
time.sleep(0.3)
server.t_input_chinese({"text": "向远钦", "deviceSerial": DEV})
time.sleep(1.0)
hits = server.ocr_match_contact("向远钦", DEV, region=[0, 0.12, 1, 0.6])
if hits:
    _, cx, cy, _ = hits[0]
    run_adb(["shell", "input", "tap", str(cx), str(cy)], device=DEV, mutating=True)
    time.sleep(1.2)
# focus input
run_adb(["shell", "input", "tap", "400", str(int(H*0.96))], device=DEV, mutating=True)
time.sleep(0.5)
multi = "第一行内容\n第二行内容"
service_set(multi)
time.sleep(0.3)
paste()
time.sleep(0.8)
seen = [b[0] for b in ocr_boxes(DEV, region=[0, 0.82, 1, 1.0])]
print("  [service] 多行: bottom_texts=", seen[:8])
# 清理：不发送，清空输入框
print("=== 清理（不发送），回到主页 ===")
server._wechat_ensure_home(DEV)
print("DONE")
