# -*- coding: utf-8 -*-
"""ocr_find 崩溃根因诊断脚本（独立运行，不影响 server.py 主逻辑）。

覆盖三类排查：
  1) 图片读取失败 / 路径非法（中文、空格、格式）
  2) 模型加载错误 / 依赖缺失
  3) 引擎调用崩溃（捕获完整 traceback，实证是否跨线程 onnxruntime 会话失效）
"""
import os, sys, traceback, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = os.path.join(HERE, "shots")
os.makedirs(SHOT_DIR, exist_ok=True)
DEVICE = os.environ.get("PHONE_MCP_DEVICE") or "134d2f8"

print("=" * 70)
print("【0】环境 / 路径基本信息")
print("=" * 70)
print("python      :", sys.version.split()[0], sys.executable)
print("SHOT_DIR    :", SHOT_DIR)

# 把 server 加进 import 路径（复用 _ocr_screenshot 与 ocr_find）
sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# 1) 依赖与模型加载检查
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("【1】依赖与模型加载检查（确认是否\"依赖缺失\"）")
print("=" * 70)

deps = {
    "cv2": "opencv-python",
    "numpy": "numpy",
    "PIL": "Pillow",
    "onnxruntime": "onnxruntime",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
}
for mod, pkg in deps.items():
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "?")
        print("  [OK]   %-22s %-28s version=%s" % (mod, "(" + pkg + ")", ver))
    except Exception as e:
        print("  [MISS] %-22s %-28s -> %s: %s" % (mod, "(" + pkg + ")", type(e).__name__, e))

# RapidOCR 模型文件是否存在（det/rec/cls 的 onnx）
try:
    import rapidocr_onnxruntime as r
    p = os.path.dirname(r.__file__)
    print("  RapidOCR 包路径 :", p)
except Exception as e:
    print("  RapidOCR 包路径 : 无法定位 ->", e)

# ---------------------------------------------------------------------------
# 2) 截图路径 / 格式合法性检查（确认是否\"图片读取失败\"）
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("【2】截图路径 / 格式合法性检查（确认是否\"图片读取失败\"）")
print("=" * 70)

import server  # 复用 server 的截图逻辑

# 现截一张图
shot = server._ocr_screenshot(DEVICE, None)
if not shot:
    print("  [FAIL] 截图失败：_ocr_screenshot 返回 None（adb/screencap 层面问题）")
    sys.exit(1)
path, scale, off_x, off_y = shot
print("  截图保存路径 :", path)

# 路径是否含非 ASCII / 空格
non_ascii = [c for c in path if ord(c) > 127]
has_space = " " in path
print("  含非ASCII字符 :", non_ascii if non_ascii else "无")
print("  含空格        :", has_space)
print("  路径合法(ASCII/无空格) :", not non_ascii and not has_space)

# 文件基本属性
st = os.stat(path)
print("  文件大小      :", st.st_size, "bytes")

# PNG 魔数
with open(path, "rb") as f:
    magic = f.read(8)
print("  文件头魔数    :", magic, "->", "合法PNG" if magic.startswith(b"\x89PNG") else "非PNG!")

# cv2 能否读取 + 尺寸/通道
import cv2
img = cv2.imread(path)
if img is None:
    print("  [FAIL] cv2.imread 返回 None -> 图片读取失败（即使文件存在）")
else:
    h, w = img.shape[:2]
    ch = img.shape[2] if img.ndim == 3 else 1
    print("  cv2 读取成功  : 尺寸=%dx%d 通道=%d" % (w, h, ch))
    print("  通道=4(BGRA)需转BGR :", ch == 4)

# ---------------------------------------------------------------------------
# 3) 引擎独立最小测试 + 根因实证（跨线程对照）
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("【3】RapidOCR 最小测试 + 跨线程对照（确认崩溃根因）")
print("=" * 70)

from rapidocr_onnxruntime import RapidOCR
import threading

def engine_call(reader, label):
    """在【当前调用线程】执行 reader(path)，捕获完整 traceback。

    仅包装 reader(path) 调用本身；打印另做，确保 reader 调用真的抛异常
    才报 CRASH，避免上一版把打印格式化错误误判为引擎崩溃。
    """
    try:
        out = reader(path)          # RapidOCR 返回 (result, elapse)；elapse 可能是 list
        call_ok = True
        call_tb = None
    except Exception as e:
        call_ok = False
        call_tb = traceback.format_exc()
    # 下面只是展示，失败也不影响判定
    if call_ok:
        try:
            result = out[0]
            elapse = out[1]
            n = len(result) if result else 0
            sample = [t for _, t, _ in (result or [])][:5]
            print("  [%s] OK  结果数=%d elapse=%s 样例=%s"
                  % (label, n, elapse if not isinstance(elapse, list) else elapse, sample))
        except Exception as e:
            print("  [%s] OK(reader调用成功) 但结果解析异常: %s" % (label, type(e).__name__))
        return True, None
    else:
        print("  [%s] CRASH reader()抛异常=%s" % (label, call_tb.splitlines()[-1] if call_tb else "?"))
        print("  ---- 完整 traceback ----")
        print(call_tb)
        return False, call_tb

# 3a) 主线程建 + 主线程调用（对照：应当正常）
print("  3a) 主线程创建 reader，主线程调用：")
r_main = RapidOCR()
engine_call(r_main, "主线程/主线程")

# 3b) 后台线程建 + 主线程调用（复现之前 MCP 的症状：session 跨线程失效）
print("  3b) 后台线程创建 reader，主线程调用（复现 MCP 旧 bug）：")
box = {}
def build_in_thread():
    try:
        box["reader"] = RapidOCR()
    except Exception as e:
        box["err"] = traceback.format_exc()
t = threading.Thread(target=build_in_thread, daemon=True)
t.start()
t.join(timeout=60)
if "err" in box:
    print("  后台线程构建 reader 本身崩溃：")
    print(box["err"])
else:
    r_thread = box.get("reader")
    if r_thread is None:
        print("  [FAIL] 后台线程未返回 reader")
    else:
        engine_call(r_thread, "后台线程/主线程")

# 3c) 直接复现 ocr_find 内部那一行 reader(image_path)，并强制打印 traceback
print("  3c) 复现 ocr_find 内部 reader(image_path) 调用（主线程 reader）：")
ok, tb = engine_call(r_main, "ocr_find内部调用")
if not ok:
    print("\n  >>> 结论：ocr_find 崩溃类别见上方 traceback。")

# ---------------------------------------------------------------------------
# 4) 直接调用 server.ocr_find 做端到端验证
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("【4】直接调用 server.ocr_find 端到端（查询已知文字）")
print("=" * 70)
try:
    hits = server.ocr_find("文件传输助手", path, scale, off_x=off_x, off_y=off_y, exact=False)
    print("  ocr_find 返回命中数:", len(hits))
    for h in hits:
        print("    命中:", h)
except Exception as e:
    print("  ocr_find 抛异常（未被内部捕获）：")
    print(traceback.format_exc())

print("\n诊断完成。")
