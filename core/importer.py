# -*- coding: utf-8 -*-
"""
PBL 案例导入解析
=================
把老师现成的 Word / PPT / 图片资料转换成 PBL 案例结构。

设计原则（与产品约定一致）：
  - 不调用大模型，完全按文档里的标题机械切分，结果可预期；
  - 幕次标题支持「第一幕 / 第1幕 / 幕一 / Act 1 / Scene 2」等写法；
  - 没有幕次标题时：Word 退化为按 Heading 1 切分，PPT 退化为逐页拆分；
  - Word 内嵌图片按出现位置归入当前幕，PPT 图片按幻灯片归入对应幕；
  - 单独上传的图片生成一幕「图像资料」；
  - 旧版 .doc/.ppt 先由 LibreOffice 转成 .docx/.pptx 再解析。
"""

import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

try:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
except Exception:  # pragma: no cover - 依赖缺失时仅影响 PPT 导入
    Presentation = None
    MSO_SHAPE_TYPE = None

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


DOC_EXTS = {".docx", ".doc", ".pptx", ".ppt"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

DEFAULT_COURSE = "导入案例"
DEFAULT_DIFFICULTY = "★★☆"
DEFAULT_MINUTES = 45

_LO_LOCK = threading.Lock()


# ------------------------------------------------------------
# 标题 / 栏目规则
# ------------------------------------------------------------

_ACT_RE = re.compile(
    r"^[\s【\[（(]*"
    r"(?:"
    r"第\s*[一二三四五六七八九十百零〇0-9]+\s*幕"
    r"|幕\s*[一二三四五六七八九十0-9]+"
    r"|act\s*\d+"
    r"|scene\s*\d+"
    r")"
    r"[\s】\]）)]*"
    r"(?:[·:：\-—、.．]\s*)?"
    r"(.*)$",
    re.I,
)

_SECTION_KEYWORDS = {
    "学习目标": "objectives",
    "教学目标": "objectives",
    "课程目标": "objectives",
    "讨论问题": "prompts",
    "讨论提纲": "prompts",
    "思考题": "prompts",
    "讨论题": "prompts",
    "引导问题": "prompts",
    "助教参考": "tutor",
    "教师参考": "tutor",
    "导师参考": "tutor",
    "教师提示": "tutor",
    "最终诊断": "final",
    "诊断结果": "final",
    "关键学习点": "keypoints",
    "复盘要点": "keypoints",
    "学习要点": "keypoints",
}
_SECTION_RE = re.compile(
    r"^(%s)\s*[:：]?\s*(.*)$" % "|".join(_SECTION_KEYWORDS), re.I)

_TUTOR_SUB = {
    "期望假设": "hyp",
    "可能假设": "hyp",
    "学生可能提出的假设": "hyp",
    "期望议题": "issue",
    "期望学习议题": "issue",
    "学习议题": "issue",
    "可能的问题": "issue",
}
_TUTOR_SUB_RE = re.compile(
    r"^(%s)\s*[:：]?\s*(.*)$" % "|".join(_TUTOR_SUB), re.I)

_META_PATTERNS = [
    ("title", re.compile(r"^(?:案例标题|案例名称|标题)\s*[:：]\s*(.+)$")),
    ("course", re.compile(r"^(?:课程|课程名称|学科)\s*[:：]\s*(.+)$")),
    ("difficulty", re.compile(r"^(?:难度|难度等级)\s*[:：]\s*(.+)$")),
    ("minutes", re.compile(r"^(?:预计时长|时长|学时)\s*[:：]?\s*(\d+)\s*(?:分钟)?$")),
]

_BULLET_RE = re.compile(
    r"^(?:[-*•·●○▪]|\d+[.、)）]|（\d+）|\(\d+\)|[①-⑳])\s*")


def _match_act(text):
    """命中幕次标题时返回标题文本，否则返回 None。"""
    s = (text or "").strip()
    if not s or len(s) > 80:
        return None
    if not _ACT_RE.match(s):
        return None
    return _clean_title(s)


def _clean_title(text, maxlen=80):
    t = (text or "").strip()
    t = re.sub(r"^[【\[（(]\s*", "", t)
    t = re.sub(r"\s*[】\]）)]$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:maxlen]


def _clean_item(text):
    t = _BULLET_RE.sub("", (text or "").strip())
    return t.strip().strip("：:;；").strip()


def _match_section(line):
    """识别栏目标题，返回 (kind, 同行剩余内容)。"""
    s = (line or "").strip().strip("【】[]").strip()
    if not s or len(s) > 60:
        return None, ""
    m = _SECTION_RE.match(s)
    if m:
        return _SECTION_KEYWORDS[m.group(1)], m.group(2).strip()
    # 「诊断：xxx」这种简写只在明确带冒号时识别
    m = re.match(r"^诊断\s*[:：]\s*(.+)$", s)
    if m:
        return "final", m.group(1).strip()
    return None, ""


def _match_tutor_sub(line):
    s = (line or "").strip()
    if not s or len(s) > 60:
        return None, ""
    m = _TUTOR_SUB_RE.match(s)
    if not m:
        return None, ""
    return _TUTOR_SUB[m.group(1)], m.group(2).strip()


def _is_heading_style(paragraph):
    try:
        name = (paragraph.style.name or "").strip().lower()
    except Exception:
        return False
    return name.startswith("heading") or name.startswith("标题")


# ------------------------------------------------------------
# 媒体文件
# ------------------------------------------------------------

class _MediaSink:
    """把图片写入 data/media/<case_id>/，返回浏览器可访问的 URL。"""

    MAX_IMAGE_BYTES = 8 * 1024 * 1024

    def __init__(self, media_root, case_id):
        self.case_id = case_id
        self.dir = Path(media_root) / case_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seen = {}
        self.warnings = []

    def save(self, blob, ext_hint="", source="图片"):
        if not blob:
            return None
        if len(blob) > self.MAX_IMAGE_BYTES:
            self.warnings.append("{}超过 8MB，已跳过".format(source))
            return None

        prepared = self._prepare(blob, ext_hint)
        if not prepared:
            self.warnings.append("{}格式不支持，已跳过".format(source))
            return None
        data, ext = prepared

        digest = hashlib.sha1(data).hexdigest()[:12]
        if digest in self._seen:
            return self._seen[digest]

        name = "img_{}{}".format(digest, ext)
        (self.dir / name).write_bytes(data)
        url = "/media/{}/{}".format(self.case_id, name)
        self._seen[digest] = url
        return url

    def _prepare(self, blob, ext_hint):
        ext = (ext_hint or "").lower()
        if ext == ".jpeg":
            ext = ".jpg"

        if Image is not None:
            try:
                with Image.open(io.BytesIO(blob)) as im:
                    fmt = (im.format or "").upper()
                    if fmt in ("JPEG", "PNG", "GIF", "WEBP"):
                        return blob, {"JPEG": ".jpg", "PNG": ".png",
                                      "GIF": ".gif", "WEBP": ".webp"}[fmt]
                    if fmt in ("BMP", "TIFF"):
                        out = io.BytesIO()
                        im.convert("RGB").save(out, format="PNG")
                        return out.getvalue(), ".png"
                    return None
            except Exception:
                return None

        # Pillow 不可用时的兜底：只认扩展名
        if ext in (".png", ".jpg", ".gif", ".webp"):
            return blob, ext
        return None


# ------------------------------------------------------------
# 单段内容解析（正文 / 栏目 / 助教参考）
# ------------------------------------------------------------

class _PieceParser:
    def __init__(self, allow_body=True):
        self.allow_body = allow_body
        self.body = []
        self.prompts = []
        self.objectives = []
        self.hyps = []
        self.issues = []
        self.notes = []
        self.final = []
        self.keypoints = []
        self.images = []
        self.ignored = 0
        self.mode = "body"
        self.tutor_mode = None

    def feed_tokens(self, tokens):
        for tok in tokens or []:
            if tok.get("kind") == "image":
                if tok.get("url"):
                    self.images.append(tok["url"])
                continue
            for line in (tok.get("text") or "").split("\n"):
                self.feed_line(line)

    def feed_line(self, line):
        s = (line or "").strip()
        if not s:
            if self.mode == "body" and self.allow_body:
                self.body.append("")
            return

        sec, rest = _match_section(s)
        if sec:
            self.mode = sec
            self.tutor_mode = None
            if rest:
                self._add(rest)
            return

        if self.mode == "tutor":
            sub, rest = _match_tutor_sub(s)
            if sub:
                self.tutor_mode = sub
                if rest:
                    self._add_tutor(rest)
                return
            self._add_tutor(s)
            return

        self._add(s)

    def _add(self, text):
        item = _clean_item(text)
        if not item:
            return
        if self.mode == "body":
            if self.allow_body:
                self.body.append(item)
            else:
                self.ignored += 1
        elif self.mode == "prompts":
            self.prompts.append(item)
        elif self.mode == "objectives":
            self.objectives.append(item)
        elif self.mode == "final":
            self.final.append(item)
        elif self.mode == "keypoints":
            self.keypoints.append(item)
        elif self.mode == "tutor":
            self._add_tutor(item)

    def _add_tutor(self, text):
        item = _clean_item(text)
        if not item:
            return
        if self.tutor_mode == "hyp":
            self.hyps.append(item)
        elif self.tutor_mode == "issue":
            self.issues.append(item)
        else:
            self.notes.append(item)

    def result(self):
        body = "\n".join(self.body).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        return {
            "body": body,
            "prompts": self.prompts,
            "objectives": self.objectives,
            "tutor": {
                "expected_hypotheses": self.hyps,
                "expected_issues": self.issues,
                "notes": self.notes,
            },
            "final": " ".join(self.final).strip(),
            "keypoints": self.keypoints,
            "images": self.images,
            "ignored": self.ignored,
        }


def _merge_results(dst, src):
    dst["prompts"].extend(src.get("prompts") or [])
    dst["objectives"].extend(src.get("objectives") or [])
    dst["keypoints"].extend(src.get("keypoints") or [])
    for key in ("expected_hypotheses", "expected_issues", "notes"):
        dst["tutor"][key].extend((src.get("tutor") or {}).get(key) or [])
    if src.get("final") and not dst.get("final"):
        dst["final"] = src["final"]
    dst["images"].extend(src.get("images") or [])


def _make_act(n, title, piece):
    title = _clean_title(title) or "第{}幕".format(n)
    body = piece.get("body") or ""
    images = piece.get("images") or []
    if not body and images:
        body = "（本幕为图片资料，请查看下方图片）"
    return {
        "act": n,
        "title": title,
        "presentation": body,
        "discussion_prompts": piece.get("prompts") or [],
        "tutor_notes": {
            "expected_hypotheses": piece["tutor"]["expected_hypotheses"],
            "expected_issues": piece["tutor"]["expected_issues"],
            "notes": piece["tutor"]["notes"],
        },
        "images": images,
    }


# ------------------------------------------------------------
# Word 解析
# ------------------------------------------------------------

def _docx_paragraph_images(paragraph, doc, sink, warnings):
    urls = []
    try:
        blips = paragraph._p.xpath(".//a:blip")
    except Exception:
        return urls
    for blip in blips:
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        part = doc.part.related_parts.get(rid)
        if part is None or not hasattr(part, "blob"):
            continue
        try:
            ext = Path(str(part.partname)).suffix
            url = sink.save(part.blob, ext, "Word 内嵌图片")
            if url:
                urls.append(url)
        except Exception:
            warnings.append("有 1 张 Word 内嵌图片读取失败，已跳过")
    return urls


def _table_text(table):
    lines = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = " / ".join(x.strip() for x in (cell.text or "").splitlines()
                              if x.strip())
            cells.append(text)
        line = " | ".join(cells).strip()
        if line.strip(" |"):
            lines.append(line)
    return "\n".join(lines)


def _docx_tokens(path, sink, warnings):
    doc = Document(str(path))
    tokens = []
    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            text = (block.text or "").strip()
            if text:
                tokens.append({"kind": "text", "text": text,
                               "heading": _is_heading_style(block)})
            for url in _docx_paragraph_images(block, doc, sink, warnings):
                tokens.append({"kind": "image", "url": url})
        elif isinstance(block, Table):
            text = _table_text(block)
            if text:
                tokens.append({"kind": "text", "text": text, "heading": False})
            for row in block.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        for url in _docx_paragraph_images(p, doc, sink, warnings):
                            tokens.append({"kind": "image", "url": url})
    return tokens


def _parse_docx_case(path, sink, warnings, fallback_title):
    tokens = _docx_tokens(path, sink, warnings)
    if not tokens:
        raise ValueError("Word 文档中没有可导入的文字或图片")

    act_positions = [i for i, t in enumerate(tokens)
                     if t.get("kind") == "text" and _match_act(t.get("text"))]
    split_mode = "act"
    if not act_positions:
        act_positions = [i for i, t in enumerate(tokens)
                         if t.get("kind") == "text" and t.get("heading")
                         and not _match_section(t.get("text"))[0]]
        split_mode = "heading"

    meta = {"title": "", "course": "", "difficulty": "", "minutes": None,
            "objectives": [], "final": "", "keypoints": []}

    if not act_positions:
        # 单幕：第一行短文本作为标题，其余作为正文
        body_tokens = list(tokens)
        first_idx = next((i for i, t in enumerate(body_tokens)
                          if t.get("kind") == "text" and (t.get("text") or "").strip()), None)
        if first_idx is not None:
            first_text = body_tokens[first_idx]["text"].strip()
            if (len(first_text) <= 40 and not first_text.endswith("。")
                    and not _match_act(first_text)
                    and not _match_section(first_text)[0]):
                meta["title"] = _clean_title(first_text)
                lines = first_text.splitlines()
                if len(lines) == 1:
                    body_tokens.pop(first_idx)
        piece = _PieceParser()
        piece.feed_tokens(body_tokens)
        res = piece.result()
        meta["objectives"].extend(res["objectives"])
        meta["final"] = res["final"]
        meta["keypoints"].extend(res["keypoints"])
        act = _make_act(1, meta["title"] or fallback_title or "案例正文", res)
        warnings.append("未检测到幕次标题，已把整篇文档作为 1 幕导入")
        return meta, [act]

    if split_mode == "heading":
        warnings.append("未检测到「第一幕」式标题，已按 Heading 1/标题 1 切分")

    header_tokens = tokens[:act_positions[0]]
    meta = _parse_docx_header(header_tokens, fallback_title)

    acts = []
    for n, start in enumerate(act_positions):
        end = act_positions[n + 1] if n + 1 < len(act_positions) else len(tokens)
        title = _match_act(tokens[start].get("text")) or tokens[start].get("text", "")
        piece = _PieceParser()
        piece.feed_tokens(tokens[start + 1:end])
        res = piece.result()
        meta["objectives"].extend(res["objectives"])
        meta["keypoints"].extend(res["keypoints"])
        if res["final"] and not meta["final"]:
            meta["final"] = res["final"]
        acts.append(_make_act(n + 1, title, res))

    return meta, acts


def _parse_docx_header(tokens, fallback_title):
    meta = {"title": "", "course": "", "difficulty": "", "minutes": None,
            "objectives": [], "final": "", "keypoints": []}
    mode = None
    final_lines = []
    for tok in tokens:
        if tok.get("kind") != "text":
            continue
        for line in (tok.get("text") or "").splitlines():
            s = line.strip()
            if not s:
                continue
            sec, rest = _match_section(s)
            if sec:
                mode = sec
                if rest:
                    _collect_header_item(meta, mode, rest, final_lines)
                continue
            m = None
            for key, pattern in _META_PATTERNS:
                m = pattern.match(s)
                if m:
                    val = _clean_item(m.group(1))
                    if key == "minutes":
                        try:
                            meta["minutes"] = int(val)
                        except Exception:
                            pass
                    elif key == "title":
                        meta["title"] = _clean_title(val)
                    else:
                        meta[key] = val
                    break
            if m:
                continue
            if mode in ("objectives", "keypoints", "final"):
                _collect_header_item(meta, mode, s, final_lines)
                continue
            if not meta["title"] and not _match_act(s) and len(s) <= 80:
                meta["title"] = _clean_title(s)
    meta["final"] = " ".join(final_lines).strip()
    if not meta["title"]:
        meta["title"] = _clean_title(fallback_title or "")
    return meta


def _collect_header_item(meta, mode, text, final_lines):
    item = _clean_item(text)
    if not item:
        return
    if mode == "objectives":
        meta["objectives"].append(item)
    elif mode == "keypoints":
        meta["keypoints"].append(item)
    elif mode == "final":
        final_lines.append(item)


# ------------------------------------------------------------
# PPT 解析
# ------------------------------------------------------------

def _iter_shapes(shapes):
    for shape in shapes:
        yield shape
        if hasattr(shape, "shapes"):
            for sub in _iter_shapes(shape.shapes):
                yield sub


def _ppt_table_text(table):
    lines = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = " / ".join(x.strip() for x in (cell.text or "").splitlines()
                              if x.strip())
            cells.append(text)
        line = " | ".join(cells).strip()
        if line.strip(" |"):
            lines.append(line)
    return "\n".join(lines)


def _ppt_slide_tokens(slide, sink, warnings):
    tokens = []
    try:
        title_shape = slide.shapes.title
    except Exception:
        title_shape = None
    title = (title_shape.text or "").strip() if title_shape is not None else ""

    for shape in _iter_shapes(slide.shapes):
        if title_shape is not None and shape.shape_id == title_shape.shape_id:
            continue
        try:
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
                if text:
                    tokens.append({"kind": "text", "text": text})
                continue
            if getattr(shape, "has_table", False):
                text = _ppt_table_text(shape.table)
                if text:
                    tokens.append({"kind": "text", "text": text})
                continue
        except Exception:
            continue
        if MSO_SHAPE_TYPE is not None and shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            try:
                ext = "." + (shape.image.ext or "png")
                url = sink.save(shape.image.blob, ext, "幻灯片图片")
                if url:
                    tokens.append({"kind": "image", "url": url})
            except Exception:
                warnings.append("有 1 张幻灯片图片读取失败，已跳过")
    return title, tokens


def _notes_text(slide):
    try:
        if slide.has_notes_slide:
            return (slide.notes_slide.notes_text_frame.text or "").strip()
    except Exception:
        pass
    return ""


def _parse_notes(text):
    """只提取备注里明确标注的栏目，避免把答案带进学生界面。"""
    if not text.strip():
        return None
    tokens = [{"kind": "text", "text": line}
              for line in text.splitlines() if line.strip()]
    parser = _PieceParser(allow_body=False)
    parser.feed_tokens(tokens)
    res = parser.result()
    recognized = bool(
        res["prompts"] or res["objectives"] or res["keypoints"] or res["final"]
        or any(res["tutor"].values())
    )
    return res if recognized else None


def _parse_pptx_case(path, sink, warnings, fallback_title):
    if Presentation is None:
        raise RuntimeError("服务器缺少 python-pptx，无法解析 PPT")

    prs = Presentation(str(path))
    slides = []
    for idx, slide in enumerate(prs.slides, 1):
        title, tokens = _ppt_slide_tokens(slide, sink, warnings)
        slides.append({
            "index": idx,
            "title": title,
            "tokens": tokens,
            "notes": _notes_text(slide),
        })
    if not slides:
        raise ValueError("PPT 中没有可导入的幻灯片")

    meta = {"title": "", "course": "", "difficulty": "", "minutes": None,
            "objectives": [], "final": "", "keypoints": []}
    has_act_titles = any(_match_act(s["title"]) for s in slides)
    ignored_notes = 0
    header_tokens = []

    if has_act_titles:
        acts_data = []
        current = None
        pending_notes = None
        for s in slides:
            boundary = _match_act(s["title"])
            if boundary:
                current = {"title": boundary, "tokens": list(s["tokens"]),
                           "notes_piece": None}
                acts_data.append(current)
            elif current is None:
                if s["title"]:
                    header_tokens.append({"kind": "text", "text": s["title"]})
                header_tokens.extend(s["tokens"])
            else:
                if s["title"]:
                    current["tokens"].append({"kind": "text", "text": s["title"]})
                current["tokens"].extend(s["tokens"])

            notes_piece = _parse_notes(s["notes"])
            if notes_piece:
                if current is not None:
                    if current["notes_piece"] is None:
                        current["notes_piece"] = notes_piece
                    else:
                        _merge_results(current["notes_piece"], notes_piece)
                elif pending_notes is None:
                    pending_notes = notes_piece
                else:
                    _merge_results(pending_notes, notes_piece)
            elif s["notes"].strip():
                ignored_notes += 1

        meta = _parse_docx_header(header_tokens, fallback_title)
        acts = []
        for n, data in enumerate(acts_data):
            parser = _PieceParser()
            parser.feed_tokens(data["tokens"])
            res = parser.result()
            if data["notes_piece"]:
                _merge_results(res, data["notes_piece"])
            if n == 0 and pending_notes:
                _merge_results(res, pending_notes)
            meta["objectives"].extend(res["objectives"])
            meta["keypoints"].extend(res["keypoints"])
            if res["final"] and not meta["final"]:
                meta["final"] = res["final"]
            acts.append(_make_act(n + 1, data["title"], res))
    else:
        cover_title = ""
        acts = []
        for i, s in enumerate(slides):
            # 首页只有标题、没有正文时视为封面
            if i == 0 and not s["tokens"] and s["title"]:
                cover_title = s["title"]
                continue
            if not s["title"] and not s["tokens"]:
                continue
            parser = _PieceParser()
            parser.feed_tokens(s["tokens"])
            res = parser.result()
            notes_piece = _parse_notes(s["notes"])
            if notes_piece:
                _merge_results(res, notes_piece)
            elif s["notes"].strip():
                ignored_notes += 1
            meta["objectives"].extend(res["objectives"])
            meta["keypoints"].extend(res["keypoints"])
            if res["final"] and not meta["final"]:
                meta["final"] = res["final"]
            acts.append(_make_act(len(acts) + 1,
                                  s["title"] or "第{}幕".format(len(acts) + 1), res))
        if cover_title:
            meta["title"] = _clean_title(cover_title)
        if not meta["title"]:
            meta["title"] = _clean_title(fallback_title or "")
        if not acts:
            raise ValueError("PPT 中没有可导入的正文或图片")
        warnings.append("未检测到幕次标题，已按幻灯片逐页拆分为 {} 幕".format(len(acts)))

    if ignored_notes:
        warnings.append("检测到 {} 页演讲者备注未带栏目标注，已忽略（避免答案泄露）".format(ignored_notes))

    return meta, acts


# ------------------------------------------------------------
# LibreOffice 旧格式转换
# ------------------------------------------------------------

def _convert_legacy(src_path, target_ext, workdir):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("服务器未安装 LibreOffice，无法解析 .doc/.ppt，请先另存为 .docx/.pptx")

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    outdir = workdir / "converted"
    outdir.mkdir(parents=True, exist_ok=True)
    profile = workdir / ("lo_profile_" + uuid.uuid4().hex)

    with _LO_LOCK:
        cmd = [
            soffice, "--headless", "--norestore", "--invisible",
            "-env:UserInstallation=file://{}".format(profile),
            "--convert-to", target_ext,
            "--outdir", str(outdir),
            str(src_path),
        ]
        try:
            subprocess.run(cmd, timeout=120, capture_output=True,
                           env={**os.environ, "HOME": str(workdir)})
        except subprocess.TimeoutExpired:
            raise RuntimeError("旧格式转换超时（120 秒），请另存为 .docx/.pptx 后重试")

    candidates = sorted(outdir.glob("*.{}".format(target_ext)),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("旧格式转换失败，请用 Office/WPS 另存为 .docx/.pptx 后重试")
    return candidates[0]


# ------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------

def _dedupe(items):
    out, seen = [], set()
    for item in items or []:
        s = (item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _assemble(case_id, meta, acts):
    return {
        "case_id": case_id,
        "title": _clean_title(meta.get("title") or "") or "导入案例",
        "course": (meta.get("course") or "").strip() or DEFAULT_COURSE,
        "difficulty": (meta.get("difficulty") or "").strip() or DEFAULT_DIFFICULTY,
        "estimated_minutes": meta.get("minutes") or DEFAULT_MINUTES,
        "student_count_hint": "4-6人小组",
        "learning_objectives": _dedupe(meta.get("objectives")),
        "acts": acts,
        "final_diagnosis": (meta.get("final") or "").strip(),
        "key_learning_points": _dedupe(meta.get("keypoints")),
        "source": "import",
    }


def _empty_meta(title):
    return {"title": title, "course": "", "difficulty": "", "minutes": None,
            "objectives": [], "final": "", "keypoints": []}


def parse_import_files(files, case_id, media_root):
    """
    解析上传文件并生成案例草稿。

    files: [{"filename": 原始文件名, "path": 已保存的临时路径}]
    返回: (case_dict, warnings)
    """
    warnings = []
    sink = _MediaSink(media_root, case_id)
    docs, images = [], []

    for f in files:
        name = f.get("filename") or "未命名"
        ext = Path(name).suffix.lower()
        if ext in DOC_EXTS:
            docs.append({**f, "ext": ext})
        elif ext in IMAGE_EXTS:
            images.append({**f, "ext": ext})
        else:
            warnings.append("跳过不支持的文件：{}".format(name))

    if len(docs) > 1:
        warnings.append("一次只导入一个 Word/PPT，已使用「{}」，其余忽略".format(docs[0]["filename"]))

    case = None
    if docs:
        doc = docs[0]
        src = Path(doc["path"])
        fallback_title = Path(doc["filename"]).stem
        workdir = Path(tempfile.mkdtemp(prefix="pbl_import_"))
        try:
            target = None
            if doc["ext"] == ".doc":
                target = "docx"
            elif doc["ext"] == ".ppt":
                target = "pptx"
            if target:
                src = _convert_legacy(src, target, workdir)
                warnings.append("{} 已转换为 .{} 后解析".format(doc["filename"], target))
            if src.suffix.lower() == ".docx":
                meta, acts = _parse_docx_case(src, sink, warnings, fallback_title)
            elif src.suffix.lower() == ".pptx":
                meta, acts = _parse_pptx_case(src, sink, warnings, fallback_title)
            else:
                raise ValueError("无法识别的文档格式：{}".format(src.suffix))
            case = _assemble(case_id, meta, acts)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # 单独上传的图片：生成「图像资料」幕
    if images:
        urls = []
        for img in images:
            try:
                blob = Path(img["path"]).read_bytes()
            except Exception:
                warnings.append("图片读取失败：{}".format(img["filename"]))
                continue
            url = sink.save(blob, img["ext"], img["filename"])
            if url:
                urls.append(url)
        if urls:
            n = (len(case["acts"]) + 1) if case else 1
            piece = {
                "body": "",
                "prompts": [],
                "objectives": [],
                "tutor": {"expected_hypotheses": [], "expected_issues": [], "notes": []},
                "final": "",
                "keypoints": [],
                "images": urls,
                "ignored": 0,
            }
            act = _make_act(n, "图像资料", piece)
            if case:
                case["acts"].append(act)
            else:
                title = Path(images[0]["filename"]).stem
                case = _assemble(case_id, _empty_meta(title), [act])

    if not case:
        raise ValueError("没有可导入的内容（支持 .docx/.doc/.pptx/.ppt 和图片）")

    warnings.extend(sink.warnings)
    return case, warnings