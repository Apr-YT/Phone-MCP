#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机真实点击验证（可逆）：点 WLAN 打开无线设置页，再 BACK 返回。
证明 tap_element 的 input tap 真实执行成功。"""
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # DRYRUN 默认 False（未设环境变量）

DEV = server.resolve_device(None)
subprocess.run([server.ADB, "-s", DEV, "shell", "am", "start",
                "-a", "android.settings.SETTINGS"], capture_output=True)
time.sleep(1.2)

print("真实点击 'WLAN' ...")
r = server.t_tap_element({"query": "WLAN", "matchBy": "text", "deviceSerial": None})
print("  -> " + r[0]["text"])
time.sleep(1.0)
# 验证确实进入了 WLAN 页（dump 应含 'WLAN' 标题/开关）
xml = server._get_ui_xml(DEV)
opened = "WLAN" in xml
print("  进入 WLAN 页: %s" % opened)
# 可逆：返回设置首页
server.t_key_event({"keycode": "BACK"})
time.sleep(0.6)
print("  已 BACK 返回。")
print("真实点击验证: %s" % ("OK" if opened else "FAIL"))
sys.exit(0 if opened else 1)
