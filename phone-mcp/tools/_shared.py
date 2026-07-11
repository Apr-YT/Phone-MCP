# -*- coding: utf-8 -*-
"""工具模块共享变量（由 server.py 注入配置后生效）。"""
import os, time

SHOT_DIR = None
FAST = False

def _ocr_debug(msg):
    """OCR 诊断日志。"""
    if SHOT_DIR is None:
        return
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        with open(os.path.join(SHOT_DIR, "ocr_debug.txt"), "a", encoding="utf-8") as f:
            f.write("[%.3f] %s\n" % (time.time(), msg))
    except Exception:
        pass

_OCR_READER = None
_OCR_LOCK = None

def get_ocr_reader():
    from threading import Lock
    global _OCR_READER, _OCR_LOCK
    if _OCR_READER is not None:
        return _OCR_READER
    if _OCR_LOCK is None:
        _OCR_LOCK = Lock()
    with _OCR_LOCK:
        if _OCR_READER is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_READER = RapidOCR()
    return _OCR_READER

def preview_ocr():
    """启动时预加载 OCR 引擎。"""
    try:
        get_ocr_reader()
        import sys
        print("[phone-mcp] OCR 引擎预加载完成(RapidOCR)", file=sys.stderr, flush=True)
    except Exception as e:
        print("[phone-mcp] OCR 预加载失败（首次调用时将重试）:", e, file=sys.stderr, flush=True)
