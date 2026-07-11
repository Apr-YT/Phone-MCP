# -*- coding: utf-8 -*-
"""诊断：切到微信 Tab 后，搜索图标坐标是否真能打开搜索。"""
import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
DEV = server.resolve_device(None)
SHOT = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"
W, H = server._screen_size(DEV)

def boxes(tag, region=None):
    bs = server.ocr_boxes(DEV, region=region, min_conf=0.2)
    print("\n=== %s 共%d块 ===" % (tag, len(bs)))
    for t, cx, cy, c in sorted(bs, key=lambda b: b[2]):
        print("  y=%-4d x=%-4d conf=%.2f  %r" % (cy, cx, c, t))
    return bs

def shot(name):
    ok = server.run_adb(["exec-out", "screencap", "-p"], device=DEV, capture=True, binary=True)
    p = os.path.join(SHOT, name)
    with open(p, "wb") as f:
        f.write(ok.stdout)
    print("[截图] %s" % p)

print("screen %dx%d" % (W, H))
server.t_launch_app({"package":"com.tencent.mm","deviceSerial":DEV})
time.sleep(1.5)
# 切到微信 Tab（底部最左）
server.run_adb(["shell","input","tap","150",str(int(H*0.965))], device=DEV, mutating=True)
time.sleep(1.0)
shot("diag_wechat.png")
boxes("微信Tab全部", None)

print("\n>>> 点搜索图标 (%d,%d)" % (int(W*0.91), int(H*0.03)))
server.run_adb(["shell","input","tap",str(int(W*0.91)),str(int(H*0.03))], device=DEV, mutating=True)
time.sleep(1.5)
shot("diag_wechat_search.png")
boxes("搜索页全部", None)
# 是否出现搜索提示
print("搜索提示出现:", server._ocr_sees(DEV, "搜索", region=[0,0,1,0.2]))
