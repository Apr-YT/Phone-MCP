# -*- coding: utf-8 -*-
"""视觉定位层：UI XML 解析 + RapidOCR + 智能路由(smart_find)。"""
import os, re, time, threading, json, base64
import numpy as np
import xml.etree.ElementTree as ET
import subprocess
import traceback

from adb import run_adb, log, with_retry, DRYRUN, resolve_device
from utils import ok, fail, text_block, image_block
from tools._shared import SHOT_DIR, FAST, _ocr_debug, get_ocr_reader

# bounds regex
_BOUNDS_RE = _BOUNDS_RE = re.compile(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]")
_NODE_RE = _NODE_RE = re.compile(r"<node\b[^>]*/?>")
_ATTR_RE = _ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')

# 空树缓存
_UI_EMPTY = {}
_UI_EMPTY_TTL = 30
_TOP_PKG_RE = _TOP_PKG_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([\w.]+)/")

def _get_ui_xml(device):
    """dump 当前界面 UI 结构并返回 XML 文本；失败返回 None。

    性能优化：优先用 `adb exec-out uiautomator dump /dev/tty` 一次流式返回，
    省去旧方案「写设备文件(/sdcard/ui_dump.xml) + pull 回本地」两步 adb 往返
    （每次点击/定位约省 0.5~1s）。仅当流式方案失败或无有效节点时回退旧方案，保证兼容。"""
    try:
        r = run_adb(["exec-out", "uiautomator", "dump", "/dev/tty"],
                    device=device, mutating=False, capture=True, binary=True,
                    what="uiautomator dump(stream)")
        out = r.stdout or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "ignore")
    except Exception as e:
        log("uiautomator dump(stream) 失败，回退旧方案:", e)
        out = None
    if not out or "<node" not in out:
        # 回退：写设备文件 + pull（最稳妥的兜底路径）
        try:
            run_adb(["shell", "uiautomator", "dump", "/sdcard/ui_dump.xml"],
                    device=device, mutating=False, what="uiautomator dump")
        except Exception as e:
            log("uiautomator dump 失败:", e)
            return None
        os.makedirs(SHOT_DIR, exist_ok=True)
        local = os.path.join(SHOT_DIR, "ui_dump.xml")
        try:
            run_adb(["pull", "/sdcard/ui_dump.xml", local], device=device, mutating=False, what="pull ui_dump")
        except Exception as e:
            log("pull ui_dump 失败:", e)
            return None
        try:
            with open(local, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            log("读取 UI dump 失败:", e)
            return None
    # 去掉 uiautomator 可能附加的提示行，仅保留 XML
    s = out.find("<?xml")
    if s < 0:
        s = out.find("<hierarchy")
    if s < 0:
        s = out.find("<node")
    e = out.rfind(">")
    if s >= 0 and e >= 0:
        out = out[s:e + 1]
    return out

def ui_find(query, xml, exact=False):
    """从 uiautomator XML 中查找 text / content-desc 含 query 的控件。
    返回 [(text, cx, cy, 1.0)]（原图像素坐标，置信度恒为 1.0 表示精确来源）。"""
    hits = []
    if not xml:
        return hits
    for node in _NODE_RE.findall(xml):
        attrs = dict(_ATTR_RE.findall(node))
        label = attrs.get("text") or ""
        desc = attrs.get("content-desc") or ""
        candidates = [c for c in (label, desc) if c]
        if not candidates:
            continue
        matched = None
        for c in candidates:
            if (c == query) if exact else (query in c):
                matched = c
                break
        if matched is None:
            continue
        m = _BOUNDS_RE.search(attrs.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        hits.append((matched, cx, cy, 1.0))
    return hits

def _top_pkg(device):
    """取当前顶层 App 包名，用于精准标记空树 App。失败返回 None（走统一 run_adb，带超时+重试）。"""
    try:
        r = run_adb(["shell", "dumpsys", "window"], device=device,
                    mutating=False, what="dumpsys window")
        m = _TOP_PKG_RE.search(r.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None

def _ui_is_empty(xml):
    """UI dump 是否完全无文字（text 与 content-desc 全空）。"""
    if not xml:
        return True
    for node in _NODE_RE.findall(xml):
        a = dict(_ATTR_RE.findall(node))
        if (a.get("text") or "").strip() or (a.get("content-desc") or "").strip():
            return False
    return True

def _ocr_only(query, device, exact, region):
    get_ocr_reader()
    shot = _ocr_screenshot_img(device, region)
    if not shot:
        raise RuntimeError("截图失败，无法 OCR。")
    img, scale, off_x, off_y, coord_scale = shot
    return ocr_find(query, img, scale, off_x=off_x, off_y=off_y, coord_scale=coord_scale, exact=exact)

def smart_find(query, device, exact=False, region=None, method="auto"):
    """统一查找入口，返回 (hits, used_method)。
      method="ui"   → 只用 UI 模式（毫秒级，系统界面/桌面/计算器可用）
      method="ocr"  → 只用 OCR 模式（微信/QQ 等空树 App 用）
      method="auto" → 先 UI；未命中或空树再回退 OCR（默认，兼顾速度与兼容）
                      空树 App 会被缓存 TTL 秒，期间直接走 OCR，避免重复 dump 拖慢。
    hits = [(text, cx, cy, conf)]，原图像素坐标。
    """
    if method == "ui":
        return ui_find(query, _get_ui_xml(device), exact=exact), "ui"
    if method == "ocr":
        return _ocr_only(query, device, exact, region), "ocr"
    # auto
    pkg = _top_pkg(device)
    key = "%s:%s" % (device, pkg)
    if pkg and time.time() < _UI_EMPTY.get(key, 0):
        return _ocr_only(query, device, exact, region), "ocr"
    xml = _get_ui_xml(device)
    hits = ui_find(query, xml, exact=exact)
    if hits:
        return hits, "ui"
    if _ui_is_empty(xml) and pkg:
        _UI_EMPTY[key] = time.time() + _UI_EMPTY_TTL
    return _ocr_only(query, device, exact, region), "ocr"

def _node_from_attrs(a):
    """把一个 node 的属性 dict 转成结构化控件信息（含中心像素坐标）。"""
    a = a or {}
    rid = a.get("resource-id", "") or ""
    text = a.get("text", "") or ""
    desc = a.get("content-desc", "") or ""
    bounds = a.get("bounds", "") or ""
    cls = a.get("class", "") or ""
    pkg = rid.split("/", 1)[0] if "/" in rid else ""
    cx = cy = None
    m = _BOUNDS_RE.search(bounds)
    if m:
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    return {
        "text": text,
        "resourceId": rid,
        "contentDesc": desc,
        "className": cls,
        "package": pkg,
        "clickable": (a.get("clickable", "false") == "true"),
        "bounds": bounds,
        "cx": cx,
        "cy": cy,
    }


def parse_ui_xml(xml):
    """解析 uiautomator dump XML，返回所有 node 的结构化 dict 列表。

    每个 dict: text, resourceId, contentDesc, className, clickable, package,
               bounds, cx, cy(中心像素坐标, 无 bounds 则为 None)。
    ElementTree 解析失败时回退到正则逐 node 提取，保证健壮性。
    """
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except Exception as e:
        log("ElementTree 解析 UI XML 失败，回退正则:", e)
        nodes = []
        for node in _NODE_RE.findall(xml):
            nodes.append(_node_from_attrs(dict(_ATTR_RE.findall(node))))
        return nodes
    return [_node_from_attrs(el.attrib) for el in root.iter("node")]


def element_find(query, xml, match_by="any", exact=False):
    """在 UI 树中按 text / resource-id / content-desc 查找控件。

    match_by: any(默认) | text | resource-id | content-desc
      - any  : 在 text / resource-id / content-desc 任一字段匹配
      - 其余 : 仅在指定字段匹配
    exact=True 要求完全相等；否则子串包含即匹配。
    仅返回带有效坐标的命中。返回 [(label, cx, cy, node)]，node 为结构化 dict。
    """
    if not xml or not query:
        return []
    hits = []
    for n in parse_ui_xml(xml):
        if match_by in ("any", "text"):
            fields = [n["text"], n["resourceId"], n["contentDesc"]]
        elif match_by == "resource-id":
            fields = [n["resourceId"]]
        elif match_by == "content-desc":
            fields = [n["contentDesc"]]
        else:  # text
            fields = [n["text"]]
        matched = None
        for fld in fields:
            if not fld:
                continue
            if (fld == query) if exact else (query in fld):
                matched = fld
                break
        if matched is None:
            continue
        if n["cx"] is None:
            continue
        hits.append((matched, n["cx"], n["cy"], n))


# ---- OCR 视觉定位 ----
_OCR_READER = None
_OCR_LOCK_COPY = None  # placeholder

def _get_ocr_reader_local():
    """内部 get_ocr_reader（已被 _shared 替代，此处为兼容保留）。"""
    from tools._shared import get_ocr_reader
    return get_ocr_reader()

def _ocr_screenshot_img(device, region=None):
    """截图并返回内存中的 numpy 图像(已降采样到 OCR 目标分辨率)，零磁盘 IO。

    返回 (img, scale, off_x, off_y, coord_scale)：
      - img        : BGR numpy 数组，直接喂 RapidOCR（不再写盘→读盘）
      - scale      : 降采样缩放比, 1.0 表示未缩放
      - off_x/off_y: 裁剪区在全图(截图原始像素)的左上角
      - coord_scale: (sx, sy) 把【截图像素】映射到【手机生效逻辑分辨率】的缩放因子；
                     OCR 最终坐标须乘此因子才能与 input tap / 内核触摸坐标系对齐。
    不做坐标记忆——每次都实时截图+实时OCR，界面动态也不怕点错。
    性能：cv2.imdecode 直接从 screencap 字节解码，省「写原图+读原图」两次磁盘 IO；
          降采样在内存完成，最终直接把 numpy 交给 RapidOCR，再省「写小图+读小图」两次 IO。
    """
    import cv2
    try:
        ok = run_adb(["exec-out", "su -c 'screencap -p'"], device=device, capture=True,
                     binary=True, what="screencap(root)")
    except Exception as e:
        _ocr_debug("截图失败: %s" % e)
        return None
    png = ok.stdout
    if not png or len(png) < 100:
        return None
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        _ocr_debug("截图 cv2.imdecode 失败")
        return None
    shot_h, shot_w = img.shape[:2]
    off_x, off_y = 0, 0
    if region:
        x1, y1, x2, y2 = [float(v) for v in region]
        cxa, cya = int(x1 * shot_w), int(y1 * shot_h)
        cxb, cyb = int(x2 * shot_w), int(y2 * shot_h)
        if cxb > cxa and cyb > cya:
            img = img[cya:cyb, cxa:cxb]
            off_x, off_y = cxa, cya
    # 降采样提速：默认 720 长边（推理耗时较 1080 约减半，中文 UI 精度足够）；
    # FAST 模式进一步降到 640。coord_scale 用降采样前的原始截图尺寸(shot_w/h)。
    max_side = 640 if FAST else 720
    scale = 1.0
    if max(img.shape[0], img.shape[1]) > max_side:
        scale = max_side / max(img.shape[0], img.shape[1])
        img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)))
    sw, sh = _screen_size(device)
    coord_scale = (float(sw) / shot_w, float(sh) / shot_h)
    _ocr_debug("截图 OK img=%dx%d scale=%.3f 逻辑=%dx%d coord_scale=(%.3f,%.3f)"
               % (shot_w, shot_h, scale, sw, sh, coord_scale[0], coord_scale[1]))
    return img, scale, off_x, off_y, coord_scale

