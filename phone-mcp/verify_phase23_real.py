#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段二/三 真机轻量冒烟：在真实设备(默认 134d2f8)上验证新工具走统一信封+调度。
仅做可逆操作（看前台、回主页、启动微信、UI 查找），不删不改数据。"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # 导入即预热 OCR（可能稍慢）

PASS = 0
FAIL = 0


def check(c, n, e=""):
    global PASS, FAIL
    if c:
        PASS += 1
        print("  PASS  %s" % n)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (n, e))


def env_of(r):
    for b in r["result"]["content"]:
        if b.get("type") == "text":
            return json.loads(b["text"])
    return {}


print("\n[真机] 当前前台应用 (phone_get_current_app)")
r = server.dispatch_tool("phone_get_current_app", {}, 1)
e = env_of(r)
check(e.get("success"), "get_current_app 成功", str(e))
check(bool(e.get("data", {}).get("package")), "data.package 有值: %s" % e.get("data", {}).get("package"))

print("\n[真机] 回主页 (phone_press_home)")
r = server.dispatch_tool("phone_press_home", {}, 1)
e = env_of(r)
check(e.get("success") and e["data"].get("code") == "3", "press_home code=3")

print("\n[真机] 启动微信并校验 (phone_launch_app + phone_get_current_app)")
r = server.dispatch_tool("phone_launch_app", {"package": "com.tencent.mm"}, 1)
e = env_of(r)
check(e.get("success"), "launch WeChat 成功", str(e))
time.sleep(2.0)
r = server.dispatch_tool("phone_get_current_app", {}, 1)
e = env_of(r)
pkg = e.get("data", {}).get("package", "")
check(pkg == "com.tencent.mm", "前台变为微信: %s" % pkg, pkg)

print("\n[真机] UI 查找控件 (phone_find_ui_element 在微信)")
r = server.dispatch_tool("phone_find_ui_element", {"query": "通讯录"}, 1)
e = env_of(r)
check("success" in e, "find_ui_element 返回信封(不崩溃)", str(e))

print("\n[真机] 回主页清理 (phone_press_home)")
r = server.dispatch_tool("phone_press_home", {}, 1)
e = env_of(r)
check(e.get("success"), "press_home 清理成功")

print("\n==== 真机冒烟结果: PASS=%d FAIL=%d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
