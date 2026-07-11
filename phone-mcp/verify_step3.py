#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段三验证：闭环能力
  - with_verification 通用'操作后自动校验 + 失败自动重试'逻辑
  - phone_wechat_open_chat 全链路示例（启动→通讯录→滑动找联系人→点击→校验进入聊天）
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
server.run_adb = lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", "")


def env_of(result):
    for blk in result["result"]["content"]:
        if blk.get("type") == "text":
            return json.loads(blk["text"])
    raise AssertionError("无 text 块")


print("\n[1] with_verification 通用校验/重试")
state = {"n": 0}


def act_ok():
    state["n"] += 1
    return state["n"]


ok_v, res = server.with_verification(act_ok, lambda x: x >= 3, max_retries=3)
check(ok_v and res == 3, "重试到满足条件(第3次)", "res=%s" % res)
check(state["n"] == 3, "实际执行3次")


def act_err():
    raise RuntimeError("boom")


ok_v2, _ = server.with_verification(act_err, lambda r: not isinstance(r, Exception), max_retries=2)
check(not ok_v2, "始终异常 → 返回 False(不崩溃)")

print("\n[2] phone_wechat_open_chat (全链路示例)")
# 桩：微信已登录、联系人存在
server.t_get_current_app = lambda a: server.ok(
    "pkg com.tencent.mm", package="com.tencent.mm", activity="x")
count_suf = {"n": 0}


def fake_suf(a):
    count_suf["n"] += 1
    return server.ok("找到联系人", found=True, label=a.get("query"),
                     cx=1, cy=1, tapped=True, swipes=1)


server.t_swipe_until_find = fake_suf


def fake_smart3(q, *a, **k):
    if q == "通讯录":
        return ([("通讯录", 10, 20, 1.0)], "ui")
    if q == "不存在的人":
        return ([], "ui")
    return ([(q, 1, 1, 1.0)], "ui")


server.smart_find = fake_smart3

# 2a DRYRUN 仅打印意图
server.DRYRUN = True
r = server.dispatch_tool("phone_wechat_open_chat", {"contact": "爸爸"}, 1)
e = env_of(r)
check(e["data"].get("dryrun") is True, "DRYRUN 返回 dryrun 意图")
server.DRYRUN = False

# 2b 全链路成功：进入聊天
r = server.dispatch_tool("phone_wechat_open_chat", {"contact": "爸爸"}, 1)
e = env_of(r)
check(e["success"] and e["data"].get("in_chat") is True, "成功进入聊天", str(e["data"]))

# 2c 联系人找不到 → 失败自动重试后 fail
count_suf["n"] = 0


def fake_suf_fail(a):
    count_suf["n"] += 1
    return server.fail("未找到联系人", found=False, query=a.get("query"))


server.t_swipe_until_find = fake_suf_fail
r = server.dispatch_tool("phone_wechat_open_chat", {"contact": "不存在的人"}, 1)
e = env_of(r)
check(e["success"] is False, "联系人不存在 → success=false")
check(count_suf["n"] == 2, "with_verification 重试2次(swipe_until_find 被调2次)，实际 %d" % count_suf["n"])

print("\n[3] 工具注册 & 描述")
names = [t["name"] for t in server.TOOLS]
check("phone_wechat_open_chat" in names, "phone_wechat_open_chat 已注册")
wt = next(t for t in server.TOOLS if t["name"] == "phone_wechat_open_chat")
check(bool((wt.get("description") or "").strip()), "phone_wechat_open_chat 有描述")
check(bool(wt["inputSchema"]["properties"].get("contact", {}).get("description")),
      "contact 参数有描述")

print("\n==== 阶段三验证结果: PASS=%d FAIL=%d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