def _ocr_screenshot(device, region=None):
    """兼容包装：返回 (path, scale, off_x, off_y, coord_scale)（写盘一次供外部调试脚本用）。
    内部已优化为内存解码 + 内存降采样；此处仅最后写一次盘以兼容旧调用方(diag/verify等)。"""
    res = _ocr_screenshot_img(device, region)
    if not res:
        return None
    img, scale, off_x, off_y, coord_scale = res
    import cv2
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = os.path.join(SHOT_DIR, "ocr_shot.png")
    cv2.imwrite(path, img)
    return path, scale, off_x, off_y, coord_scale
def ocr_find(query, image, scale, off_x=0, off_y=0, coord_scale=(1.0, 1.0),
             exact=False, min_conf=0.25):
    """在截图中查找包含 query 的文字块，返回 [(text, cx, cy, conf)]。

    坐标为【手机生效逻辑分辨率】空间(=input tap / 内核触摸坐标系)，已由 coord_scale
    把 OCR 原始“截图像素”换算到位；coord_scale 默认 (1.0,1.0) 表示截图分辨率即逻辑分辨率。
    image 可为 numpy(BGR) 数组（推荐，零 IO）或图像路径（兼容旧调用）。
    """
    import cv2
    if isinstance(image, str):
        img = cv2.imread(image)
        if img is None:
            _ocr_debug("ocr_find: imread 失败 path=%s" % image)
            return []
    else:
        img = image
    sx, sy = coord_scale
    reader = get_ocr_reader()
    result = None
    last_err = None
    for _attempt in range(2):
        try:
            result, _ = reader(img)   # RapidOCR 接受 numpy(BGR) 或路径
            break
        except Exception as e:
            last_err = e
            _ocr_debug("ocr_find: reader 异常(尝试%d) %s:\n%s" % (
                _attempt + 1, type(e).__name__, traceback.format_exc()))
    if result is None:
        _ocr_debug("ocr_find: 两次调用均失败 last_err=%r path=%s" % (last_err, image_path))
        return []
    hits = []
    if not result:
        _ocr_debug("ocr_find: 空结果 path=%s" % image_path)
        return hits
    for bbox, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        match = (txt == query) if exact else (query in txt)
        if match:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            # 1) OCR 框中心(降采样空间) → 截图原始像素；2) 加裁剪偏移；3) 映射至逻辑分辨率
            cx_img = (min(xs) + max(xs)) / 2 / scale + off_x
            cy_img = (min(ys) + max(ys)) / 2 / scale + off_y
            cx = int(round(cx_img * sx))
            cy = int(round(cy_img * sy))
            hits.append((txt, cx, cy, conf))
    if not hits:
        _ocr_debug("ocr_find: 无匹配 query=%r 总数=%d 样例=%s" % (
            query, len(result), [(t, round(float(c), 2)) for _, t, c in result[:15]]))
    return hits
