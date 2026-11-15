# -*- coding: utf-8 -*-
"""
PBL 白板 —— 显式写入 + AI 美化
================================
白板不再自动刷屏。只有学生明确对导师说「写进白板 / 记一下」时，
才调用模型把内容基于案例整理成一条美化的结构化节点：

  hypothesis  疾病假设（附案例中的支持依据）
  knowledge   医学知识点（相关疾病 / 分型 / 追问要点）
  issue       学习议题（具体可回答的问题）

模型不可用时退化为规则分类，保证离线也能写入。
"""

import json
import re

from core.llm_client import chat, utility_models


# ------------------------------------------------------------
# 显式写入意图识别
# ------------------------------------------------------------

_WRITE_RES = [
    # 写/记/加 … 进/到/上 … 白板/思维导图
    re.compile(
        r"(?:写|记|添加|加|放|收录|整理|记录)\s*(?:进|到|入|上|一下|下来)?\s*"
        r"(?:白板|思维导图|导图|黑板)[：:，,]?\s*(.*)"),
    # 白板/思维导图 … 写/记/加
    re.compile(
        r"(?:白板|思维导图|导图)\s*(?:上|里|中)?\s*(?:写|记|添加|记录|加)\s*"
        r"(?:一下|进去|下来)?[：:，,]?\s*(.*)"),
    # 帮我记一下 / 记下来：…
    re.compile(
        r"(?:帮我|请|麻烦)?\s*(?:记|写|记录)\s*(?:一下|下来)[：:，,]?\s*(.*)"),
]


def detect_write_intent(text):
    """命中显式写入指令时返回要写的内容（可能为空串），否则返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    for rx in _WRITE_RES:
        m = rx.search(t)
        if m:
            return (m.group(1) or "").strip()
    return None


# ------------------------------------------------------------
# AI 美化
# ------------------------------------------------------------

_SYSTEM = "你是 PBL 导师的板书助手，只输出合法 JSON，不要 markdown 代码块。"

_PROMPT = (
    "学生明确要求把内容写进白板（思维导图）。请基于案例，把它整理成一条美化、结构化的节点。\n\n"
    "【当前案例幕内容】\n{act}\n\n"
    "【最近讨论】\n{recent}\n\n"
    "【学生请求】\n{raw}\n\n"
    "先判断类型：hypothesis(疾病假设) / knowledge(医学知识点) / issue(学习议题)。\n"
    "只输出一个 JSON 对象：\n"
    'hypothesis: {{"kind":"hypothesis","title":"规范疾病名","evidence":["案例中支持的具体信息"],"status":"proposed"}}\n'
    'knowledge: {{"kind":"knowledge","topic":"规范术语","diseases":[],"types":[],"notes":[]}}\n'
    'issue: {{"kind":"issue","title":"具体可回答的问题"}}\n'
    "要求：title/topic 简洁规范(≤20字)；数组各 1-4 条、每条≤25字；"
    "必须基于案例，不编造案例里没有的检查结果或数据。"
)


def _clean_list(items, limit=4, maxlen=25):
    out = []
    for x in items or []:
        s = str(x).strip()
        if s and s not in out:
            out.append(s[:maxlen])
        if len(out) >= limit:
            break
    return out


def _fallback_entry(raw):
    """模型不可用时的规则分类。"""
    from core.discussion_coach import DX_ALIASES, QUESTION_WORDS
    t = (raw or "").strip()
    for dx, aliases in DX_ALIASES.items():
        if any(a in t for a in aliases):
            return {"kind": "hypothesis", "title": dx, "evidence": [], "status": "proposed"}
    if any(w in t for w in QUESTION_WORDS):
        return {"kind": "issue", "title": t[:40]}
    return {"kind": "knowledge", "topic": t[:20] or "知识点",
            "diseases": [], "types": [], "notes": []}


def beautify_board_entry(case, act_no, raw_text, recent_text, cfg):
    """把学生请求美化成结构化白板节点；失败时返回规则兜底结果。"""
    raw = (raw_text or "").strip()
    if not raw:
        return None
    acts = case.get("acts") or []
    act = acts[min(act_no, len(acts)) - 1] if acts else {}
    act_text = (act.get("presentation") or "")[:1200]

    entry = None
    if cfg.get("mode") == "api" and cfg.get("api_key"):
        models = utility_models(cfg)
        for model in models:
            ecfg = dict(cfg)
            ecfg["model"] = model
            ecfg["max_tokens"] = 700
            ecfg["temperature"] = 0.3
            prompt = _PROMPT.format(act=act_text, recent=(recent_text or "")[:600], raw=raw[:400])
            reply, _ = chat(_SYSTEM, prompt, ecfg)
            if not reply:
                continue
            m = re.search(r"\{[\s\S]*\}", reply)
            if not m:
                continue
            try:
                obj = json.loads(m.group(0))
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("kind") in ("hypothesis", "knowledge", "issue"):
                entry = obj
                break

    if entry is None:
        entry = _fallback_entry(raw)

    kind = entry.get("kind")
    if kind == "hypothesis":
        title = str(entry.get("title") or "").strip()[:30]
        if not title:
            return None
        return {"kind": "hypothesis", "title": title,
                "evidence": _clean_list(entry.get("evidence")),
                "status": "proposed"}
    if kind == "knowledge":
        topic = str(entry.get("topic") or "").strip()[:30]
        if not topic:
            return None
        return {"kind": "knowledge", "topic": topic,
                "diseases": _clean_list(entry.get("diseases")),
                "types": _clean_list(entry.get("types")),
                "notes": _clean_list(entry.get("notes"))}
    if kind == "issue":
        title = str(entry.get("title") or "").strip()[:60]
        if not title:
            return None
    return {"kind": "issue", "title": title}