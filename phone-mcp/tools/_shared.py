# -*- coding: utf-8 -*-
"""工具模块共享变量（由 server.py 注入配置后生效）。"""
import os, time

# 默认截图目录（server.py 可在启动后覆盖），避免模块导入时拿到 None
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shots')
FAST = False

# ADBKeyBoard IME 标识（用于中文输入注入）
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"

# ADBKeyBoard 安装包路径（用于 phone_ui_input_setup 自动安装）
ADB_KEYBOARD_APK = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "ADBKeyboard.apk"
)

def _req(args, key, kind="str"):
    """取必填参数；缺失或类型不符时抛 ValueError（由 dispatch 转为参数错误）。"""
    if key not in args or args[key] is None:
        raise ValueError("缺少必填参数: %s" % key)
    v = args[key]
    if kind == "int":
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError("参数 %s 必须为整数，收到: %r" % (key, v))
    s = str(v)
    if kind == "str" and not s.strip():
        raise ValueError("参数 %s 不能为空" % key)
    return s.strip() if kind == "str" else v

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
