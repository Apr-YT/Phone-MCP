# -*- coding: utf-8 -*-
"""经验原则库 + 检索/反思 —— 像人一样学习的核心。

设计定位（区别于 app_learning 的单操作参数拟合）：
  - 记「原理(为什么)」而非只记数字；经验是跨操作、可迁移的因果规律。
  - 做事前 recall(情境)：按相关度检索已有经验，主动套用，避免重复踩坑。
  - 做事后 reflect(lesson)：把「发生了什么 / 为什么 / 以后怎么办」提炼成原则，
    已存在则强化(置信提升 + 证据追加 + hits++)，被反驳则降级标记 stale。

存储：data/lessons.json
  lesson = {id, title, principle, why, triggers[], evidence[], applies_to[],
            confidence, created, last_verified, hits, stale}

与现有工具同构：from adb import run_adb, resolve_device / from utils import ok, fail
"""
import os, re, json, time

from adb import run_adb, resolve_device
from utils import ok, fail

_LESSON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "lessons.json"
)


# ----------------------------- 持久化 -----------------------------
def _load():
    if os.path.exists(_LESSON_PATH):
        try:
            with open(_LESSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": 1, "updated": "", "lessons": []}


def _save(db):
    os.makedirs(os.path.dirname(_LESSON_PATH), exist_ok=True)
    db["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(_LESSON_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _tokenize(s):
    return [w for w in re.split(r"[\s,./_\-:;]+", (s or "").lower()) if w and len(w) > 1]


# ----------------------------- 检索 -----------------------------
def recall(situation, limit=5):
    """根据当前情境检索相关经验原则，返回按相关度降序的 lesson 列表。"""
    db = _load()
    toks = set(_tokenize(situation))
    if not toks:
        return []
    scored = []
    for L in db.get("lessons", []):
        score = 0
        blob = " ".join(L.get("triggers", [])) + " " + L.get("title", "") + " " + L.get("principle", "")
        blob_tok = set(_tokenize(blob))
        for t in toks:
            if t in blob_tok:
                score += 1
        for tr in L.get("triggers", []):   # 触发器命中加权
            if tr.lower() in situation.lower():
                score += 2
        if score > 0:
            scored.append((score, L))
    scored.sort(key=lambda x: -x[0])
    return [L for _, L in scored[:limit]]


def recall_text(situation, limit=5):
    """返回可直接喂给决策的经验文本（无相关经验时返回提示串）。"""
    ls = recall(situation, limit=limit)
    if not ls:
        return "(无相关经验)"
    out = []
    for L in ls:
        out.append("[经验 %s] %s\n  原理: %s\n  为什么: %s\n  置信: %.0f%%" % (
            L["id"], L["title"], L["principle"], L.get("why", ""),
            (L.get("confidence") or 0) * 100))
    return "\n".join(out)


# ----------------------------- 反思/沉淀 -----------------------------
def reflect(lesson, verified=True):
    """把一条经验 upsert 进库。

    - 已存在(同 id)：verified 时强化——置信 +0.1(封顶1.0)、hits++、证据/triggers 合并、刷新 last_verified；
      被反驳(verified=False) 时置信 -0.3、标记 stale。
    - 不存在：新增一条。
    返回最终落库的 lesson 字典。
    """
    db = _load()
    lessons = db.setdefault("lessons", [])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lid = lesson.get("id")
    if not lid or not lesson.get("principle"):
        raise ValueError("lesson 需至少含 id 与 principle")
    for L in lessons:
        if L.get("id") == lid:
            if verified:
                L["confidence"] = min(1.0, (L.get("confidence") or 0.5) + 0.1)
                L["hits"] = L.get("hits", 0) + 1
                ev = lesson.get("evidence")
                if ev:
                    L.setdefault("evidence", []).extend(ev if isinstance(ev, list) else [ev])
                for t in lesson.get("triggers", []):
                    if t not in L.setdefault("triggers", []):
                        L["triggers"].append(t)
                L["stale"] = False
            else:
                L["confidence"] = max(0.0, (L.get("confidence") or 0.5) - 0.3)
                L["stale"] = True
            L["last_verified"] = now
            L["principle"] = lesson.get("principle", L["principle"])
            if lesson.get("why"):
                L["why"] = lesson["why"]
            if lesson.get("title"):
                L["title"] = lesson["title"]
            _save(db)
            return L
    conf = lesson.get("confidence")
    if conf is None:
        conf = 0.7 if verified else 0.3
    L = {
        "id": lid,
        "title": lesson.get("title", lid),
        "principle": lesson["principle"],
        "why": lesson.get("why", ""),
        "triggers": list(lesson.get("triggers", [])),
        "evidence": list(lesson.get("evidence", [])),
        "applies_to": list(lesson.get("applies_to", [])),
        "confidence": conf,
        "created": now,
        "last_verified": now,
        "hits": 1 if verified else 0,
        "stale": (not verified),
    }
    lessons.append(L)
    _save(db)
    return L


def list_lessons():
    db = _load()
    return db.get("lessons", [])


# ----------------------------- 对外 MCP 工具 -----------------------------
def t_learn_recall(args):
    """根据当前情境检索相关经验原则（做事前调用，主动套用已学经验）。

    args:
      situation: 当前任务/情境的自然语言描述(必填)
      limit:     返回条数(默认 5)
    """
    sit = args.get("situation") or args.get("query") or ""
    if not sit:
        return fail("缺少 situation/query 参数")
    lim = int(args.get("limit", 5))
    ls = recall(sit, limit=lim)
    return ok("检索到 %d 条相关经验" % len(ls),
              count=len(ls), lessons=ls, text=recall_text(sit, limit=lim))


def t_learn_reflect(args):
    """把一条经验沉淀进学习库（做事后调用）。已存在则强化，被反驳则降级。

    args:
      lesson:   {id, principle, [title, why, triggers, evidence, applies_to, confidence]}
      verified: True=确认/新增, False=被反驳降级(默认 True)
    """
    lesson = args.get("lesson")
    if not isinstance(lesson, dict) or not lesson.get("id") or not lesson.get("principle"):
        return fail("lesson 需为含 id 与 principle 的对象")
    verified = bool(args.get("verified", True))
    L = reflect(lesson, verified=verified)
    return ok("经验已%s: %s" % ("强化/新增" if verified else "反驳降级", L["id"]),
              lesson=L)
