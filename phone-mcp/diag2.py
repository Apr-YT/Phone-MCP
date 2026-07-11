# -*- coding: utf-8 -*-
"""诊断：微信主页 / 搜索打开后 / 输入联系人后 的真实布局（打印全部 OCR 块坐标）。"""
import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
DEV = server.resolve_device(None)
SHOT = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"
W, H = server._screen_size(DEV)
print("screen %dx%d" % (W, H))

def boxes(tag, region=None):
    bs = server.ocr_boxes(DEV, region=region, min_conf=0.2)
    print("\n=== %s (区域=%s) 共%d块 ===" % (tag, region, len(bs)))
    for t, cx, cy, c in sorted(bs, key=lambda b: b[2]):
        print("  y=%-4d x=%-4d conf=%.2f  %r" % (cy, cx, c, t))
    return bs

def shot(name):
    ok = server.run_adb(["exec-out", "screencap", "-p"], device=DEV, capture=True, binary=True)
    p = os.path.join(SHOT, name)
    with open(p, "wb") as f:
        f.write(ok.stdout)
    print("[截图] %s" % p)

print("\n>>> 1) 回主页")
server._wechat_ensure_home(DEV)
time.sleep(1.0)
shot("diag_home.png")
boxes("主页全部", None)

print("\n>>> 2) 点击搜索图标 (%d,%d)" % (int(W*0.91), int(H*0.03)))
server.run_adb(["shell","input","tap",str(int(W*0.91)),str(int(H*0.03))], device=DEV, mutating=True)
time.sleep(1.5)
shot("diag_search.png")
boxes("搜索页全部", None)
boxes("搜索页顶部[0,0,1,0.2]", [0,0,1,0.2])

print("\n>>> 3) 粘贴联系人 向远钦")
server.t_input_chinese({"text":"向远钦","deviceSerial":DEV})
time.sleep(1.5)
shot("diag_typed.png")
boxes("输入后全部", None)
boxes("结果区[0,0.1,1,0.6]", [0,0.10,1,0.6])
