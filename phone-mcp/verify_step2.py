#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段二验证：中层工具封装
  - 新增工具均已注册：phone_find_ui_element / phone_press_back / phone_press_home / phone_swipe_until_find
  - phone_find_text(严格/模糊) / phone_tap_text(自动重试) 行为正确
  - 信封格式保持一致
"""
import os
import sys
import json
import subprocess

os.environ["PHONE_MCP_DRYRUN"] = "1"
os.environ["PHONE_MCP_ALLOW_SHELL"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

PASS = 0
FAIL = 0


def check(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, extra))


server.resolve_device = lambda x=None: "FAKE"
# 关闭 dry-run 以便验证真实的点击/滑动分支（run_adb 已被桩，不会触碰真机）。
server.DRYRUN = False
captured = []


def fake_run(*a, **k):
    captured.append(a)
    return subprocess.CompletedProcess(a[0] if a else [], 0, "", "")


server.run_adb = fake_run
smart_hits = []


def fake_smart(*a, **k):
    return (list(smart_hits), "ui")


server.smart_find = fake_smart
# t_find_element 走 _get_ui_xml(控件树)，与 smart_find 无关，单独桩掉返回固定 XML。
server._get_ui_xml = lambda device: '<node text="设置" bounds="[0,0][200,400]"/>'


def env_of(result):
    for blk in result["result"]["content"]:
        if blk.get("type") == "text":
            return json.loads(blk["text"])
    raise AssertionError("无 text 块")


names = [t["name"] for t in server.TOOLS]
print("\n[1] 新增/封装工具已注册")
for n in ["phone_find_text", "phone_find_ui_element", "phone_tap_text",
          "phone_swipe_until_find", "phone_press_back", "phone_press_home"]:
    check(n in names, "已注册: %s" % n)

print("\n[2] phone_find_ui_element (uiautomator 控件树按文字/ID)")
smart_hits = [("设置", 100, 200, 1.0)]
r = server.dispatch_tool("phone_find_ui_element", {"query": "设置"}, 1)
e = env_of(r)
check(e["success"] and e["data"].get("count") == 1, "找到 1 个控件", str(e))
check(e["data"]["hits"][0]["label"] == "设置", "label=设置")

print("\n[3] phone_press_back / phone_press_home (快捷按键)")
captured.clear()
r = server.dispatch_tool("phone_press_back", {}, 1)
e = env_of(r)
check(e["data"].get("code") == "4", "press_back 发送 BACK code=4", str(e["data"]))
check(any(c[0] == ["shell", "input", "keyevent", "4"] for c in captured), "adb 调用含 keyevent 4")
captured.clear()
r = server.dispatch_tool("phone_press_home", {}, 1)
e = env_of(r)
check(e["data"].get("code") == "3", "press_home 发送 HOME code=3", str(e["data"]))

print("\n[4] phone_swipe_until_find (自动滑动找文字)")
# 4a 立即找到
smart_hits = [("目标", 50, 60, 1.0)]
captured.clear()
r = server.dispatch_tool("phone_swipe_until_find", {"query": "目标"}, 1)
e = env_of(r)
check(e["success"] and e["data"].get("found"), "找到目标", str(e["data"]))
check(e["data"].get("swipes") == 1, "swipes=1")
check(not any("swipe" in c[0] for c in captured), "立即找到未触发滑动")
# 4b 找不到 -> 失败，滑动 maxSwipes 次
smart_hits = []
captured.clear()
r = server.dispatch_tool("phone_swipe_until_find", {"query": "x", "maxSwipes": 3}, 1)
e = env_of(r)
check((not e["success"]) and e["data"].get("found") is False, "未找到 success=false", str(e))
check(e["data"].get("swipes") == 3, "swipes=3")
check(sum(1 for c in captured if "swipe" in c[0]) == 3, "实际滑动 3 次")
# 4c tapOnFind
smart_hits = [("目标", 50, 60, 1.0)]
captured.clear()
r = server.dispatch_tool("phone_swipe_until_find", {"query": "目标", "tapOnFind": True}, 1)
e = env_of(r)
check(e["data"].get("tapped") is True, "tapOnFind 后 tapped=true")
check(any(c[0] == ["shell", "input", "tap", "50", "60"] for c in captured), "找到后点击 (50,60)")

print("\n[5] phone_tap_text 自动重试")
state = {"n": 0}


def fake_smart_retry(*a, **k):
    state["n"] += 1
    if state["n"] < 2:
        return ([], "ui")
    return ([("爸爸", 10, 20, 1.0)], "ui")


server.smart_find = fake_smart_retry
r = server.dispatch_tool("phone_tap_text", {"text": "爸爸", "maxRetries": 2}, 1)
e = env_of(r)
check(e["success"], "重试后点击成功", str(e))
check(e["data"].get("attempts") == 2, "attempts=2(重试1次)", str(e["data"]))
check(e["data"].get("label") == "爸爸", "label=爸爸")

print("\n[6] phone_find_text 严格/模糊(exact) 透传")
server.smart_find = lambda *a, **k: ([("爸爸的微信", 1, 1, 1.0)], "ui")
r = server.dispatch_tool("phone_find_text", {"text": "爸爸", "exact": False}, 1)
e = env_of(r)
check(e["data"].get("found") is True, "模糊(exact=false) 命中包含项")
server.smart_find = lambda *a, **k: ([], "ui")
r = server.dispatch_tool("phone_find_text", {"text": "爸爸", "exact": True}, 1)
e = env_of(r)
check(e["data"].get("found") is False, "严格(exact=true) 未命中(无完全相等项)")

print("\n==== 阶段二验证结果: PASS=%d FAIL=%d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
