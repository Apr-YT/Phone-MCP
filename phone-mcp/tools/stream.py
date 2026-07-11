# -*- coding: utf-8 -*-
"""minicap 等价流式截图子系统（root screencap + 后台持续截帧）。"""
import os, time, threading, re, base64
import cv2

from adb import run_adb, log
from utils import ok, fail, text_block, image_block
from tools._shared import SHOT_DIR, _ocr_debug, get_ocr_reader
from tools.vision import _screen_size
def _cap_sync(device):
    """同步设备屏幕参数（minicap banner 等价）：物理分辨率、旋转、刷新率。"""
    w = h = rotation = None
    fps = None
    try:
        so = run_adb(["shell", "wm", "size"], device=device, capture=True, timeout=10)
        txt = getattr(so, "stdout", "") or ""
        for line in txt.splitlines():
            if "Physical size" in line:
                m = re.search(r"(\d+)x(\d+)", line)
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
    except Exception as e:
        _ocr_debug("cap_sync wm size 失败: %r" % e)
    try:
        dsp = run_adb(["shell", "dumpsys", "display"], device=device, capture=True, timeout=15)
        txt = getattr(dsp, "stdout", "") or ""
        m = re.search(r"rotation\s+(\d)", txt)
        if m:
            rotation = int(m.group(1))
        m2 = re.search(r"renderFrameRate\s+([\d.]+)", txt)
        if m2:
            fps = float(m2.group(1))
    except Exception as e:
        _ocr_debug("cap_sync dumpsys 失败: %r" % e)
    return {"width": w, "height": h, "rotation": rotation or 0, "fps": fps}
def _cap_frame_root(device, dest_local, region=None, max_side=None):
    """root 直连截图（无弹窗/无权限拦截），写入 dest_local，返回 (path, w, h, off_x, off_y)。

    region: 可选 [x1,y1,x2,y2] 归一化裁剪；max_side: 长边像素上限，超则缩放(提速)。
    """
    import cv2
    ok = run_adb(["exec-out", "su -c 'screencap -p'"], device=device,
                 capture=True, binary=True, what="screencap(root)")
    png = ok.stdout
    if not png or len(png) < 100:
        raise RuntimeError("root 截图返回空（设备未就绪或 su 不可用）")
    d = os.path.dirname(dest_local)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(dest_local, "wb") as f:
        f.write(png)
    img = cv2.imread(dest_local)
    if img is None:
        raise RuntimeError("截图解码失败 path=%s" % dest_local)
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        cv2.imwrite(dest_local, img)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region:
        x1, y1, x2, y2 = [float(v) for v in region]
        cxa, cya = int(x1 * w), int(y1 * h)
        cxb, cyb = int(x2 * w), int(y2 * h)
        if cxb > cxa and cyb > cya:
            img = img[cya:cyb, cxa:cxb]
            off_x, off_y = cxa, cya
            cv2.imwrite(dest_local, img)
            h, w = img.shape[:2]
    if max_side and max(h, w) > max_side:
        scale = max_side / max(h, w)
        small = cv2.resize(img, (int(w * scale), int(h * scale)))
        cv2.imwrite(dest_local, small)
        h, w = small.shape[:2]
    return dest_local, w, h, off_x, off_y
# ---- 持续截帧流（等价 minicap 的 socket 持续图像流）----
_CAP_STREAMS = {}
_CAP_STREAMS_LOCK = threading.Lock()


def _cap_stream_start(device, fps=4, max_frames=12):
    with _CAP_STREAMS_LOCK:
        cur = _CAP_STREAMS.get(device)
        if cur and not cur["stop"].is_set():
            cur["fps"] = fps
            return cur
        stop = threading.Event()
        entry = {"stop": stop, "latest": None,
                 "frames": collections.deque(maxlen=max_frames),
                 "lock": threading.Lock(), "seq": 0, "fps": fps}
        _CAP_STREAMS[device] = entry
    th = threading.Thread(target=_cap_stream_loop, args=(device, fps, stop, entry),
                          name="capstream-%s" % device, daemon=True)
    th.start()
    return entry


def _cap_stream_loop(device, fps, stop, entry):
    interval = max(0.05, 1.0 / max(1, fps))
    basedir = os.path.join(SHOT_DIR, "stream")
    try:
        os.makedirs(basedir, exist_ok=True)
    except Exception:
        pass
    while not stop.is_set():
        try:
            seq = entry["seq"]
            dest = os.path.join(basedir, "frame_%06d.png" % seq)
            _cap_frame_root(device, dest)
            with entry["lock"]:
                entry["seq"] = seq + 1
                entry["latest"] = dest
                entry["frames"].append(dest)
        except Exception as e:
            _ocr_debug("stream 帧捕获失败(设备=%s): %r" % (device, e))
            if stop.wait(interval):
                break
            continue
        if stop.wait(interval):
            break


def _cap_stream_stop(device):
    with _CAP_STREAMS_LOCK:
        entry = _CAP_STREAMS.pop(device, None)
    if entry:
        entry["stop"].set()
    return entry


