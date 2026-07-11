# -*- coding: utf-8 -*-
"""验证 phone_input_chinese 剪贴板写入+粘贴（中文/混合/标点空格）+ 打通 send 全流程。
用连接器的 venv python 运行（已含 rapidocr_onnxruntime）。"""
import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
DEV = server.resolve_device(None)
W, H = server._screen_size(DEV)
SHOT = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"
os.makedirs(SHOT, exist_ok=True)
print("设备: %s  尺寸: %dx%d" % (DEV, W, H))


def snap(name):
    try:
        ok = server.run_adb(["exec-out", "screencap", "-p"], device=DEV, capture=True, binary=True)
        with open(os.path.join(SHOT, name), "wb") as f:
            f.write(ok.stdout)
    except Exception:
        pass


def open_search_focus():
    """回主页并打开微信搜索、聚焦搜索框，准备接收粘贴。"""
    server._wechat_ensure_home(DEV)
    server.run_adb(["shell", "input", "tap", str(int(W * 0.83)), str(int(H * 0.07))],
                   device=DEV, mutating=True)
    time.sleep(0.7)
    server.run_adb(["shell", "input", "tap", str(int(W * 0.5)), str(int(H * 0.07))],
                   device=DEV, mutating=True)
    time.sleep(0.3)


def ocr_field_tokens():
    # 粘贴内容落在顶部搜索栏(y≈0.04~0.16)，不是下方的搜索结果列表
    boxes = server.ocr_boxes(DEV, region=[0, 0.04, 1, 0.16])
    return [b[0] for b in boxes]


# ---------------------------------------------------------------------------
# 1) 三类文本：写入+粘贴+OCR 校验
# ---------------------------------------------------------------------------
cases = [
    ("纯中文",     "微信中文输入测试",            ["微信", "中文", "输入"]),
    ("中英文混合", "Hello 世界 mix 123",          ["Hello", "mix", "世界"]),
    ("带标点空格", "你好，world! 测试 123？",     ["你好", "world", "测试"]),
]

all_ok = True
for name, text, tokens in cases:
    open_search_focus()
    t0 = time.time()
    res = server.t_input_chinese({"text": text, "deviceSerial": DEV})
    time.sleep(0.3)
    field = ocr_field_tokens()
    found = [t for t in tokens if any(t in s for s in field)]
    passed = res.get("success") and len(found) > 0
    all_ok = all_ok and passed
    d = res.get("data", {})
    print("[%s] 耗时%.1fs write=%s paste=%s method=%s verified=%s ocr命中=%s -> %s"
          % (name, time.time() - t0, d.get("written"), d.get("pasted"),
             d.get("method"), d.get("verified"), found, "PASS" if passed else "FAIL"))
    # 退出搜索框，准备下一轮
    server.run_adb(["shell", "input", "keyevent", "4"], device=DEV, mutating=True)
    time.sleep(0.5)

# ---------------------------------------------------------------------------
# 2) 打通 send_wechat_message 全流程（带标点空格的消息）
# ---------------------------------------------------------------------------
print("\n=== 全流程 send_wechat_message（消息含标点空格）===")
snap("clip_before.png")
t0 = time.time()
r = server.t_send_wechat_message({"contact_name": "向远钦", "message": "你好，world! 测试 123",
                                  "deviceSerial": DEV})
dt = time.time() - t0
snap("clip_after.png")
print("success :", r["success"])
print("message :", r["message"])
print("steps   :", r.get("data", {}).get("steps"))
print("耗时    : %.1fs" % dt)
if r["success"]:
    print("✅ 全流程发送成功（含标点空格消息）")
else:
    print("❌ 全流程发送失败")
    all_ok = False

print("\n==== 总结 ====")
print("输入三类文本 + 全流程发送: %s" % ("全部通过 ✅" if all_ok else "存在失败 ❌"))
