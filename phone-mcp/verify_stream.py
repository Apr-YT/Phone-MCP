#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证 minicap 等价流式截图子系统（root 直连，无弹窗）。"""
import os, sys, time, json, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

D = server.resolve_device(None)
print("device =", D)

Adb = server.ADB
def adb(args):
    subprocess.run([Adb, "-s", D] + args, capture_output=True, text=True, timeout=30)

# 1) 唤醒 + 打开设置页（保证有中文文字可 OCR）
t0 = time.time()
adb(["shell", "input", "keyevent", "224"])           # WAKEUP
adb(["shell", "am", "start", "-a", "android.settings.SETTINGS"])
time.sleep(1.2)
print("[wake+launch] %.2fs" % (time.time() - t0))

# 2) phone_cap_sync（banner）
t0 = time.time()
r = server.t_cap_sync({"deviceSerial": D})
dt = time.time() - t0
print("[cap_sync] %.2fs ->" % dt, r["data"] if r.get("success") else r)

# 3) phone_screenshot_stream（单帧 root 截图）
t0 = time.time()
r = server.t_screenshot_stream({"deviceSerial": D})
dt = time.time() - t0
ok = r.get("success")
path = r["data"].get("path") if ok else None
exists = os.path.exists(path) if path else False
print("[screenshot_stream] %.2fs ok=%s bytes=%s exists=%s path=%s"
      % (dt, ok, r["data"].get("bytes") if ok else "-", exists, path))

# 4) 现有 OCR 工具(root 截图) 是否仍正常 —— ocr_boxes
t0 = time.time()
boxes = server.ocr_boxes(D, min_conf=0.3)
dt = time.time() - t0
print("[ocr_boxes(现有工具, root)] %.2fs 识别文字块数=%d" % (dt, len(boxes)))
sample = boxes[:8]
print("  样例:", [(t, round(c, 2)) for t, _, _, c in sample])

# 5) phone_stream_start（持续截帧流）
t0 = time.time()
r = server.t_stream_start({"deviceSerial": D, "fps": 4})
print("[stream_start] %.2fs ->" % (time.time() - t0), r["message"])
time.sleep(1.6)   # 让流抓若干帧
stream_dir = os.path.join(server.SHOT_DIR, "stream")
frames = sorted(f for f in os.listdir(stream_dir)) if os.path.isdir(stream_dir) else []
print("  流目录帧数(>=1 即通过):", len(frames), frames[:3])

# 6) phone_ocr_stream 取最新帧 OCR（无 query）
t0 = time.time()
r = server.t_ocr_stream({"deviceSerial": D})
dt = time.time() - t0
ok = r.get("success")
cnt = r["data"].get("count", 0) if ok else 0
print("[ocr_stream(无query)] %.2fs ok=%s 文字块=%d" % (dt, ok, cnt))
print("  样例:", r["data"].get("boxes", [])[:8] if ok else r)

# 7) phone_ocr_stream 带 query（用刚识别到的某个文字做精确匹配校验）
if ok and cnt:
    q = r["data"]["boxes"][0]["text"]
    t0 = time.time()
    r2 = server.t_ocr_stream({"deviceSerial": D, "query": q, "exact": True})
    dt = time.time() - t0
    ok2 = r2.get("success")
    hits = r2["data"].get("hitCount", 0) if ok2 else 0
    print("[ocr_stream(query=%r, exact)] %.2fs ok=%s 命中=%d"
          % (q, dt, ok2, hits))

# 8) phone_stream_stop
t0 = time.time()
r = server.t_stream_stop({"deviceSerial": D})
print("[stream_stop] %.2fs ->" % (time.time() - t0), r["message"])

print("\nALL DONE")
