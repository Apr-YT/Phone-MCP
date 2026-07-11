"""
精简 e2e：直接调用 t_send_wechat_message 给「向远钦」发消息，
验证聊天框 ADBKeyBoard 输入焦点修复 + 发送成功 + 输入法无感切回。
"""
import sys, time
sys.path.insert(0, '.')
import server
DEV = '134d2f8'
orig = server._ime_current(DEV)
print('原始输入法:', orig)
msg = '你好😀，ADBKeyBoard 聚焦测试！今天天气不错。'
t0 = time.time()
r = server.t_send_wechat_message({'contact_name': '向远钦', 'message': msg, 'deviceSerial': DEV})
total = time.time() - t0
print('\n=== 结果 ===')
print('success:', r.get('success'))
print('total_seconds:', round(total, 1))
for s in (r.get('data', {}).get('steps') or r.get('steps') or []):
    print('  ', s)
ime_final = server._ime_current(DEV)
print('发送后输入法:', ime_final, '| 无感切换:', ime_final == orig)
