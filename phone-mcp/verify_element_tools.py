#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证新增的控件级定位工具：ui_dump / find_element / tap_element。
包含离线样例 XML 测试 + 真机测试（只读 + DRYRUN 点击，绝不真点）。
安全：先设 PHONE_MCP_DRYRUN=1 再 import server，确保 tap 只预览不执行。"""
import os
import sys

# 必须在 import server 前开启 DRYRUN，避免真机点击。
os.environ["PHONE_MCP_DRYRUN"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server  # 导入即加载最新 server.py（此时 DRYRUN 已为真）
server.DRYRUN = True

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


# ---------------------------------------------------------------------------
# 1) 离线：样例 XML 解析与查找（不依赖设备）
# ---------------------------------------------------------------------------
print("== 离线：样例 XML 解析 / 查找 ==")
SAMPLE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="文件传输助手" resource-id="com.tencent.mm:id/title" class="android.widget.TextView" package="com.tencent.mm" content-desc="" bounds="[50,100][400,150]" clickable="true"/>
  <node index="1" text="" resource-id="com.tencent.mm:id/send_btn" class="android.widget.Button" package="com.tencent.mm" content-desc="发送" bounds="[900,1800][980,1880]" clickable="true"/>
  <node index="2" text="搜索" resource-id="" class="android.widget.EditText" package="com.tencent.mm" content-desc="" bounds="[20,40][700,90]" clickable="false"/>
</hierarchy>
"""
nodes = server.parse_ui_xml(SAMPLE)
check("解析节点数=3", len(nodes) == 3, "got %d" % len(nodes))
if nodes:
    check("坐标解析正确(text)", nodes[0]["cx"] == 225 and nodes[0]["cy"] == 125,
          "(%s,%s)" % (nodes[0]["cx"], nodes[0]["cy"]))
    check("content-desc 解析", nodes[1]["contentDesc"] == "发送")

h1 = server.element_find("文件传输助手", SAMPLE, match_by="text")
check("按文字查找命中", len(h1) == 1 and h1[0][1] == 225 and h1[0][2] == 125, str(h1))
h2 = server.element_find("com.tencent.mm:id/send_btn", SAMPLE, match_by="resource-id")
check("按resource-id查找命中", len(h2) == 1 and h2[0][2] == 1840, str(h2))
h3 = server.element_find("发送", SAMPLE, match_by="content-desc")
check("按content-desc查找命中", len(h3) == 1 and h3[0][2] == 1840, str(h3))
h4 = server.element_find("文件", SAMPLE, match_by="any")
check("any 子串命中", len(h4) == 1, str(h4))
h5 = server.element_find("文件", SAMPLE, match_by="text", exact=True)
check("exact 不匹配子串", len(h5) == 0, str(h5))
h6 = server.element_find("文件传输助手", SAMPLE, match_by="text", exact=True)
check("exact 完全匹配", len(h6) == 1, str(h6))


# ---------------------------------------------------------------------------
# 2) 真机：ui_dump / find_element / tap_element(DRYRUN)
# ---------------------------------------------------------------------------
print("\n== 真机：当前界面 dump + 查找 + DRYRUN 点击 ==")
DEV = server.resolve_device(None)
print("  设备: %s" % DEV)

res = server.t_ui_dump({"deviceSerial": None})
txt = res[0]["text"] if isinstance(res, list) else res
print("  ui_dump 摘要(前 400 字):")
print("   " + txt[:400].replace("\n", "\n   "))

nodes = server.parse_ui_xml(server._get_ui_xml(DEV))
named = [n for n in nodes if n["text"]]
check("真机解析到具名控件", len(named) > 0, "具名=%d" % len(named))
if named:
    q = named[0]["text"]
    fr = server.t_find_element({"query": q, "matchBy": "text", "deviceSerial": None})
    frtxt = fr[0]["text"]
    print("  find_element('%s') -> %s" % (q, frtxt[:120].replace("\n", " ")))
    check("真机 find_element 命中", "找到" in frtxt and "未找到" not in frtxt, "")

    tr = server.t_tap_element({"query": q, "matchBy": "text", "deviceSerial": None})
    print("  tap_element DRYRUN -> %s" % tr[0]["text"])
    check("真机 tap_element(DRYRUN) 解析到坐标", "将点击" in tr[0]["text"], tr[0]["text"])

print("\n结果: PASS=%d  FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
