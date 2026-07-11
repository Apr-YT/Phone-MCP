#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tier3 冒烟测试：验证 uiautomator2 底层封装的控件工具可用（含 OCR 回退）。
当前设备应停留在与「向远钦」的聊天页，故 '发送' 按钮应可被定位。"""
import sys, json
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

print("=== t_ui_dump (uiautomator2 dump_hierarchy) ===")
r = server.t_ui_dump({})
print("success:", r.get("success"))
d = r.get("data", {})
print("total nodes:", d.get("total"), "| named:", d.get("named"), "| json:", d.get("json_path"))

print("\n=== t_find_element '发送' (u2 -> OCR 回退) ===")
r2 = server.t_find_element({"query": "发送", "matchBy": "text"})
print("success:", r2.get("success"))
d2 = r2.get("data", {})
print("method:", d2.get("method"), "| count:", d2.get("count"))
print("message:", (r2.get("message") or "")[:240])

print("\n=== t_tap_element DRYRUN '发送' ===")
r3 = server.t_tap_element({"query": "发送", "matchBy": "text", "fallback": True})
print("success:", r3.get("success"))
d3 = r3.get("data", {})
print("method:", d3.get("method"), "| cx,cy:", d3.get("cx"), d3.get("cy"))
