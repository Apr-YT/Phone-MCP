# -*- coding: utf-8 -*-
"""硬件控制工具：背光、振动、CPU、音频、防火墙。"""
import os, re

from adb import run_adb, log, resolve_device, require_shell, with_retry
from utils import ok, fail, text_block
# /sys 硬件节点调控（背光、震动、CPU）
# ---------------------------------------------------------------------------

def _backlight_node(device):
    """返回 (brightness_path, max_brightness) 或 (None, 0)。"""
    # 先用已知路径快匹配（避免 su -c 多命令引号问题）
    known = [
        "/sys/class/backlight/panel0-backlight",
        "/sys/class/backlight/backlight",
        "/sys/class/backlight/wled",
        "/sys/class/backlight/lcd-backlight",
    ]
    for p in known:
        try:
            mx = run_adb(["shell", "su", "-c", "cat %s" % shlex.quote(p + "/max_brightness")],
                        device=device, capture=True, what="bl-max").stdout or ""
            if mx.strip() and mx.strip() != "0":
                return "%s/brightness" % p, int(mx.strip())
        except Exception:
            continue
    # 回退：动态扫描（作为单引号包裹的完整 shell 命令）
    try:
        out = run_adb(["shell",
                       "su -c 'for d in /sys/class/backlight/*/; do b=$d/brightness; m=$d/max_brightness; "
                       "if [ -e $b ]; then echo $b $(cat $m 2>/dev/null); break; fi; done'"],
                      device=device, capture=True, what="bl-find").stdout or ""
        parts = out.split()
        if len(parts) >= 2:
            return parts[0], int(parts[1])
    except Exception:
        pass
    return None, 0


