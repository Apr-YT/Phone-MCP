#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tier1+Tier2 验证驱动：
  1) phone_input_method_setup：确认 ADBKeyBoard 安装/启用逻辑在无 APK 时优雅降级（不崩溃）。
  2) phone_send_wechat_message：给「向远钦」发测试消息，端到端验证完整闭环（无假成功）。
"""
import sys, time, json
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

print("=== 0) 设备检查 ===")
devs = server.list_devices()
print("devices:", devs)
if not devs:
    print("NO DEVICE — 中止")
    sys.exit(1)

print("\n=== 1) phone_input_method_setup（APK 缺失应优雅降级）===")
st = server.t_setup_adbkeyboard({})
print("success:", st.get("success"))
print("message:", st.get("message"))
print("data:", json.dumps(st.get("data", {}), ensure_ascii=False))

print("\n=== 2) phone_send_wechat_message 端到端 ===")
contact = "向远钦"
message = "你好，这是自动化测试消息😀，来自 phone MCP"
print("目标联系人: %s | 消息: %s" % (contact, message))
t0 = time.time()
res = server.t_send_wechat_message({"contact_name": contact, "message": message})
wall = time.time() - t0
print("\nsuccess:", res.get("success"))
print("message:", res.get("message"))
data = res.get("data", {})
print("total_seconds(工具内):", data.get("total_seconds"))
print("wall clock: %.1fs" % wall)
print("steps:")
for s in data.get("steps", []):
    print("  -", s)
