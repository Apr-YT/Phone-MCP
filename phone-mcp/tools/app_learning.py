# -*- coding: utf-8 -*-
"""app 遍历 + 自我学习闭环。

四步落地：
  1. 遍历(traverse)：枚举有 launcher activity 的已装 app，逐个用 learned 最优方式启动并计时，回到首页。
  2. 优化空间(optimization)：对比 UI 点击 / am start / monkey 三种启动代价，算出可加速比、
     可跳过(必崩)清单、最慢 Top10。
  3. 自学习(self-learning)：每轮把每个 app 的各启动方式 avg_ms/n/crashes 持久化到
     data/app_perf.json；预期代价 = avg_ms + 崩溃惩罚，取最小为 best_method；崩溃率超阈值的
     app 进入 skip_list，下一轮自动跳过。
  4. 优化项目(optimize)：phone_app_launch 默认查 app_perf.json 用最优方式启动、自动跳过崩溃 app。

设计要点：
  - 与现有工具同构：from adb import run_adb, resolve_device / from utils import ok, fail
  - 数据落在项目内 data/app_perf.json（非 /tmp），跨会话累积学习。
"""
import os, re, json, time

from adb import run_adb, resolve_device
from utils import ok, fail

# 持久化学习库路径（项目内，跨会话累积）
_PERF_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "app_perf.json"
)

# 启动方式对照（用于优化空间估算的“朴素基线”）
UI_CLICK_COST_MS = 3000          # 视觉定位+点击+等待 的估算单 app 代价
CRASH_PENALTY_MS = 5000          # 一次崩溃的预期代价惩罚
SKIP_CRASH_RATE = 0.5            # 崩溃率 >= 此值且样本>=2 → 进入 skip_list