def ocr_boxes(device, region=None, min_conf=0.25):
    """全屏(或 region 归一化区域)OCR，返回 [(text, cx, cy, conf)]。

    坐标为【手机生效逻辑分辨率】空间(=input tap / 内核触摸坐标系)，已通过
    _ocr_screenshot 的 coord_scale 把 OCR 原始截图像素换算到位。
    region: 可选 [x1,y1,x2,y2] 归一化(0~1)区域，只识别该区域以提速并避免误匹配。"""
    shot = _ocr_screenshot_img(device, region)
    if not shot:
        return []
    img, scale, off_x, off_y, coord_scale = shot
    sx, sy = coord_scale
    try:
        result, _ = get_ocr_reader()(img)
    except Exception as e:  # noqa: BLE001
        _ocr_debug("ocr_boxes reader 异常: %r" % e)
        return []
    if not result:
        return []
    boxes = []
    for bbox, txt, conf in result:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        cx_img = (min(xs) + max(xs)) / 2 / scale + off_x
        cy_img = (min(ys) + max(ys)) / 2 / scale + off_y
        cx = int(round(cx_img * sx))
        cy = int(round(cy_img * sy))
        boxes.append((txt, cx, cy, conf))
    return boxes


def ocr_match_contact(query, device, region=None, min_conf=0.3):
    """OCR 精准匹配联系人条目：返回按 y 升序(最顶部优先)的 [(text, cx, cy, conf)]。

    针对搜索结果多匹配问题：
      - 区域过滤(region)避开底部聊天记录与输入框；
      - 优先精确匹配(整块文字 == query)，其次包含匹配，排除聊天记录里出现的同名片段；
      - 结果按 y 升序，调用方取 [0] 即最顶部的联系人条目，避免误点到聊天记录。
    """
    boxes = ocr_boxes(device, region=region, min_conf=min_conf)
    hits = [b for b in boxes if query in b[0]]
    if not hits:
        return []
    exact = [b for b in hits if b[0].strip() == query]
    pool = exact if exact else hits
    return sorted(pool, key=lambda b: b[2])
