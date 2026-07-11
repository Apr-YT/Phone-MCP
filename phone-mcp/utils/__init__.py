# -*- coding: utf-8 -*-
"""
统一结果信封 —— 所有工具处理函数的返回值规范。

- ok(message, **data) → 成功信封
- fail(message, **data) → 失败信封
- text_block(text)   → MCP content 块
- image_block(b64, mime) → 图片 content 块
"""


def ok(message, **data):
    """构造统一成功信封 {success, message, data}。data 传结构化字段。"""
    return {"__envelope__": True, "success": True, "message": message, "data": data}


def fail(message, **data):
    """构造统一失败信封。"""
    return {"__envelope__": True, "success": False, "message": message, "data": data}


def text_block(text):
    return {"type": "text", "text": text}


def image_block(b64, mime):
    return {"type": "image", "data": b64, "mimeType": mime}
