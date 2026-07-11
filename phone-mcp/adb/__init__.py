# -*- coding: utf-8 -*-
"""ADB 控制层 —— 设备管理 + 命令执行 + 重试"""
from .executor import (
    configure,
    list_devices,
    resolve_device,
    require_shell,
    forbid_catastrophic,
    run_adb,
    ADB,
    DEFAULT_DEVICE,
    DRYRUN,
    ALLOW_SHELL,
    ADB_TIMEOUT,
    ADB_RETRIES,
    log,
)
from .retry import with_retry, with_verification, poll
