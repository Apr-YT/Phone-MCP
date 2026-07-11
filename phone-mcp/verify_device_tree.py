#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证：在会导出 UI 树的真实 App(系统设置)上跑通
ui_dump / find_element / tap_element(DRYRUN)。"""
import os
import sys
import time
import subprocess

os.environ["PHONE_MCP_DRYRUN"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server
server.DRYRUN = True


def first_text(r):
    """handler 返回 [block] 或 ([block], True)，统一取出首段文本。"""
    content = r[0] if isinstance(r, tuple) else r
    return content[0]["text"]


DEV = server.resolve_device(None)
print("设备:", DEV)

subprocess.run([server.ADB, "-s", DEV, "shell", "am", "start",
                "-a", "android.settings.SETTINGS"], capture_output=True)
time.sleep(1.2)

xml = server._get_ui_xml(DEV)
nodes = server.parse_ui_xml(xml)
named = [n for n in nodes if (n["text"] or n["contentDesc"] or n["resourceId"])]
print("节点总数=%d  具名控件=%d" % (len(nodes), len(named)))
if not named:
    print("[FAIL] 设置页未导出节点，无法验证")
    sys.exit(1)

# 选一个真正带文字的控件做往返验证
text_nodes = [n for n in named if n["text"]]
print("\n带文字的控件样例：")
for n in text_nodes[:6]:
    print("  - %s @ (%s,%s)" % (n["text"], n["cx"], n["cy"]))

q = text_nodes[0]["text"]
print("\n用文字 '%s' 做 find_element(matchBy=text) ..." % q)
fr = server.t_find_element({"query": q, "matchBy": "text", "deviceSerial": None})
frt = first_text(fr)
print("  " + frt.replace("\n", "\n  "))
ok_find = "找到" in frt and "未找到" not in frt

tr = server.t_tap_element({"query": q, "matchBy": "text", "deviceSerial": None})
trt = first_text(tr)
print("\ntap_element DRYRUN -> " + trt)
ok_tap = "将点击" in trt

rids = [n["resourceId"] for n in named if n["resourceId"]]
ok_rid = False
if rids:
    rq = rids[0]
    rr = server.t_find_element({"query": rq, "matchBy": "resource-id", "deviceSerial": None})
    ok_rid = "找到" in first_text(rr)
    print("\nfind_element(resource-id='%s') -> %s" % (rq, "OK" if ok_rid else "FAIL"))

print("\n真机结果: find(文字)=%s  tap(DRYRUN)=%s  resource-id查找=%s"
      % (ok_find, ok_tap, ok_rid))
sys.exit(0 if (ok_find and ok_tap and ok_rid) else 1)
