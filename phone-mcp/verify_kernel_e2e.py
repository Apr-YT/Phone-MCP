"""端到端验证：微信发消息闭环全程走内核级 sendevent 点击 + ADBKeyBoard 输入。"""
import sys, time
sys.path.insert(0, '.')
import server

DEV = '134d2f8'
info = server._mt_detect(DEV)
print("=== 内核点击探测 ===")
print("MT device:", info)
print("IME 当前:", server._ime_current(DEV))

t0 = time.time()
r = server.t_send_wechat_message({
    'contact_name': '向远钦',
    'message': '你好😀，内核点击+ADBKeyBoard 端到端验证！今天天气不错。',
    'deviceSerial': DEV,
})
dt = time.time() - t0

print("\n=== 发送结果 ===")
print("success:", r.get('success'))
print("总耗时: %.1fs" % dt)
print("每步耗时/状态:")
for s in (r.get('data') or {}).get('steps', []):
    print("  ", s)
print("\n=== 输入法无感切回验证 ===")
print("发送后 IME:", server._ime_current(DEV))
print("是否为用户原输入法(wetype):", 'wetype' in (server._ime_current(DEV) or ''))
