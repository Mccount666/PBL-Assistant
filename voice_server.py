# -*- coding: utf-8 -*-
"""
PBL 语音讨论助手 —— FastAPI 本地服务
=====================================
课堂围坐讨论场景：麦克风拾取讨论 → 实时转写 → AI 导师按 PBL 规则介入 → 语音播报。

启动：
    python voice_server.py
打开：
    http://localhost:8600

架构：
    浏览器（Web Speech 识别 + TTS 播报）
        ↕ HTTP / WebSocket
    本服务（案例/幕次/假设板/议题/SQLite/LLM 编排）
        ↕
    LLM API（豆包 / DeepSeek / OpenAI 兼容，复用 .bedside_config.json）

RealTalk 语音适配：
    见 core/voice_adapter.py —— 提供文档后接入真实语音流的低延迟通道。
"""

import os
import sys
import json
import time
import uuid
import sqlite3
import datetime
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cases.case_data import PBL_CASES, KNOWLEDGE_BASE
from core.discussion_coach import DiscussionCoach
from core.board import STATUS_LABELS, STATUS_ICONS
from core.report import generate_minutes
from core.llm_client import load_config, mask_key, save_config, test_connection

DB_PATH = ROOT / "data" / "voice.sqlite"
REPORTS_DIR = ROOT / "reports"
UI_DIR = ROOT / "voice_ui"

app = FastAPI(title="PBL 语音讨论助手")

