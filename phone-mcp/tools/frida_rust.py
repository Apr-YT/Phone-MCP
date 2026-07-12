# -*- coding: utf-8 -*-
"""frida-rust 动态插桩集成。"""
import os, re, shlex

from adb import run_adb, log, resolve_device
from utils import ok, fail, text_block
from tools._shared import SHOT_DIR, _req

_FRIDA_BIN = "/data/local/tmp/frida-rust"

def _frida_run(device, subcmd, extra_args=None, timeout=60):
    parts = [_FRIDA_BIN, subcmd] + [shlex.quote(a) for a in (extra_args or [])]
    cmd = ["shell", "su", "-c", " ".join(parts)]
    r = run_adb(cmd, device=device, capture=True, timeout=timeout, what="frida-" + subcmd)
    return r.stdout or "", r.stderr or "", r.returncode


_SCRIPT_LINE = re.compile(r"\[脚本\]\s*(.*)")

def _extract_script_lines(out):
    """frida-rust 通过 `[脚本] <内容>` 打印脚本输出，提取每行 `[脚本]` 之后的内容。"""
    res = []
    for line in (out or "").splitlines():
        m = _SCRIPT_LINE.search(line)
        if m:
            res.append(m.group(1).strip())
    return res

def _blob_str_to_hex(out):
    """从脚本输出中提取首个 Blob.to_string() 结果 [41 42 43]，转成纯 hex 字符串。"""
    for ln in _extract_script_lines(out):
        cleaned = re.sub(r"[\s\[\]]+", "", ln)
        if cleaned:
            return cleaned
    return ""


def _build_blob_script(data_bytes, tail_lines):
    """生成构造 blob 的 Rhai 脚本：let __b = blob(N); __b[i]=0xNN; ... 后接 tail_lines。

    部署到设备的 frida-rust (v0.1.0, Rhai 1.25.1) 没有全局 blob([...]) 构造器，
    只能先用 blob(N) 创建定长 blob，再逐字节赋值。
    """
    n = len(data_bytes)
    lines = ["let __b = blob(%d);" % n]
    for i, b in enumerate(data_bytes):
        lines.append("__b[%d] = 0x%02x;" % (i, b))
    lines.extend(tail_lines)
    return "\n".join(lines)

def t_frida_inject(args):
    """使用 frida-rust 注入共享库到目标进程（ptrace + dlopen）。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    lib = args.get("libPath", "/data/local/tmp/libfrida_agent.so")
    out, err, rc = _frida_run(device, "inject", [str(pid), lib])
    if rc == 0:
        return ok("已注入 '%s' -> PID=%d" % (lib, pid), stdout=out.strip())
    return fail("注入失败 (rc=%d): %s" % (rc, (err or out).strip()))



def t_frida_attach(args):
    """使用 frida-rust ptrace 附着到目标进程。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    name = _req(args, "processName")
    out, err, rc = _frida_run(device, "attach", [name])
    if rc == 0:
        return ok("已附着 '%s'" % name, stdout=out.strip())
    return fail("附着失败 (rc=%d): %s" % (rc, (err or out).strip()))



def t_frida_script(args):
    """在目标进程上执行 Rhai 脚本（可选反检测）。脚本内容直接传入。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    script_content = _req(args, "script")
    pid = args.get("pid")
    anti_detect = args.get("antiDetect", False)

    # 写脚本到设备临时文件
    local_tmp = os.path.join(SHOT_DIR, "_frida_tmp.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script_content)
    device_tmp = "/data/local/tmp/_frida_tmp.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    cmd_args = [device_tmp]
    if pid is not None:
        cmd_args += ["--pid", str(pid)]
    if anti_detect:
        cmd_args += ["--anti-detect"]

    out, err, rc = _frida_run(device, "script", cmd_args, timeout=120)
    # 清理
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        script_out = "\n".join(_extract_script_lines(out)) or out.strip()
        return ok("脚本执行完成", stdout=script_out, stderr=err.strip())
    return fail("脚本执行失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_read_mem(args):
    """跨进程读取目标内存（返回十六进制）。frida-rust MemoryScanner。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    address = _req(args, "address")
    size = _req(args, "size", "int")
    if size > 0x100000:
        return fail("最大读取 1MB")

    # 用 Rhai 脚本读内存 (确保地址带 0x 前缀)
    if not address.startswith("0x"):
        address = "0x" + address
    script = 'let data = read_memory(%s, %d); log_info(data.to_string());' % (address, size)
    local_tmp = os.path.join(SHOT_DIR, "_frida_read.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_read.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--pid", str(pid)],
                               timeout=30)
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        return ok("已读取 %d 字节 @ %s (PID=%d)" % (size, address, pid),
                  hex_data=_blob_str_to_hex(out))
    return fail("内存读取失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_write_mem(args):
    """跨进程写入目标内存（hex_data 为十六进制字符串）。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    address = _req(args, "address")
    hex_data = _req(args, "hexData")
    data_bytes = bytes.fromhex(hex_data.replace(" ", ""))

    if not address.startswith("0x"):
        address = "0x" + address
    script = _build_blob_script(data_bytes, ['write_memory(%s, __b);' % address])
    local_tmp = os.path.join(SHOT_DIR, "_frida_write.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_write.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--pid", str(pid)],
                               timeout=30)
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        return ok("已写入 %d 字节 -> %s (PID=%d)" % (len(data_bytes), address, pid))
    return fail("内存写入失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_scan_mem(args):
    """在目标进程内存中搜索字节模式（返回匹配地址列表）。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    pattern = _req(args, "pattern")
    data_bytes = bytes.fromhex(pattern.replace(" ", ""))

    script = _build_blob_script(
        data_bytes,
        ['let results = search_bytes(__b);',
         'for addr in results { log_info("0x" + addr.to_string()); }'])
    local_tmp = os.path.join(SHOT_DIR, "_frida_scan.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_scan.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--pid", str(pid)],
                               timeout=60)
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        addresses = []
        for ln in _extract_script_lines(out):
            addresses += re.findall(r"0x[0-9a-fA-F]+", ln)
        return ok("找到 %d 个匹配" % len(addresses), addresses=addresses, raw=out.strip())
    return fail("内存扫描失败 (rc=%d): %s" % (rc, (err or out).strip()))


def t_frida_stealth(args):
    """对目标进程应用 frida-rust 全部反检测措施(TracerPid/maps/特征擦除)。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    pid = args.get("pid", 0)
    # binary 的 script 子命令必须指定脚本文件路径，这里推送一个 no-op 脚本承载 --anti-detect
    script = 'log_info("stealth-applied");'
    local_tmp = os.path.join(SHOT_DIR, "_frida_stealth.rhai")
    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    device_tmp = "/data/local/tmp/_frida_stealth.rhai"
    run_adb(["push", local_tmp, device_tmp], device=device, what="frida-push")

    out, err, rc = _frida_run(device, "script",
                               [device_tmp, "--anti-detect", "--pid", str(pid)])
    run_adb(["shell", "rm", "-f", device_tmp], device=device, what="frida-cleanup")
    try:
        os.remove(local_tmp)
    except OSError:
        pass
    if rc == 0:
        return ok("反检测措施已应用", stdout=out.strip())
    return fail("反检测失败 (rc=%d): %s" % (rc, (err or out).strip()))

