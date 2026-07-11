import time, sys
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server as S

D = "134d2f8"
def run(name, fn):
    t0 = time.time()
    try:
        r = fn()
        ok = (r.get("success") if isinstance(r, dict) else False)
        print("[%s] %.2fs ok=%s" % (name, time.time()-t0, ok))
        return (r or {}).get("data", r or {})
    except Exception as e:
        print("[%s] %.2fs EXC=%r" % (name, time.time()-t0, e))
        return None

print("==== phone_ps (filter=com.tencent.mm) ====")
r = run("ps_mm", lambda: S.t_ps({"filter": "com.tencent.mm", "deviceSerial": D}))
if r:
    print("  foreground:", r.get("foreground"))
    for p in r.get("processes", [])[:6]:
        print("   pid=%d uid=%s rss=%sKB fg=%s cmd=%s" % (p["pid"], p["uid"], p["rssKb"], p.get("foreground"), p["cmd"][:60]))

print("==== phone_ps (no filter, count only) ====")
r = run("ps_all", lambda: S.t_ps({"deviceSerial": D}))
if r:
    print("  total processes:", r.get("count"), "foreground:", r.get("foreground"))

print("==== phone_proc_read (wechat main pid) ====")
# find wechat main pid
ps = S._proc_list(D, flt="com.tencent.mm")
main = [p for p in ps if p["cmd"] == "com.tencent.mm"]
pid = main[0]["pid"] if main else ps[0]["pid"]
print("  using pid=", pid)
r = run("proc_read", lambda: S.t_proc_read({"pid": pid, "deviceSerial": D}))
if r:
    print("  cmdline:", (r.get("cmdline") or "")[:80])
    print("  status:", r.get("status"))

print("==== phone_kill (test on temp sleep process) ====")
# spawn a harmless sleep on device
S.run_adb(["shell", "su", "-c", "nohup sleep 60 >/dev/null 2>&1 &"], device=D, mutating=True, what="spawn-sleep")
time.sleep(0.5)
out = S.run_adb(["shell", "pgrep", "-f", "sleep 60"], device=D, capture=True).stdout or ""
spid = (out.strip().splitlines() or [""])[0].strip()
print("  spawned sleep pid=", spid)
if spid:
    r = run("kill", lambda: S.t_kill({"pid": int(spid), "deviceSerial": D}))
    if r:
        print("  killed=", r.get("killed"), "msg=", r.get("message"))
    # verify gone (avoid pgrep self-match: check the exact pid via ps)
    remain = S._proc_list(D, flt=spid)
    alive = any(p["pid"] == int(spid) for p in remain)
    print("  still alive after kill (exact pid check):", alive)

print("==== phone_wechat_db_pull ====")
r = run("db_pull", lambda: S.t_wechat_db_pull({"deviceSerial": D}))
if r:
    print("  dbPath:", r.get("dbPath"))
    print("  pulled:", r.get("pulled"))

print("==== phone_wechat_db_decrypt (no lib expected -> legacy candidate) ====")
env = S.t_wechat_db_decrypt({"deviceSerial": D, "legacy": True})
print("  success=", env.get("success"))
print("  message:\n   " + (env.get("message") or "")[:500])
print("DONE")
