"""
ADBKeyBoard 步骤4 验证：四类文本 + 端到端微信发消息。
- 纯中文 / 中英文混合 / 带标点空格 / 带 emoji 各一组（在微信搜索框验证 ADBKeyBoard 输入）
- 端到端给「向远钦」发消息（含中文+emoji+标点），确认发送成功
输出每步耗时、总耗时、输入法切换是否无感知（发送前后 default_input_method 一致）。
"""
import sys, time
sys.path.insert(0, '.')
import server

DEV = '134d2f8'

def focus_and_clear_search():
    w, h = server._screen_size(DEV)
    server._wechat_ensure_home(DEV)
    server.run_adb(['shell', 'input', 'tap', str(int(w*0.83)), str(int(h*0.07))], device=DEV, mutating=True)
    time.sleep(0.5)
    # 聚焦 + 清空
    server.wechat_tap_input_box(DEV, 'search')
    server.wechat_clear_input(DEV, 'search')

print('========== 步骤4a：四类文本 ADBKeyBoard 输入验证 ==========')
tests = [
    ("纯中文",   "你好世界测试中文输入"),
    ("中英文混合", "Hello你好World世界123"),
    ("带标点空格", "你好，世界！这是 ADB 输入。"),
    ("带emoji",  "你好😀世界🚀测试emoji"),
]
orig_ime = server._ime_current(DEV)
print('原始输入法:', orig_ime)
summary = []
for name, txt in tests:
    focus_and_clear_search()
    t0 = time.time()
    r = server.t_input_text({'text': txt, 'deviceSerial': DEV, 'field': 'search'})
    dt = time.time() - t0
    d = r.get('data', {})
    ime_after = server._ime_current(DEV)
    transparent = (ime_after == orig_ime)
    method = d.get('method')
    verified = d.get('verified')
    ok_flag = r.get('success')
    print('[%s] 用时%.2fs method=%s verified=%s 输入法无感=%s IME=%s'
          % (name, dt, method, verified, transparent, ime_after))
    if not transparent:
        print('   ⚠ 输入法未切回原值！需修复')
    summary.append((name, ok_flag, method, verified, transparent, round(dt, 2)))

print('\n========== 步骤4b：端到端微信发消息 ==========')
msg = '你好😀，这是 ADBKeyBoard 输入测试！今天天气不错。'
t0 = time.time()
r = server.t_send_wechat_message({'contact_name': '向远钦', 'message': msg, 'deviceSerial': DEV})
total = time.time() - t0
print('发送结果 success=%s total=%.1fs' % (r.get('success'), total))
for s in (r.get('data', {}).get('steps') or r.get('steps') or []):
    print('  ', s)
ime_final = server._ime_current(DEV)
print('发送后输入法:', ime_final, '| 无感切换:', ime_final == orig_ime)

print('\n========== 汇总 ==========')
all_method_adb = all(m == 'adbkeyboard' for _, _, m, _, _, _ in summary)
all_transparent = all(t for _, _, _, _, t, _ in summary)
print('四类文本均走 adbkeyboard:', all_method_adb)
print('四类文本均输入法无感切回:', all_transparent)
print('端到端发送成功:', r.get('success'))
print('端到端输入法无感:', ime_final == orig_ime)
