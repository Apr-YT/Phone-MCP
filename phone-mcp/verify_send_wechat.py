# -*- coding: utf-8 -*-
"""端到端验证：向「向远钦」发送「你好」。
用连接器的 venv python 运行（已含 rapidocr_onnxruntime）。输出每步日志与最终结果。
"""
import sys, time, os
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
DEV = server.resolve_device(None)
SHOT = r"C:\Users\AprYT\.workbuddy\phone-mcp\shots"

print("=" * 64)
print("端到端验证 phone_send_wechat_message")
print("设备: %s   目标: 向远钦   内容: 你好" % DEV)
print("=" * 64)

def snap(name):
    try:
        ok = server.run_adb(["exec-out", "screencap", "-p"], device=DEV, capture=True, binary=True)
        p = os.path.join(SHOT, name)
        with open(p, "wb") as f:
            f.write(ok.stdout)
        print("   [截图] %s" % p)
    except Exception as e:
        print("   [截图失败] %r" % e)

t0 = time.time()
snap("e2e_before.png")
res = server.t_send_wechat_message({"contact_name": "向远钦", "message": "你好", "deviceSerial": DEV})
dt = time.time() - t0

print("\n--- 执行日志(按步骤) ---")
for s in res.get("data", {}).get("steps", []):
    print("  " + s)
snap("e2e_after.png")

print("\n--- 最终结果 ---")
print("success :", res["success"])
print("message :", res["message"])
print("data    :", server.json.dumps(res.get("data", {}), ensure_ascii=False))
print("耗时    : %.1fs" % dt)
print("=" * 64)
if res["success"]:
    print("✅ 发送成功：已给「向远钦」发送「你好」")
else:
    print("❌ 发送未成功，请查看上方步骤日志与 e2e_after.png")
