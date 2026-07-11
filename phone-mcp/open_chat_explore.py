# -*- coding: utf-8 -*-
"""Phase 1: 用 OCR 打开「向远钦」聊天界面并截图验证，不输入/不发送。"""
import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
device = server.resolve_device(None)
SHOT = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"


def save_shot(name):
    ok = server.run_adb(["exec-out", "screencap", "-p"], device=device, capture=True, binary=True)
    p = os.path.join(SHOT, name)
    with open(p, "wb") as f:
        f.write(ok.stdout)
    print("  截图 ->", p)
    return p


def ocr_boxes(region=None, min_conf=0.3):
    shot = server._ocr_screenshot(device, region)
    if not shot:
        return []
    path, scale, off_x, off_y = shot
    reader = server.get_ocr_reader()
    result, _ = reader(path)
    out = []
    for bbox, txt, conf in (result or []):
        try:
            conf = float(conf)
        except Exception:
            conf = 0.0
        if conf < min_conf:
            continue
        xs = [p[0] for p in bbox]; ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2 / scale) + off_x
        cy = int((min(ys) + max(ys)) / 2 / scale) + off_y
        out.append((txt, cx, cy, conf))
    return out


def find_tap(query, strategy="lowest", region=None, min_conf=0.3):
    boxes = [(t, cx, cy, c) for (t, cx, cy, c) in ocr_boxes(region, min_conf) if query in t]
    if not boxes:
        print("  [未找到] %r" % query)
        return None
    boxes.sort(key=lambda b: b[2])
    sel = boxes[-1] if strategy == "lowest" else boxes[0]
    print("  找到 %r @ (%d,%d) conf=%.2f (共%d处)" % (sel[0], sel[1], sel[2], sel[3], len(boxes)))
    server.run_adb(["shell", "input", "tap", str(sel[1]), str(sel[2])], device=device, mutating=True)
    return sel


def in_chat_with(name, timeout=4.0):
    """轮询 OCR：聊天头部出现 name 即视为进入正确聊天。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        boxes = ocr_boxes(min_conf=0.25)
        top = [b for b in boxes if b[2] < 500 and name in b[0]]
        if top:
            print("  头部识别到 %r @ y=%d" % (name, top[0][2]))
            return True
        time.sleep(0.6)
    return False


print("== 确保微信前台 ==")
cur = server.t_get_current_app({"deviceSerial": device})
pkg = cur.get("data", {}).get("package") if isinstance(cur, dict) else None
if pkg != "com.tencent.mm":
    server.t_launch_app({"package": "com.tencent.mm", "deviceSerial": device})
    time.sleep(2.5)
save_shot("phase1_home.png")
print("HOME OCR 概览:")
for b in ocr_boxes(min_conf=0.25)[:40]:
    print("   ", b)

print("\n== 尝试在首页直接找到聊天行 向远钦 ==")
hit = find_tap("向远钦", strategy="lowest")
time.sleep(1.5)
if in_chat_with("向远钦"):
    save_shot("phase1_chat.png")
    print("\n[OK] 首页即进入 向远钦 聊天")
    sys.exit(0)

print("\n== 首页未找到，走搜索 ==")
find_tap("搜索", strategy="lowest")
time.sleep(1.2)
# 聚焦搜索框（再点一次 搜索 提示）
find_tap("搜索", strategy="lowest")
time.sleep(0.8)
# 粘贴查询
server.t_input_text({"text": "向远钦", "deviceSerial": device})
time.sleep(1.2)
save_shot("phase1_search.png")
print("SEARCH OCR 概览:")
for b in ocr_boxes(min_conf=0.25)[:40]:
    print("   ", b)
# 点结果（最低处，避开顶部搜索框里的输入文字）
find_tap("向远钦", strategy="lowest")
time.sleep(1.5)
if in_chat_with("向远钦"):
    save_shot("phase1_chat.png")
    print("\n[OK] 经搜索进入 向远钦 聊天")
    sys.exit(0)

save_shot("phase1_fail.png")
print("\n[x] 未能确认进入 向远钦 聊天，已停止（未发送）。")
sys.exit(2)
