#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等价验证：新起一个干净的 phone-mcp server 进程（等同重启后连接器加载的新代码），
通过真实 MCP 协议发送中文指令 phone_tap_text(text="文件传输助手")，验证：
  1) UTF-8 编码修复（服务端收到的 query 是正常中文，见 ocr_debug.txt）
  2) OCR 跨线程崩溃修复（reader 在主线程构建，能正常识别并点击）
"""
import subprocess, json, time, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
env = dict(os.environ)
env.update({
    "PHONE_MCP_DEVICE": "134d2f8",
    "PHONE_MCP_ALLOW_SHELL": "1",
    "PYTHONIOENCODING": "utf-8",
})

# 清空旧 debug 日志，只看本次
dbg = os.path.join(HERE, "shots", "ocr_debug.txt")
if os.path.exists(dbg):
    os.remove(dbg)

p = subprocess.Popen(
    [sys.executable, SERVER],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    env=env, bufsize=1, encoding="utf-8", errors="replace",
)

def send(o):
    p.stdin.write(json.dumps(o) + "\n")
    p.stdin.flush()

def recv():
    while True:
        line = p.stdout.readline()
        if line.strip():
            return json.loads(line)

def call(name, args, cid=99):
    send({"jsonrpc": "2.0", "id": cid, "method": "tools/call",
          "params": {"name": name, "arguments": args}})
    return recv()["result"]["content"][0]["text"]

# 1) initialize
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "verify", "version": "1"}}})
print("INIT serverInfo:", recv().get("result", {}).get("serverInfo"))

# 2) 启动微信并回到聊天列表
print("LAUNCH:", call("phone_shell", {"command": "am start -n com.tencent.mm/.ui.LauncherUI"})[:80])
time.sleep(3)

# 3) 滑到列表顶部（用专用 phone_swipe，绕过 shell 的 wipe 黑名单）
for _ in range(4):
    call("phone_swipe", {"x1": 540, "y1": 400, "x2": 540, "y2": 1900})
    time.sleep(0.6)

# 4) 向下查找并点击「文件传输助手」
found = False
for i in range(8):
    r = call("phone_find_text", {"text": "文件传输助手", "method": "ocr"})
    if "未找到" not in r:
        print("FIND #%d: %s" % (i, r[:200]))
        found = True
        break
    call("phone_swipe", {"x1": 540, "y1": 1900, "x2": 540, "y2": 400})
    time.sleep(0.8)

if found:
    r = call("phone_tap_text", {"text": "文件传输助手", "method": "ocr"})
    print("TAP RESULT:", r[:200])
else:
    print("NOT FOUND after scrolling")

p.terminate()
try:
    p.wait(timeout=5)
except Exception:
    p.kill()

print("\n=== ocr_debug.txt (本次 query 原文) ===")
if os.path.exists(dbg):
    with open(dbg, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if "收到" in ln or "无匹配" in ln or "两次调用均失败" in ln:
                print(ln.rstrip())
else:
    print("NO debug file")
