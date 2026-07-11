"""验证 v0.9.1 新增工具：phone_brightness / phone_vibrate / phone_cpu / phone_audio / phone_net_firewall
设备 134d2f8，root
"""
import sys, time, os
sys.path.insert(0, ".")
os.environ["PHONE_MCP_DRYRUN"] = "0"
import server as S

D = "134d2f8"

OK = 0; FAIL = 0
def chk(name, r):
    global OK, FAIL
    ok = (r.get("success") if isinstance(r, dict) else False)
    d = r.get("data", {}) if isinstance(r, dict) else {}
    if ok:
        print("[PASS] %s  ok" % name)
        OK += 1
    else:
        print("[FAIL] %s  ok=%s  msg=%s" % (name, ok, (r.get("message", "") if isinstance(r, dict) else str(r))[:200]))
        FAIL += 1
    return d

print("=" * 50)
print("phone-mcp v0.9.1 内核硬件/音频/防火墙 真机验证")
print("=" * 50)

# 1. Brightness
print("\n>>> phone_brightness")
r = S.t_brightness({"deviceSerial": D, "action": "get"})
b = chk("brightness_get", r)
orig_bright = b.get("brightness")
print("  当前: %s / max=%s (%s%%)" % (orig_bright, b.get("max"), b.get("percent")))

r = S.t_brightness({"deviceSerial": D, "action": "set", "level": 30})
b = chk("brightness_set(30%%)", r)
time.sleep(0.3)

# restore
r = S.t_brightness({"deviceSerial": D, "action": "set", "level": orig_bright, "raw": True})
chk("brightness_restore", r)

# 2. Vibrate (expected: may fail on this device, but fallback chain should run)
print("\n>>> phone_vibrate")
r = S.t_vibrate({"deviceSerial": D, "durationMs": 150})
b = chk("vibrate(150ms)", r)  # 不计入 FAIL 预期失败不是 bug
print("  method=%s  msg=%s" % (b.get("method"), b.get("message", (r.get("message", "") if isinstance(r, dict) else ""))[:200]))

# 3. CPU
print("\n>>> phone_cpu")
r = S.t_cpu({"deviceSerial": D, "action": "list"})
b = chk("cpu_list", r)
print("  online=%s  governor=%s  max_freq=%s kHz  governors=%s" %
      (b.get("online"), b.get("governor"), b.get("maxFreqKHz"), b.get("availableGovernors", [])[:3]))

# safe toggle: offline cpu7 then immediately online
r = S.t_cpu({"deviceSerial": D, "action": "offline_core", "core": 7})
chk("cpu_offline_core7", r)
time.sleep(0.3)
r = S.t_cpu({"deviceSerial": D, "action": "online_core", "core": 7})
chk("cpu_online_core7", r)

# set governor (non-destructive: switch to schedutil, confirm, restore to walt)
orig_gov = S.t_cpu({"deviceSerial": D, "action": "list"}).get("data", {}).get("governor", "walt")
r = S.t_cpu({"deviceSerial": D, "action": "set_governor", "governor": "schedutil"})
chk("cpu_set_governor_schedutil", r)
r = S.t_cpu({"deviceSerial": D, "action": "set_governor", "governor": orig_gov})
chk("cpu_restore_governor", r)

# 4. Audio
print("\n>>> phone_audio")
r = S.t_audio({"deviceSerial": D, "action": "get", "stream": "music"})
b = chk("audio_get", r)
orig_vol = b.get("volume")
print("  stream=%s vol=%s / %s (%s%%)" % (b.get("stream"), orig_vol, b.get("max"), b.get("percent")))

r = S.t_audio({"deviceSerial": D, "action": "set_volume", "stream": "music", "level": 5})
chk("audio_set(5)", r)
time.sleep(0.2)
r = S.t_audio({"deviceSerial": D, "action": "set_volume", "stream": "music", "level": orig_vol})
chk("audio_restore", r)

# mute / unmute (toggle: mute then unmute)
r = S.t_audio({"deviceSerial": D, "action": "mute", "stream": "music"})
chk("audio_mute", r)
time.sleep(0.2)
r = S.t_audio({"deviceSerial": D, "action": "unmute", "stream": "music"})
chk("audio_unmute", r)

# 5. Firewall
print("\n>>> phone_net_firewall")
r = S.t_net_firewall({"deviceSerial": D, "action": "list"})
b = chk("fw_list", r)
print("  规则数: %d" % len(b.get("rules", [])))

# safe block/unblock with dummy uid 99999 (non-existent app)
r = S.t_net_firewall({"deviceSerial": D, "action": "block_app", "uid": 99999})
chk("fw_block_dummy", r)
time.sleep(0.3)
r2 = S.t_net_firewall({"deviceSerial": D, "action": "unblock_app", "uid": 99999})
chk("fw_unblock_dummy", r2)

# block/unblock wechat uid (live test, then immediately unblock)
r = S.t_net_firewall({"deviceSerial": D, "action": "block_app", "package": "com.tencent.mm"})
b = chk("fw_block_wx", r)
time.sleep(0.3)
r2 = S.t_net_firewall({"deviceSerial": D, "action": "unblock_app", "package": "com.tencent.mm"})
chk("fw_unblock_wx", r2)

print("\n" + "=" * 50)
print("结果: %d PASS / %d FAIL" % (OK, FAIL))
if FAIL == 0:
    print("全部通过!")
else:
    print("有 %d 项失败，请检查。" % FAIL)
print("server.py 行数: %d" % (lambda: None))
import subprocess
out = subprocess.run(["wc", "-l", "server.py"], capture_output=True, text=True)
print(out.stdout.strip())
