# -*- coding: utf-8 -*-
"""诊断：打印微信主页(微信 Tab)的完整 OCR 坐标，定位搜索入口与底部 Tab。"""
import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
DEV = server.resolve_device(None)
SHOT = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"
os.makedirs(SHOT, exist_ok=True)

def snap(name):
    try:
        ok = server.run_adb(["exec-out", "screencap", "-p"], device=DEV, capture=True, binary=True)
        with open(os.path.join(SHOT, name), "wb") as f:
            f.write(ok.stdout)
    except Exception as e:
        print("   [截图失败] %r" % e)

w, h = server._screen_size(DEV)
print("屏幕: %dx%d" % (w, h))

# 1) 确保微信 Tab
print("\n[启动微信 + 回主页]")
server._wechat_ensure_home(DEV)
time.sleep(0.8)
# 显式点微信 Tab(左下)确保处于微信主页而非通讯录
server.run_adb(["shell", "input", "tap", "150", str(int(h * 0.965))], device=DEV, mutating=True)
time.sleep(1.2)

snap("diag_home.png")
boxes = server.ocr_boxes(DEV, min_conf=0.2)
print("\n[微信主页完整 OCR 盒子] (text, cx, cy, conf)")
for txt, cx, cy, conf in sorted(boxes, key=lambda b: b[2]):
    print("  %-16s @ (%4d, %4d)  conf=%.2f" % (txt, cx, cy, conf))

# 2) 探测搜索图标：在右上动作栏区域尝试若干候选坐标，检测是否打开搜索
print("\n[探测搜索图标候选坐标] (点候选 -> 检测顶部是否变化 -> 返回)")
candidates = [(int(w*0.91), int(h*0.05)), (int(w*0.91), int(h*0.07)),
              (int(w*0.88), int(h*0.06)), (int(w*0.85), int(h*0.08)),
              (int(w*0.9), int(h*0.09)), (int(w*0.9), int(h*0.11)),
              (int(w*0.83), int(h*0.07))]
# 先记录主页顶部文字
def top_texts():
    b = server.ocr_boxes(DEV, region=[0, 0.0, 1, 0.18], min_conf=0.2)
    return set(t.strip() for t, *_ in b)

home_top = top_texts()
print("主页顶部文字:", home_top)
for (cx, cy) in candidates:
    server.run_adb(["shell", "input", "tap", str(cx), str(cy)], device=DEV, mutating=True)
    time.sleep(1.0)
    after = top_texts()
    opened = ("微信" not in after) or (after != home_top and len(after) != len(home_top))
    print("  候选 (%4d,%4d) -> 顶部文字=%s  | 可能打开搜索=%s" % (cx, cy, after, opened))
    # 返回重置
    server.run_adb(["shell", "input", "keyevent", "4"], device=DEV, mutating=True)
    time.sleep(0.8)
    # 确保在微信 Tab
    server.run_adb(["shell", "input", "tap", "150", str(int(h * 0.965))], device=DEV, mutating=True)
    time.sleep(0.8)

print("\n诊断结束。")
