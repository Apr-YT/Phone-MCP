"""
聚焦专项验证：进入与「向远钦」的聊天后，用 ADBKeyBoard 在聊天输入框输入测试文本，
OCR 确认文本写入输入框，并确认输入法无感切回。仅验证输入环节（不发实际消息）。
"""
import sys, time
sys.path.insert(0, '.')
import server

DEV = '134d2f8'
w, h = server._screen_size(DEV)

def enter_chat(contact):
    server._wechat_ensure_home(DEV)
    if not server._search_opened(DEV):
        server.run_adb(['shell', 'input', 'tap', str(int(w*0.83)), str(int(h*0.07))], device=DEV, mutating=True)
        time.sleep(0.5)
    # 输入联系人
    server.run_adb(['shell', 'input', 'tap', str(int(w*0.5)), str(int(h*0.07))], device=DEV, mutating=True)
    time.sleep(0.2)
    server.t_input_text({'text': contact, 'deviceSerial': DEV, 'field': 'search'})
    time.sleep(0.6)
    # 点最顶部结果
    hits = server.ocr_match_contact(contact, DEV, region=[0, 0.12, 1, 0.6])
    if not hits:
        return False, 'no match'
    _, cx, cy, _ = hits[0]
    server.run_adb(['shell', 'input', 'tap', str(cx), str(cy)], device=DEV, mutating=True)
    time.sleep(1.0)
    if server._chat_header_is(DEV, contact):
        return True, 'ok'
    if server._ocr_tap(DEV, '发消息', region=[0, 0.2, 1, 0.9]):
        time.sleep(1.0)
        return server._chat_header_is(DEV, contact), 'via profile'
    return False, 'enter failed'

print('========== 聚焦专项：聊天框 ADBKeyBoard 输入 ==========')
ok_enter, why = enter_chat('向远钦')
print('进入聊天:', ok_enter, why)
if not ok_enter:
    print('进入聊天失败，终止'); sys.exit(1)

orig = server._ime_current(DEV)
print('原始输入法:', orig)

# 激活输入框（模拟 t_send_wechat_message 步骤⑤）
for q in ('发送消息', '按住 说话'):
    if server._ocr_tap(DEV, q, region=[0, 0.85, 1, 1.0]):
        break
else:
    server.run_adb(['shell', 'input', 'tap', '400', str(int(h*0.96))], device=DEV, mutating=True)
time.sleep(0.3)

# 用 ADBKeyBoard 输入测试文本到聊天框
test = '你好世界😀这是聚焦验证'
t0 = time.time()
r = server.t_input_text({'text': test, 'deviceSerial': DEV, 'field': 'chat'})
dt = time.time() - t0
d = r.get('data', {})
print('输入结果 success=%s method=%s verified=%s 用时%.2fs' % (r.get('success'), d.get('method'), d.get('verified'), dt))
print('adbkeyboard_info:', d.get('adbkeyboard_info'))
ime_after = server._ime_current(DEV)
print('输入法无感切回:', ime_after == orig, '| IME=', ime_after)

# 清空输入框，避免残留
server.wechat_clear_input(DEV, 'chat')
print('聚焦专项完成')
