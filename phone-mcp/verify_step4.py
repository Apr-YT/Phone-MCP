#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第四步验证：闭环(phone_auto_click) + 工具描述完整性 + 重试/超时。

无需真机：通过 mock smart_find / run_adb / resolve_device 覆盖各分支。
运行：python verify_step4.py
"""
import os
import sys
import types

os.environ["PHONE_MCP_DRYRUN"] = "1"
os.environ["PHONE_MCP_DEVICE"] = "134d2f8"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

PASS = 0
FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS", name)
    else:
        FAIL += 1
        print("FAIL", name)


# ---- 1. with_retry 重试逻辑 ----
calls = {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] < 2:
        raise RuntimeError("boom")
    return "ok"


ok, res = server.with_retry(flaky, retries=3, delay=0.0)
check(ok and res == "ok" and calls["n"] == 2, "with_retry: 失败1次后成功")

calls2 = {"n": 0}


def always_fail():
    calls2["n"] += 1
    raise ValueError("x")


ok2, exc = server.with_retry(always_fail, retries=2, delay=0.0)
check((not ok2) and isinstance(exc, ValueError) and calls2["n"] == 2,
      "with_retry: 全失败返回最后一次异常")


# ---- 2. 重试/超时环境变量 ----
check(isinstance(server.ADB_TIMEOUT, float) and server.ADB_TIMEOUT > 0,
      "ADB_TIMEOUT 已定义且 > 0")
check(isinstance(server.ADB_RETRIES, int) and server.ADB_RETRIES >= 1,
      "ADB_RETRIES 已定义且 >= 1")


# ---- 3. 所有工具 description + 参数说明 完整 ----
missing_tool_desc = []
missing_param_desc = []
for t in server.TOOLS:
    if not (t.get("description") or "").strip():
        missing_tool_desc.append(t["name"])
    props = (t.get("inputSchema") or {}).get("properties") or {}
    for pname, pinfo in props.items():
        if not (pinfo.get("description") or "").strip():
            missing_param_desc.append("%s.%s" % (t["name"], pname))
check(not missing_tool_desc, "所有工具均有 description: %s" % missing_tool_desc)
check(not missing_param_desc, "所有参数均有 description: %s" % missing_param_desc)
print("  工具总数:", len(server.TOOLS))


# ---- 4. phone_auto_click 注册 + schema ----
ac = next((t for t in server.TOOLS if t["name"] == "phone_auto_click"), None)
check(ac is not None, "phone_auto_click 已注册")
if ac:
    check("query" in (ac["inputSchema"].get("required") or []),
          "phone_auto_click 必填 query")
    ps = ac["inputSchema"]["properties"]
    for p in ("query", "matchBy", "exact", "method", "index", "maxRetries", "verify"):
        check(p in ps, "phone_auto_click 含参数 %s" % p)
    check(ac["handler"] is server.t_auto_click, "phone_auto_click 绑定 t_auto_click")


# ---- 5. t_auto_click DRYRUN 分支 ----
server.resolve_device = lambda dev: "134d2f8"
server.smart_find = lambda *a, **k: ([("WLAN", 100, 200, 1.0)], "ui")
res = server.t_auto_click({"query": "WLAN"})
check(any("DRYRUN" in b.get("text", "") for b in res),
      "t_auto_click DRYRUN 返回意图说明")


# ---- 6. t_auto_click 成功路径（定位命中→点击→验证目标已离开）----
server.DRYRUN = False
calls_tap = {"n": 0}


def fake_run_adb(*a, **k):
    calls_tap["n"] += 1
    return types.SimpleNamespace(stdout="", stderr="")


server.run_adb = fake_run_adb
seq = {"n": 0}


def fake_smart(*a, **k):
    seq["n"] += 1
    if seq["n"] == 1:
        return ([("WLAN", 100, 200, 1.0)], "ui")   # 定位命中
    return ([], "ui")                                # 验证：目标已离开


server.smart_find = fake_smart
res2 = server.t_auto_click({"query": "WLAN"})
txt2 = res2[0]["text"] if isinstance(res2, list) else ""
check("✅" in txt2 and calls_tap["n"] == 1,
      "t_auto_click 点击+验证成功（单次点击）")


# ---- 7. t_auto_click 目标仍在 → 重试耗尽 → 警告 ----
server.DRYRUN = False
calls_tap2 = {"n": 0}


def fake_run_adb2(*a, **k):
    calls_tap2["n"] += 1
    return types.SimpleNamespace(stdout="", stderr="")


server.run_adb = fake_run_adb2


def fake_smart_stay(*a, **k):
    return ([("WLAN", 100, 200, 1.0)], "ui")   # 始终命中


server.smart_find = fake_smart_stay
res3 = server.t_auto_click({"query": "WLAN", "maxRetries": 2})
is_err = isinstance(res3, tuple) and res3[1] is True
warn_text = res3[0][0]["text"] if (isinstance(res3, tuple) and res3[0]) else ""
check(is_err and "⚠️" in warn_text and calls_tap2["n"] == 2,
      "t_auto_click 目标仍在→重试耗尽返回警告(点击2次)")


print("\n=== PASS=%d FAIL=%d ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
