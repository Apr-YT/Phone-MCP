# -*- coding: utf-8 -*-
"""验证 phone_input_text（方案二 UiAutomator 升级）：
A) 三类文本（纯中文 / 中英文混合 / 带标点空格）输入，确认剪贴板写入+粘贴正常；
B) 打通到 send_wechat_message 全流程，确认消息正确输入并发送成功，并打印每步输入方式；
C) 探测微信聊天输入框是否暴露 android.widget.EditText（决定主路径是否命中）。
"""
import sys, time
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

DEV = server.resolve_device(None)
W, H = server._screen_size(DEV)


def open_search():
    server._wechat_ensure_home(DEV)
    server.run_adb(["shell", "input", "tap", str(int(W * 0.83)), str(int(H * 0.07))],
                   device=DEV, mutating=True)
    time.sleep(0.8)


print("=== A) 三类文本 phone_input_text（微信搜索框，无 EditText → 走剪贴板兜底） ===")
for t in ["测试中文输入", "Hello世界123", "你好，world! 测试 123"]:
    open_search()
    server.run_adb(["shell", "input", "tap", str(int(W * 0.5)), str(int(H * 0.07))],
                   device=DEV, mutating=True)
    time.sleep(0.2)
    r = server.t_input_text({"text": t, "deviceSerial": DEV})
    time.sleep(0.4)
    d = r.get("data") or {}
    # 软校验：OCR 是否可见（中文易被误识，仅作辅助，不阻断）
    seen = server._ocr_sees(DEV, t, region=[0, 0.0, 1, 0.2]) or \
           server._ocr_sees(DEV, t, region=[0, 0.10, 1, 0.6])
    print("  文本=%r -> success=%s 方法=%s 已写=%s 已粘贴=%s OCR可见≈%s"
          % (t, r["success"], d.get("method"), d.get("written"), d.get("pasted"), seen))
    # 退出搜索，准备下一条
    server.run_adb(["shell", "input", "keyevent", "4"], device=DEV, mutating=True)
    time.sleep(0.5)

print("\n=== B) 全链路 send_wechat_message（向远钦 / 方案二测试 你好） ===")
r = server.t_send_wechat_message({"contact_name": "向远钦", "message": "方案二测试 你好", "deviceSerial": DEV})
d = r.get("data") or {}
print("success=%s sent=%s" % (r["success"], d.get("sent")))
for s in d.get("steps", []):
    print("   " + s)

print("\n=== C) 探测当前聊天页 EditText 数量（决定是否命中 UiAutomator 主路径） ===")
import uiautomator2 as u2
dd = u2.connect(DEV)
print("   chat EditText count =", dd(className="android.widget.EditText").count)

print("\n=== 结果摘要 ===")
print("B 发送成功=%s (sent=%s)" % (r["success"], d.get("sent")))
