"""Tier1 真机端到端验证：用 ADBKeyBoard 走微信发消息闭环，验证无感切换+输入生效+耗时。"""
import sys, time, json
sys.path.insert(0, '.')
import server

DEV = '134d2f8'


def main():
    orig_before = server._ime_current(DEV)
    print('[前置] 原输入法(发送前):', orig_before)

    msg = 'ADBKeyBoard测试：你好😀，来自 phone MCP 自动验证'
    t0 = time.time()
    r = server.t_send_wechat_message({
        'contact_name': '向远钦',
        'message': msg,
        'deviceSerial': DEV,
    })
    elapsed = time.time() - t0

    print('\n=== 发送结果 ===')
    print('success:', r.get('success'))
    print('message:', r.get('message'))
    print('耗时: %.1fs' % elapsed)
    data = r.get('data') or {}
    inp = data.get('inp')
    print('输入方式(contact/message):', inp)
    steps = data.get('steps')
    if steps:
        print('--- steps ---')
        for s in steps:
            print('  ', json.dumps(s, ensure_ascii=False))

    # 无感切换验证：发送后输入法应切回原输入法
    time.sleep(1.0)
    ime_after = server._ime_current(DEV)
    print('\n[后置] 当前输入法(发送后):', ime_after)
    print('[无感切换] 已切回原输入法:' , (ime_after == orig_before))

    # ADBKeyBoard 安装/启用状态
    print('\n[状态] ADBKeyBoard 已安装:', server._adbkeyboard_installed(DEV))
    print('[状态] enabled_input_methods:', server._ime_enabled_list(DEV))


if __name__ == '__main__':
    main()
