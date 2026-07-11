#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第四步真机验证：实测 phone_auto_click 闭环（UI 模式，无需 OCR 引擎）。"""
import os
import sys
import time

os.environ["PHONE_MCP_ALLOW_SHELL"] = "1"
os.environ["PHONE_MCP_DEVICE"] = "134d2f8"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


def show(label, res):
    if isinstance(res, tuple):
        res = res[0]
    for b in res:
        if b.get("type") == "text":
            print("[%s] %s" % (label, b["text"]))


print("== 前置前台 ==")
show("BEFORE", server.t_get_current_app({}))

print("\n== 启动设置 ==")
show("LAUNCH", server.t_launch_app({"package": "com.android.settings"}))
time.sleep(2.5)

print("\n== auto_click 'WLAN' (UI模式, verify=any) ==")
show("AUTO", server.t_auto_click({"query": "WLAN", "method": "ui", "verify": "any", "maxRetries": 3}))
time.sleep(1.5)

print("\n== 点击后前台(应进入 WLAN 设置) ==")
show("AFTER", server.t_get_current_app({}))

print("\n== 返回桌面 ==")
show("BACK", server.t_key_event({"keycode": "BACK"}))
time.sleep(1.0)
show("HOME", server.t_key_event({"keycode": "HOME"}))
time.sleep(0.5)
show("FINAL", server.t_get_current_app({}))
print("\nDONE")
