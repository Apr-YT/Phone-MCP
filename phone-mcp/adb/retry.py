# -*- coding: utf-8 -*-
"""
重试 / 轮询 / 校验工具 —— 纯逻辑，无模块依赖。
"""

import time


def log(*args):
    """日志写 stderr。"""
    import sys
    print("[phone-mcp]", *args, file=sys.stderr, flush=True)


def with_retry(fn, retries=2, delay=0.4, what="操作"):
    """执行 fn（无参可调用）；失败自动重试。

    返回 (ok, result)：成功时 ok=True，result 为 fn 的返回值；
    全部失败时 ok=False，result 为最后一次异常。
    """
    last = None
    for i in range(1, retries + 1):
        try:
            return True, fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log("%s 第 %d/%d 次失败: %r" % (what, i, retries, e))
            if i < retries:
                time.sleep(delay)
    return False, last


def with_verification(action_fn, verify_fn, max_retries=2, delay=0.6):
    """操作后自动校验：执行 action_fn，再用 verify_fn 检查是否成功；失败则重试。"""
    last = None
    for i in range(1, max_retries + 1):
        try:
            last = action_fn()
        except Exception as e:  # noqa: BLE001
            last = e
        try:
            if verify_fn(last):
                return True, last
        except Exception:
            pass
        if i < max_retries:
            time.sleep(delay)
    return False, last


def poll(verify_fn, tries=5, interval=0.4):
    """轮询检测：每 interval 秒调用 verify_fn()，一旦返回真立即返回 True。"""
    for _ in range(tries):
        try:
            if verify_fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False