def t_brightness(args):
    """Root 直写背光滑块 sysfs 节点：读写亮度，支持百分比/原始值。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "get")
    node, mx = _backlight_node(device)
    if not node:
        return fail("未找到背光滑块 sysfs 节点(/sys/class/backlight/*/brightness)。可能不是受支持的设备。")
    cur = (run_adb(["shell", "su", "-c", "cat %s" % shlex.quote(node)],
                   device=device, capture=True, what="bright-get").stdout or "").strip()
    try:
        cur_v = int(cur)
    except Exception:
        cur_v = None
    if action == "get":
        return ok("当前亮度: %s / max %d%s" % (
            cur_v, mx, (" (%d%%)" % round(cur_v * 100.0 / mx)) if (cur_v is not None and mx) else ""),
            brightness=cur_v, max=mx,
            percent=(round(cur_v * 100.0 / mx) if (cur_v is not None and mx) else None))
    # set
    raw = args.get("raw", False)
    level = args.get("level")
    if level is None:
        return fail("set 动作需要 level 参数(0-100 百分比，或 raw=True 时 0-max 原始值)。")
    level = int(level)
    if raw:
        target = level
    else:
        if not (0 <= level <= 100):
            return fail("level 百分比需在 0-100 之间，或用 raw=True 传原始值。")
        target = int(round(level * mx / 100.0))
    target = max(0, min(target, mx))
    run_adb(["shell", "su", "-c", "echo %d > %s" % (target, node)],
            device=device, mutating=True, what="bright-set")
    new_v = (run_adb(["shell", "su", "-c", "cat %s" % shlex.quote(node)],
                     device=device, capture=True, what="bright-get2").stdout or "").strip()
    try:
        new_v = int(new_v)
    except Exception:
        new_v = target
    return ok("亮度已设置: %s -> %s (max %d)" % (cur_v, new_v, mx),
              old=cur_v, new=new_v, max=mx,
              percent=(round(new_v * 100.0 / mx) if mx else None))


def t_vibrate(args):
    """触发手机震动：sysfs 节点 -> cmd vibrator -> AIDL HAL service call 三级回退。"""
    device = resolve_device(args.get("deviceSerial"))
    ms = int(args.get("durationMs", 200))
    ms = max(10, min(ms, 60000))
    # 1) 经典 sysfs 节点
    sysfs = "/sys/class/timed_output/vibrator/enable"
    try:
        test = (run_adb(["shell", "su", "-c", "test -w %s && echo Y" % sysfs],
                        device=device, capture=True, what="vib-sysfs").stdout or "").strip()
        if test == "Y":
            run_adb(["shell", "su", "-c", "echo %d > %s" % (ms, sysfs)],
                    device=device, mutating=True, what="vib-set")
            return ok("已通过 sysfs 节点触发震动(%dms)。" % ms, method="sysfs", durationMs=ms)
    except Exception:
        pass
    # 2) cmd vibrator
    try:
        r = run_adb(["shell", "cmd", "vibrator", "vibrate", str(ms)],
                    device=device, capture=True, what="vib-cmd")
        if r.returncode == 0:
            return ok("已通过 cmd vibrator 触发震动(%dms)。" % ms, method="cmd", durationMs=ms)
    except Exception:
        pass
    # 3) AIDL HAL (best effort)
    try:
        r = run_adb(["shell", "service", "call",
                     "android.hardware.vibrator.IVibrator/default", "1", "i32", str(ms)],
                    device=device, capture=True, what="vib-hal")
        combined = (r.stdout or "") + (r.stderr or "")
        if "does not exist" not in combined and "error" not in combined.lower():
            return ok("已通过 AIDL HAL 触发震动(%dms)。" % ms, method="hal", durationMs=ms)
    except Exception:
        pass
    return fail("本设备无法通过 shell/sysfs 触发震动。\n"
                "原因: Android 16 / HyperOS 已移除 /sys/class/timed_output/vibrator/enable，"
                "且无 `cmd vibrator` 命令；vibrator 仅暴露为 binder HAL 无 shell 入口。\n"
                "可行替代: 安装调用 VibratorManager 的小 App，或用通知(带 vibrate)实现提醒。",
                method=None)


def t_cpu(args):
    """Root 调控 CPU：查看状态、切换 governor、上线/下线核心、设最大频率。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "list")
    if action == "list":
        online = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/online"],
                         device=device, capture=True, what="cpu-online").stdout or "").strip()
        gov = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"],
                       device=device, capture=True, what="cpu-gov").stdout or "").strip()
        avail = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"],
                         device=device, capture=True, what="cpu-avail").stdout or "").strip()
        avail_f = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies"],
                           device=device, capture=True, what="cpu-freq").stdout or "").strip()
        maxf = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq"],
                        device=device, capture=True, what="cpu-maxf").stdout or "").strip()
        return ok("CPU 状态", online=online, governor=gov,
                  availableGovernors=avail.split(), availableFrequenciesKHz=avail_f.split(),
                  maxFreqKHz=maxf)
    if action == "set_governor":
        gov = args.get("governor")
        if not gov:
            return fail("set_governor 需要 governor 参数。")
        run_adb(["shell", "su", "-c",
                 "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo %s > $f; done"
                 % shlex.quote(gov)], device=device, mutating=True, what="cpu-gov-set")
        return ok("已设置所有 CPU 核心 governor=%s。" % gov, governor=gov)
    if action in ("online_core", "offline_core"):
        core = args.get("core")
        if core is None:
            return fail("%s 需要 core 参数(如 7 表示 cpu7)。" % action)
        core = int(core)
        val = 1 if action == "online_core" else 0
        run_adb(["shell", "su", "-c", "echo %d > /sys/devices/system/cpu/cpu%d/online" % (val, core)],
                device=device, mutating=True, what="cpu-online-set")
        now = (run_adb(["shell", "su", "-c", "cat /sys/devices/system/cpu/cpu%d/online" % core],
                       device=device, capture=True, what="cpu-online-get").stdout or "").strip()
        return ok("已%s cpu%d，当前 online=%s。" % (
            "上线" if val else "下线", core, now), core=core, online=now)
    if action == "set_max_freq":
        freq = args.get("freqKHz")
        if freq is None:
            return fail("set_max_freq 需要 freqKHz 参数(单位 kHz，需是 availableFrequencies 之一)。")
        run_adb(["shell", "su", "-c",
                 "for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo %d > $f; done"
                 % int(freq)], device=device, mutating=True, what="cpu-freq-set")
        return ok("已设置所有核心 max_freq=%d kHz。" % int(freq), freqKHz=int(freq))
    return fail("未知 action: %s" % action)


# ---------------------------------------------------------------------------
# 音频底层操控（cmd audio，绕过系统设置 UI）
# ---------------------------------------------------------------------------

_AUDIO_STREAMS = {
    "voice_call": 0, "system": 1, "ring": 2, "music": 3, "media": 3,
    "alarm": 4, "notification": 5,
}


def _audio_stream_idx(stream):
    s = str(stream or "3")
    if s.isdigit():
        return int(s)
    return _AUDIO_STREAMS.get(s.lower(), 3)


