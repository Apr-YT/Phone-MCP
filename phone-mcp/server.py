#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phone-mcp —— MCP (Model Context Protocol) stdio 服务。
通过 ADB 操作 Android 手机（界面层 + 系统级/底层）。

模块结构：
  adb/        ADB 控制层（设备管理 + 命令执行 + 重试）
  tools/      工具层（ui / vision / wechat / system / hardware / stream / frida）
  protocol/   协议层（调度 + 注册表）
  utils/      工具层（信封 / 日志）

协议：MCP over stdio，使用换行分隔的 JSON-RPC 2.0 消息。
"""

import os
import sys
import json

# ---- 编码修复 ----
try:
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ---- 导入模块 ----
from adb import (
    configure as adb_configure,
    ADB, DEFAULT_DEVICE, DRYRUN, ALLOW_SHELL,
    ADB_TIMEOUT, ADB_RETRIES,
    log,
)
from tools._shared import SHOT_DIR as _shot_dir
from tools import preview_ocr
from protocol.registry import TOOLS
from protocol.dispatch import dispatch_tool

# ---- 配置注入 ----
SHOT_DIR = os.environ.get("PHONE_MCP_SHOTDIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "shots"
)

ADB_BIN = (
    os.environ.get("ADB_BIN")
    or __import__("shutil").which("adb")
    or ""  # removed hardcoded Windows path per code review
)

adb_configure(
    adb=ADB_BIN,
    default_device=os.environ.get("PHONE_MCP_DEVICE") or "",
    dryrun=os.environ.get("PHONE_MCP_DRYRUN") == "1",
    allow_shell=os.environ.get("PHONE_MCP_ALLOW_SHELL") == "1",
    adb_timeout=float(os.environ.get("PHONE_MCP_TIMEOUT", "30")),
    adb_retries=int(os.environ.get("PHONE_MCP_RETRIES", "2")),
)

# 注入 SHOT_DIR 给共享模块
from tools._shared import SHOT_DIR as _sd
import tools._shared as _shared_mod
_shared_mod.SHOT_DIR = SHOT_DIR
_shared_mod.FAST = os.environ.get("PHONE_MCP_FAST") == "1"

# ---- MCP 协议常量 ----
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "phone-mcp", "version": "0.11.0"}


def handle_request(req):
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        tools = [{"name": t["name"], "description": t["description"],
                   "inputSchema": t["inputSchema"]} for t in TOOLS]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return dispatch_tool(name, arguments, req_id)
    if req_id is not None:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return None


def main():
    log("phone-mcp v%s 启动, adb=%s, 默认设备=%s, DRYRUN=%s, ALLOW_SHELL=%s"
        % (SERVER_INFO["version"], ADB, DEFAULT_DEVICE, DRYRUN, ALLOW_SHELL))

    # 预加载 OCR
    try:
        preview_ocr()
    except Exception:
        log("OCR 预加载跳过（未安装或不可用）")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("无法解析的行:", line[:200])
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
