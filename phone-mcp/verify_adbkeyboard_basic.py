"""
ADBKeyBoard 基础链路隔离验证（步骤1）。
不依赖微信闭环，独立验证：
  1) ime list 是否含 ADBKeyBoard 且已 enabled
  2) 手动 ime set 切换 + 校验 default_input_method 是否真变
  3) 聚焦搜索框 -> 广播 ADB_INPUT_B64 -> OCR 校验文本出现
  4) 切回原输入法 -> 校验恢复
输出每项的 PASS/FAIL 与排查结论。
"""
import sys, time, base64, subprocess
sys.path.insert(0, '.')
import server

DEV = '134d2f8'
print('========== 步骤1：ADBKeyBoard 基础链路验证 ==========')

def adb(args):
    return server.run_adb(['shell'] + args, device=DEV, capture=True).stdout or ''

# ---- 1) ime list / enabled / current ----
ime_list = adb(['ime', 'list', '-s'])
enabled = adb(['settings', 'get', 'secure', 'enabled_input_methods']).strip()
current = adb(['settings', 'get', 'secure', 'default_input_method']).strip()
print('[1] ime list -s:\n', ime_list.strip())
print('[1] enabled_input_methods:', enabled)
print('[1] default_input_method (当前):', current)
installed = server.ADB_KEYBOARD_IME in ime_list
is_enabled = server.ADB_KEYBOARD_IME in enabled
print('[1] 已安装=%s  已enabled=%s' % (installed, is_enabled))

# ---- 2) 手动切换 IME 并校验 ----
print('\n[2] 手动 ime set -> ADBKeyBoard ...')
adb(['ime', 'set', server.ADB_KEYBOARD_IME])
time.sleep(0.5)
after_set = adb(['settings', 'get', 'secure', 'default_input_method']).strip()
print('[2] set 后 default_input_method:', after_set)
set_ok = after_set == server.ADB_KEYBOARD_IME
print('[2] IME 切换结果校验:', 'PASS' if set_ok else 'FAIL')

# ---- 3) 聚焦搜索框 + 广播 + OCR 校验 ----
print('\n[3] 聚焦微信搜索框并发广播 ...')
# 确保在微信主页并打开搜索
server._wechat_ensure_home(DEV)
w, h = server._screen_size(DEV)
adb(['input', 'tap', str(int(w*0.83)), str(int(h*0.07))])  # 打开搜索
time.sleep(0.6)
adb(['input', 'tap', str(int(w*0.5)), str(int(h*0.07))])    # 聚焦搜索框
time.sleep(0.4)
test_text = '测试abc123'
b64 = base64.b64encode(test_text.encode('utf-8')).decode('ascii')
# 发广播并捕获返回码
result = server.run_adb(
    ['shell', 'am', 'broadcast', '--user', '0', '-a', 'ADB_INPUT_B64',
     '--es', 'msg', b64],
    device=DEV, mutating=True, capture=True)
print('[3] 广播输出:', repr(result.stdout))
bc = 'result=' in (result.stdout or '')
broadcast_ok = bc
print('[3] 广播返回校验:', 'PASS(有result)' if broadcast_ok else 'FAIL')
time.sleep(0.8)
boxes = server.ocr_boxes(DEV, region=[0, 0.06, 1, 0.16], min_conf=0.2)
flat = ' '.join(b[0] for b in boxes)
print('[3] 搜索框OCR内容:', repr(flat))
inject_ok = test_text in flat or '测试' in flat
print('[3] 文本注入校验:', 'PASS' if inject_ok else 'FAIL')

# ---- 4) 切回原输入法并校验 ----
print('\n[4] 切回原输入法 ...')
adb(['ime', 'set', current])
time.sleep(0.5)
after_restore = adb(['settings', 'get', 'secure', 'default_input_method']).strip()
print('[4] 恢复后 default_input_method:', after_restore)
restore_ok = (after_restore == current)
print('[4] IME 恢复校验:', 'PASS' if restore_ok else 'FAIL')

print('\n========== 排查结论 ==========')
if installed and is_enabled and set_ok and broadcast_ok and inject_ok and restore_ok:
    print('结论: 环境安装正常，基础链路通畅。若上层仍失败，问题在代码逻辑(切换/广播/校验/降级)。')
elif not (installed and is_enabled):
    print('结论: 环境/安装问题 —— ADBKeyBoard 未安装或未 enabled，需先安装启用。')
else:
    print('结论: 链路存在断点 —— 已安装但切换/广播/注入某环节失败，问题在代码逻辑或设备状态。')
print('细节: installed=%s enabled=%s set=%s broadcast=%s inject=%s restore=%s'
      % (installed, is_enabled, set_ok, broadcast_ok, inject_ok, restore_ok))
