# -*- coding: utf-8 -*-
"""工具注册表 —— 所有 MCP 工具的定义与 handler 绑定。

三层架构：
  原子工具层 : 单一原子操作（tap/input/keyevent/shell）
  封装组合层 : 多个原子+校验组合（auto_click/swipe_until_find/input_chinese）
  场景闭环层 : 面向业务场景的完整闭环（send_wechat_message）
"""

from adb import log as _adb_log


# ---- 导入工具 handler ----
from tools.ui import (
    t_get_devices, t_screenshot, t_tap, t_swipe, t_a11y_tap,
    t_input_text, t_paste_text, t_launch_app, t_key_event,
    t_press_back, t_press_home, t_dump_ui,
    t_ui_dump, t_find_element, t_tap_element,
    t_auto_click, t_swipe_until_find,
    t_input_chinese, t_setup_adbkeyboard,
)
from tools.vision import t_find_text, t_tap_text
from tools.wechat import t_wechat_open_chat, t_send_wechat_message
from tools.system import (
    t_shell, t_run_adb, t_list_packages, t_list_processes,
    t_start_service, t_force_stop, t_get_current_app, t_kill_process, t_kill,
    t_getprop, t_setprop, t_settings_get, t_settings_put,
    t_file_read, t_file_write, t_install_apk, t_uninstall,
    t_ps, t_proc_read, t_wechat_db_pull, t_wechat_db_decrypt,
)
from tools.stream import (
    t_cap_sync, t_screenshot_stream, t_stream_start, t_stream_stop, t_ocr_stream,
)
from tools.hardware import (
    t_brightness, t_vibrate, t_cpu, t_audio, t_net_firewall,
)
from tools.frida import (
    t_frida_inject, t_frida_attach, t_frida_script,
    t_frida_read_mem, t_frida_write_mem, t_frida_scan_mem, t_frida_stealth,
)

