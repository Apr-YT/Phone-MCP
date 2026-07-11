"""验证：用内核级 sendevent (evdev) 模拟触摸，绕过 InputManager。
关键修复：su -c 只接受单个参数，必须用单引号整体包裹整条脚本。
"""
import sys, time
sys.path.insert(0, '.')
import server

DEV = '134d2f8'
w, h = server._screen_size(DEV)
EV = '/dev/input/event6'          # focaltech_ts 多点触控节点
MAX_X, MAX_Y = 119999, 260799     # ABS_MT_POSITION_X/Y 的 max
SX, SY = w, h

def dev_coord(x, y):
    return int(round(x * MAX_X / SX)), int(round(y * MAX_Y / SY))

def _run_as_root(script):
    # su -c 只接受【单个参数】作为要执行的命令，必须把整条脚本用单引号整体包裹
    server.run_adb(["shell", "su -c '%s'" % script], device=DEV, mutating=True)

def kernel_tap(x, y, hold=0.08):
    dx, dy = dev_coord(x, y)
    press = [
        "sendevent %s 3 57 0" % EV,           # ABS_MT_TRACKING_ID = 0
        "sendevent %s 3 53 %d" % (EV, dx),    # ABS_MT_POSITION_X
        "sendevent %s 3 54 %d" % (EV, dy),    # ABS_MT_POSITION_Y
        "sendevent %s 3 48 8" % EV,           # ABS_MT_TOUCH_MAJOR
        "sendevent %s 3 49 8" % EV,           # ABS_MT_WIDTH_MAJOR
        "sendevent %s 3 51 100" % EV,         # ABS_MT_PRESSURE
        "sendevent %s 1 330 1" % EV,          # BTN_TOUCH down
        "sendevent %s 0 0 0" % EV,            # SYN_REPORT
    ]
    _run_as_root("; ".join(press))
    time.sleep(hold)
    release = [
        "sendevent %s 1 330 0" % EV,          # BTN_TOUCH up
        "sendevent %s 3 57 4294967295" % EV,  # ABS_MT_TRACKING_ID = -1 (lift)
        "sendevent %s 0 0 0" % EV,            # SYN_REPORT
    ]
    _run_as_root("; ".join(release))
    time.sleep(0.05)

print("=== 1) 内核级点击微信搜索图标 ===")
server._wechat_ensure_home(DEV)
time.sleep(0.5)
t0 = time.time()
kernel_tap(int(w * 0.83), int(h * 0.07))
dt = time.time() - t0
time.sleep(0.6)
opened = server._search_opened(DEV)
print("kernel tap took %.3fs; search_opened = %s" % (dt, opened))
print("top text after tap:", [b[0] for b in server.ocr_boxes(DEV, region=[0,0,1,0.2], min_conf=0.2)][:5])

print("=== 2) 内核级点击进入「向远钦」聊天并验证焦点 ===")
server.run_adb(["shell", "input", "tap", str(int(w*0.5)), str(int(h*0.07))], device=DEV, mutating=True)
time.sleep(0.2)
server.t_input_text({'text': '向远钦', 'deviceSerial': DEV, 'field': 'search'})
time.sleep(0.6)
hits = server.ocr_match_contact('向远钦', DEV, region=[0,0.12,1,0.6])
print("contact hits:", hits[:1])
if hits:
    _, cx, cy, _ = hits[0]
    kernel_tap(cx, cy)
    time.sleep(1.2)
    print("chat_header_is(向远钦) =", server._chat_header_is(DEV, '向远钦'))

print("=== 3) 内核级点击聊天输入框 + ADBKeyBoard 输入，验证焦点 ===")
kernel_tap(400, int(h*0.96))
time.sleep(0.3)
r = server.t_input_text({'text': '内核点击测试', 'deviceSerial': DEV, 'field': 'chat'})
print("input result:", r.get('success'), "method=", (r.get('data') or {}).get('method'))
verified = server._input_region_has(DEV, 'chat', '内核点击测试')
print("input_region_has(内核点击测试) =", verified)
