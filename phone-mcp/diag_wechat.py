# -*- coding: utf-8 -*-
"""诊断当前微信界面：列出关键标签及坐标，便于确定可靠进入聊天的方式。"""
import sys, re
sys.path.insert(0, r"C:\Users\AprYT\.workbuddy\phone-mcp")
import server

server.DRYRUN = False
device = server.resolve_device(None)

# 当前前台
cur = server.t_get_current_app({"deviceSerial": device})
print("前台:", cur.get("data") if isinstance(cur, dict) else cur)

# 屏幕尺寸
sz = server.run_adb(["shell", "wm", "size"], device=device, mutating=False)
print("屏幕:", (sz.stdout or "").strip())

# UI 结构
xml = server._get_ui_xml(device)
if not xml:
    print("UI 获取失败")
    sys.exit(1)

# 列出所有含有关键字的节点文本与坐标
keywords = ["通讯录", "向远钦", "搜索", "微信", "我", "发现", "发送", "你好", "文件传输助手"]
print("\n--- 关键标签坐标 ---")
for kw in keywords:
    for m in re.finditer(
        r'<node[^>]*text="([^"]*%s[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"' % re.escape(kw),
        xml,
    ):
        t, x1, y1, x2, y2 = m.groups()
        print("  %-12s @ (%s,%s)-(%s,%s) 中心=(%d,%d)" % (
            t, x1, y1, x2, y2, (int(x1) + int(x2)) // 2, (int(y1) + int(y2)) // 2))
    # 反向顺序
    for m in re.finditer(
        r'<node[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="([^"]*%s[^"]*)"' % re.escape(kw),
        xml,
    ):
        x1, y1, x2, y2, t = m.groups()
        print("  %-12s @ (%s,%s)-(%s,%s) 中心=(%d,%d)" % (
            t, x1, y1, x2, y2, (int(x1) + int(x2)) // 2, (int(y1) + int(y2)) // 2))

# 统计 EditText
edits = re.findall(r'class="[^"]*EditText"[^>]*', xml)
print("\nEditText 数量:", len(edits))
for e in edits[:5]:
    print("  ", e[:120])