def _cap_stream_latest(device):
    with _CAP_STREAMS_LOCK:
        entry = _CAP_STREAMS.get(device)
    if not entry:
        return None
    with entry["lock"]:
        return entry["latest"]


def _ocr_boxes_from_image(src_path, region=None, min_conf=0.25):
    """对一张已存在的截图跑 RapidOCR，返回 [(text, cx, cy, conf)]（原图像素坐标）。"""
    import cv2
    img = cv2.imread(src_path)
    if img is None:
        return []
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    h, w = img.shape[:2]
    off_x, off_y = 0, 0
    if region:
        x1, y1, x2, y2 = [float(v) for v in region]
        cxa, cya = int(x1 * w), int(y1 * h)
        cxb, cyb = int(x2 * w), int(y2 * h)
        if cxb > cxa and cyb > cya:
            img = img[cya:cyb, cxa:cxb]
            off_x, off_y = cxa, cya
            h, w = img.shape[:2]
    max_side = 720 if FAST else 1080
    scale = 1.0
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    tmp = src_path + ".ocr.png"
    cv2.imwrite(tmp, img)
    try:
        result, _ = get_ocr_reader()(tmp)
    except Exception as e:
        _ocr_debug("ocr_boxes_from_image reader 异常: %r" % e)
        return []
    if not result:
        return []
    boxes = []
    for bbox, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx = int((min(xs) + max(xs)) / 2 / scale) + off_x
        cy = int((min(ys) + max(ys)) / 2 / scale) + off_y
        boxes.append((txt, cx, cy, conf))
    return boxes


def _box_to_dict(b):
    t, x, y, c = b
    return {"text": t, "x": x, "y": y, "conf": round(c, 3)}


def t_cap_sync(args):
    device = resolve_device(args.get("deviceSerial"))
    info = _cap_sync(device)
    return ok("已同步设备屏幕参数（minicap banner 等价）。",
              width=info["width"], height=info["height"],
              rotation=info["rotation"], fps=info["fps"])


def t_screenshot_stream(args):
    device = resolve_device(args.get("deviceSerial"))
    os.makedirs(SHOT_DIR, exist_ok=True)
    dest = os.path.join(SHOT_DIR, "screen_stream.png")
    try:
        _, w, h, _, _ = _cap_frame_root(device, dest)
    except Exception as e:
        return fail("root 截图失败: %s" % e)
    with open(dest, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return ok("已 root 截图（无弹窗/无权限拦截），保存至: %s" % dest,
              path=dest, width=w, height=h, bytes=os.path.getsize(dest),
              image_b64=b64)


def t_stream_start(args):
    device = resolve_device(args.get("deviceSerial"))
    fps = 4
    if "fps" in args and args["fps"] is not None:
        fps = _req(args, "fps", "int")
    fps = max(1, min(30, fps))
    _cap_stream_start(device, fps=fps)
    return ok("已启动持续截帧流（root, 无弹窗）。fps≈%d，帧写入 %s/stream/。"
              % (fps, SHOT_DIR), fps=fps, streamDir=os.path.join(SHOT_DIR, "stream"))


def t_stream_stop(args):
    device = resolve_device(args.get("deviceSerial"))
    entry = _cap_stream_stop(device)
    return ok("已停止截帧流。" if entry else "截帧流本就未运行。", stopped=bool(entry))


def t_ocr_stream(args):
    device = resolve_device(args.get("deviceSerial"))
    region = args.get("region")
    query = args.get("query")
    exact = bool(args.get("exact", False))
    min_conf = 0.3
    if "minConf" in args and args["minConf"] is not None:
        try:
            min_conf = float(args["minConf"])
        except (TypeError, ValueError):
            pass
    latest = _cap_stream_latest(device)
    src = latest if (latest and os.path.exists(latest)) else None
    if not src:
        os.makedirs(SHOT_DIR, exist_ok=True)
        dest = os.path.join(SHOT_DIR, "ocr_stream_snap.png")
        try:
            _cap_frame_root(device, dest)
        except Exception as e:
            return fail("截图失败: %s" % e)
        src = dest
    boxes = _ocr_boxes_from_image(src, region=region, min_conf=min_conf)
    if query:
        if exact:
            hits = [b for b in boxes if b[0].strip() == query]
        else:
            hits = [b for b in boxes if query in b[0]]
        return ok("OCR 命中 %d 处含 %r 的文字块（共识别 %d 块）。"
                  % (len(hits), query, len(boxes)),
                  query=query, hitCount=len(hits),
                  hits=[_box_to_dict(b) for b in hits],
                  boxes=[_box_to_dict(b) for b in boxes])
    return ok("OCR 完成，共识别 %d 个文字块。" % len(boxes),
              count=len(boxes), boxes=[_box_to_dict(b) for b in boxes])


# ---------------------------------------------------------------------------
# 内核态进程 / 微信数据库直读（原生 Linux 底层）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
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