# ----------------------------- 持久化读写 -----------------------------
def _load_perf():
    if os.path.exists(_PERF_PATH):
        try:
            with open(_PERF_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": 1, "updated": "", "apps": {}, "meta": {}}


def _save_perf(db):
    os.makedirs(os.path.dirname(_PERF_PATH), exist_ok=True)
    db["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(_PERF_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ----------------------------- 枚举 -----------------------------
def _enumerate_launcher_activities(device):
    """返回 {pkg: activity} —— 所有带 LAUNCHER activity 的已装 app。"""
    r = run_adb(["shell", "cmd", "package", "query-activities", "--brief",
                 "-a", "android.intent.action.MAIN",
                 "-c", "android.intent.category.LAUNCHER"],
                device=device, capture=True, what="query-acts")
    acts = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # 形如 "priority 0 com.x/.Y" 或 "com.x/.Y"
        m = re.search(r"(\S+)/([\w./]+)$", line)
        if m:
            acts[m.group(1)] = m.group(2)
    return acts


def _third_party_set(device):
    r = run_adb(["shell", "pm", "list", "packages", "-3"],
                device=device, capture=True, what="pm-list")
    s = set()
    for line in (r.stdout or "").splitlines():
        m = re.match(r"package:(\S+)", line.strip())
        if m:
            s.add(m.group(1))
    return s


def _enumerate_apps(device, third_party_only=True):
    """返回 [{"pkg":..,"activity":..}, ...]。默认只保留第三方 app。"""
    acts = _enumerate_launcher_activities(device)
    if third_party_only:
        tp = _third_party_set(device)
        return [{"pkg": p, "activity": a} for p, a in acts.items() if p in tp]
    return [{"pkg": p, "activity": a} for p, a in acts.items()]


# ----------------------------- 启动 + 计时 -----------------------------
def _launch_am(device, pkg, activity):
    """冷启动：先 force-stop，再 am start -W -S，返回 (TotalTime_ms, success, raw)。

    TotalTime 优先从 am 的回显里抓；抓不到时回退到墙钟耗时，
    避免个别 app 输出格式差异导致计时刻度缺失（记为 None）。
    """
    run_adb(["shell", "am", "force-stop", pkg], device=device, capture=True, what="force-stop")
    t0 = time.time()
    r = run_adb(["shell", "am", "start", "-W", "-S", "%s/%s" % (pkg, activity)],
                device=device, capture=True, timeout=30, what="am-start")
    t1 = time.time()
    buf = "%s\n%s" % (r.stdout or "", r.stderr or "")
    total = None
    for line in buf.splitlines():
        m = re.search(r"TotalTime:\s*(\d+)", line)
        if m:
            total = int(m.group(1))
            break
    if total is None:  # 输出无 TotalTime（格式异常/部分启动）：墙钟兜底
        total = int((t1 - t0) * 1000)
    success = (r.returncode == 0) and ("Error" not in buf) and ("Exception" not in buf)
    return total, success, (r.stdout or "").strip()


def _launch_monkey(device, pkg):
    """monkey 启动（无精确计时，用前台轮询校验）。"""
    run_adb(["shell", "am", "force-stop", pkg], device=device, capture=True, what="force-stop")
    t0 = time.time()
    r = run_adb(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
                device=device, capture=True, timeout=30, what="monkey")
    t1 = time.time()
    fr = run_adb(["shell", "dumpsys", "window", "windows"], device=device, capture=True, what="dumpsys")
    success = pkg in (fr.stdout or "")
    return int((t1 - t0) * 1000), success, (r.stdout or "").strip()


def _launch_and_measure(device, pkg, activity, method):
    """按 method 启动并计时。失败返回 (None, False, err)。"""
    try:
        if method == "am_start":
            if not activity:
                return None, False, "no launcher activity"
            return _launch_am(device, pkg, activity)
        if method == "monkey":
            return _launch_monkey(device, pkg)
        return None, False, "unknown method: %s" % method
    except Exception as e:  # adb 重试耗尽等
        return None, False, "launch error: %s" % e


def _go_home(device):
    try:
        run_adb(["shell", "input", "keyevent", "KEYCODE_HOME"],
                device=device, capture=True, what="home")
    except Exception:
        pass
    time.sleep(0.3)


# ----------------------------- 自学习更新 -----------------------------
def _record(db, pkg, method, ms, success):
    app = db["apps"].setdefault(pkg, {
        "launch_methods": {}, "best_method": None, "total": 0, "last_error": None
    })
    mm = app["launch_methods"].setdefault(method, {"avg_ms": 0, "n": 0, "crashes": 0})
    n = mm["n"] + 1
    if ms is not None:
        mm["avg_ms"] = (mm["avg_ms"] * (n - 1) + ms) / n
    mm["n"] = n
    if not success:
        mm["crashes"] += 1
        app["last_error"] = "launch failed"
    app["total"] += 1
    # 自适应：预期代价最小者为 best_method
    best, best_cost = None, None
    for m, d in app["launch_methods"].items():
        if d["n"] == 0:
            continue
        crash_rate = d["crashes"] / d["n"]
        cost = d["avg_ms"] + CRASH_PENALTY_MS * crash_rate
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, m
    app["best_method"] = best


def _should_skip(db, pkg):
    app = db["apps"].get(pkg)
    if not app:
        return False
    for d in app["launch_methods"].values():
        if d["n"] >= 2 and (d["crashes"] / d["n"]) >= SKIP_CRASH_RATE:
            return True
    return False


# ----------------------------- 优化空间报告 -----------------------------
def _optimization_report(db, pkgs):
    apps = db["apps"]
    measured = {p: a for p, a in apps.items() if a.get("launch_methods")}
    naive_total = len(pkgs) * UI_CLICK_COST_MS
    opt_total, skip, slow = 0, [], []
    for p, a in measured.items():
        best = a.get("best_method")
        bm = a["launch_methods"].get(best, {})
        opt_total += bm.get("avg_ms", UI_CLICK_COST_MS)
        crashy = any(d["n"] >= 2 and (d["crashes"] / d["n"]) >= SKIP_CRASH_RATE
                     for d in a["launch_methods"].values())
        if crashy:
            skip.append(p)
        slow.append((p, int(bm.get("avg_ms", 0)), best))
    slow.sort(key=lambda x: -x[1])
    speedup = (naive_total - opt_total) / naive_total if naive_total else 0
    return {
        "measured_apps": len(measured),
        "naive_total_ms": naive_total,
        "optimized_total_ms": int(opt_total),
        "speedup_pct": round(speedup * 100, 1),
        "skip_list": skip,
        "top_slow": [{"pkg": p, "ms": m, "method": me} for p, m, me in slow[:10]],
    }


# ----------------------------- 对外工具 -----------------------------
def t_app_traverse(args):
    """逐个启动已装 app 并计时，持久化学习，输出优化空间报告。

    args:
      deviceSerial: 目标设备(可选)
      limit:       最多遍历前 N 个 app(可选，省略=全部第三方 app)
      methods:      参与学习的启动方式列表(默认 ["am_start"])
    """
    device = resolve_device(args.get("deviceSerial"))
    db = _load_perf()
    methods = args.get("methods") or ["am_start"]
    limit = args.get("limit")
    apps = _enumerate_apps(device, third_party_only=True)
    if limit:
        apps = apps[: int(limit)]

    results = []
    for item in apps:
        pkg, act = item["pkg"], item["activity"]
        if _should_skip(db, pkg):
            results.append({"pkg": pkg, "skipped": True, "reason": "learned crash-prone"})
            continue
        method = db["apps"].get(pkg, {}).get("best_method") or methods[0]
        ms, success, err = _launch_and_measure(device, pkg, act, method)
        _record(db, pkg, method, ms, success)
        results.append({
            "pkg": pkg, "method": method, "ms": ms, "ok": success,
            "err": (err[:160] if err else None),
        })
        _go_home(device)

    _save_perf(db)
    report = _optimization_report(db, [a["pkg"] for a in apps])
    return ok("遍历完成，共 %d 个 app（已学习 %d 个）" % (len(apps), report["measured_apps"]),
              count=len(apps), results=results, optimization=report)


def t_app_launch(args):
    """用 learned 最优方式启动单个 app；历史崩溃率过高的自动跳过(可 force 覆盖)。

    args:
      package:      目标包名(必填)
      deviceSerial: 目标设备(可选)
      force:        True 时忽略 skip_list 强制启动
    """
    device = resolve_device(args.get("deviceSerial"))
    pkg = args.get("package")
    if not pkg:
        return fail("缺少 package 参数")
    db = _load_perf()
    if not args.get("force") and _should_skip(db, pkg):
        return fail("该 app 历史崩溃率过高，已跳过（加 force=true 可强制启动）", pkg=pkg)
    acts = _enumerate_launcher_activities(device)
    act = acts.get(pkg)
    method = db["apps"].get(pkg, {}).get("best_method") or ("am_start" if act else "monkey")
    ms, success, err = _launch_and_measure(device, pkg, act, method)
    _record(db, pkg, method, ms, success)
    _save_perf(db)
    if success:
        return ok("已用 %s 启动 %s，耗时 %dms" % (method, pkg, ms),
                  pkg=pkg, method=method, ms=ms)
    return fail("启动失败: %s" % (err[:200] if err else "unknown"), pkg=pkg, method=method)
