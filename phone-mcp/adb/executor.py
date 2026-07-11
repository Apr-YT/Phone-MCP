# -*- coding: utf-8 -*-
"""
ADB 执行层 —— 封装所有 adb 命令的调用、设备管理、安全闸门。

本模块无内部依赖（仅依赖 Python 标准库 + subprocess）。
"""

import os
import shutil
import subprocess
import re
import time

# ---- 配置（由 config 模块在首次 import 时注入）----
ADB = None
DEFAULT_DEVICE = None
DRYRUN = False
ALLOW_SHELL = False
ADB_TIMEOUT = 30
ADB_RETRIES = 2


def configure(adb=None, default_device=None, dryrun=False, allow_shell=False,
              adb_timeout=30, adb_retries=2):
    """注入运行时配置（由 server.py 在启动时调用）。"""
    global ADB, DEFAULT_DEVICE, DRYRUN, ALLOW_SHELL, ADB_TIMEOUT, ADB_RETRIES
    if adb is not None:
        ADB = adb
    if default_device is not None:
        DEFAULT_DEVICE = default_device
    DRYRUN = dryrun
    ALLOW_SHELL = allow_shell
    ADB_TIMEOUT = adb_timeout
    ADB_RETRIES = adb_retries


# 灾难性命令黑名单（即使开启底层也禁止，防止变砖/丢数据）
CATASTROPHIC_RE = [
    re.compile(r"\b(reboot)\b"),
    re.compile(r"\b(wipe)\b"),
    re.compile(r"\b(format)\b"),
    re.compile(r"\b(mkfs)\b"),
    re.compile(r"\b(fastboot)\b"),
    re.compile(r"dd\s+if="),
    # 增强防护：
    re.compile(r"\brm\b\s+.*(-rf?|--recursive)"),
    re.compile(r">\s*/dev/"),
]


def log(*args):
    """日志写 stderr，绝不污染 stdout。"""
    print("[phone-mcp]", *args, file=sys.stderr, flush=True)


import sys


# ---- 设备管理 ----

def list_devices():
    out = subprocess.run(
        [ADB, "devices"], capture_output=True, text=True
    ).stdout
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def resolve_device(explicit):
    """确定要操作的设备序列号。"""
    if explicit:
        return explicit
    if DEFAULT_DEVICE and DEFAULT_DEVICE in list_devices():
        return DEFAULT_DEVICE
    devs = list_devices()
    if len(devs) == 1:
        return devs[0]
    if len(devs) == 0:
        raise RuntimeError("未检测到已连接的 adb 设备，请先 `adb devices` 确认。")
    raise RuntimeError(
        "检测到多台设备 %s，请通过 deviceSerial 参数明确指定。" % devs
    )


def require_shell():
    """底层/系统级命令的闸门。"""
    if not ALLOW_SHELL:
        raise PermissionError(
            "底层(系统级)命令已禁用。如需启用，请在启动服务时设置环境变量 "
            "PHONE_MCP_ALLOW_SHELL=1（建议在 mcp.json 的 env 中设置）。"
        )


def forbid_catastrophic(text):
    low = (text or "").lower()
    # 归一化多空格为单空格，防止绕过（如 `dd  if=`）
    normalized = re.sub(r'\s+', ' ', low)
    for rx in CATASTROPHIC_RE:
        if rx.search(low) or rx.search(normalized):
            m = rx.search(low) or rx.search(normalized)
            raise PermissionError("出于安全考虑，禁止执行灾难性命令: %s" % m.group(0))


# ---- ADB 命令执行 ----

def run_adb(args, device=None, mutating=False, capture=True, binary=False,
            timeout=None, retries=None, delay=0.4, what="adb"):
    """执行一条 adb 命令，带超时与失败重试。

    mutating 且在 DRYRUN 模式下只打印不执行（不真正连接设备）。
    binary=True 时用字节模式捕获（适用于截图等二进制输出）。
    """
    if timeout is None:
        timeout = ADB_TIMEOUT
    if retries is None:
        retries = ADB_RETRIES

    cmd = [ADB]
    if device:
        cmd += ["-s", device]
    cmd += args

    if mutating and DRYRUN:
        log("[DRYRUN] 将执行:", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"" if binary else "", "")

    log("执行:", " ".join(cmd))

    def _run():
        return subprocess.run(
            cmd, capture_output=capture, text=(capture and not binary), timeout=timeout
        )

    from . import retry as retry_mod
    ok_flag, proc = retry_mod.with_retry(_run, retries=retries, delay=delay,
                          what="%s %s" % (what, " ".join(args[:2])))
    if not ok_flag:
        raise RuntimeError("adb 命令执行失败: %s（%s）" % (" ".join(cmd), proc))
    return proc