# ============================================================
# 数据库
# ============================================================

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        case_id TEXT NOT NULL,
        team TEXT DEFAULT '',
        released_acts INTEGER DEFAULT 1,
        include_final INTEGER DEFAULT 0,
        coach_state TEXT DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS dialogue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        act_no INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hypotheses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'proposed',
        created_act INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hypothesis_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hyp_id INTEGER NOT NULL,
        act_no INTEGER DEFAULT 1,
        status TEXT NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS hypothesis_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hyp_id INTEGER NOT NULL,
        act_no INTEGER DEFAULT 1,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        source_act INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS canvas_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        text TEXT NOT NULL,
        detail TEXT DEFAULT '',
        act_no INTEGER DEFAULT 1,
        source TEXT DEFAULT 'ai',
        created_at TEXT NOT NULL
    );
    """)
    # 旧库迁移：补充 detail 列（已存在则忽略）
    try:
        conn.execute("ALTER TABLE canvas_nodes ADD COLUMN detail TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def db():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 案例与状态
# ============================================================

def find_case(case_id):
    for c in PBL_CASES:
        if c["case_id"] == case_id:
            return c
    return None


def get_session(conn, token):
    if not token:
        return None
    return conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()


def public_case(case):
    """学生可见的案例（去掉最终诊断与助教参考）。"""
    out = dict(case)
    out["acts"] = []
    for a in case["acts"]:
        act = {k: v for k, v in a.items() if k != "tutor_notes"}
        out["acts"].append(act)
    out.pop("final_diagnosis", None)
    out.pop("key_learning_points", None)
    return out


def load_coach(case, session):
    cfg = load_config()
    state = json.loads(session["coach_state"] or "{}")
    coach = DiscussionCoach(case, cfg, KNOWLEDGE_BASE)
    coach.turns = int(state.get("turns", 0))
    coach._evidence_count = int(state.get("evidence_count", 0))
    coach._dimension_ptr = int(state.get("dimension_ptr", 0))
    coach._act_no_hint = int(session["released_acts"])
    return coach


def save_coach(conn, session_id, coach):
    state = json.dumps({
        "turns": coach.turns,
        "evidence_count": coach._evidence_count,
        "dimension_ptr": coach._dimension_ptr,
    }, ensure_ascii=False)
    conn.execute("UPDATE sessions SET coach_state = ? WHERE id = ?", (state, session_id))


def add_dialogue(conn, session_id, role, content, act_no):
    conn.execute(
        "INSERT INTO dialogue (session_id, role, content, act_no, created_at) VALUES (?,?,?,?,?)",
        (session_id, role, content, act_no, now_str()))


def get_dialogue(conn, session_id):
    rows = conn.execute(
        "SELECT role, content, act_no, created_at FROM dialogue WHERE session_id = ? ORDER BY id",
        (session_id,)).fetchall()
    return [dict(r) for r in rows]


def get_board(conn, session_id):
    items = []
    rows = conn.execute(
        "SELECT * FROM hypotheses WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    for r in rows:
        h = dict(r)
        h["events"] = [dict(x) for x in conn.execute(
            "SELECT act_no, status, note FROM hypothesis_events WHERE hyp_id = ? ORDER BY id",
            (r["id"],)).fetchall()]
        h["evidence"] = [dict(x) for x in conn.execute(
            "SELECT act_no, note FROM hypothesis_evidence WHERE hyp_id = ? ORDER BY id",
            (r["id"],)).fetchall()]
        items.append(h)
    return items


def get_issues(conn, session_id):
    rows = conn.execute(
        "SELECT * FROM issues WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 笔记画布 ----------

def add_canvas_node(conn, session_id, ntype, text, act_no, source="ai"):
    """AI 在画布上写一条笔记（自动去重）。"""
    text = (text or "").strip()
    if not text:
        return None
    dup = conn.execute(
        "SELECT id FROM canvas_nodes WHERE session_id = ? AND type = ? AND text = ?",
        (session_id, ntype, text)).fetchone()
    if dup:
        return None
    cur = conn.execute(
        "INSERT INTO canvas_nodes (session_id, type, text, act_no, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, ntype, text, act_no, source, now_str()))
    return cur.lastrowid


def get_canvas(conn, session_id):
    rows = conn.execute(
        "SELECT id, type, text, detail, act_no, source, created_at FROM canvas_nodes "
        "WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    return [dict(r) for r in rows]


def summarize_note(text, max_len=46):
    """从 AI 回复里提炼一句笔记。"""
    t = (text or "").strip().replace("\n", " ")
    t = t.replace("**", "")
    for sep in ["。", "？", "！", "；"]:
        idx = t.find(sep)
        if 0 < idx <= max_len:
            return t[:idx + 1]
    return t[:max_len] + ("…" if len(t) > max_len else "")


# 导师回复 → 关键点标签（画布只记关键点，不抄原文）
KEYPOINT_MAP = [
    ("依据", "追问依据"),
    ("证据", "追问证据"),
    ("支持", "追问支持证据"),
    ("反证", "提示反证"),
    ("排除", "提示鉴别排除"),
    ("区分", "提示假设区分"),
    ("鉴别", "提示鉴别"),
    ("机制", "提示机制"),
    ("为什么", "提示机制"),
    ("危险", "提示危险信号"),
    ("急诊", "提示危险信号"),
    ("穿孔", "提示并发症"),
    ("阶段", "提示病程阶段"),
    ("体征", "提示体征意义"),
    ("检查", "提示检查选择"),
    ("治疗", "提示治疗方向"),
    ("假设", "提示检验假设"),
]


def key_point(reply, max_len=18):
    """
    把导师回复压缩成一个有信息量的关键点标签。
    提炼不出实质内容（纯语气/肯定句）时返回空字符串 —— 调用方不写画布。
    """
    t = (reply or "").replace("**", "").replace("\n", " ").strip()
    if not t:
        return ""
    # 纯语气/肯定句：无信息量，不上画布
    if any(w in t for w in ["特别好", "好问题", "很好", "不错", "问得好"]):
        if not any(kw in t for kw, _ in KEYPOINT_MAP):
            return ""
    if len(t) <= max_len:
        # 短句仍需含实质关键词，否则视为闲聊
        if any(kw in t for kw, _ in KEYPOINT_MAP):
            return t
        return ""
    labels = []
    for kw, label in KEYPOINT_MAP:
        if kw in t and label not in labels:
            labels.append(label)
        if len(labels) >= 2:
            break
    return "·".join(labels)


def canvas_note_for_intervention(conn, session_id, reply, actions, act_no):
    """AI 介入后，把要点写到画布上。"""
    for a in actions or []:
        if a.get("type") == "add_hypothesis":
            add_canvas_node(conn, session_id, "hypothesis", "假设：" + a["text"], act_no)
        elif a.get("type") == "add_issue":
            add_canvas_node(conn, session_id, "question", "议题：" + a["text"], act_no)
    if reply:
        kp = key_point(reply)
        if kp:
            add_canvas_node(conn, session_id, "insight", "导师：" + kp, act_no)


def canvas_note_from_speech(conn, session_id, text, act_no):
    """从学生语音转写中提取假设/议题，实时写到画布。"""
    if not text:
        return
    from core.discussion_coach import DX_ALIASES, QUESTION_WORDS

    t = text
    for dx, aliases in DX_ALIASES.items():
        if any(a in t for a in aliases):
            add_canvas_node(conn, session_id, "hypothesis", "假设：" + dx, act_no)
            break
    if any(w in t for w in QUESTION_WORDS):
        add_canvas_node(conn, session_id, "question",
                        "议题：" + summarize_note(t, 22), act_no)


# ============================================================
# 医学知识点提炼（画板核心：症状/体征 → 对应知识展开）
# ============================================================

# 常见症状/体征词表（LLM 不可用时的兜底识别）
SYMPTOM_WORDS = [
    "头痛", "头晕", "腹痛", "腹泻", "便秘", "恶心", "呕吐", "发热", "寒战", "乏力",
    "咳嗽", "咳痰", "咯血", "胸痛", "呼吸困难", "气促", "心悸", "水肿", "黄疸",
    "皮疹", "瘙痒", "消瘦", "多尿", "多饮", "口渴", "视物模糊", "耳鸣", "失眠",
    "关节痛", "腰痛", "血尿", "尿频", "尿急", "尿痛", "意识障碍", "抽搐", "晕厥",
    "反跳痛", "肌紧张", "压痛", "肝大", "脾大", "淋巴结肿大", "紫绀", "苍白",
]

KNOWLEDGE_SYSTEM = "你是严谨的医学教学助手，只输出合法 JSON。"

KNOWLEDGE_PROMPT = (
    "从下面的 PBL 讨论/案例内容中提取**所有**出现的医学知识点（症状、体征、检查、疾病名称），"
    "每个独立成一条，不要合并（例如同时出现腹痛、恶心、呕吐、发热，就输出 4 条）。\n"
    "对每个知识点给出结构化信息。只输出 JSON 数组，不要 markdown 代码块，不要任何其他文字：\n"
    '[{"topic":"头痛","diseases":["偏头痛","颅内感染","蛛网膜下腔出血"],'
    '"types":["原发性：偏头痛/紧张性/丛集性","继发性：感染/血管/占位"],'
    '"notes":["追问性质、部位、诱因","伴发热呕吐提示颅内感染"]}]\n'
    "要求：topic 用规范医学术语（如“转移性右下腹痛”而非笼统“肚子疼”）；"
    "diseases 填该症状/体征提示的常见疾病；types 填分类分型；"
    "notes 填需要进一步明确/追问的要点；各 2-4 条、每条不超过 20 字。\n"
    "内容：\n"
)

# 节流：每个会话最多每 20 秒提炼一次
_last_knowledge_ts = {}

# 知识点提取专用模型（轻量、快、便宜；推理型主模型不适合这种结构化提取）
EXTRACT_MODEL = "step-1o-turbo-vision"
EXTRACT_FALLBACK_MODELS = ["step-1o-turbo-vision", "step-3.5-flash"]


def extract_knowledge_llm(text, cfg):
    """用 LLM 提炼知识点（JSON）。不可用时返回症状词表兜底结果。"""
    topics = []
    if cfg.get("mode") == "api" and cfg.get("api_key"):
        from core.llm_client import chat
        import re
        for model in EXTRACT_FALLBACK_MODELS:
            ecfg = dict(cfg)
            ecfg["model"] = model
            ecfg["max_tokens"] = 900
            ecfg["temperature"] = 0.3
            reply, mode = chat(KNOWLEDGE_SYSTEM, KNOWLEDGE_PROMPT + (text or "")[:1400], ecfg)
            if not reply:
                continue
            m = re.search(r"\[[\s\S]*\]", reply)
            if not m:
                continue
            try:
                arr = json.loads(m.group(0))
            except Exception:
                continue
            if not isinstance(arr, list):
                continue
            for t in arr:
                if isinstance(t, dict) and (t.get("topic") or "").strip():
                    topics.append({
                        "topic": t["topic"].strip()[:30],
                        "diseases": [str(x)[:40] for x in (t.get("diseases") or [])][:4],
                        "types": [str(x)[:40] for x in (t.get("types") or [])][:4],
                        "notes": [str(x)[:40] for x in (t.get("notes") or [])][:4],
                    })
            if topics:
                break
    if not topics:
        # 兜底：只识别症状名称，无展开
        for w in SYMPTOM_WORDS:
            if w in (text or ""):
                topics.append({"topic": w, "diseases": [], "types": [], "notes": []})
    return topics


def add_knowledge_nodes(conn, session_id, topics, act_no):
    """把知识点写成画板卡片（同名去重）。"""
    added = 0
    for t in topics or []:
        topic = (t.get("topic") or "").strip()
        if not topic:
            continue
        dup = conn.execute(
            "SELECT id FROM canvas_nodes WHERE session_id = ? AND type = 'knowledge' AND text = ?",
            (session_id, topic)).fetchone()
        if dup:
            continue
        detail = json.dumps({
            "diseases": t.get("diseases") or [],
            "types": t.get("types") or [],
            "notes": t.get("notes") or [],
        }, ensure_ascii=False)
        conn.execute(
            "INSERT INTO canvas_nodes (session_id, type, text, detail, act_no, source, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, "knowledge", topic, detail, act_no, "ai", now_str()))
        added += 1
    return added


def maybe_extract_knowledge(conn, session_id, act_no):
    """学生讨论后提炼知识点（节流 + 合并最近发言）。"""
    now = time.time()
    if now - _last_knowledge_ts.get(session_id, 0) < 20:
        return 0
    _last_knowledge_ts[session_id] = now

    rows = conn.execute(
        "SELECT content FROM dialogue WHERE session_id = ? AND role = 'student' "
        "ORDER BY id DESC LIMIT 4", (session_id,)).fetchall()
    merged = " ".join(r["content"] for r in reversed(rows))
    if len(merged.strip()) < 6:
        return 0
    cfg = load_config()
    topics = extract_knowledge_llm(merged, cfg)
    return add_knowledge_nodes(conn, session_id, topics, act_no)


def state_payload(conn, session, case):
    return {
        "ok": True,
        "case": public_case(case),
        "team": session["team"],
        "released_acts": session["released_acts"],
        "include_final": bool(session["include_final"]),
        "dialogue": get_dialogue(conn, session["id"]),
        "board": get_board(conn, session["id"]),
        "issues": get_issues(conn, session["id"]),
        "canvas": get_canvas(conn, session["id"]),
        "status_labels": STATUS_LABELS,
        "status_icons": STATUS_ICONS,
        "engine": engine_label(),
    }


def engine_label():
    cfg = load_config()
    if cfg.get("mode") == "api":
        return "AI 导师（{}）".format(cfg.get("model"))
    return "离线规则引擎（未配置 AI Key）"


def require_session(conn, token):
    s = get_session(conn, token)
    if not s:
        return None
    return s


# ============================================================
# AI 介入策略（语音场景关键：不要每句都插话）
# ============================================================

INTERVENE_HINTS = ["为什么", "怎么", "机制", "是不是", "要不要", "考虑", "可能",
                   "因为", "依据", "支持", "排除", "鉴别", "是什么", "什么意思"]
DX_WORDS = ["阑尾炎", "阑尾", "胃肠炎", "溃疡", "结石", "胆囊", "胰腺炎", "肺炎",
            "肺结核", "肺脓肿", "感冒", "流感", "心梗", "糖尿病", "甲亢", "穿孔", "气胸"]


def should_intervene(text, silent_streak):
    """
    判断 AI 是否该介入这段讨论。
    返回 (bool, 原因)
    """
    if not text or len(text.strip()) < 4:
        return False, "too_short"
    t = text.strip()
    # 明确需要 AI 的时候
    if any(w in t for w in ["老师", "导师", "AI", "助手"]):
        return True, "called"
    if any(w in t for w in INTERVENE_HINTS):
        return True, "question_or_evidence"
    if any(w in t for w in DX_WORDS):
        return True, "diagnosis_mention"
    # 长时间没介入 → 强制刷存在感
    if silent_streak >= 4:
        return True, "silent_too_long"
    # 一般陈述：隔一段介入一次（用轮次奇偶做确定性节奏，避免随机不可复现）
    return False, "normal_discussion"


# ============================================================
# API：案例与讨论
# ============================================================

@app.get("/api/health")
def health():
    return {"ok": True, "engine": engine_label(), "db": str(DB_PATH)}


# ============================================================
# API：模型设置（网页填写 Key + 检测）
# ============================================================

@app.get("/api/settings")
def get_settings():
    cfg = load_config()
    return {
        "ok": True,
        "mode": cfg.get("mode"),
        "api_base": cfg.get("api_base"),
        "model": cfg.get("model"),
        "has_key": bool(cfg.get("api_key")),
        "key_masked": mask_key(cfg.get("api_key", "")),
    }


@app.post("/api/settings")
async def post_settings(payload: dict):
    """
    保存模型配置。
    接受：{api_base, api_key, model}
    api_key 留空表示保留原 Key；传 "__clear__" 表示清除。
    """
    updates = {}
    api_base = (payload.get("api_base") or "").strip()
    model = (payload.get("model") or "").strip()
    api_key = (payload.get("api_key") or "").strip()

    if api_base:
        updates["api_base"] = api_base
    if model:
        updates["model"] = model
    if api_key:
        if api_key == "__clear__":
            updates["api_key"] = ""
            updates["mode"] = "offline"
        else:
            updates["api_key"] = api_key
            updates["mode"] = "api"

    cfg = save_config(updates)
    return {
        "ok": True,
        "mode": cfg.get("mode"),
        "api_base": cfg.get("api_base"),
        "model": cfg.get("model"),
        "has_key": bool(cfg.get("api_key")),
        "key_masked": mask_key(cfg.get("api_key", "")),
        "engine": engine_label(),
    }


@app.post("/api/test_connection")
async def api_test_connection():
    """检测当前配置的 API 是否可用（发一个最小请求）。"""
    result = test_connection()
    return {"ok": True, "test": result, "engine": engine_label()}


# ============================================================
# API：笔记画布
# ============================================================

@app.get("/api/canvas")
def canvas_get(token: str = Query("")):
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        return {"ok": True, "nodes": get_canvas(conn, session["id"])}


@app.post("/api/canvas/add")
async def canvas_add(payload: dict):
    token = payload.get("token", "")
    text = (payload.get("text") or "").strip()[:200]
    ntype = payload.get("type", "insight")
    if not text:
        return {"ok": False, "error": "内容为空"}
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        nid = add_canvas_node(conn, session["id"], ntype, text,
                              session["released_acts"], source="student")
        return {"ok": True, "id": nid}


@app.post("/api/canvas/delete")
async def canvas_delete(payload: dict):
    token = payload.get("token", "")
    nid = int(payload.get("node_id") or payload.get("id") or 0)
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT id FROM canvas_nodes WHERE id = ? AND session_id = ?",
            (nid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "节点不存在"}, status_code=404)
        conn.execute("DELETE FROM canvas_nodes WHERE id = ?", (nid,))
        return {"ok": True}


@app.get("/api/cases")
def cases():
    out = []
    for c in PBL_CASES:
        out.append({
            "case_id": c["case_id"],
            "title": c["title"],
            "course": c["course"],
            "difficulty": c["difficulty"],
            "estimated_minutes": c["estimated_minutes"],
            "act_count": len(c["acts"]),
            "objectives": c["learning_objectives"],
        })
    return {"ok": True, "cases": out, "engine": engine_label()}


@app.post("/api/start")
async def start(payload: dict):
    case_id = payload.get("case_id", "")
    team = (payload.get("team") or "").strip()[:64]
    case = find_case(case_id)
    if not case:
        return JSONResponse({"ok": False, "error": "案例不存在"}, status_code=404)

    token = uuid.uuid4().hex
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (token, case_id, team, released_acts, coach_state, created_at) "
            "VALUES (?,?,?,1,'{}',?)", (token, case_id, team, now_str()))
        sid = cur.lastrowid
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        coach = load_coach(case, session)
        coach._act_no_hint = 1
        save_coach(conn, sid, coach)

        add_dialogue(conn, sid, "system", case["acts"][0]["title"] + " 已发放", 1)
        opening = coach.on_act_released(case["acts"][0])
        add_dialogue(conn, sid, "coach", opening, 1)
        # 画布：开场笔记（只记幕次标题，不抄案例原文）
        first = case["acts"][0]
        add_canvas_node(conn, sid, "act", first["title"], 1)
        # 画布：从第一幕正文提取医学知识点（症状/体征 → 疾病方向/分型/待明确）
        try:
            _cfg = load_config()
            _topics = extract_knowledge_llm(first["presentation"], _cfg)
            add_knowledge_nodes(conn, sid, _topics, 1)
        except Exception:
            pass

        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        payload = state_payload(conn, session, case)
        payload["token"] = token
        return payload


@app.post("/api/release")
async def release(payload: dict):
    token = payload.get("token", "")
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        case = find_case(session["case_id"])
        released = session["released_acts"]
        if released >= len(case["acts"]):
            return {"ok": False, "error": "已全部发放"}

        act = case["acts"][released]
        new_no = released + 1
        conn.execute("UPDATE sessions SET released_acts = ? WHERE id = ?", (new_no, session["id"]))
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone()

        coach = load_coach(case, session)
        coach._act_no_hint = new_no
        save_coach(conn, session["id"], coach)

        add_dialogue(conn, session["id"], "system", act["title"] + " 已发放", new_no)
        opening = coach.on_act_released(act)
        add_dialogue(conn, session["id"], "coach", opening, new_no)
        add_canvas_node(conn, session["id"], "act", act["title"], new_no)
        # 画布：从这一幕正文提取医学知识点
        try:
            _cfg = load_config()
            _topics = extract_knowledge_llm(act["presentation"], _cfg)
            add_knowledge_nodes(conn, session["id"], _topics, new_no)
        except Exception:
            pass

        # 新幕回顾旧假设
        board = get_board(conn, session["id"])
        review = coach.review_old_hypotheses(new_no, board)
        if review:
            add_dialogue(conn, session["id"], "coach", review, new_no)
            opening = opening + "\n\n" + review

        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone()
        payload = state_payload(conn, session, case)
        payload["spoken"] = opening
        return payload


@app.post("/api/discuss")
async def discuss(payload: dict):
    """
    学生讨论文本 → AI 判断是否介入 → 回复（或保持安静）。
    payload: {token, text, force?}
    """
    token = payload.get("token", "")
    text = (payload.get("text") or "").strip()[:500]
    force = bool(payload.get("force"))
    if not text:
        return {"ok": False, "error": "内容为空"}

    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        case = find_case(session["case_id"])
        act_no = session["released_acts"]

        add_dialogue(conn, session["id"], "student", text, act_no)

        # 最近连续未介入次数
        recent = conn.execute(
            "SELECT role, content FROM dialogue WHERE session_id = ? ORDER BY id DESC LIMIT 12",
            (session["id"],)).fetchall()
        silent_streak = 0
        for r in recent:
            if r["role"] == "coach":
                break
            if r["role"] == "student":
                silent_streak += 1

        if force:
            intervene, reason = True, "forced"
        else:
            intervene, reason = should_intervene(text, silent_streak)

        if not intervene:
            return {"ok": True, "reply": None, "actions": [], "reason": reason}

        board = get_board(conn, session["id"])
        issues = get_issues(conn, session["id"])
        coach = load_coach(case, session)
        reply, actions = coach.on_student_message(
            text,
            [{"text": b["text"], "status": b["status"],
              "status_cn": STATUS_LABELS.get(b["status"], b["status"])} for b in board],
            [{"text": i["text"], "status": i["status"]} for i in issues])
        save_coach(conn, session["id"], coach)
        add_dialogue(conn, session["id"], "coach", reply, act_no)

        # 画布：AI 把本轮讨论要点写上去
        canvas_note_for_intervention(conn, session["id"], reply, actions, act_no)

        return {"ok": True, "reply": reply, "actions": actions, "reason": reason}


@app.get("/api/state")
def state(token: str = Query("")):
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        case = find_case(session["case_id"])
        return state_payload(conn, session, case)


# ============================================================
# API：假设板 / 议题
# ============================================================

@app.post("/api/board/add")
async def board_add(payload: dict):
    token = payload.get("token", "")
    text = (payload.get("text") or "").strip()[:255]
    if not text:
        return {"ok": False, "error": "内容为空"}
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        act_no = session["released_acts"]
        cur = conn.execute(
            "INSERT INTO hypotheses (session_id, text, status, created_act, created_at) VALUES (?,?,?,?,?)",
            (session["id"], text, "proposed", act_no, now_str()))
        hid = cur.lastrowid
        conn.execute(
            "INSERT INTO hypothesis_events (hyp_id, act_no, status, note, created_at) VALUES (?,?,?,?,?)",
            (hid, act_no, "proposed", "提出假设", now_str()))
        return {"ok": True, "id": hid, "text": text, "status": "proposed", "created_act": act_no}


@app.post("/api/board/status")
async def board_status(payload: dict):
    token = payload.get("token", "")
    hid = int(payload.get("hyp_id") or payload.get("id") or 0)
    status = payload.get("status", "")
    note = (payload.get("note") or "")[:255]
    if status not in ("proposed", "supported", "refuted", "pending"):
        return {"ok": False, "error": "非法状态"}
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT id FROM hypotheses WHERE id = ? AND session_id = ?", (hid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "假设不存在"}, status_code=404)
        conn.execute("UPDATE hypotheses SET status = ? WHERE id = ?", (status, hid))
        conn.execute(
            "INSERT INTO hypothesis_events (hyp_id, act_no, status, note, created_at) VALUES (?,?,?,?,?)",
            (hid, session["released_acts"], status, note, now_str()))
        return {"ok": True}


@app.post("/api/board/evidence")
async def board_evidence(payload: dict):
    token = payload.get("token", "")
    hid = int(payload.get("hyp_id") or payload.get("id") or 0)
    note = (payload.get("note") or "").strip()[:500]
    if not note:
        return {"ok": False, "error": "依据不能为空"}
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT id FROM hypotheses WHERE id = ? AND session_id = ?", (hid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "假设不存在"}, status_code=404)
        conn.execute(
            "INSERT INTO hypothesis_evidence (hyp_id, act_no, note, created_at) VALUES (?,?,?,?)",
            (hid, session["released_acts"], note, now_str()))
        return {"ok": True}


@app.post("/api/board/delete")
async def board_delete(payload: dict):
    token = payload.get("token", "")
    hid = int(payload.get("hyp_id") or payload.get("id") or 0)
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT id FROM hypotheses WHERE id = ? AND session_id = ?", (hid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "假设不存在"}, status_code=404)
        conn.execute("DELETE FROM hypothesis_events WHERE hyp_id = ?", (hid,))
        conn.execute("DELETE FROM hypothesis_evidence WHERE hyp_id = ?", (hid,))
        conn.execute("DELETE FROM hypotheses WHERE id = ?", (hid,))
        return {"ok": True}


@app.post("/api/issue/add")
async def issue_add(payload: dict):
    token = payload.get("token", "")
    text = (payload.get("text") or "").strip()[:500]
    if not text:
        return {"ok": False, "error": "内容为空"}
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        act_no = session["released_acts"]
        cur = conn.execute(
            "INSERT INTO issues (session_id, text, status, source_act, created_at) VALUES (?,?,?,?,?)",
            (session["id"], text, "open", act_no, now_str()))
        return {"ok": True, "id": cur.lastrowid, "text": text, "status": "open", "source_act": act_no}


@app.post("/api/issue/status")
async def issue_status(payload: dict):
    token = payload.get("token", "")
    iid = int(payload.get("issue_id") or payload.get("id") or 0)
    status = payload.get("status", "")
    if status not in ("open", "resolved"):
        return {"ok": False, "error": "非法状态"}
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT id FROM issues WHERE id = ? AND session_id = ?", (iid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "议题不存在"}, status_code=404)
        conn.execute("UPDATE issues SET status = ? WHERE id = ?", (status, iid))
        return {"ok": True}


@app.post("/api/issue/delete")
async def issue_delete(payload: dict):
    token = payload.get("token", "")
    iid = int(payload.get("issue_id") or payload.get("id") or 0)
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT id FROM issues WHERE id = ? AND session_id = ?", (iid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "议题不存在"}, status_code=404)
        conn.execute("DELETE FROM issues WHERE id = ?", (iid,))
        return {"ok": True}


@app.post("/api/review_mode")
async def review_mode(payload: dict):
    token = payload.get("token", "")
    on = 1 if payload.get("include_final") else 0
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        conn.execute("UPDATE sessions SET include_final = ? WHERE id = ?", (on, session["id"]))
        return {"ok": True, "include_final": bool(on)}


def _build_export(token: str, include_final_override=None):
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        case = find_case(session["case_id"])
        board = get_board(conn, session["id"])
        issues = get_issues(conn, session["id"])
        dialogue = get_dialogue(conn, session["id"])

        # 适配 report.py 的输入结构（HypothesisBoard/IssueTracker 形状）
        class _B:
            def __init__(self, items):
                self.items = items
        class _I:
            def __init__(self, items):
                self.items = items
            def open_count(self):
                return sum(1 for it in self.items if it["status"] == "open")

        class _Item:
            def __init__(self, d):
                self.__dict__.update(d)

        board_items = []
        for b in board:
            board_items.append({
                "id": "H{}".format(b["id"]),
                "text": b["text"],
                "status": b["status"],
                "created_act": b["created_act"],
                "evidence": [{"act": e["act_no"], "note": e["note"]} for e in b["evidence"]],
                "history": [{"act": e["act_no"], "status": e["status"], "note": e["note"]}
                            for e in b["events"]],
            })

        issues_items = []
        for it in issues:
            issues_items.append({
                "id": "I{}".format(it["id"]),
                "text": it["text"],
                "status": it["status"],
                "source_act": it["source_act"],
                "notes": [],
            })

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        fname = "PBL纪要_{}_{}.docx".format(case["case_id"],
                                            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        out = REPORTS_DIR / fname
        meta = {
            "team": session["team"] or "讨论小组",
            "released_acts": session["released_acts"],
            "include_final": bool(session["include_final"]) if include_final_override is None
                             else bool(include_final_override),
            "mode": engine_label(),
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
        }
        generate_minutes(case, _B(board_items), _I(issues_items), dialogue, meta, str(out))
        return FileResponse(str(out), filename=fname,
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/api/export")
def export_get(token: str = Query("")):
    return _build_export(token)


@app.post("/api/export")
async def export_post(payload: dict):
    return _build_export(payload.get("token", ""), payload.get("include_final"))


# ============================================================
# WebSocket：实时语音通道（StepFun Realtime 代理）
# ============================================================

STEPFUN_REALTIME_URL = "wss://api.stepfun.com/v1/realtime"
REALTIME_MODELS = {
    "stepaudio-3-realtime-preview",   # 免费预览版（推荐测试）
    "stepaudio-2.5-realtime",
    "step-1o-audio",
    "step-audio-2",
    "step-audio-2-mini",
    "step-audio-r1.5",
}
# 允许浏览器发往上游的事件类型（白名单，防注入）
UPSTREAM_CLIENT_EVENTS = {
    "input_audio_buffer.append",
    "input_audio_buffer.commit",
    "input_audio_buffer.clear",
    "response.create",
    "response.cancel",
    "conversation.item.create",
}


@app.post("/api/knowledge/refs")
async def knowledge_refs(payload: dict):
    """为画板知识点检索相关文献（Europe PMC）。"""
    token = payload.get("token", "")
    nid = int(payload.get("node_id") or payload.get("id") or 0)
    with db() as conn:
        session = require_session(conn, token)
        if not session:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=401)
        row = conn.execute(
            "SELECT text FROM canvas_nodes WHERE id = ? AND session_id = ? AND type = 'knowledge'",
            (nid, session["id"])).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "知识点不存在"}, status_code=404)
        topic = row["text"]

    cfg = load_config()
    try:
        from core.literature import search_for_topic
        res = search_for_topic(topic, cfg, 3)
    except Exception as e:
        return {"ok": False, "error": "文献检索失败: {}".format(e)}
    return {"ok": True, "topic": topic, **res}


@app.get("/api/literature")
def literature(q: str = Query(""), limit: int = 3):
    """自由检索文献（Europe PMC）。"""
    if not q.strip():
        return {"ok": False, "error": "缺少查询词"}
    cfg = load_config()
    try:
        from core.literature import search_for_topic
        res = search_for_topic(q.strip(), cfg, max(1, min(limit, 10)))
    except Exception as e:
        return {"ok": False, "error": "文献检索失败: {}".format(e)}
    return {"ok": True, **res}


@app.get("/api/realtime/status")
def realtime_status():
    cfg = load_config()
    return {
        "ok": True,
        "available": bool(cfg.get("api_key")),
        "models": sorted(REALTIME_MODELS),
        "default_model": "stepaudio-3-realtime-preview",
        "hint": "需要阶跃星辰 API Key（同一 Key 同时用于文本与语音）",
    }


@app.websocket("/ws/realtime/{session_token}")
async def ws_realtime(ws: WebSocket, session_token: str, model: str = "stepaudio-3-realtime-preview"):
    """
    浏览器 ⇄ 本服务 ⇄ 阶跃星辰 Realtime。
    Key 只在服务端使用；浏览器只连接本地 WS。
    """
    await ws.accept()

    if model not in REALTIME_MODELS:
        await ws.send_text(json.dumps({"type": "error", "error": "不支持的语音模型: " + model}))
        await ws.close()
        return

    cfg = load_config()
    if not cfg.get("api_key"):
        await ws.send_text(json.dumps({
            "type": "error",
            "error": "未配置 API Key，请先在「⚙ 设置」中填入阶跃星辰 Key",
        }))
        await ws.close()
        return

    # 取会话上下文，组装 PBL 导师指令
    with db() as conn:
        session = require_session(conn, session_token)
        if not session:
            await ws.send_text(json.dumps({"type": "error", "error": "会话不存在"}))
            await ws.close()
            return
        case = find_case(session["case_id"])
        act_no = session["released_acts"]
        board = get_board(conn, session["id"])
        issues = get_issues(conn, session["id"])
        session_id = session["id"]

    from core.voice_adapter import build_pbl_instructions
    instructions = build_pbl_instructions(case, act_no, board, issues)

    try:
        import websockets
        upstream = await websockets.connect(
            "{}?model={}".format(STEPFUN_REALTIME_URL, model),
            additional_headers={"Authorization": "Bearer " + cfg["api_key"]},
            max_size=8 * 1024 * 1024,
            open_timeout=15,
        )
    except Exception as e:
        await ws.send_text(json.dumps({
            "type": "error",
            "error": "连接阶跃星辰失败: {}: {}".format(type(e).__name__, e),
        }))
        await ws.close()
        return

    # 会话配置：PBL 导师指令 + PCM16 + 服务端 VAD
    await upstream.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": instructions,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": {
                "type": "server_vad",
                "prefix_padding_ms": 400,
                "silence_duration_ms": 600,
            },
        },
    }, ensure_ascii=False))

    import asyncio

    async def client_to_upstream():
        """转发浏览器音频/控制事件到上游（事件类型白名单）。"""
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                # 文本发言在转发时直接落库（服务器事件不含文本内容，不可依赖）
                if mtype == "conversation.item.create":
                    item = msg.get("item") or {}
                    if item.get("role") == "user":
                        for c in item.get("content") or []:
                            if c.get("type") in ("input_text", "text") and (c.get("text") or "").strip():
                                with db() as conn:
                                    add_dialogue(conn, session_id, "student",
                                                 c["text"].strip(), act_no)
                                break
                if mtype in UPSTREAM_CLIENT_EVENTS:
                    await upstream.send(json.dumps(msg, ensure_ascii=False))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    async def upstream_to_client():
        """转发上游事件；捕获转写与回复写入讨论记录。"""
        try:
            async for raw in upstream:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type", "")

                # 学生语音转写完成 → 记入讨论流 + 画布提取
                if mtype == "conversation.item.input_audio_transcription.completed":
                    text = (msg.get("transcript") or "").strip()
                    if text:
                        with db() as conn:
                            add_dialogue(conn, session_id, "student", text, act_no)
                            canvas_note_from_speech(conn, session_id, text, act_no)
                            # 讨论后提炼医学知识点（节流）
                            try:
                                maybe_extract_knowledge(conn, session_id, act_no)
                            except Exception:
                                pass
                # 导师语音回复转写完成 → 记入讨论流 + 画布笔记
                elif mtype == "response.audio_transcript.done":
                    text = (msg.get("transcript") or "").strip()
                    if text:
                        kp = key_point(text)
                        with db() as conn:
                            add_dialogue(conn, session_id, "coach", text, act_no)
                            if kp:
                                add_canvas_node(conn, session_id, "insight",
                                                "导师：" + kp, act_no)

                await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ============================================================
# WebSocket：文本桥接通道（保留）
# ============================================================

@app.websocket("/ws/voice/{token}")
async def ws_voice(ws: WebSocket, token: str):
    """
    语音实时通道。
    当前：回显 + 文本讨论桥接（浏览器端 Web Speech 识别后仍走 /api/discuss）。
    接入 RealTalk 后：本端点作为音频流转发的代理或信令通道。
    """
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            if data.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
            else:
                await ws.send_text(json.dumps({
                    "type": "info",
                    "message": "语音实时通道已连接（RealTalk 接入后启用音频流）",
                }))
    except WebSocketDisconnect:
        pass


# ============================================================
# 静态页面
# ============================================================

app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


@app.get("/")
def index():
    return FileResponse(str(UI_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 56)
    print("PBL 语音讨论助手")
    print("  引擎:", engine_label())
    print("  打开: http://localhost:8600")
    print("=" * 56)
    uvicorn.run(app, host="127.0.0.1", port=8600, log_level="warning")