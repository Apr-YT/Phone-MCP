# -*- coding: utf-8 -*-
"""工具调度层：统一异常捕获 + 信封归一化 + 工具级重试 + 日志。"""
import json, time, subprocess, os
import sys

from utils import ok, fail, text_block, image_block
from adb import log
from protocol.registry import TOOLS
from tools.learner import recall_text

_TOOL_RETRIES = int(__import__('os').environ.get("PHONE_MCP_TOOL_RETRIES", "2"))

def _is_transient(e):
    """判断异常是否'瞬时/可重试'（adb 抖动、设备断开、超时等）。
    入参校验(ValueError)与权限(PermissionError)不属于可重试。"""
    if isinstance(e, (ValueError, PermissionError)):
        return False
    if isinstance(e, (subprocess.TimeoutExpired, OSError, BrokenPipeError)):
        return True
    msg = str(e).lower()
    return any(k in msg for k in (
        "device not found", "closed", "broken pipe", "timed out",
        "connection", "no such device", "transport",
    ))


def _normalize_result(res):
    """把 handler 返回值统一成 (envelope_dict, image_block_or_None)。
    兼容：① 新式信封 dict(ok/fail) ② 旧式 (content_list, is_error) ③ 旧式 content_list。"""
    if isinstance(res, dict) and res.get("__envelope__"):
        env = {"success": res["success"], "message": res["message"],
               "data": res.get("data") or {}}
        image = None
        if env["data"].get("image_b64"):
            image = image_block(env["data"]["image_b64"],
                                env["data"].get("image_mime", "image/png"))
            env["data"].pop("image_b64", None)
            env["data"].pop("image_mime", None)
        return env, image
    if isinstance(res, tuple):
        content, is_error = res
    else:
        content, is_error = res, False
    texts = []
    image = None
    for blk in content:
        t = blk.get("type")
        if t == "text":
            texts.append(blk.get("text", ""))
        elif t == "image":
            image = blk
    message = "\n".join(x for x in texts if x).strip()
    return {"success": not is_error, "message": message, "data": {}}, image


def log_tool(name, args, success, message, dt_ms, attempts=1):
    """统一工具日志：入参(脱敏预览) + 结果 + 耗时。"""
    try:
        preview = {k: (v if not isinstance(v, str) or len(v) < 80 else v[:80] + "…")
                   for k, v in (args or {}).items()}
        arg_s = json.dumps(preview, ensure_ascii=False)
    except Exception:
        arg_s = str(args)
    tag = "OK " if success else "ERR"
    log("[%s] %s 入参=%s 耗时=%.1fms 重试=%d 结果=%s"
        % (tag, name, arg_s, dt_ms, attempts, (message or "")[:200]))


# ---- 经验自动召回（phone-mcp 训练机制的关键闭环）----
# 原设计靠 LLM 主动调 phone_learn_recall，经常漏调 → 经验白训。
# 这里在每次工具执行后、结果返回给 LLM 之前，自动用
#   「工具名 + 入参 + 结果摘要」拼成情境召回相关经验，追加进返回文本。
# 这样 LLM 在决定下一步操作时，必然能看到匹配经验，训练真正生效。
_AUTO_RECALL_SKIP = {"phone_learn_recall", "phone_learn_reflect"}  # 跳过自身，防递归/冗余
_AUTO_RECALL_LIMIT = int(os.environ.get("PHONE_MCP_RECALL_LIMIT", "3"))


def _auto_recall(name, arguments, env):
    """把相关经验追加进 env['message']。纯锦上添花：任何异常都静默跳过。"""
    try:
        if os.environ.get("PHONE_MCP_AUTO_RECALL", "1") == "0":
            return  # 开关关了就不召回（默认开）
        if name in _AUTO_RECALL_SKIP:
            return
        # 构造情境。注意：所有工具名都以 "phone_" 开头，若不去除，
        # "phone" 这个 token 会零区分度地命中几乎所有经验 → 召回变噪声。
        raw = "%s %s %s" % (
            name,
            json.dumps(arguments or {}, ensure_ascii=False)[:160],
            (env.get("message") or "")[:200],
        )
        situation = raw.replace("phone_", " ")  # 清洗零区分度前缀
        text = recall_text(situation, limit=_AUTO_RECALL_LIMIT)
        if text and text != "(无相关经验)":
            env["message"] = (env.get("message") or "") \
                + "\n\n【📚 相关经验自动召回】\n" + text
    except Exception as e:
        log("[AUTO_RECALL] 跳过(异常): %r" % e)


def _env_content(env, image):
    content = [text_block(json.dumps(env, ensure_ascii=False))]
    if image is not None:
        content.insert(0, image)
    return content


def dispatch_tool(name, arguments, req_id):
    """统一调度：异常捕获(绝不崩溃) + 瞬时异常重试 + 统一信封 + 日志。"""
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if not tool:
        env = fail("未知工具: %s" % name)
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"isError": True, "content": _env_content(env, None)}}
    attempts = 0
    while attempts < _TOOL_RETRIES + 1:
        attempts += 1
        t0 = time.time()
        try:
            res = tool["handler"](arguments)
            env, image = _normalize_result(res)
            dt = (time.time() - t0) * 1000
            log_tool(name, arguments, env["success"], env["message"], dt, attempts)
            _auto_recall(name, arguments, env)
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": _env_content(env, image),
                               "isError": (not env["success"])}}
        except (ValueError, PermissionError) as e:
            dt = (time.time() - t0) * 1000
            msg = str(e)
            if isinstance(e, ValueError):
                msg = "参数错误: " + msg
            env = fail(msg)
            log_tool(name, arguments, False, env["message"], dt, attempts)
            _auto_recall(name, arguments, env)
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": _env_content(env, None), "isError": True}}
        except Exception as e:
            dt = (time.time() - t0) * 1000
            if not _is_transient(e) or attempts >= _TOOL_RETRIES + 1:
                env = fail("执行失败: %s" % e)
                log_tool(name, arguments, False, env["message"], dt, attempts)
                _auto_recall(name, arguments, env)
                return {"jsonrpc": "2.0", "id": req_id,
                        "result": {"content": _env_content(env, None), "isError": True}}
            log("[RETRY] %s 第 %d/%d 次瞬时异常: %r"
                % (name, attempts, _TOOL_RETRIES, e))
            time.sleep(0.4)
    env = fail("执行失败: 未知错误")
    _auto_recall(name, arguments, env)
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": _env_content(env, None), "isError": True}}
