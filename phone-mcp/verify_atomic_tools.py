#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证基础原子操作工具集（真机 134d2f8）。
- DRYRUN 段：tap/swipe/press_key/input_text(中文)/run_shell 命令生成 + 入参校验。
- 真实段：get_current_app(只读) + launch_app(settings)→get_current_app→stop_app(settings) 端到端。
"""
import os
import sys
import time

os.environ["PHONE_MCP_DRYRUN"] = "1"
os.environ["PHONE_MCP_ALLOW_SHELL"] = "1"  # stop_app/run_shell 需要
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server
server.DRYRUN = True
server.ALLOW_SHELL = True

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


def txt(r):
    content = r[0] if isinstance(r, tuple) else r
    return content[0]["text"]


def raises(fn):
    try:
        fn()
        return False, ""
    except ValueError as e:
        return True, str(e)
    except Exception as e:
        return False, "非预期异常: %s" % type(e).__name__


DEV = server.resolve_device(None)
print("设备:", DEV)

# ---- 入参校验（应抛 ValueError） ----
print("\n== 入参校验 ==")
ok, e = raises(lambda: server.t_tap({}))
check("t_tap 缺参报错", ok, e)
ok, e = raises(lambda: server.t_tap({"x": "abc", "y": 10}))
check("t_tap x 非整数报错", ok, e)
ok, e = raises(lambda: server.t_swipe({"x1": 1, "y1": 2, "x2": 3}))
check("t_swipe 缺参报错", ok, e)
ok, e = raises(lambda: server.t_input_text({"text": "   "}))
check("t_input_text 空串报错", ok, e)
ok, e = raises(lambda: server.t_key_event({}))
check("press_key 缺参报错", ok, e)

# ---- DRYRUN：命令生成（不真执行） ----
print("\n== DRYRUN 命令生成 ==")
r = server.t_tap({"x": 100, "y": 200})
print("  tap ->", txt(r))
check("tap 命令生成", "已在 (100, 200) 点击" in txt(r))

r = server.t_swipe({"x1": 0, "y1": 0, "x2": 0, "y2": 500})
print("  swipe ->", txt(r))
check("swipe 命令生成", "滑动到 (0,500)" in txt(r))

r = server.t_key_event({"keycode": "BACK"})
print("  press_key BACK ->", txt(r))
check("press_key(BACK) 命令生成", "已发送按键: BACK (code=4)" in txt(r))

r = server.t_key_event({"keycode": "POWER"})
print("  press_key POWER ->", txt(r))
check("press_key(POWER) 命令生成", "code=26" in txt(r))

r = server.t_input_text({"text": "hello world"})
print("  input_text(ASCII) ->", txt(r))
check("input_text ASCII 路由", "ASCII input" in txt(r))

r = server.t_input_text({"text": "你好世界"})
print("  input_text(中文) ->", txt(r))
check("input_text 中文 自动粘贴", "剪贴板粘贴" in txt(r))

r = server.t_shell({"command": "echo hi"})  # run_shell 别名，handler=t_shell
print("  run_shell(echo hi) DRYRUN ->", txt(r).replace("\n", " "))
check("run_shell DRYRUN 不真执行", "无输出" in txt(r))  # DRYRUN 不应真正执行

# ---- 真实只读：get_current_app ----
print("\n== 真实 get_current_app(只读) ==")
r = server.t_get_current_app({})
gca = txt(r)
print("  " + gca.replace("\n", "  "))
check("get_current_app 返回包名", "包名(package)" in gca and "Activity" in gca, gca[:60])

# ---- 真实 launch/stop 端到端（可逆） ----
print("\n== 真实 launch_app/stop_app 端到端 ==")
server.DRYRUN = False  # 关闭 DRYRUN 做真实动作
server.t_launch_app({"package": "com.android.settings"})
time.sleep(1.2)
r = server.t_get_current_app({})
gca2 = txt(r)
print("  launch 后:", gca2.replace("\n", "  "))
check("launch_app settings 生效", "com.android.settings" in gca2, gca2[:60])

r = server.t_shell({"command": "echo atomic_ok"})
print("  run_shell(echo atomic_ok) 真实 ->", txt(r).strip())
check("run_shell 真实执行返回输出", "atomic_ok" in txt(r))

server.t_force_stop({"package": "com.android.settings"})  # stop_app 别名
time.sleep(0.8)
r = server.t_get_current_app({})
gca3 = txt(r)
print("  stop 后:", gca3.replace("\n", "  "))
check("stop_app 已停止 settings", "com.android.settings" not in gca3, gca3[:60])

print("\n结果: PASS=%d  FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
