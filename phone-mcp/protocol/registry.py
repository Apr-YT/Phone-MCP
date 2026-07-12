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
    t_auto_click, t_swipe_until_find, t_locate,
    t_input_chinese, t_setup_adbkeyboard,
)
from tools.vision import t_find_text, t_tap_text  # 底层 handler，由 t_locate 间接调用
from tools.wechat import (t_wechat_open_chat, t_send_wechat_message,
                           t_wechat_list_contacts, t_wechat_list_chats,
                           t_wechat_read_messages, t_wechat_search_contact)
from tools.system import (
    t_shell, t_run_adb, t_list_packages, t_list_processes,
    t_start_service, t_force_stop, t_get_current_app, t_kill_process, t_kill,
    t_getprop, t_setprop, t_settings_get, t_settings_put,
    t_file_read, t_file_write, t_install_apk, t_uninstall,
    t_prop, t_settings, t_file, t_package,
    t_wechat_db,
    t_ps, t_proc_read, t_wechat_db_pull, t_wechat_db_decrypt,  # 底层 handler
    t_wechat_db,
)
from tools.stream import (
    t_cap_sync, t_screenshot_stream, t_stream_start, t_stream_stop, t_ocr_stream,
)
from tools.hardware import (
    t_brightness, t_vibrate, t_cpu, t_audio, t_net_firewall,
)
from tools.frida_rust import (
    t_frida_inject, t_frida_attach, t_frida_script,
    t_frida_read_mem, t_frida_write_mem, t_frida_scan_mem, t_frida_stealth,
)
from tools.learner import (
    t_learn_recall, t_learn_reflect,
)
from tools.app_learning import (t_app_traverse, t_app_launch)