def _ocr_sees(device, query, region=None, min_conf=0.25):
    """屏幕(或 region)上是否存在含 query 的文字块。"""
    return any(query in b[0] for b in ocr_boxes(device, region=region, min_conf=min_conf))


def _ocr_tap(device, query, region=None, min_conf=0.25, strategy="top"):
    """OCR 找到含 query 的文字块并点击；strategy=top 点最靠上的，lowest 点最靠下的。
    命中返回 True，未找到返回 False。"""
    hits = [b for b in ocr_boxes(device, region=region, min_conf=min_conf) if query in b[0]]
    if not hits:
        return False
    sel = sorted(hits, key=lambda b: b[2])[0 if strategy == "top" else -1]
    cx, cy = sel[1], sel[2]
    run_adb(["shell", "input", "tap", str(cx), str(cy)], device=device, mutating=True)
    return True

_SCREEN_SIZE_CACHE = {}

def _screen_size(device):
    """取【当前生效的逻辑显示分辨率】(w, h)，即 input tap / 内核触摸 / UI 坐标系所用的空间。

    关键修正：wm size 可能同时存在 Physical size 与 Override size。screencap 始终按
    Physical(物理面板)分辨率出图，而 input tap 等触摸注入按【Override(生效逻辑)】
    分辨率工作。两者不一致(用户改过“显示大小”或 wm size override)时，OCR 坐标若
    直接用 screencap 像素会整体偏移 → 点不准。这里统一返回生效逻辑分辨率作为所有
    点击/坐标的参考系，配合 _ocr_screenshot 的 coord_scale 把 OCR 坐标换算到该空间。

    带缓存，避免每次 adb wm size；失败回退 1080x2340。
    """
    if device in _SCREEN_SIZE_CACHE:
        return _SCREEN_SIZE_CACHE[device]
    try:
        r = run_adb(["shell", "wm", "size"], device=device, mutating=False, what="wm size")
        txt = r.stdout or ""
        # 优先 Override(生效逻辑分辨率)，其次 Physical
        m = re.search(r"Override size:\s*(\d+)x(\d+)", txt)
        if not m:
            m = re.search(r"Physical size:\s*(\d+)x(\d+)", txt)
        if m:
            sz = (int(m.group(1)), int(m.group(2)))
            _SCREEN_SIZE_CACHE[device] = sz
            return sz
    except Exception:
        pass
    return 1080, 2340