def t_audio(args):
    """Root 操控音频：获取/设置媒体音量、静音/取消静音。走 cmd audio(AudioService)，绕过系统设置 UI。
    注：/dev/snd 原始 PCM 读写改变的是采样数据，不是音量；音量通过 cmd audio 控制才是正确的底层 CLI。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "get")
    stream = _audio_stream_idx(args.get("stream", "music"))
    if action == "get":
        cur = (run_adb(["shell", "cmd", "audio", "get-stream-volume", str(stream)],
                       device=device, capture=True, what="aud-get").stdout or "").strip()
        mx = (run_adb(["shell", "cmd", "audio", "get-max-volume", str(stream)],
                      device=device, capture=True, what="aud-max").stdout or "").strip()
        m1 = re.search(r"->\s*(\d+)", cur)
        m2 = re.search(r"->\s*(\d+)", mx)
        cv = int(m1.group(1)) if m1 else None
        mv = int(m2.group(1)) if m2 else None
        return ok("音量(流%d): %s / %s" % (stream, cv, mv), stream=stream,
                  volume=cv, max=mv,
                  percent=(round(cv * 100.0 / mv) if (cv is not None and mv) else None))
    if action == "set_volume":
        level = args.get("level")
        if level is None:
            return fail("set_volume 需要 level 参数(0-max 整数)。")
        run_adb(["shell", "cmd", "audio", "set-volume", str(stream), str(int(level))],
                device=device, mutating=True, what="aud-set")
        return ok("已设置流%d音量为 %d。" % (stream, int(level)), stream=stream, volume=int(level))
    if action == "mute":
        run_adb(["shell", "cmd", "audio", "adj-mute", str(stream)],
                device=device, mutating=True, what="aud-mute")
        return ok("已静音流%d。" % stream, stream=stream, muted=True)
    if action == "unmute":
        run_adb(["shell", "cmd", "audio", "adj-unmute", str(stream)],
                device=device, mutating=True, what="aud-unmute")
        return ok("已取消静音流%d。" % stream, stream=stream, muted=False)
    return fail("未知 action: %s" % action)


# ---------------------------------------------------------------------------
# iptables Root 防火墙
# ---------------------------------------------------------------------------

def _app_uid(device, package):
    """由包名解析 Android uid: 先查 /data/system/packages.list(root)，再回退 dumpsys。"""
    try:
        out = (run_adb(["shell", "su", "-c",
                        "grep ^%s /data/system/packages.list 2>/dev/null | awk '{print $2}'"
                        % shlex.quote(package)],
                       device=device, capture=True, what="pkg-uid").stdout or "").strip()
        if out and out.isdigit():
            return int(out)
    except Exception:
        pass
    out2 = (run_adb(["shell", "dumpsys", "package", package],
                    device=device, capture=True, what="pkg-dumpsys").stdout or "")
    m = re.search(r"userId=(\d+)", out2)
    if m:
        return int(m.group(1))
    return None


def t_net_firewall(args):
    """Root iptables 防火墙：按 App uid/包名拦截网络(OUTPUT DROP)、解封、查看规则、清空。需 root。"""
    device = resolve_device(args.get("deviceSerial"))
    action = args.get("action", "list")
    if action == "list":
        out = (run_adb(["shell", "su", "-c",
                        "iptables -L OUTPUT -n -v 2>&1; echo ---V6---; ip6tables -L OUTPUT -n -v 2>&1"],
                       device=device, capture=True, what="fw-list").stdout or "")
        rules = [l.strip() for l in out.splitlines() if l.strip()]
        return ok("当前 OUTPUT 链规则(含 owner 拦截): %d 条" % len(rules),
                  rules=rules, raw=out)
    if action in ("block_app", "unblock_app"):
        package = args.get("package")
        uid = args.get("uid")
        if uid is None and package:
            uid = _app_uid(device, package)
        if uid is None:
            return fail("需要 uid 或有效的 package(以解析 uid)。")
        uid = int(uid)
        if action == "block_app":
            run_adb(["shell", "su", "-c",
                     "iptables -C OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null || "
                     "iptables -A OUTPUT -m owner --uid-owner %d -j DROP" % (uid, uid)],
                    device=device, mutating=True, what="fw-block")
            run_adb(["shell", "su", "-c",
                     "ip6tables -C OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null || "
                     "ip6tables -A OUTPUT -m owner --uid-owner %d -j DROP" % (uid, uid)],
                    device=device, mutating=True, what="fw-block6")
            return ok("已拦截 uid=%d 的所有网络(IPv4+IPv6 OUTPUT DROP)。" % uid,
                      uid=uid, package=package, blocked=True)
        else:
            run_adb(["shell", "su", "-c",
                     "iptables -D OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null; "
                     "ip6tables -D OUTPUT -m owner --uid-owner %d -j DROP 2>/dev/null" % (uid, uid)],
                    device=device, mutating=True, what="fw-unblock")
            return ok("已解除对 uid=%d 的网络拦截。" % uid, uid=uid, package=package, blocked=False)
    if action == "clear_all":
        run_adb(["shell", "su", "-c", "iptables -F OUTPUT; ip6tables -F OUTPUT"],
                device=device, mutating=True, what="fw-clear")
        return ok("已清空 OUTPUT 链全部规则（含非本工具添加的规则，请慎用）。", cleared=True)
    return fail("未知 action: %s" % action)



# ======================== frida-rust 集成 ========================
_FRIDA_BIN = "/data/local/tmp/frida-rust"