TOOLS = [
    {
    "name": "phone_device_current",
    "description": "返回当前前台应用的包名与 Activity（dumpsys window 解析 mCurrentFocus/mFocusedApp）。只读。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        }
},
    "handler": t_get_current_app
},
    {
    "name": "phone_device_list",
    "description": "列出当前通过 adb 连接的设备。只读，建议先调用确认设备在线。",
    "inputSchema": {
        "type": "object",
        "properties": {}
},
    "handler": t_get_devices
},
    {
    "name": "phone_device_packages",
    "description": "列出已安装应用包名，可选 filter 关键字过滤。只读。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "filter": {
                        "type": "string",
                        "description": "可选，包名关键字过滤"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        }
},
    "handler": t_list_packages
},
    {
    "name": "phone_ui_a11y",
    "description": "无障碍服务坐标点击（input 注入被系统拦截时的备选路径）。发送广播给已安装的 phone-mcp 无障碍服务，由它用 dispatchGesture点击。需手机端先安装并启用该无障碍服务，否则无效。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "x": {
                        "type": "integer",
                        "description": "横坐标像素"
                },
                "y": {
                        "type": "integer",
                        "description": "纵坐标像素"
                },
                "durationMs": {
                        "type": "integer",
                        "description": "按下时长，默认 80ms"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "x",
                "y"
        ]
},
    "handler": t_a11y_tap
},
    {
    "name": "phone_ui_click",
    "description": "【一键闭环】自动完成『截图/定位 → 点击 → 验证』。先按文字/ID/描述定位控件(自动选 UI 无障碍或 OCR 视觉，微信/QQ 等空树 App 自动回退OCR)，点击后再次定位确认目标已离开屏幕(说明页面已切换、操作生效)。用户说『点击 XX』时优先用本工具，比单独调 phone_tap_text 更稳、自带重试。query为要点的目标文字或控件值；method 同 phone_find_text(auto/ui/ocr)；verify=gone(默认，要求点后目标消失)或any(只确认点击已执行)。整体失败会自动重试 maxRetries 轮。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "query": {
                        "type": "string",
                        "description": "要点击的目标文字或控件值，如 '文件传输助手' / 'WLAN' / '设置' / '返回'"
                },
                "matchBy": {
                        "type": "string",
                        "enum": [
                                "any",
                                "text",
                                "resource-id",
                                "content-desc"
                        ],
                        "description": "匹配字段(仅 UI 模式生效)：any=三字段任一(默认)；text=仅文字；resource-id=仅ID；content-desc=仅描述"
                },
                "exact": {
                        "type": "boolean",
                        "description": "true=完全匹配；false=包含即可(默认)"
                },
                "method": {
                        "type": "string",
                        "enum": [
                                "auto",
                                "ui",
                                "ocr"
                        ],
                        "description": "定位方式：auto=先UI后OCR(默认)；ui=只用无障碍(最快)；ocr=只用视觉(微信/QQ等空树App用)"
                },
                "index": {
                        "type": "integer",
                        "description": "多个匹配时点的第几个，默认 1"
                },
                "maxRetries": {
                        "type": "integer",
                        "description": "最多尝试轮数(每轮=定位+点击+验证)，默认 3"
                },
                "verify": {
                        "type": "string",
                        "enum": [
                                "gone",
                                "any"
                        ],
                        "description": "验证方式：gone=要求点击后目标离开屏幕(默认，强确认)；any=只确认点击已执行"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "query"
        ]
},
    "handler": t_auto_click
},
    {
    "name": "phone_ui_input",
    "description": "【统一文本输入·行业标准】自动选择最优输入方式：优先 ADBKeyBoard（ADB输入法+广播注入，支持中文/英文/emoji/特殊符号/多行，且输入法无感知切换，用完自动切回用户原输入法，首次使用自动从本地 ADBKeyboard.apk安装启用）；ADBKeyBoard 不可用/异常时自动降级剪贴板(cmd/service call+粘贴键)兜底。微信场景自动判定 search/chat 区域；也可用 field显式指定('search'|'chat'|'auto')。返回 data.method 标注实际方式('adbkeyboard'|'clipboard')。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "text": {
                        "type": "string",
                        "description": "要输入的文本（支持中文/英文/emoji/特殊符号/多行换行）"
                },
                "field": {
                        "type": "string",
                        "enum": [
                                "auto",
                                "search",
                                "chat"
                        ],
                        "description": "输入区域：auto=自动判定(默认)；search=搜索框；chat=聊天输入框"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "text"
        ]
},
    "handler": t_input_text
},
    {
    "name": "phone_ui_input_setup",
    "description": "安装并启用 ADBKeyBoard 输入法（行业标准中文输入方案），返回当前/可用输入法状态，供显式预置与排障。需要本地 ADBKeyboard.apk 存在（放至 phone-mcp 目录）；缺失时会提示路径且不影响 phone_input_text 的剪贴板兜底。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        }
},
    "handler": t_setup_adbkeyboard
},
    {
    "name": "phone_ui_key",
    "description": "发送按键事件。支持名称(HOME/BACK/VOLUME_UP/RECENT 等)或数字 keycode。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "keycode": {
                        "type": "string",
                        "description": "如 HOME / BACK / 3 / 187"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "keycode"
        ]
},
    "handler": t_key_event
},
    {
    "name": "phone_ui_locate",
    "description": "统一查找/点击/导览入口——替代了旧版 find_text/tap_text/tap_element/find_element/dump_ui/ui_dump 六个工具。action='find' 按文字/id查坐标并返回 / 'tap' 查并点击 / 'dump' 导出界面控件树。method='auto'(默认,先UI树后OCR) / 'ui'(仅无障碍树,快但微信空) / 'ocr'(仅视觉,通用但慢)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "query": {
                        "type": "string",
                        "description": "要查找的文字/资源ID/内容描述(action=dump无需)"
                },
                "action": {
                        "type": "string",
                        "enum": [
                                "find",
                                "tap",
                                "dump"
                        ],
                        "description": "find=返回坐标(默认), tap=查并点击, dump=导出界面控件树"
                },
                "method": {
                        "type": "string",
                        "enum": [
                                "auto",
                                "ui",
                                "ocr"
                        ],
                        "description": "auto=先UI后OCR(默认), ui=仅无障碍树, ocr=仅视觉"
                },
                "exact": {
                        "type": "boolean",
                        "description": "true=完全匹配文字"
                },
                "index": {
                        "type": "integer",
                        "description": "多个匹配时点的第几个，默认1"
                },
                "matchBy": {
                        "type": "string",
                        "enum": [
                                "any",
                                "text",
                                "resource-id",
                                "content-desc"
                        ],
                        "description": "仅method=ui生效：匹配字段"
                },
                "region": {
                        "type": "array",
                        "items": {
                                "type": "number"
                        },
                        "description": "仅OCR生效：[x1,y1,x2,y2]归一化区域"
                },
                "maxRetries": {
                        "type": "integer",
                        "description": "tap 模式最大重试次数,默认2"
                },
                "fallback": {
                        "type": "boolean",
                        "description": "UI未命中时是否回退OCR(tap模式,默认true)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": []
},
    "handler": t_locate
},
    {
    "name": "phone_ui_ocr",
    "description": "对截帧流最新帧(或现截一帧)运行 RapidOCR 文字识别。可传 query 精确/包含匹配返回命中坐标，用于'页面是否显示某文字/某状态'的低延迟校验。无弹窗、无权限拦截。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "query": {
                        "type": "string",
                        "description": "可选，要查找的文字；不传则返回全部文字块"
                },
                "exact": {
                        "type": "boolean",
                        "description": "true=完全匹配；false=包含(默认)"
                },
                "region": {
                        "type": "array",
                        "items": {
                                "type": "number"
                        },
                        "description": "可选归一化裁剪 [x1,y1,x2,y2](0~1)，只识别该区域提速"
                },
                "minConf": {
                        "type": "number",
                        "description": "最小置信度(默认0.3)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_ocr_stream
},
    {
    "name": "phone_ui_screenshot",
    "description": "截取手机当前屏幕，返回图片与本地保存路径。AI 可据此'看到'手机界面。只读。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则用默认设备"
                }
        }
},
    "handler": t_screenshot
},
    {
    "name": "phone_ui_screenshot_raw",
    "description": "root 直连截图(绕过应用层截图 API，无系统弹窗/无权限拦截)，保存本地 PNG 并返回路径与 base64。等价于 minicap 单帧抓取，比phone_screenshot 更稳。只读。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_screenshot_stream
},
    {
    "name": "phone_ui_scroll",
    "description": "自动滑动屏幕直到找到目标文字：每滑一次就重新定位，找到即停（可顺带点击）。适合'滚动长列表找某条'。direction=up(默认，内容下滚找下方项)/down/left/right；maxSwipes 最大滑动次数(默认8)；exact 严格匹配；tapOnFind=true 找到后顺手点击；method 同phone_find_text(auto/ui/ocr)；swipeStep 单次滑动占屏比例(默认0.6)。返回是否找到、坐标与所用滑动次数。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "query": {
                        "type": "string",
                        "description": "要查找的目标文字"
                },
                "direction": {
                        "type": "string",
                        "enum": [
                                "up",
                                "down",
                                "left",
                                "right"
                        ],
                        "description": "滑动方向：up=向上滑(默认，找下方项)；down=向下滑；left/right=横向滑动"
                },
                "maxSwipes": {
                        "type": "integer",
                        "description": "最多滑动次数，默认 8"
                },
                "exact": {
                        "type": "boolean",
                        "description": "true=完全匹配；false=包含即可(默认)"
                },
                "tapOnFind": {
                        "type": "boolean",
                        "description": "找到后是否顺手点击，默认 false(只定位不点)"
                },
                "method": {
                        "type": "string",
                        "enum": [
                                "auto",
                                "ui",
                                "ocr"
                        ],
                        "description": "定位方式：auto=先UI后OCR(默认)；ui=只用无障碍；ocr=只用视觉"
                },
                "swipeStep": {
                        "type": "number",
                        "description": "单次滑动占屏比例(0.1~0.9)，默认 0.6"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "query"
        ]
},
    "handler": t_swipe_until_find
},
    {
    "name": "phone_ui_stream_start",
    "description": "启动持续截帧流(root, 无弹窗)：后台以 fps 频率持续截图写入本地目录，供 phone_ocr_stream 低延迟取最新帧做文字识别/页面状态校验。等价于 minicap的 socket 持续图像流。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "fps": {
                        "type": "integer",
                        "description": "截帧频率(1~30，默认4)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_stream_start
},
    {
    "name": "phone_ui_stream_stop",
    "description": "停止持续截帧流，释放后台线程。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_stream_stop
},
    {
    "name": "phone_ui_swipe",
    "description": "从 (x1,y1) 滑动到 (x2,y2)，可指定时长(ms)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "x1": {
                        "type": "integer",
                        "description": "起点横坐标像素"
                },
                "y1": {
                        "type": "integer",
                        "description": "起点纵坐标像素"
                },
                "x2": {
                        "type": "integer",
                        "description": "终点横坐标像素"
                },
                "y2": {
                        "type": "integer",
                        "description": "终点纵坐标像素"
                },
                "durationMs": {
                        "type": "integer",
                        "description": "滑动时长，默认 300"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "x1",
                "y1",
                "x2",
                "y2"
        ]
},
    "handler": t_swipe
},
    {
    "name": "phone_ui_sync",
    "description": "同步设备屏幕参数(分辨率/旋转/刷新率)，等价于 minicap 的握手 banner。启动持续截帧流前调用，让 AI 获知当前屏幕宽高与朝向。只读。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则用默认设备"
                }
        }
},
    "handler": t_cap_sync
},
    {
    "name": "phone_ui_tap",
    "description": "在屏幕坐标 (x, y) 点击。坐标为像素，需先截图确认尺寸。默认用内核直触(绕过 InputManager)，部分 App 按钮(如微信发送键)不响应内核触摸时用method='adb' 降级。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "x": {
                        "type": "integer",
                        "description": "横坐标像素"
                },
                "y": {
                        "type": "integer",
                        "description": "纵坐标像素"
                },
                "method": {
                        "type": "string",
                        "enum": [
                                "kernel",
                                "adb"
                        ],
                        "description": "输入方式：kernel=内核直触(默认，绕过InputManager)；adb=adb input tap(微信发送按钮等场景用)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "x",
                "y"
        ]
},
    "handler": t_tap
},
    {
    "name": "phone_sys_adb",
    "description": "执行原始 adb 命令(host 侧,数组或字符串)。需 ALLOW_SHELL=1；拦截 reboot/wipe/rm 等危险指令。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "args": {
                        "type": "string",
                        "description": "adb 参数，如 'shell pm list packages'"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "args"
        ]
},
    "handler": t_run_adb
},
    {
    "name": "phone_sys_file",
    "description": "在设备上读写文本文件。action='read'(默认) 读取 / 'write' 写入(path+content)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "enum": [
                                "read",
                                "write"
                        ],
                        "description": "read=读取(默认), write=写入"
                },
                "path": {
                        "type": "string",
                        "description": "设备上的文件绝对路径"
                },
                "content": {
                        "type": "string",
                        "description": "要写入的文本内容(action=write 必填)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "path"
        ]
},
    "handler": t_file
},
    {
    "name": "phone_sys_launch",
    "description": "启动应用。给 package(如 com.tencent.mm)；省略 activity 时用 monkey 启动主 Activity。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "package": {
                        "type": "string",
                        "description": "应用包名"
                },
                "activity": {
                        "type": "string",
                        "description": "可选，完整 Activity 名"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "package"
        ]
},
    "handler": t_launch_app
},
    {
    "name": "phone_sys_package",
    "description": "安装或卸载应用。action='install'(默认,需 localPath) / 'uninstall'(需 package)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "enum": [
                                "install",
                                "uninstall"
                        ],
                        "description": "install(默认)/uninstall"
                },
                "localPath": {
                        "type": "string",
                        "description": "本地 APK 路径(action=install 必填)"
                },
                "package": {
                        "type": "string",
                        "description": "要卸载的包名(action=uninstall 必填)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": []
},
    "handler": t_package
},
    {
    "name": "phone_sys_prop",
    "description": "读取/写入 Android 系统属性(getprop/setprop)。action='get'(默认) 读取 / 'set' 写入(key+value)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "enum": [
                                "get",
                                "set"
                        ],
                        "description": "get=读取(默认), set=写入"
                },
                "key": {
                        "type": "string",
                        "description": "属性名，如 ro.build.version.sdk"
                },
                "value": {
                        "type": "string",
                        "description": "写入时的值(action=set 必填)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "key"
        ]
},
    "handler": t_prop
},
    {
    "name": "phone_sys_service",
    "description": "启动一个 Android 服务(am startservice -n pkg/Service)。需 ALLOW_SHELL=1。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "package": {
                        "type": "string",
                        "description": "应用包名，如 com.tencent.mm / com.android.settings"
                },
                "service": {
                        "type": "string",
                        "description": "服务类名，如 .MyService"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "package",
                "service"
        ]
},
    "handler": t_start_service
},
    {
    "name": "phone_sys_settings",
    "description": "读取/写入 Android Settings 数据库(settings get/put)。action='get'(默认) / 'put'(namespace+key+value)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "enum": [
                                "get",
                                "put"
                        ],
                        "description": "get=读取(默认), put=写入"
                },
                "namespace": {
                        "type": "string",
                        "description": "命名空间: system/secure/global"
                },
                "key": {
                        "type": "string",
                        "description": "设置键名"
                },
                "value": {
                        "type": "string",
                        "description": "写入时的值(action=put 必填)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "namespace",
                "key"
        ]
},
    "handler": t_settings
},
    {
    "name": "phone_sys_shell",
    "description": "在设备上执行任意 shell 命令(单条，支持管道/重定向)。需 ALLOW_SHELL=1；禁止 reboot/wipe 等灾难命令。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "command": {
                        "type": "string",
                        "description": "如 'ps -A | grep tencent'"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "command"
        ]
},
    "handler": t_shell
},
    {
    "name": "phone_sys_stop",
    "description": "强制停止某应用(am force-stop pkg)，会结束其所有进程与后台服务。需 ALLOW_SHELL=1。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "package": {
                        "type": "string",
                        "description": "应用包名，如 com.tencent.mm / com.android.settings"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "package"
        ]
},
    "handler": t_force_stop
},
    {
    "name": "phone_proc_force_kill",
    "description": "强制杀进程(无视应用保活): 给指定 pid 或 package 发 SIGKILL(kill -9 / pkill -9)。用于重启微信、干掉卡死进程。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "可选，要杀的进程 PID"
                },
                "package": {
                        "type": "string",
                        "description": "可选，按包名杀全部相关进程，如 com.tencent.mm"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_kill
},
    {
    "name": "phone_proc_info",
    "description": "直读单个进程的 /proc/<pid>/cmdline 与 /proc/<pid>/status 原始内核信息(Name/State/PPid/Uid/VmRSS/VmSize)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "进程 PID"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_proc_read
},
    {
    "name": "phone_proc_kill",
    "description": "结束进程。target 为数字 PID 用 kill；为包名则用 force-stop。需 ALLOW_SHELL=1。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "target": {
                        "type": "string",
                        "description": "PID 或包名"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "target"
        ]
},
    "handler": t_kill_process
},
    {
    "name": "phone_proc_list",
    "description": "枚举设备全部进程(/proc 等价: PID/PPID/UID/RSS/CMD)，并解析当前前台 Activity。可直接看到微信等进程 PID。可选 filter 按包名/PID过滤。只读。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "filter": {
                        "type": "string",
                        "description": "可选，按包名或 PID 子串过滤"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_ps
},
    {
    "name": "phone_wx_chats",
    "description": "列出微信首页的聊天会话列表（最近联系人）。OCR 识别可见会话，可选滚动加载更多。返回会话名称列表。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "maxScrolls": {
                        "type": "integer",
                        "description": "可选，最多滚动次数(默认 3, 最大 10)"
                },
                "minRecent": {
                        "type": "integer",
                        "description": "可选，最少列出数(默认 10)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_wechat_list_chats
},
    {
    "name": "phone_wx_contacts",
    "description": "列出微信通讯录中的所有联系人。自动切到通讯录 Tab，OCR 识别可见联系人，可选滚动加载更多。返回联系人名称列表。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "maxScrolls": {
                        "type": "integer",
                        "description": "可选，最多滚动次数(默认 5, 最大 20)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_wechat_list_contacts
},
    {
    "name": "phone_wx_db",
    "description": "拉取/解密微信数据库 EnMicroMsg.db。action='pull'(默认,root直拉加密DB+WAL到本机) / 'decrypt'(用推算密钥解密)。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "enum": [
                                "pull",
                                "decrypt"
                        ],
                        "description": "pull=拉取加密DB(默认), decrypt=解密"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": []
},
    "handler": t_wechat_db
},
    {
    "name": "phone_wx_open",
    "description": "【全链路示例】进入微信某联系人的聊天界面：启动微信→切到通讯录→(自动校验)在联系人列表滑动找到并点击该联系人→校验进入聊天。演示'操作后自动校验+失败自动重试'闭环。需手机已登录微信且该联系人存在；微信版本/界面差异可能需微调。contact 为联系人备注/昵称。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "contact": {
                        "type": "string",
                        "description": "要打开聊天的联系人备注或昵称，如 '爸爸' / '文件传输助手'"
                },
                "maxSwipes": {
                        "type": "integer",
                        "description": "联系人列表最多滑动次数(默认12)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "contact"
        ]
},
    "handler": t_wechat_open_chat
},
    {
    "name": "phone_wx_read",
    "description": "读取与某联系人的聊天记录。打开聊天后 OCR 识别可见消息，可选向上滚动读取历史消息。contact=联系人名称(必填)。返回消息内容列表。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "contact": {
                        "type": "string",
                        "description": "联系人名称（必填），如 '向远钦' / '文件传输助手'"
                },
                "maxScrolls": {
                        "type": "integer",
                        "description": "可选，最大向上滚动次数(默认 5, 最大 20)"
                },
                "maxMessages": {
                        "type": "integer",
                        "description": "可选，最多读取条数(默认 50, 最大 200)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "contact"
        ]
},
    "handler": t_wechat_read_messages
},
    {
    "name": "phone_wx_search",
    "description": "在微信中全局搜索联系人（通过首页搜索入口）。输入关键词搜索，返回匹配的联系人列表。可选 openChat=true 自动点击第一个匹配进入聊天。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "query": {
                        "type": "string",
                        "description": "搜索关键词（必填），如 '张三' / '文件传输助手'"
                },
                "openChat": {
                        "type": "boolean",
                        "description": "可选，是否自动点击第一个匹配进入聊天(默认 false)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "query"
        ]
},
    "handler": t_wechat_search_contact
},
    {
    "name": "phone_wx_send",
    "description": "【完整闭环】给微信联系人发消息：启动微信→回主页→打开搜索→输入联系人→精准点击最顶部联系人条目进入聊天→激活输入框→粘贴消息→点发送。每步都做 OCR 校验、失败自动重试 2次，返回结构化结果(含每步steps)。contact_name=联系人名称(备注/昵称)，message=消息内容。需手机已登录微信且该联系人存在。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "contact_name": {
                        "type": "string",
                        "description": "联系人名称（备注或昵称），如 '向远钦' / '文件传输助手'"
                },
                "message": {
                        "type": "string",
                        "description": "要发送的消息内容，如 '你好'"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号；省略则使用默认设备(由 PHONE_MCP_DEVICE 指定，默认 134d2f8)"
                }
        },
        "required": [
                "contact_name",
                "message"
        ]
},
    "handler": t_send_wechat_message
},
    {
    "name": "phone_hw_audio",
    "description": "Root 操控音频：获取/设置音量(stream: music/system/ring/alarm/notification 或数字)、静音/取消静音。走 cmdaudio(AudioService CLI)，绕过系统设置 UI。注: /dev/snd 原始 PCM 写的是采样数据非音量，正确音量由 cmd audio 控制。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "description": "get / set_volume / mute / unmute"
                },
                "stream": {
                        "type": "string",
                        "description": "音频流: music(默认)/system/ring/alarm/notification 或数字 0-5"
                },
                "level": {
                        "type": "integer",
                        "description": "set_volume 时: 音量值(0-max 整数)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_audio
},
    {
    "name": "phone_hw_brightness",
    "description": "Root 直写背光滑块 /sys 节点：获取/设置屏幕亮度。action: get 返回当前+max；set 设百分比(0-100)或 raw=True传原始值。自动化时先调暗省电、完成后恢复。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "description": "get 或 set"
                },
                "level": {
                        "type": "integer",
                        "description": "set 时: 0-100 百分比，或 raw=True 时 0-max 原始值"
                },
                "raw": {
                        "type": "boolean",
                        "description": "set 时: True 传原始值而非百分比"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_brightness
},
    {
    "name": "phone_hw_cpu",
    "description": "Root 调控 CPU：list(查看在线核心/governor/可用频率)、set_governor(切换调度器，如walt/schedutil)、online_core/offline_core(上线/下线指定核心)、set_max_freq(限制最大频率 kHz)。用于自动化时降低功耗。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "description": "list / set_governor / online_core / offline_core / set_max_freq"
                },
                "governor": {
                        "type": "string",
                        "description": "set_governor 时: 目标调度器名"
                },
                "core": {
                        "type": "integer",
                        "description": "online_core/offline_core 时: 核心编号(如 7 表示 cpu7)"
                },
                "freqKHz": {
                        "type": "integer",
                        "description": "set_max_freq 时: 频率上限(kHz，需是 availableFrequencies 之一)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_cpu
},
    {
    "name": "phone_hw_firewall",
    "description": "Root iptables 防火墙：按 App uid/包名拦截所有网络(IPv4+IPv6 OUTPUT DROP)、解封、查看规则、清空全部。用于断网调试自动化 App离线行为。⚠️ clear_all 会清空全部 OUTPUT 规则！",
    "inputSchema": {
        "type": "object",
        "properties": {
                "action": {
                        "type": "string",
                        "description": "list / block_app / unblock_app / clear_all"
                },
                "package": {
                        "type": "string",
                        "description": "block/unblock 时: 包名(如 com.tencent.mm)，自动解析 uid"
                },
                "uid": {
                        "type": "integer",
                        "description": "block/unblock 时: 直接指定 uid"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_net_firewall
},
    {
    "name": "phone_hw_vibrate",
    "description": "触发手机震动指定毫秒(10-60000)。三级回退: sysfs 节点 -> cmd vibrator -> AIDL HAL service call。用于任务完成提醒。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "durationMs": {
                        "type": "integer",
                        "description": "震动时长(毫秒)，默认 200"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_vibrate
},
    {
    "name": "phone_frida_attach",
    "description": "使用 frida-rust ptrace 附着到目标进程（按进程名查找）。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "processName": {
                        "type": "string",
                        "description": "目标进程名称(如 com.tencent.mm)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "processName"
        ]
},
    "handler": t_frida_attach
},
    {
    "name": "phone_frida_inject",
    "description": "使用 frida-rust 将共享库注入到目标进程(ptrace+dlopen)。需 root，设备上需有 frida-rust 二进制。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "目标进程 PID"
                },
                "libPath": {
                        "type": "string",
                        "description": "可选，共享库路径(默认 /data/local/tmp/libfrida_agent.so)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "pid"
        ]
},
    "handler": t_frida_inject
},
    {
    "name": "phone_frida_read_mem",
    "description": "跨进程读取目标内存，返回十六进制数据。通过 frida-rust Rhai 脚本的 read_memory API。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "目标进程 PID"
                },
                "address": {
                        "type": "string",
                        "description": "起始地址(十六进制，如 0x7f12345000)"
                },
                "size": {
                        "type": "integer",
                        "description": "读取字节数(最大 1MB)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "pid",
                "address",
                "size"
        ]
},
    "handler": t_frida_read_mem
},
    {
    "name": "phone_frida_scan_mem",
    "description": "在目标进程内存中搜索字节模式，返回所有匹配地址。通过 frida-rust search_bytes API。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "目标进程 PID"
                },
                "pattern": {
                        "type": "string",
                        "description": "十六进制字节模式(如 48895C24)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "pid",
                "pattern"
        ]
},
    "handler": t_frida_scan_mem
},
    {
    "name": "phone_frida_script",
    "description": "在目标进程上执行 Rhai 脚本（frida-rust 脚本引擎）。支持内存读写、Hook、搜索等 API。可选 --anti-detect。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "script": {
                        "type": "string",
                        "description": "Rhai 脚本内容（支持 find_module_base/read_memory/write_memory/search_bytes/hook_function 等 API）"
                },
                "pid": {
                        "type": "integer",
                        "description": "可选，目标进程 PID"
                },
                "antiDetect": {
                        "type": "boolean",
                        "description": "可选，是否启用反检测(默认 false)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "script"
        ]
},
    "handler": t_frida_script
},
    {
    "name": "phone_frida_stealth",
    "description": "对目标进程应用 frida-rust 全部反检测措施：TracerPid 清零、/proc/maps 隐藏、Frida 特征擦除。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "可选，目标进程 PID(默认 0 表示自身)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        }
},
    "handler": t_frida_stealth
},
    {
    "name": "phone_frida_write_mem",
    "description": "跨进程写入目标内存(hexData 为十六进制字符串)。通过 frida-rust Rhai 脚本的 write_memory API。需 root。",
    "inputSchema": {
        "type": "object",
        "properties": {
                "pid": {
                        "type": "integer",
                        "description": "目标进程 PID"
                },
                "address": {
                        "type": "string",
                        "description": "目标地址(十六进制)"
                },
                "hexData": {
                        "type": "string",
                        "description": "要写入的十六进制数据(如 90909090)"
                },
                "deviceSerial": {
                        "type": "string",
                        "description": "可选，设备序列号"
                }
        },
        "required": [
                "pid",
                "address",
                "hexData"
        ]
},
    "handler": t_frida_write_mem
},
    {
    "name": "phone_app_traverse",
    "description": "逐个启动已装第三方 app 并计时，持久化学习到 data/app_perf.json，输出优化空间报告（可加速比/可跳过崩溃清单/最慢 Top10）。支持 limit 限制数量。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "可选，最多遍历前 N 个 app；省略则遍历全部第三方 app"
            },
            "methods": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，参与学习的启动方式列表，默认 [\"am_start\"]"
            },
            "deviceSerial": {
                "type": "string",
                "description": "可选，设备序列号"
            }
        }
    },
    "handler": t_app_traverse
},
    {
    "name": "phone_app_launch",
    "description": "用 learned 最优方式启动单个 app；历史崩溃率过高的 app 自动跳过(可 force=true 强制)。学习库位于 data/app_perf.json。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "package": {
                "type": "string",
                "description": "目标应用包名(必填)"
            },
            "force": {
                "type": "boolean",
                "description": "可选，True 时忽略 skip_list 强制启动"
            },
            "deviceSerial": {
                "type": "string",
                "description": "可选，设备序列号"
            }
        },
        "required": ["package"]
    },
    "handler": t_app_launch
},
    {
    "name": "phone_learn_recall",
    "description": "根据当前任务情境检索已学经验原则（做事前调用，主动套用，避免重复踩坑）。返回按相关度排序的 principle/why/confidence。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "situation": {
                "type": "string",
                "description": "当前任务/情境的自然语言描述(必填)"
            },
            "limit": {
                "type": "integer",
                "description": "可选，返回条数(默认 5)"
            }
        },
        "required": ["situation"]
    },
    "handler": t_learn_recall
},
    {
    "name": "phone_learn_reflect",
    "description": "把一条经验沉淀进学习库（做事后调用）。已存在则强化(置信提升+证据追加)，verified=false 则反驳降级。经验含 id/principle 及可选 title/why/triggers/evidence。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "lesson": {
                "type": "object",
                "description": "经验对象，至少含 id 与 principle"
            },
            "verified": {
                "type": "boolean",
                "description": "可选，True=确认/新增(默认)，False=被反驳降级"
            }
        },
        "required": ["lesson"]
    },
    "handler": t_learn_reflect
}
]
