#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段一验证：工程化加固
  - 所有工具 description 与参数 description 非空
  - 所有工具返回统一信封 {success,message,data}
  - 异常捕获绝不崩溃；瞬时异常重试；参数错误不重试
  - 统一日志(入参/耗时)输出
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


# ---- 捕获日志 ----
logs = []
_orig_log = server.log


def _cap(*a):
    logs.append(" ".join(str(x) for x in a))


server.log = _cap

# ---- 桩：去掉真实设备依赖 ----
server.resolve_device = lambda x=None: "FAKE"
server.run_adb = lambda *a, **k: subprocess.CompletedProcess(
    a[0] if a else [], 0, "", "")
server.smart_find = lambda *a, **k: ([("WLAN", 100, 200, 1.0)], "ui")


def envelope_of(result):
    """从 dispatch 结果里取出信封 dict。"""
    content = result["result"]["content"]
    for blk in content:
        if blk.get("type") == "text":
            return json.loads(blk["text"])
    raise AssertionError("无 text 块")


print("\n[1] 工具描述完整性")
for t in server.TOOLS:
    name = t["name"]
    check(bool((t.get("description") or "").strip()), "description 非空: %s" % name)
    props = (t.get("inputSchema") or {}).get("properties") or {}
    for pname, pspec in props.items():
        check(bool((pspec.get("description") or "").strip()),
              "参数描述非空: %s.%s" % (name, pname))

print("\n[2] 统一信封 {success,message,data}")
for tool_name in ["phone_tap", "phone_screenshot", "phone_get_current_app",
                  "phone_find_text", "phone_auto_click", "phone_launch_app",
                  "phone_press_key", "phone_dump_ui", "phone_find_element"]:
    r = server.dispatch_tool(tool_name, {"x": 1, "y": 2, "query": "WLAN",
                                         "text": "WLAN", "package": "x",
                                         "keycode": "BACK"}, 1)
    env = envelope_of(r)
    ok_keys = set(env.keys()) == {"success", "message", "data"}
    check(ok_keys, "信封键齐: %s (得到 %s)" % (tool_name, list(env.keys())),
          str(env.keys()))
    check(isinstance(env["success"], bool), "success 为 bool: %s" % tool_name)
    check(isinstance(env["message"], str), "message 为 str: %s" % tool_name)
    check(isinstance(env["data"], dict), "data 为 dict: %s" % tool_name)
    check(r["result"]["isError"] == (not env["success"]),
          "isError 与 success 一致: %s" % tool_name)

print("\n[3] 异常捕获：不崩溃，返回失败信封")
# 3a 参数缺失 -> ValueError -> 立即失败，不重试
logs.clear()
r = server.dispatch_tool("phone_tap", {}, 1)
env = envelope_of(r)
check(env["success"] is False, "缺参返回 success=false")
check("参数错误" in env["message"], "缺参提示'参数错误': %s" % env["message"])
check(not any("RETRY" in l for l in logs), "参数错误不重试(无 RETRY 日志)")

# 3b 非瞬时异常 -> 不重试，直接失败
calls = {"n": 0}


def _boom(*a, **k):
    calls["n"] += 1
    raise RuntimeError("boom 逻辑错误")


server.run_adb = _boom
logs.clear()
r = server.dispatch_tool("phone_tap", {"x": 1, "y": 2}, 1)
env = envelope_of(r)
check(env["success"] is False, "非瞬时异常 success=false")
check(calls["n"] == 1, "非瞬时异常仅执行1次(不重试)，实际 %d" % calls["n"])
check(not any("RETRY" in l for l in logs), "非瞬时异常无 RETRY 日志")

print("\n[4] 瞬时异常重试(默认2次)")
server._TOOL_RETRIES = 2
calls = {"n": 0}


def _transient(*a, **k):
    calls["n"] += 1
    if calls["n"] <= 2:
        raise subprocess.TimeoutExpired("adb", 5)
    return subprocess.CompletedProcess(a[0] if a else [], 0, "", "")


server.run_adb = _transient
logs.clear()
r = server.dispatch_tool("phone_tap", {"x": 1, "y": 2}, 1)
env = envelope_of(r)
check(env["success"] is True, "瞬时异常重试后成功")
check(calls["n"] == 3, "瞬时异常重试2次共3次调用，实际 %d" % calls["n"])
check(any("RETRY" in l for l in logs), "出现 RETRY 日志")

print("\n[5] 统一日志(入参/耗时)")
server.run_adb = lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", "")
logs.clear()
server.dispatch_tool("phone_tap", {"x": 5, "y": 6}, 1)
joined = "\n".join(logs)
check("入参" in joined, "日志含入参")
check("耗时" in joined, "日志含耗时")
check("phone_tap" in joined, "日志含工具名")

print("\n==== 阶段一验证结果: PASS=%d FAIL=%d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
