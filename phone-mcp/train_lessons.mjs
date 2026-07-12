/**
 * ============================================================================
 *  train_lessons.mjs  ——  phone-mcp 经验库「训练」脚本（说明文档 + 训练工具）
 * ============================================================================
 *
 *  【这是什么？】
 *  phone-mcp 的"智能"不是神经网络，而是一个"经验库"文件：
 *      phone-mcp/data/lessons.json
 *  每条经验(lesson)记录一条"踩坑→为什么→以后怎么办"的因果规律。
 *  AI 干活前会调用 phone_learn_recall(情境) 把相关经验召回，避免重复踩坑。
 *
 *  【一条 lesson 长啥样？（必懂字段）】
 *  {
 *    id:         "唯一标识，同 id 再次写入=强化而非新增",
 *    title:      "一句话标题",
 *    principle:  "可执行的规律（最重要，AI 照做）",
 *    why:        "为什么这样（让经验可迁移）",
 *    triggers:   ["触发词数组", "微信","wechat","发送"…]   ← 召回靠它！
 *                （中文靠子串匹配，英文/技术词靠分词匹配，所以两类都要写满）
 *    evidence:   ["证据1","证据2"]  实测过的现象，不是瞎编,
 *    applies_to: ["wechat","ui","any"]  适用领域,
 *    confidence: 0.0~1.0  置信度（0.7 起，被验证+0.1，被反驳-0.3）
 *  }
 *
 *  【本脚本做三件事】
 *   1) 把 NEW_LESSONS 里的新经验 upsert（新增则追加，同 id 则强化）进 lessons.json
 *   2) 备份原文件到 lessons.json.bak
 *   3) 跑一遍"召回 demo"，用几句情境证明新经验能被检索到（否则等于白训）
 *
 *  运行： node train_lessons.mjs
 * ============================================================================
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// 注意路径里带空格（"phone mcp"），用字符串字面量即可，不要被 shell 拆词
const LESSON_PATH = path.join(__dirname, "data", "lessons.json");
const BAK_PATH = LESSON_PATH + ".bak";

// ---------------------------------------------------------------------------
//  1) 召回逻辑（与 tools/learner.py 的 recall() 保持一致，用于验证可检索性）
// ---------------------------------------------------------------------------
function tokenize(s) {
  // 按空白/标点切词，转小写，去掉单字
  return (s || "")
    .toLowerCase()
    .split(/[\s,./_\-:;]+/)
    .filter((w) => w && w.length > 1);
}

function recall(db, situation, limit = 5) {
  const toks = new Set(tokenize(situation));
  if (toks.size === 0) return [];
  const scored = [];
  for (const L of db.lessons || []) {
    let score = 0;
    // 把 触发词+标题+原理 拼成文本并分词，做词交集匹配（每个命中 +1）
    const blob =
      (L.triggers || []).join(" ") + " " + (L.title || "") + " " + (L.principle || "");
    const blobTok = new Set(tokenize(blob));
    for (const t of toks) if (blobTok.has(t)) score += 1;
    // 触发词做"子串包含"匹配（中文天然走这条，每个命中 +2）
    for (const tr of L.triggers || []) {
      if (tr && situation.toLowerCase().includes(tr.toLowerCase())) score += 2;
    }
    if (score > 0) scored.push([score, L]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  return scored.slice(0, limit).map((x) => x[1]);
}

// ---------------------------------------------------------------------------
//  3) 本轮新增经验（从「adbkey 微信发送没焦点」真机验证中提炼）
//     证据来源（本轮）：verify_adbkey_unit.py 隔离验证（修复前
//             method=adbkeyboard/verified=False，修复后 method=clipboard/success=True）
//     含 1 条全新根因经验：微信必须用剪贴板，ADBKeyBoard 在微信里射空(无焦点)
// ---------------------------------------------------------------------------
const NEW_LESSONS = [
  {
    id: "kernel-sendevent-click",
    title: "input 被 InputManager 限制时，用内核 sendevent 注入触摸事件绕过",
    principle:
      "当 `input tap/swipe` 被系统 InputManager 限制（某些 ROM / 安全窗口 / 无 INJECT_EVENTS 且无障碍不可用）时，用内核级 `sendevent` 向 `/dev/input/eventX` 写入触摸事件序列（DOWN→UP）绕过限制。微信发消息闭环已验证全程走内核点击可行，坐标体系与 input tap 一致。",
    why:
      "InputManager 是 Android 上层输入分发器；sendevent 直接写内核 input 子系统，不经 InputManager，故能绕过其拦截/限制。",
    triggers: [
      "kernel", "sendevent", "input tap", "InputManager", "限制", "绕过",
      "点击", "dev/input", "触摸事件", "注入", "被拦", "swipe",
    ],
    evidence: [
      "TEST_REPORT.md：phone_ui_swipe 返回『输入方式=kernel，绕过 InputManager 模拟点击限制』",
      "verify_kernel_e2e.py 注释：微信发消息闭环全程走内核级 sendevent 点击",
    ],
    applies_to: ["ui", "input", "wechat", "any"],
    confidence: 0.85,
  },
  {
    id: "input-clear-before-type",
    title: "输入框填字前必须先清空，否则新旧文本拼接发错内容",
    principle:
      "往微信等 App 的输入框填字前，必须先清空旧内容（wechat_clear_input / 全选删除），再输入新文本。否则输入框可能残留上次草稿，新旧文本拼接导致发送错误内容。",
    why:
      "移动端输入框常保留上一次输入草稿；不清空直接 input 会追加而非替换。",
    triggers: [
      "输入", "输入框", "清空", "clear", "残留", "拼接", "微信",
      "发送", "内容", "wechat_clear_input", "chat", "search", "草稿",
    ],
    evidence: [
      "verify_input_v2.py：每轮输入前都先调 wechat_clear_input(DEV, 'search'/'chat') 再 t_input_text",
      "脚本 Part A/B 均在 t_input_text 前清空，避免残留",
    ],
    applies_to: ["input", "wechat", "qq", "any"],
    confidence: 0.85,
  },
  {
    id: "ime-restore-after-input",
    title: "用 ADBKeyBoard 输完中文后，必须切回用户原输入法",
    principle:
      "用 ADBKeyBoard 等替代输入法完成中文/特殊字符输入后，必须切回用户原来的输入法（如 wetype），否则会残留为当前输入法，影响用户后续手动打字。",
    why:
      "ADBKeyBoard 是临时注入用的输入法；若不复原，用户真实输入会落到 ADBKeyBoard 导致异常。",
    triggers: [
      "输入法", "IME", "ADBKeyBoard", "切回", "wetype", "残留", "恢复",
      "默认输入法", "keyboard", "复原", "中文输入",
    ],
    evidence: [
      "verify_kernel_e2e.py 末尾『发送后 IME 无感切回验证』：确认切回 wetype",
      "phone_mcp 输入流程在发送后自动 restore IME（_ime_restore）",
    ],
    applies_to: ["input", "wechat", "any"],
    confidence: 0.85,
  },
  {
    id: "ocr-engine-thread-safe",
    title: "OCR 引擎必须在创建它的同一线程调用，跨线程会 session 失效崩溃",
    principle:
      "OCR 引擎（RapidOCR/onnxruntime）的 reader 必须在『创建它的同一线程』调用。后台线程创建、主线程调用会导致 session 跨线程失效而崩溃（MCP 旧 bug 根因）。应缓存 reader 到调用线程，或确保主线程创建+主线程调用。",
    why:
      "onnxruntime 的 InferenceSession 非线程安全，跨线程复用会触发 session 失效 / 崩溃。",
    triggers: [
      "OCR", "RapidOCR", "onnxruntime", "崩溃", "线程", "thread", "session",
      "跨线程", "失效", "ocr_find", "reader", "异常",
    ],
    evidence: [
      "diag_ocr.py 3b：后台线程建 reader、主线程调用复现 MCP 旧 bug（session 跨线程失效）",
      "diag_ocr.py 标题明确：『跨线程 onnxruntime 会话失效』根因诊断",
    ],
    applies_to: ["ocr", "vision", "any"],
    confidence: 0.8,
  },
  {
    id: "screenshot-path-ascii",
    title: "截图/图片路径必须 ASCII 且无空格，否则 cv2.imread 读取失败",
    principle:
      "截图/图片/临时文件路径必须用 ASCII 字符且不含空格。含中文或空格的路径会导致 cv2.imread 返回 None（图片读取失败），进而 OCR 崩溃。",
    why:
      "OpenCV 的 imread 对含非 ASCII / 空格路径在某些平台下读取失败，即使文件确实存在。",
    triggers: [
      "截图", "screenshot", "路径", "中文路径", "空格", "cv2", "imread",
      "读取失败", "PNG", "文件", "ocr", "path", "路径非法",
    ],
    evidence: [
      "diag_ocr.py【2】：显式检查路径含非ASCII/空格，判定『路径合法(ASCII/无空格)』",
      "diag_ocr.py：cv2.imread 返回 None -> 图片读取失败（即使文件存在）",
    ],
    applies_to: ["ocr", "vision", "any"],
    confidence: 0.8,
  },
  {
    // ↓ 对既有 tap-verify-effect 做「强化」：追加 OCR 复验输入落地的证据，不覆盖原核心
    id: "tap-verify-effect",
    title: "操作后验证真实状态再判成功（点击看 UI 变化、输入看 OCR 复验）",
    evidence: [
      "verify_input_v2.py：t_input_text 后用 _input_region_has(DEV, field, txt) 做 OCR 复验，确认输入框区域真出现内容才判成功",
      "输入场景中『命令成功但框里是空的/错的』很常见，OCR 复验能拦住这类假成功",
    ],
    triggers: [
      "输入校验", "OCR复验", "输入框", "内容确认", "真实落地", "验证输入",
    ],
  },
  {
    // ↓ 本轮「adbkey 微信没焦点」根因经验：微信自研控件，ADBKeyBoard 射空
    id: "wx-input-clipboard-not-adbkeyboard",
    title: "微信输入必须走剪贴板，ADBKeyBoard 在微信里射空(无焦点)",
    principle:
      "微信输入框是自研控件，无标准 EditText，不暴露 InputConnection。ADBKeyBoard 靠 InputConnection.commitText() 注入文本，在微信里射空（verified=False），表现为『adbkey 发送没焦点』。微信场景应直接走剪贴板方案(写剪贴板+KEYCODE_PASTE 粘贴)，不依赖 InputConnection，对自研控件有效。t_input_text 已改为：_wechat_foreground 时强制 _input_via_clipboard，非微信才用 ADBKeyBoard(其焦点重建修复才有意义)。",
    why:
      "实测：微信里 t_input_text 返回 method=adbkeyboard 但 verified=False(OCR 校验未通过=文本没进框)；改成剪贴板后 method=clipboard 且 success=true，文本经粘贴键真进框。",
    triggers: [
      "微信", "wechat", "adbkey", "adbkeyboard", "输入法", "commitText",
      "InputConnection", "没焦点", "射空", "输入失败", "剪贴板", "焦点",
      "自研控件", "EditText", "发送",
    ],
    evidence: [
      "verify_adbkey_unit.py 隔离验证：修复前 method=adbkeyboard verified=False；修复后 method=clipboard success=true",
      "wechat_tap_input_box 注释：微信输入框为自研控件，无标准 EditText",
    ],
    applies_to: ["wechat", "input", "ui"],
    confidence: 0.95,
  },
];

// ---------------------------------------------------------------------------
//  3) 执行：备份 → 读取 → upsert → 写回 → 召回验证
// ---------------------------------------------------------------------------
function loadDB() {
  if (fs.existsSync(LESSON_PATH)) {
    try {
      return JSON.parse(fs.readFileSync(LESSON_PATH, "utf-8"));
    } catch {
      /* 解析失败则重建 */
    }
  }
  return { version: 1, updated: "", lessons: [] };
}

