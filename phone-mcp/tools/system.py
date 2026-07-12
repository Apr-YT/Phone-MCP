# -*- coding: utf-8 -*-
"""系统操作工具：shell、进程、属性、文件、安装/卸载、内核进程、微信数据库。"""
import os, re, shlex, subprocess, hashlib, time

from adb import run_adb, log, resolve_device, require_shell, forbid_catastrophic, list_devices
from utils import ok, fail, text_block
from tools._shared import SHOT_DIR, _req

# 解析当前前台应用（包名 + Activity），用于 get_current_app
_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\b([\w.\-/$]+)/([\w.\-/$]+)")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=AppWindowToken\{[^}]*?\b([\w.\-/$]+)/([\w.\-/$]+)")
def t_shell(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    cmd = args["command"]
    forbid_catastrophic(cmd)
    r = run_adb(["shell", cmd], device=device, mutating=True)
    out = (r.stdout or "") + (r.stderr or "")
    return [text_block(out or "(无输出)")]
def t_run_adb(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    raw = args["args"]
    if isinstance(raw, str):
        raw = raw.split()
    forbid_catastrophic(" ".join(raw))
    r = run_adb(raw, device=device, mutating=True)
    out = (r.stdout or "") + (r.stderr or "")
    return [text_block(out or "(无输出)")]
def t_list_packages(args):
    device = resolve_device(args.get("deviceSerial"))
    base = ["shell", "pm", "list", "packages"]
    filt = args.get("filter")
    if filt:
        base.append(filt)
    r = run_adb(base, device=device, mutating=False)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > 60000:
        out = out[:60000] + "\n... (已截断)"
    return [text_block(out or "(无输出)")]
def t_list_processes(args):
    device = resolve_device(args.get("deviceSerial"))
    r = run_adb(["shell", "ps", "-A"], device=device, mutating=False)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > 60000:
        out = out[:60000] + "\n... (已截断)"
    return [text_block(out or "(无输出)")]
def t_start_service(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    pkg, svc = args["package"], args["service"]
    run_adb(["shell", "am", "startservice", "-n", "%s/%s" % (pkg, svc)],
            device=device, mutating=True)
    return [text_block("已尝试启动服务 %s/%s。" % (pkg, svc))]
def t_force_stop(args):
    require_shell()
    pkg = _req(args, "package")
    device = resolve_device(args.get("deviceSerial"))
    run_adb(["shell", "am", "force-stop", pkg], device=device, mutating=True)
    return [text_block("已强制停止 %s。" % pkg)]
def t_get_current_app(args):
    """返回当前前台应用包名与 Activity。
    依次尝试：mCurrentFocus / mFocusedApp / mResumedActivity（兜底，覆盖更多 ROM/版本）。
    只读。"""
    device = resolve_device(args.get("deviceSerial"))
    try:
        r = run_adb(["shell", "dumpsys", "window"], device=device,
                    mutating=False, what="dumpsys window")
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return fail("获取当前应用失败: %s" % e)
    m = _FOCUS_RE.search(out) or _FOCUSED_APP_RE.search(out)
    if m:
        pkg, act = m.group(1), m.group(2)
        return ok("当前前台应用：\n  包名(package): %s\n  Activity: %s" % (pkg, act),
                  package=pkg, activity=act)
    # 兜底：mResumedActivity（_foreground_activity 已验证在 Android 16 上可用）
    try:
        fg = _foreground_activity(device)
        if fg.get("package"):
            return ok("当前前台应用：\n  包名(package): %s\n  Activity: %s"
                      % (fg.get("package"), fg.get("activity")),
                      package=fg.get("package"), activity=fg.get("activity"),
                      via="mResumedActivity")
    except Exception:
        pass
    return fail("未能解析当前前台应用（可能无前台界面或 dumpsys 无输出）。")

def t_kill_process(args):
    """结束进程。type="pid" 用 kill；否则用 force-stop。"""
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    target = str(args["target"])
    if args.get("type") == "pid":
        run_adb(["shell", "kill", target], device=device, mutating=True)
    else:
        run_adb(["shell", "am", "force-stop", target], device=device, mutating=True)
    return [text_block("已结束进程: %s。" % target)]
def t_getprop(args):
    device = resolve_device(args.get("deviceSerial"))
    key = args.get("key")
    cmd = ["shell", "getprop"] + ([key] if key else [])
    r = run_adb(cmd, device=device, mutating=False)
    return [text_block((r.stdout or r.stderr or "(无输出)").strip())]
def t_setprop(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    forbid_catastrophic(args.get("key", ""))
    run_adb(["shell", "setprop", args["key"], args["value"]],
            device=device, mutating=True)
    return [text_block("已设置属性 %s=%s。" % (args["key"], args["value"]))]
def t_settings_get(args):
    device = resolve_device(args.get("deviceSerial"))
    r = run_adb(["shell", "settings", "get", args["namespace"], args["key"]],
                device=device, mutating=False)
    return [text_block((r.stdout or r.stderr or "(无输出)").strip())]
def t_settings_put(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    run_adb(["shell", "settings", "put", args["namespace"], args["key"], args["value"]],
            device=device, mutating=True)
    return [text_block("已设置 settings %s/%s=%s。" % (args["namespace"], args["key"], args["value"]))]
def t_file_read(args):
    device = resolve_device(args.get("deviceSerial"))
    path = args["path"]
    r = run_adb(["shell", "cat", path], device=device, mutating=False)
    out = (r.stdout or "") + (r.stderr or "")
    if len(out) > 200000:
        out = out[:200000] + "\n... (已截断)"
    return [text_block("读取 %s:\n%s" % (path, out or "(空或无权读取)"))]

def t_file_write(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    path = args["path"]
    content = args["content"]
    os.makedirs(SHOT_DIR, exist_ok=True)
    tmp = os.path.join(SHOT_DIR, "_write_tmp.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        run_adb(["push", tmp, path], device=device, mutating=True)
    finally:
        try: os.remove(tmp)
        except OSError: pass
    return [text_block("已通过 push 写入 %s。" % path)]
def t_install_apk(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    local = args["localPath"]
    if not os.path.isfile(local):
        return [text_block("本地 APK 不存在: %s" % local)], True
    run_adb(["install", local], device=device, mutating=True)
    return [text_block("已安装 %s。" % local)]
def t_uninstall(args):
    require_shell()
    device = resolve_device(args.get("deviceSerial"))
    forbid_catastrophic(args.get("package", ""))
    run_adb(["uninstall", args["package"]], device=device, mutating=True)
    return [text_block("已卸载 %s（应用数据一并清除）。" % args["package"])]

# ---- 内核态进程 + 微信数据库 ----
def _proc_list(device, flt=None, limit=3000):
    """枚举全部进程(PID/PPID/UID/RSS/CMD)。
    等价于遍历 /proc/[0-9]*/cmdline+status；ps 本身就是 /proc 读取器，
    走 root 以看到其它用户(u0_a*)的进程。"""
    proc = run_adb(["shell", "su", "-c", "ps -A -o PID,PPID,UID,RSS,CMD"],
                   device=device, capture=True, what="ps")
    out = proc.stdout or ""
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, uid, rss, cmd = parts
        if flt and flt not in cmd and flt not in pid:
            continue
        try:
            rows.append({"pid": int(pid), "ppid": int(ppid), "uid": uid,
                         "rssKb": int(rss), "cmd": cmd.strip()})
        except ValueError:
            continue
        if len(rows) >= limit:
            break
    return rows
def _foreground_activity(device):
    """取前台 Activity（package + activity）。
    dumpsys activity 解析 mResumedActivity，交叉校验 window mCurrentFocus。"""
    out = (run_adb(["shell", "dumpsys", "activity", "activities"],
                   device=device, capture=True, what="dumpsys").stdout or "")
    pkg = act = None
    m = re.search(r"mResumedActivity.*?(\S+)/(\S+)", out)
    if not m:
        m = re.search(r"ResumedActivity.*?(\S+)/(\S+)", out)
    if m:
        pkg, act = m.group(1), m.group(2)
    try:
        w = (run_adb(["shell", "dumpsys", "window", "windows"],
                     device=device, capture=True, what="dumpsys").stdout or "")
        fm = re.search(r"mCurrentFocus=.*?(\S+)/(\S+)", w)
        if fm and not pkg:
            pkg, act = fm.group(1), fm.group(2)
    except Exception:
        pass
    return {"package": pkg, "activity": act}
def _proc_read(device, pid):
    """直读 /proc/<pid>/cmdline 与 /proc/<pid>/status（内核态原始信息）。"""
    pid = int(pid)
    cmd = (run_adb(["shell", "su", "-c", "cat /proc/%d/cmdline" % pid],
                   device=device, capture=True, binary=True, what="proc-cmdline").stdout or b"").replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    status = (run_adb(["shell", "su", "-c", "cat /proc/%d/status" % pid],
                      device=device, capture=True, binary=True, what="proc-status").stdout or b"").decode("utf-8", "replace")
    info = {}
    for ln in status.splitlines():
        if ln.startswith(("Name:", "State:", "PPid:", "Uid:", "VmRSS:", "VmSize:")):
            k, _, v = ln.partition(":")
            info[k.strip()] = v.strip()
    return {"pid": pid, "cmdline": cmd, "status": info}
def _wechat_db_path(device):
    out = (run_adb(["shell", "su", "-c",
                    "ls /data/data/com.tencent.mm/MicroMsg/*/EnMicroMsg.db"],
                   device=device, capture=True, what="ls-db").stdout or "")
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.endswith("EnMicroMsg.db"):
            return ln
    return None
def _wechat_pulled_db_local():
    return os.path.join(SHOT_DIR, "wechat", "EnMicroMsg.db")
def t_ps(args):
    device = resolve_device(args.get("deviceSerial"))
    flt = args.get("filter")
    rows = _proc_list(device, flt=flt)
    fg = _foreground_activity(device)
    fpkg = fg.get("package")
    for r in rows:
        r["foreground"] = bool(fpkg) and r["cmd"].startswith(fpkg)
    n_wechat = sum(1 for r in rows if "com.tencent.mm" in r["cmd"])
    summary = "进程数: %d（含微信相关 %d）\n前台: %s/%s" % (
        len(rows), n_wechat, fg.get("package"), fg.get("activity"))
    return ok(summary, count=len(rows), foreground=fg,
              processes=rows[:200])
def t_proc_read(args):
    device = resolve_device(args.get("deviceSerial"))
    pid = _req(args, "pid", "int")
    return ok("已读取 /proc/%d 原始信息。" % pid, **_proc_read(device, pid))
def t_kill(args):
    device = resolve_device(args.get("deviceSerial"))
    pid = args.get("pid")
    package = args.get("package")
    if pid is None and not package:
        return fail("请提供 pid 或 package 之一。")
    targets = []
    if pid is not None:
        pid = int(pid)
        run_adb(["shell", "su", "-c", "kill -9 %d" % pid],
                device=device, mutating=True, what="kill")
        targets.append("pid=%d" % pid)
    if package:
        run_adb(["shell", "su", "-c", "pkill -9 -f %s" % shlex.quote(package)],
                device=device, mutating=True, what="pkill")
        targets.append("package=%s" % package)
    time.sleep(0.5)
    remain = []
    if pid is not None:
        try:
            remain = _proc_list(device, flt=str(pid))
        except Exception:
            pass
    gone = (pid is None) or (not any(r["pid"] == pid for r in remain))
    return ok("已发送 SIGKILL: %s。%s" % (
        ", ".join(targets),
        "进程已退出。" if gone else "进程仍在(可能已被同类保活机制重新拉起)。"),
        targets=targets, killed=gone)
def t_wechat_db_pull(args):
    device = resolve_device(args.get("deviceSerial"))
    src = _wechat_db_path(device)
    if not src:
        return fail("未找到 EnMicroMsg.db（微信未安装或未初始化？）")
    base = src[:-3]  # 去掉 .db
    dest_dir = os.path.join(SHOT_DIR, "wechat")
    os.makedirs(dest_dir, exist_ok=True)
    listing = (run_adb(["shell", "su", "-c", "ls -1 %s*" % shlex.quote(base)],
                       device=device, capture=True, what="ls-db").stdout or "").split()
    pulled = []
    for s in listing:
        s = s.strip()
        if not s:
            continue
        remote = "/data/local/tmp/wxdb_%s" % os.path.basename(s).replace(".", "_")
        local = os.path.join(dest_dir, os.path.basename(s))
        try:
            run_adb(["shell", "su", "-c", "cp %s %s" % (shlex.quote(s), remote)],
                    device=device, mutating=True, what="cp-db")
            run_adb(["pull", remote, local], device=device, what="pull-db")
            pulled.append(local)
        except Exception:
            pass
    return ok("已 root 拉取微信加密数据库(EnMicroMsg.db + 配套文件)到本地。\n"
              "注意：这是 SQLCipher 加密库，需用密钥解密才能读内容(见 phone_wechat_db_decrypt)。",
              dbPath=src, pulled=pulled,
              localDb=_wechat_pulled_db_local() if os.path.exists(_wechat_pulled_db_local()) else None)
def t_wechat_db_decrypt(args):
    """尝试解密微信 EnMicroMsg.db。
    注：微信 8.x 的密钥是随机 256-bit，存于微信进程内存，本机需 SQLCipher 库 +
    frida 取出的密钥才能真正解密。本函数做：①legacy 候选密钥(md5(imei+uin)[:7])计算；
    ②若本机装了 SQLCipher 且提供了 key，则直接解密读联系人/消息。"""
    device = resolve_device(args.get("deviceSerial"))
    key = args.get("key")
    use_legacy = bool(args.get("legacy", True))
    candidate = None
    if use_legacy and not key:
        try:
            import hashlib
            prefs = (run_adb(["shell", "su", "-c",
                              "cat /data/data/com.tencent.mm/shared_prefs/system_config_prefs.xml"],
                             device=device, capture=True, what="prefs").stdout or "")
            m = re.search(r'name="(?:_uin|default_uin)"\s+value="(-?\d+)"', prefs)
            uin = m.group(1) if m else None
            if uin:
                cand = hashlib.md5(("1234567890ABCDEF" + uin).encode()).hexdigest()[:7].lower()
                candidate = cand
        except Exception:
            candidate = None
    sqlcipher = None
    try:
        import pysqlcipher3.dbapi2 as sqlcipher
    except Exception:
        try:
            import sqlcipher3.dbapi2 as sqlcipher
        except Exception:
            sqlcipher = None
    if sqlcipher is None:
        msg = ("本机未安装 SQLCipher 库(pysqlcipher3/sqlcipher3)，无法在此解密。\n"
               "可行路径：\n"
               "1) 安装: pip install pysqlcipher3（需本机有 libsqlcipher 开发文件/编译链，Windows 常失败）\n"
               "2) 用 frida 挂钩微信 sqlite3_key 取出 8.0.x 的 256-bit 密钥，再作为 key 参数传入\n"
               "3) 把已拉取的 EnMicroMsg.db 用 sqlcipher CLI / DB Browser 离线解密")
        if candidate:
            msg += ("\n\nlegacy 候选密钥 md5(imei+uin)[:7] = %s\n"
                    "（仅对微信 <7.x 有效，对 8.0.74 大概率无效）" % candidate)
        return fail(msg)
    db_local = args.get("dbPath") or _wechat_pulled_db_local()
    if not os.path.exists(db_local):
        return fail("本地未找到数据库文件: %s（请先调用 phone_wechat_db_pull）" % db_local)
    if not key:
        return fail("已检测到 SQLCipher 库，但缺密钥(key 参数)。\n"
                    "8.0.x 需要 frida 取出的 256-bit 密钥；legacy 候选=%s(可能无效)。" % candidate)
    try:
        conn = sqlcipher.connect(db_local)
        conn.execute("PRAGMA key = \"%s\"" % key)
        n = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        contacts = []
        try:
            for row in conn.execute(
                    "SELECT username, nickname FROM rcontact "
                    "WHERE username LIKE '%@chatroom' OR username LIKE 'wxid%' LIMIT 50"):
                contacts.append({"username": row[0], "nickname": row[1]})
        except Exception:
            pass
        conn.close()
        return ok("解密成功！sqlite_master 表数=%d。" % n,
                  tableCount=n, contacts=contacts[:50], legacyCandidate=candidate)
    except Exception as e:
        return fail("解密失败(密钥错误或无法打开): %s" % e)


# ===========================================================================
# 统一入口：合并 get/set 对等工具
# ===========================================================================

def t_prop(args):
    """统一系统属性操作：action='get' 读取 / 'set' 写入。"""
    action = args.get("action", "get")
    if action == "set":
        return t_setprop(args)
    return t_getprop(args)


def t_settings(args):
    """统一 Settings 操作：action='get' 读取 / 'put' 写入。"""
    action = args.get("action", "get")
    if action == "put":
        return t_settings_put(args)
    return t_settings_get(args)


def t_file(args):
    """统一设备文件操作：action='read' 读取 / 'write' 写入。"""
    action = args.get("action", "read")
    if action == "write":
        return t_file_write(args)
    return t_file_read(args)


def t_package(args):
    """统一应用包管理：action='install' 安装本地 APK / 'uninstall' 卸载。"""
    action = args.get("action", "install")
    if action == "uninstall":
        return t_uninstall(args)
    return t_install_apk(args)


def t_wechat_db(args):
    """统一微信数据库操作：action='pull' 拉取加密 DB / 'decrypt' 尝试解密。"""
    action = args.get("action", "pull")
    if action == "decrypt":
        return t_wechat_db_decrypt(args)
    return t_wechat_db_pull(args)
