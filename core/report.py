# -*- coding: utf-8 -*-
"""
PBL 课堂讨论助手 —— 讨论纪要导出（python-docx）
================================================
PBL 讨论的标准产出：
  一、讨论概况（案例、日期、幕次进度）
  二、假设演变时间线（哪幕提出 → 状态如何变化 → 依据）
  三、学习议题清单（课后自学任务）
  四、讨论摘录
  五、关键学习点回顾
"""

import datetime
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn

from core.board import STATUS_LABELS


def _set_cn_font(run, size=10.5, bold=False, color=None):
    run.font.name = "Calibri"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color


def _add_heading_cn(doc, text, level=1):
    h = doc.add_heading("", level=level)
    run = h.add_run(text)
    run.font.name = "Calibri"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    run.font.color.rgb = RGBColor(0x1F, 0x3B, 0x63)
    run.font.bold = True
    run.font.size = Pt(14 if level == 1 else 12)
    return h


def _add_para(doc, text, size=10.5, bold=False, color=None):
    p = doc.add_paragraph()
    run = p.add_run(text)
    _set_cn_font(run, size=size, bold=bold, color=color)
    return p


def generate_minutes(case, board, issues, dialogue, meta, output_path):
    """
    生成 PBL 讨论纪要 docx。
    board:   HypothesisBoard
    issues:  IssueTracker
    dialogue: [("student"|"coach"|"system", text)]
    meta: {"team": ..., "released_acts": int, "include_final": bool, "mode": ...}
    """
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    style.font.size = Pt(10.5)

    # ---------- 标题 ----------
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = title.add_run("PBL 讨论纪要 · {}".format(case["title"]))
    _set_cn_font(r, size=18, bold=True, color=RGBColor(0x1F, 0x3B, 0x63))

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = sub.add_run("{} · {} · 共发放 {} 幕".format(
        case["course"], case["case_id"], meta.get("released_acts", 0)))
    _set_cn_font(r, size=9, color=RGBColor(0x88, 0x88, 0x88))
    doc.add_paragraph()

    # ---------- 一、讨论概况 ----------
    _add_heading_cn(doc, "一、讨论概况", 1)
    info_table = doc.add_table(rows=0, cols=2)
    info_table.style = "Light Grid Accent 1"
    info_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    rows = [
        ("讨论日期", meta.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))),
        ("小组/记录人", meta.get("team", "—")),
        ("讨论形式", meta.get("mode", "课堂实时讨论")),
        ("发放进度", "第 {} 幕 / 共 {} 幕".format(
            meta.get("released_acts", 0), len(case["acts"]))),
        ("学习目标", "；".join(case.get("learning_objectives", [])) or "—"),
    ]
    for k, v in rows:
        row = info_table.add_row()
        row.cells[0].text = k
        row.cells[1].text = v
        for cell in row.cells:
            for p in cell.paragraphs:
                for run in p.runs:
                    _set_cn_font(run, size=10)
    doc.add_paragraph()

    # ---------- 二、假设演变时间线 ----------
    _add_heading_cn(doc, "二、假设演变时间线", 1)
    if board.items:
        tl_table = doc.add_table(rows=1, cols=4)
        tl_table.style = "Light Grid Accent 1"
        hdr = tl_table.rows[0].cells
        for i, t in enumerate(["幕次", "假设", "最终状态", "记录依据"]):
            hdr[i].text = t
            for p in hdr[i].paragraphs:
                for run in p.runs:
                    _set_cn_font(run, size=10, bold=True)
        for it in board.items:
            evs = "；".join("第{}幕 {}".format(e["act"], e["note"] or e["status_cn"])
                            for e in it["evidence"]) or "—"
            row = tl_table.add_row().cells
            row[0].text = "第{}幕提出".format(it["created_act"])
            row[1].text = it["text"]
            row[2].text = STATUS_LABELS[it["status"]]
            row[3].text = evs
            for cell in row:
                for p in cell.paragraphs:
                    for run in p.runs:
                        _set_cn_font(run, size=10,
                                     color=RGBColor(0xB0, 0x30, 0x30) if it["status"] == "refuted"
                                     else (RGBColor(0x1E, 0x7A, 0x2E) if it["status"] == "supported" else None))
        doc.add_paragraph()
        # 每个假设的详细历史
        for it in board.items:
            _add_para(doc, "{}「{}」状态轨迹：".format(
                {"proposed": "🔵", "supported": "🟢", "refuted": "🔴", "pending": "⚪"}[it["status"]],
                it["text"]), size=10, bold=True)
            for h in it["history"]:
                _add_para(doc, "    第{}幕 → {}{}".format(
                    h["act"], STATUS_LABELS[h["status"]],
                    "（{}）".format(h["note"]) if h.get("note") else ""), size=10)
    else:
        _add_para(doc, "本次讨论未记录假设。", size=10)
    doc.add_paragraph()

    # ---------- 三、学习议题清单 ----------
    _add_heading_cn(doc, "三、学习议题清单", 1)
    if issues.items:
        for it in issues.items:
            mark = "✅" if it["status"] == "resolved" else "⬜"
            _add_para(doc, "{} [第{}幕] {}".format(mark, it["source_act"], it["text"]),
                      size=10.5, bold=(it["status"] == "open"))
            for n in it["notes"]:
                _add_para(doc, "    笔记：{}".format(n), size=9.5)
        open_cnt = issues.open_count()
        _add_para(doc, "共 {} 项议题，其中 {} 项待课后查清。".format(
            len(issues.items), open_cnt), size=10, bold=True)
    else:
        _add_para(doc, "本次讨论未生成学习议题。", size=10)
    doc.add_paragraph()

    # ---------- 四、讨论摘录 ----------
    _add_heading_cn(doc, "四、讨论摘录", 1)
    role_names = {"student": "学生", "coach": "助手", "system": "系统"}
    for d in dialogue:
        # 兼容两种输入：("student", "文本") 或 {"role": ..., "content": ...}
        if isinstance(d, dict):
            role, text = d.get("role", ""), d.get("content", "")
        else:
            role, text = d[0], d[1]
        if role == "system":
            p = doc.add_paragraph()
            r = p.add_run("—— {} ——".format(text))
            _set_cn_font(r, size=10, bold=True, color=RGBColor(0x1F, 0x3B, 0x63))
        else:
            p = doc.add_paragraph()
            r = p.add_run("{}：{}".format(role_names.get(role, role), text))
            _set_cn_font(r, size=10,
                         color=RGBColor(0x1E, 0x7A, 0x2E) if role == "coach" else None)
    doc.add_paragraph()

    # ---------- 五、关键学习点回顾（复盘模式） ----------
    if meta.get("include_final"):
        _add_heading_cn(doc, "五、关键学习点回顾（复盘）", 1)
        _add_para(doc, "本案例最终诊断：**{}**".format(case["final_diagnosis"]),
                  size=11, bold=True, color=RGBColor(0x1F, 0x3B, 0x63))
        _add_para(doc, "对照你们的假设与议题，回顾以下要点：", size=10)
        for i, p in enumerate(case.get("key_learning_points", []), 1):
            para = doc.add_paragraph(style="List Number")
            r = para.add_run(p)
            _set_cn_font(r, size=10)

    # ---------- 页脚 ----------
    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = footer.add_run("本纪要由「PBL 讨论助手」自动生成 · 全流程本地运行")
    _set_cn_font(r, size=8, color=RGBColor(0x99, 0x99, 0x99))

    doc.save(output_path)
    return output_path