function upsert(db, lesson, verified = true) {
  const lessons = (db.lessons ||= []);
  const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const exist = lessons.find((L) => L.id === lesson.id);
  if (exist) {
    // 同 id：强化（与 learner.reflect 一致）
    if (verified) {
      exist.confidence = Math.min(1.0, (exist.confidence ?? 0.5) + 0.1);
      exist.hits = (exist.hits ?? 0) + 1;
      if (lesson.evidence)
        exist.evidence = (exist.evidence || []).concat(lesson.evidence);
      for (const t of lesson.triggers || [])
        if (!exist.triggers?.includes(t)) exist.triggers.push(t);
      exist.stale = false;
    } else {
      exist.confidence = Math.max(0.0, (exist.confidence ?? 0.5) - 0.3);
      exist.stale = true;
    }
    exist.last_verified = now;
    if (lesson.principle) exist.principle = lesson.principle;
    if (lesson.why) exist.why = lesson.why;
    if (lesson.title) exist.title = lesson.title;
    return { action: "reinforced", lesson: exist };
  }
  // 新增
  const conf = lesson.confidence ?? (verified ? 0.7 : 0.3);
  const L = {
    id: lesson.id,
    title: lesson.title ?? lesson.id,
    principle: lesson.principle,
    why: lesson.why ?? "",
    triggers: [...(lesson.triggers || [])],
    evidence: [...(lesson.evidence || [])],
    applies_to: [...(lesson.applies_to || [])],
    confidence: conf,
    created: now,
    last_verified: now,
    hits: verified ? 1 : 0,
    stale: !verified,
  };
  lessons.push(L);
  return { action: "added", lesson: L };
}