def t_find_text(args):
    device = resolve_device(args.get("deviceSerial"))
    query = str(args["text"])
    _ocr_debug("收到 t_find_text query=%r method=%s" % (query, args.get("method")))
    exact = bool(args.get("exact", False))
    region = args.get("region")
    method = str(args.get("method", "auto")).lower()
    try:
        hits, used = smart_find(query, device, exact=exact, region=region, method=method)
    except RuntimeError as e:
        return fail(str(e))
    mode_label = {"ui": "无障碍/UI(毫秒级)", "ocr": "OCR(视觉)"}.get(used, used)
    if not hits:
        return ok("未找到包含 '%s' 的文字（模式: %s）。" % (query, mode_label),
                  found=False, query=query, method=used)
    hit_list = [{"text": txt, "cx": cx, "cy": cy, "conf": round(float(conf), 2)}
                for (txt, cx, cy, conf) in hits]
    lines = ["在屏幕上找到 %d 处匹配 '%s'（模式: %s）：" % (len(hits), query, mode_label)]
    for i, h in enumerate(hit_list, 1):
        lines.append("  %d. '%s' @ (%d, %d) 置信度 %.2f" % (i, h["text"], h["cx"], h["cy"], h["conf"]))
    return ok("\n".join(lines), found=True, count=len(hits), hits=hit_list,
              query=query, method=used)

def t_tap_text(args):
    device = resolve_device(args.get("deviceSerial"))
    query = str(args["text"])
    _ocr_debug("收到 t_tap_text query=%r method=%s" % (query, args.get("method")))
    exact = bool(args.get("exact", False))
    index = int(args.get("index", 1))
    region = args.get("region")
    method = str(args.get("method", "auto")).lower()
    max_retries = max(1, int(args.get("maxRetries", 2)))
    # 自动重试：界面可能未渲染完导致一时找不到，稍后重定位通常即可点到。
    for attempt in range(1, max_retries + 1):
        try:
            hits, used = smart_find(query, device, exact=exact, region=region, method=method)
        except RuntimeError as e:
            if attempt >= max_retries:
                return fail(str(e))
            time.sleep(0.5)
            continue
        mode_label = {"ui": "无障碍/UI(毫秒级)", "ocr": "OCR(视觉)"}.get(used, used)
        if not hits:
            if attempt >= max_retries:
                return fail("未找到包含 '%s' 的文字，未点击（模式: %s）。" % (query, mode_label),
                            query=query, method=used, found=False)
            time.sleep(0.6)
            continue
        if index < 1 or index > len(hits):
            return fail("index 超出范围（共 %d 处匹配，请用 index 指定）。" % len(hits),
                        query=query, method=used, count=len(hits))
        txt, cx, cy, conf = hits[index - 1]
        if DRYRUN:
            return ok("[DRYRUN] 将点击 '%s' @ (%d, %d)（模式: %s），未真正执行。" % (txt, cx, cy, mode_label),
                      dryrun=True, label=txt, cx=cx, cy=cy, method=used, conf=round(float(conf), 2),
                      attempts=attempt)
        run_adb(["shell", "input", "tap", str(cx), str(cy)], device=device, mutating=True)
        return ok("已点击 '%s' @ (%d, %d)（模式: %s，置信度 %.2f）。" % (txt, cx, cy, mode_label, conf),
                  label=txt, cx=cx, cy=cy, method=used, conf=round(float(conf), 2), attempts=attempt)
    return fail("未点击 '%s'（已重试 %d 次）。" % (query, max_retries),
                query=query, attempts=max_retries)