TOOLS = [
    # ---- 内核态 / 系统级（需 root）----
    {
        "name": "phone_ps",
        "description": "枚举设备全部进程(/proc 等价: PID/PPID/UID/RSS/CMD)，并解析当前前台 Activity。可直接看到微信等进程 PID。可选 filter 按包名/PID 过滤。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "可选，按包名或 PID 子串过滤"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_ps,
    },
    {
        "name": "phone_proc_read",
        "description": "直读单个进程的 /proc/<pid>/cmdline 与 /proc/<pid>/status 原始内核信息(Name/State/PPid/Uid/VmRSS/VmSize)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "进程 PID"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_proc_read,
    },
    {
        "name": "phone_kill",
        "description": "强制杀进程(无视应用保活): 给指定 pid 或 package 发 SIGKILL(kill -9 / pkill -9)。用于重启微信、干掉卡死进程。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "可选，要杀的进程 PID"},
                "package": {"type": "string", "description": "可选，按包名杀全部相关进程，如 com.tencent.mm"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_kill,
    },
    {
        "name": "phone_wechat_db_pull",
        "description": "root 直拉微信加密数据库 EnMicroMsg.db(+wal/shm/ini)到本机。绕过应用层，无需 OCR。注意文件是 SQLCipher 加密，需 phone_wechat_db_decrypt 解密。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_wechat_db_pull,
    },
    {
        "name": "phone_wechat_db_decrypt",
        "description": "尝试解密微信 EnMicroMsg.db。计算 legacy 候选密钥(md5(imei+uin)[:7])；若本机装了 SQLCipher 且提供 key(8.x 需 frida 取出的 256-bit 密钥)则直接解密读联系人/消息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "可选，SQLCipher 密钥；8.x 需 frida 取出的 256-bit 密钥"},
                "dbPath": {"type": "string", "description": "可选，本地 db 路径；缺省用 phone_wechat_db_pull 拉取的"},
                "legacy": {"type": "boolean", "description": "是否计算 legacy 候选密钥(默认 true)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_wechat_db_decrypt,
    },
    {
        "name": "phone_brightness",
        "description": "Root 直写背光滑块 /sys 节点：获取/设置屏幕亮度。action: get 返回当前+max；set 设百分比(0-100)或 raw=True 传原始值。自动化时先调暗省电、完成后恢复。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "get 或 set"},
                "level": {"type": "integer", "description": "set 时: 0-100 百分比，或 raw=True 时 0-max 原始值"},
                "raw": {"type": "boolean", "description": "set 时: True 传原始值而非百分比"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_brightness,
    },
    {
        "name": "phone_vibrate",
        "description": "触发手机震动指定毫秒(10-60000)。三级回退: sysfs 节点 -> cmd vibrator -> AIDL HAL service call。用于任务完成提醒。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "durationMs": {"type": "integer", "description": "震动时长(毫秒)，默认 200"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_vibrate,
    },
    {
        "name": "phone_cpu",
        "description": "Root 调控 CPU：list(查看在线核心/governor/可用频率)、set_governor(切换调度器，如 walt/schedutil)、online_core/offline_core(上线/下线指定核心)、set_max_freq(限制最大频率 kHz)。用于自动化时降低功耗。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list / set_governor / online_core / offline_core / set_max_freq"},
                "governor": {"type": "string", "description": "set_governor 时: 目标调度器名"},
                "core": {"type": "integer", "description": "online_core/offline_core 时: 核心编号(如 7 表示 cpu7)"},
                "freqKHz": {"type": "integer", "description": "set_max_freq 时: 频率上限(kHz，需是 availableFrequencies 之一)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_cpu,
    },
    {
        "name": "phone_audio",
        "description": "Root 操控音频：获取/设置音量(stream: music/system/ring/alarm/notification 或数字)、静音/取消静音。走 cmd audio(AudioService CLI)，绕过系统设置 UI。注: /dev/snd 原始 PCM 写的是采样数据非音量，正确音量由 cmd audio 控制。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "get / set_volume / mute / unmute"},
                "stream": {"type": "string", "description": "音频流: music(默认)/system/ring/alarm/notification 或数字 0-5"},
                "level": {"type": "integer", "description": "set_volume 时: 音量值(0-max 整数)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_audio,
    },
    {
        "name": "phone_net_firewall",
        "description": "Root iptables 防火墙：按 App uid/包名拦截所有网络(IPv4+IPv6 OUTPUT DROP)、解封、查看规则、清空全部。用于断网调试自动化 App 离线行为。⚠️ clear_all 会清空全部 OUTPUT 规则！",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list / block_app / unblock_app / clear_all"},
                "package": {"type": "string", "description": "block/unblock 时: 包名(如 com.tencent.mm)，自动解析 uid"},
                "uid": {"type": "integer", "description": "block/unblock 时: 直接指定 uid"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_net_firewall,
    },
    # ---- 界面层（只读）----
    {
        "name": "phone_get_devices",
        "description": "列出当前通过 adb 连接的设备。只读，建议先调用确认设备在线。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_get_devices,
    },
    {
        "name": "phone_screenshot",
        "description": "截取手机当前屏幕，返回图片与本地保存路径。AI 可据此'看到'手机界面。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则用默认设备"}
            },
        },
        "handler": t_screenshot,
    },
    {
        "name": "phone_cap_sync",
        "description": "同步设备屏幕参数(分辨率/旋转/刷新率)，等价于 minicap 的握手 banner。启动持续截帧流前调用，让 AI 获知当前屏幕宽高与朝向。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则用默认设备"}},
        },
        "handler": t_cap_sync,
    },
    {
        "name": "phone_screenshot_stream",
        "description": "root 直连截图(绕过应用层截图 API，无系统弹窗/无权限拦截)，保存本地 PNG 并返回路径与 base64。等价于 minicap 单帧抓取，比 phone_screenshot 更稳。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号"}},
        },
        "handler": t_screenshot_stream,
    },
    {
        "name": "phone_stream_start",
        "description": "启动持续截帧流(root, 无弹窗)：后台以 fps 频率持续截图写入本地目录，供 phone_ocr_stream 低延迟取最新帧做文字识别/页面状态校验。等价于 minicap 的 socket 持续图像流。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "fps": {"type": "integer", "description": "截帧频率(1~30，默认4)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_stream_start,
    },
    {
        "name": "phone_stream_stop",
        "description": "停止持续截帧流，释放后台线程。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号"}},
        },
        "handler": t_stream_stop,
    },
    {
        "name": "phone_ocr_stream",
        "description": "对截帧流最新帧(或现截一帧)运行 RapidOCR 文字识别。可传 query 精确/包含匹配返回命中坐标，用于'页面是否显示某文字/某状态'的低延迟校验。无弹窗、无权限拦截。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选，要查找的文字；不传则返回全部文字块"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含(默认)"},
                "region": {"type": "array", "items": {"type": "number"}, "description": "可选归一化裁剪 [x1,y1,x2,y2](0~1)，只识别该区域提速"},
                "minConf": {"type": "number", "description": "最小置信度(默认0.3)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"}
            },
        },
        "handler": t_ocr_stream,
    },
    {
        "name": "phone_dump_ui",
        "description": "dump 当前界面 UI 结构(XML)，含各控件文本与坐标边界，便于 AI 定位按钮。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_dump_ui,
    },
    # ---- 控件级定位（element）：比 OCR 更稳定，主力方案 ----
    {
        "name": "phone_ui_dump",
        "description": "解析当前界面控件树，返回所有具名控件(含文字/resource-id/content-desc)的中心坐标；完整树另存为 JSON。比 OCR 更稳定，毫秒级。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_ui_dump,
    },
    {
        "name": "phone_find_element",
        "description": "按 文字 / resource-id / content-desc 查找控件并返回坐标。matchBy 可指定字段(默认 any 全字段匹配)，exact 控制完全/包含匹配。只读、不点击。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的值，如 '文件传输助手' 或 'com.tencent.mm:id/xxx'"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_find_element,
    },
    {
        "name": "phone_find_ui_element",
        "description": "解析 uiautomator 控件树，按 文字 / resource-id / content-desc 查找控件坐标（比 OCR 更稳、毫秒级）。matchBy 指定字段(默认 any 全字段)；exact 控制完全/包含匹配。只读、不点击。即 phone_find_element 的同功能别名，命名更直白。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的值，如 '文件传输助手' 或 'com.tencent.mm:id/xxx'"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_find_element,
    },
    {
        "name": "phone_tap_element",
        "description": "按 文字 / resource-id / content-desc 直接点击控件，作为比 OCR 更稳定的主力定位方案。UI 树为空(微信/QQ等)时自动回退 OCR。多个匹配用 index(从1)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要点击的控件值，如 '文件传输助手' 或 resource-id"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段：any=三字段任一(默认)；text；resource-id；content-desc"},
                "exact": {"type": "boolean", "description": "true=完全匹配"},
                "index": {"type": "integer", "description": "多个匹配时点的第几个，默认 1"},
                "fallback": {"type": "boolean", "description": "UI 未命中时是否回退 OCR(默认 true)，仅文字查找生效"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_tap_element,
    },
    {
        "name": "phone_find_text",
        "description": "按文字定位坐标(只读、不点击)。默认 auto：先用无障碍/UI(解析uiautomator dump，毫秒级、精准)，拿不到再回退 OCR(视觉)。可用 method 强制单一模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要查找的文字，如 '爸爸'"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"},
                "region": {"type": "array", "items": {"type": "number"}, "description": "仅OCR模式生效：[x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以提速"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_find_text,
    },
    {
        "name": "phone_tap_text",
        "description": "按文字自动点击。默认 auto：先用无障碍/UI(毫秒级、精准)，拿不到再回退 OCR(视觉)。多个匹配用 index(从1)。可用 method 强制单一模式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要点击的文字，如 '爸爸'"},
                "exact": {"type": "boolean", "description": "true=完全匹配"},
                "index": {"type": "integer", "description": "多个匹配时点的第几个，默认 1"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"},
                "region": {"type": "array", "items": {"type": "number"}, "description": "仅OCR模式生效：[x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以提速"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_tap_text,
    },
    # ---- 一键闭环：截图→定位→点击→验证（用户说"点击 XX"优先用本工具）----
    {
        "name": "phone_auto_click",
        "description": "【一键闭环】自动完成『截图/定位 → 点击 → 验证』。先按文字/ID/描述定位控件(自动选 UI 无障碍或 OCR 视觉，微信/QQ 等空树 App 自动回退 OCR)，点击后再次定位确认目标已离开屏幕(说明页面已切换、操作生效)。用户说『点击 XX』时优先用本工具，比单独调 phone_tap_text 更稳、自带重试。query 为要点的目标文字或控件值；method 同 phone_find_text(auto/ui/ocr)；verify=gone(默认，要求点后目标消失)或 any(只确认点击已执行)。整体失败会自动重试 maxRetries 轮。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要点击的目标文字或控件值，如 '文件传输助手' / 'WLAN' / '设置' / '返回'"},
                "matchBy": {"type": "string", "enum": ["any", "text", "resource-id", "content-desc"], "description": "匹配字段(仅 UI 模式生效)：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "定位方式：auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"},
                "index": {"type": "integer", "description": "多个匹配时点的第几个，默认 1"},
                "maxRetries": {"type": "integer", "description": "最多尝试轮数(每轮=定位+点击+验证)，默认 3"},
                "verify": {"type": "string", "enum": ["gone", "any"], "description": "验证方式：gone=要求点击后目标离开屏幕(默认，强确认)；any=只确认点击已执行"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_auto_click,
    },
    {
        "name": "phone_swipe_until_find",
        "description": "自动滑动屏幕直到找到目标文字：每滑一次就重新定位，找到即停（可顺带点击）。适合'滚动长列表找某条'。direction=up(默认，内容下滚找下方项)/down/left/right；maxSwipes 最大滑动次数(默认8)；exact 严格匹配；tapOnFind=true 找到后顺手点击；method 同 phone_find_text(auto/ui/ocr)；swipeStep 单次滑动占屏比例(默认0.6)。返回是否找到、坐标与所用滑动次数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查找的目标文字"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "滑动方向：up=向上滑(默认，找下方项)；down=向下滑；left/right=横向滑动"},
                "maxSwipes": {"type": "integer", "description": "最多滑动次数，默认 8"},
                "exact": {"type": "boolean", "description": "true=完全匹配；false=包含即可(默认)"},
                "tapOnFind": {"type": "boolean", "description": "找到后是否顺手点击，默认 false(只定位不点)"},
                "method": {"type": "string", "enum": ["auto", "ui", "ocr"], "description": "定位方式：auto=先UI后OCR(默认)；ui=只用无障碍；ocr=只用视觉"},
                "swipeStep": {"type": "number", "description": "单次滑动占屏比例(0.1~0.9)，默认 0.6"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["query"],
        },
        "handler": t_swipe_until_find,
    },
    {
        "name": "phone_wechat_open_chat",
        "description": "【全链路示例】进入微信某联系人的聊天界面：启动微信→切到通讯录→(自动校验)在联系人列表滑动找到并点击该联系人→校验进入聊天。演示'操作后自动校验+失败自动重试'闭环。需手机已登录微信且该联系人存在；微信版本/界面差异可能需微调。contact 为联系人备注/昵称。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "要打开聊天的联系人备注或昵称，如 '爸爸' / '文件传输助手'"},
                "maxSwipes": {"type": "integer", "description": "联系人列表最多滑动次数(默认12)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["contact"],
        },
        "handler": t_wechat_open_chat,
    },
    {
        "name": "phone_send_wechat_message",
        "description": "【完整闭环】给微信联系人发消息：启动微信→回主页→打开搜索→输入联系人→精准点击最顶部联系人条目进入聊天→激活输入框→粘贴消息→点发送。每步都做 OCR 校验、失败自动重试 2 次，返回结构化结果(含每步steps)。contact_name=联系人名称(备注/昵称)，message=消息内容。需手机已登录微信且该联系人存在。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_name": {"type": "string", "description": "联系人名称（备注或昵称），如 '向远钦' / '文件传输助手'"},
                "message": {"type": "string", "description": "要发送的消息内容，如 '你好'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["contact_name", "message"],
        },
        "handler": t_send_wechat_message,
    },
    # ---- 界面层（写）----
    {
        "name": "phone_tap",
        "description": "在屏幕坐标 (x, y) 点击。坐标为像素，需先截图确认尺寸。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "横坐标像素"},
                "y": {"type": "integer", "description": "纵坐标像素"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["x", "y"],
        },
        "handler": t_tap,
    },
    {
        "name": "phone_swipe",
        "description": "从 (x1,y1) 滑动到 (x2,y2)，可指定时长(ms)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer", "description": "起点横坐标像素"},
                "y1": {"type": "integer", "description": "起点纵坐标像素"},
                "x2": {"type": "integer", "description": "终点横坐标像素"},
                "y2": {"type": "integer", "description": "终点纵坐标像素"},
                "durationMs": {"type": "integer", "description": "滑动时长，默认 300"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
        "handler": t_swipe,
    },
    {
        "name": "phone_a11y_tap",
        "description": "无障碍服务坐标点击（input 注入被系统拦截时的备选路径）。发送广播给已安装的 phone-mcp 无障碍服务，由它用 dispatchGesture 点击。需手机端先安装并启用该无障碍服务，否则无效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "横坐标像素"},
                "y": {"type": "integer", "description": "纵坐标像素"},
                "durationMs": {"type": "integer", "description": "按下时长，默认 80ms"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["x", "y"],
        },
        "handler": t_a11y_tap,
    },
    {
        "name": "phone_input_text",
        "description": "【统一文本输入·行业标准】自动选择最优输入方式：优先 ADBKeyBoard（ADB 输入法+广播注入，支持中文/英文/emoji/特殊符号/多行，且输入法无感知切换，用完自动切回用户原输入法，首次使用自动从本地 ADBKeyboard.apk 安装启用）；ADBKeyBoard 不可用/异常时自动降级剪贴板(cmd/service call+粘贴键)兜底。微信场景自动判定 search/chat 区域；也可用 field 显式指定('search'|'chat'|'auto')。返回 data.method 标注实际方式('adbkeyboard'|'clipboard')。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本（支持中文/英文/emoji/特殊符号/多行换行）"},
                "field": {"type": "string", "enum": ["auto", "search", "chat"], "description": "输入区域：auto=自动判定(默认)；search=搜索框；chat=聊天输入框"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_input_text,
    },
    {
        "name": "phone_paste_text",
        "description": "通过剪贴板+粘贴键输入任意 Unicode 文本（含中文）。先把文本写入剪贴板，再发送 PASTE 键(279)。当需要显式用粘贴而非 input text 时用本工具；一般中文输入用 phone_input_text 即可自动路由。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要粘贴的文本（支持中文等任意 Unicode）"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_paste_text,
    },
    {
        "name": "phone_input_chinese",
        "description": "中文输入专用工具：把文本写入手机剪贴板并触发粘贴键(PASTE=279)，解决 adb input text 不支持中文的问题。适用于搜索框、聊天输入框等任意可粘贴焦点。优先用本工具输入中文；英文也可使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本（中文走剪贴板粘贴，英文同样支持）"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["text"],
        },
        "handler": t_input_chinese,
    },
    {
        "name": "phone_input_method_setup",
        "description": "安装并启用 ADBKeyBoard 输入法（行业标准中文输入方案），返回当前/可用输入法状态，供显式预置与排障。需要本地 ADBKeyboard.apk 存在（放至 phone-mcp 目录）；缺失时会提示路径且不影响 phone_input_text 的剪贴板兜底。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_setup_adbkeyboard,
    },
    {
        "name": "phone_launch_app",
        "description": "启动应用。给 package(如 com.tencent.mm)；省略 activity 时用 monkey 启动主 Activity。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名"},
                "activity": {"type": "string", "description": "可选，完整 Activity 名"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_launch_app,
    },
    {
        "name": "phone_key_event",
        "description": "发送按键事件。支持名称(HOME/BACK/VOLUME_UP/RECENT 等)或数字 keycode。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keycode": {"type": "string", "description": "如 HOME / BACK / 3 / 187"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["keycode"],
        },
        "handler": t_key_event,
    },
    {
        "name": "phone_press_key",
        "description": "发送按键(返回/主页/电源等)。keycode 支持名称(HOME/BACK/POWER/VOLUME_UP/RECENT...)或数字(如 26=电源, 4=返回)。即 phone_key_event 的别名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keycode": {"type": "string", "description": "如 HOME / BACK / POWER / 26 / 4"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["keycode"],
        },
        "handler": t_key_event,
    },
    {
        "name": "phone_press_back",
        "description": "发送返回键(BACK=4)。快捷别名，等价于 phone_key_event 传 BACK。进入子页面后回退常用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_press_back,
    },
    {
        "name": "phone_press_home",
        "description": "发送主页键(HOME=3)。快捷别名，等价于 phone_key_event 传 HOME。一键回桌面常用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_press_home,
    },
    # ---- 系统级 / 底层（只读，常开）----
    {
        "name": "phone_list_packages",
        "description": "列出已安装应用包名，可选 filter 关键字过滤。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "可选，包名关键字过滤"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_list_packages,
    },
    {
        "name": "phone_list_processes",
        "description": "列出设备上正在运行的进程(ps -A)。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_list_processes,
    },
    {
        "name": "phone_getprop",
        "description": "读取 Android 系统属性(getprop)。可指定 key，省略则列出全部。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "可选属性名"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
        },
        "handler": t_getprop,
    },
    {
        "name": "phone_settings_get",
        "description": "读取系统设置(settings get)。namespace 如 global/system/secure。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "global / system / secure"},
                "key": {"type": "string", "description": "属性名 / 设置项键名，如 bluetooth_on / wifi_on"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["namespace", "key"],
        },
        "handler": t_settings_get,
    },
    {
        "name": "phone_get_current_app",
        "description": "返回当前前台应用的包名与 Activity（dumpsys window 解析 mCurrentFocus/mFocusedApp）。只读。",
        "inputSchema": {
            "type": "object",
            "properties": {"deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"}},
        },
        "handler": t_get_current_app,
    },
    {
        "name": "phone_file_read",
        "description": "读取设备上的文本文件内容(cat)。只读。受权限限制的路径可能读不到。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "设备内文件绝对路径"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["path"],
        },
        "handler": t_file_read,
    },
    # ---- 系统级 / 底层（写，需 PHONE_MCP_ALLOW_SHELL=1）----
    {
        "name": "phone_shell",
        "description": "在设备上执行任意 shell 命令(单条，支持管道/重定向)。需 ALLOW_SHELL=1；禁止 reboot/wipe 等灾难命令。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "如 'ps -A | grep tencent'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["command"],
        },
        "handler": t_shell,
    },
    {
        "name": "phone_run_shell",
        "description": "安全透传 adb shell 命令(单条，支持管道/重定向)。需 ALLOW_SHELL=1；禁止 reboot/wipe/format/dd if= 等灾难命令。即 phone_shell 的别名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "如 'ps -A | grep tencent'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["command"],
        },
        "handler": t_shell,
    },
    {
        "name": "phone_run_adb",
        "description": "执行原始 adb 命令(host 侧,数组或字符串)。需 ALLOW_SHELL=1；拦截 reboot/wipe/rm 等危险指令。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "adb 参数，如 'shell pm list packages'"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["args"],
        },
        "handler": t_run_adb,
    },
    {
        "name": "phone_start_service",
        "description": "启动一个 Android 服务(am startservice -n pkg/Service)。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "service": {"type": "string", "description": "服务类名，如 .MyService"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package", "service"],
        },
        "handler": t_start_service,
    },
    {
        "name": "phone_force_stop",
        "description": "强制停止某应用(am force-stop pkg)，会结束其所有进程与后台服务。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_force_stop,
    },
    {
        "name": "phone_stop_app",
        "description": "停止应用(am force-stop pkg)：结束其所有进程与后台服务，回到桌面。需 ALLOW_SHELL=1。即 phone_force_stop 的别名。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_force_stop,
    },
    {
        "name": "phone_kill_process",
        "description": "结束进程。target 为数字 PID 用 kill；为包名则用 force-stop。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "PID 或包名"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["target"],
        },
        "handler": t_kill_process,
    },
    {
        "name": "phone_setprop",
        "description": "设置 Android 系统属性(setprop)。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "属性名 / 设置项键名，如 bluetooth_on / wifi_on"},
                "value": {"type": "string", "description": "要写入的值"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["key", "value"],
        },
        "handler": t_setprop,
    },
    {
        "name": "phone_settings_put",
        "description": "修改系统设置(settings put)。namespace 如 global/system/secure。需 ALLOW_SHELL=1。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "设置命名空间：global / system / secure"},
                "key": {"type": "string", "description": "属性名 / 设置项键名，如 bluetooth_on / wifi_on"},
                "value": {"type": "string", "description": "要写入的值"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["namespace", "key", "value"],
        },
        "handler": t_settings_put,
    },
    {
        "name": "phone_file_write",
        "description": "向设备写文本文件(push)。需 ALLOW_SHELL=1。写系统分区可能需 root/remount。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "设备内目标路径"},
                "content": {"type": "string", "description": "要写入的文本"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["path", "content"],
        },
        "handler": t_file_write,
    },
    {
        "name": "phone_install_apk",
        "description": "安装本地 APK 到设备(adb install)。需 ALLOW_SHELL=1。localPath 为电脑上的 apk 路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "localPath": {"type": "string", "description": "本机 apk 绝对路径"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["localPath"],
        },
        "handler": t_install_apk,
    },
    {
        "name": "phone_uninstall",
        "description": "卸载应用并清除数据(adb uninstall)。需 ALLOW_SHELL=1。会丢失应用数据！",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "应用包名，如 com.tencent.mm / com.android.settings"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"},
            },
            "required": ["package"],
        },
        "handler": t_uninstall,
    },
    # ---- frida-rust 动态插桩（需 root + frida-rust 部署到设备）----
    {
        "name": "phone_frida_inject",
        "description": "使用 frida-rust 将共享库注入到目标进程(ptrace+dlopen)。需 root，设备上需有 frida-rust 二进制。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "libPath": {"type": "string", "description": "可选，共享库路径(默认 /data/local/tmp/libfrida_agent.so)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid"],
        },
        "handler": t_frida_inject,
    },
    {
        "name": "phone_frida_attach",
        "description": "使用 frida-rust ptrace 附着到目标进程（按进程名查找）。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "processName": {"type": "string", "description": "目标进程名称(如 com.tencent.mm)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["processName"],
        },
        "handler": t_frida_attach,
    },
    {
        "name": "phone_frida_script",
        "description": "在目标进程上执行 Rhai 脚本（frida-rust 脚本引擎）。支持内存读写、Hook、搜索等 API。可选 --anti-detect。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Rhai 脚本内容（支持 find_module_base/read_memory/write_memory/search_bytes/hook_function 等 API）"},
                "pid": {"type": "integer", "description": "可选，目标进程 PID"},
                "antiDetect": {"type": "boolean", "description": "可选，是否启用反检测(默认 false)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["script"],
        },
        "handler": t_frida_script,
    },
    {
        "name": "phone_frida_read_mem",
        "description": "跨进程读取目标内存，返回十六进制数据。通过 frida-rust Rhai 脚本的 read_memory API。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "address": {"type": "string", "description": "起始地址(十六进制，如 0x7f12345000)"},
                "size": {"type": "integer", "description": "读取字节数(最大 1MB)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid", "address", "size"],
        },
        "handler": t_frida_read_mem,
    },
    {
        "name": "phone_frida_write_mem",
        "description": "跨进程写入目标内存(hexData 为十六进制字符串)。通过 frida-rust Rhai 脚本的 write_memory API。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "address": {"type": "string", "description": "目标地址(十六进制)"},
                "hexData": {"type": "string", "description": "要写入的十六进制数据(如 90909090)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid", "address", "hexData"],
        },
        "handler": t_frida_write_mem,
    },
    {
        "name": "phone_frida_scan_mem",
        "description": "在目标进程内存中搜索字节模式，返回所有匹配地址。通过 frida-rust search_bytes API。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "目标进程 PID"},
                "pattern": {"type": "string", "description": "十六进制字节模式(如 48895C24)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
            "required": ["pid", "pattern"],
        },
        "handler": t_frida_scan_mem,
    },
    {
        "name": "phone_frida_stealth",
        "description": "对目标进程应用 frida-rust 全部反检测措施：TracerPid 清零、/proc/maps 隐藏、Frida 特征擦除。需 root。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "可选，目标进程 PID(默认 0 表示自身)"},
                "deviceSerial": {"type": "string", "description": "可选，设备序列号"},
            },
        },
        "handler": t_frida_stealth,
    },
]