// --- 主流程 ---
function main() {
  // 备份原文件
  if (fs.existsSync(LESSON_PATH)) {
    fs.copyFileSync(LESSON_PATH, BAK_PATH);
    console.log("✅ 已备份原文件 ->", path.basename(BAK_PATH));
  }

  const db = loadDB();
  const before = (db.lessons || []).length;
  const report = [];
  for (const L of NEW_LESSONS) {
    const r = upsert(db, L);
    report.push(`  [${r.action === "added" ? "新增" : "强化"}] ${L.id}`);
  }
  db.updated = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  fs.writeFileSync(LESSON_PATH, JSON.stringify(db, null, 2), "utf-8");
  const after = (db.lessons || []).length;

  console.log("\n===== 训练结果 =====");
  console.log(report.join("\n"));
  console.log(`\n经验总数： ${before} → ${after}（+${after - before}）`);

  // --- 召回验证 demo ---
  console.log("\n===== 召回验证（证明新经验能被情境检索到）=====");
  const demos = [
    "input tap 被 InputManager 限制了点不动",
    "微信输入框里还有上次的内容，怎么清空再输入",
    "用 ADBKeyBoard 输完中文后切不回原来的输入法",
    "OCR 突然崩溃，可能是线程问题",
    "截图路径带中文导致读取失败",
    "输入完文本怎么确认真的写进去了",
    "微信里用 adbkeyboard 输入法发送消息没焦点，文本没进去",
  ];
  for (const sit of demos) {
    const hits = recall(db, sit, 3);
    const ids = hits.map((h) => h.id).join(", ") || "(无)";
    console.log(`\n情境：「${sit}」`);
    console.log(`  → 召回：${ids}`);
  }
  console.log(
    "\n✅ 训练完成。备份在 lessons.json.bak，新经验已写入 lessons.json。"
  );
}

main();
