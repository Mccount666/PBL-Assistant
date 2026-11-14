# -*- coding: utf-8 -*-
"""
PBL 课堂讨论助手 —— 讨论催化剂引擎
====================================
讨论催化剂 = PBL tutor（导师）的 AI 化身。

PBL 教学中导师不讲课、不喂答案，只做四件事：
  1. 追问依据   —— “你们这么推测，依据是案例里哪句话？”
  2. 激发怀疑   —— 唱反调：“有没有另一种解释也能说明这些表现？”
  3. 提示维度   —— 提醒遗漏的讨论角度（时间线/危重排除/心理社会因素）
  4. 显式化过程 —— 把口头假设变成假设板记录、把疑问变成学习议题

双轨实现：
  - API 轨：大模型扮演 tutor（提示词内含助教参考，严格禁说答案）
  - 离线轨：规则 + 模板库 + BM25（默认，无 Key 可跑）
"""

from core.text_utils import tokenize, bm25_scores
from core.llm_client import chat, is_api_mode
from core.board import STATUS_LABELS

# ------------------------------------------------------------
# 诊断词表（识别学生发言中出现的疾病推测）
# ------------------------------------------------------------
DX_ALIASES = {
    "阑尾炎": ["阑尾炎", "阑尾", "转移性右下腹痛"],
    "胃肠炎": ["胃肠炎", "肠胃炎", "急性胃肠炎", "拉肚子", "腹泻"],
    "溃疡": ["溃疡", "胃溃疡", "十二指肠溃疡", "穿孔"],
    "结石": ["输尿管结石", "尿路结石", "肾结石", "结石"],
    "胆囊炎": ["胆囊炎", "胆囊结石", "胆绞痛", "胆囊"],
    "胰腺炎": ["胰腺炎"],
    "肺炎": ["肺炎", "大叶性肺炎", "肺部感染", "社区获得性肺炎"],
    "肺结核": ["肺结核", "结核"],
    "肺脓肿": ["肺脓肿"],
    "上呼吸道感染": ["上呼吸道感染", "上感", "感冒", "流感"],
    "胸膜炎": ["胸膜炎", "胸膜"],
    "气胸": ["气胸"],
    "心梗": ["心梗", "心肌梗死", "心肌梗塞"],
    "糖尿病": ["糖尿病"],
    "甲亢": ["甲亢", "甲状腺功能亢进"],
}

# 疑问/议题触发词
QUESTION_WORDS = ["为什么", "怎么会", "怎么", "机制", "是什么", "什么意思",
                  "什么原因", "如何", "原理", "道理"]
# 依据陈述词
EVIDENCE_WORDS = ["因为", "依据", "支持", "提示", "所以", "说明", "考虑",
                  "伴有", "合并", "表现为", "出现", "存在", "阳性", "升高"]
# 直接要答案
ANSWER_SEEKING = ["答案", "到底是什么病", "告诉", "直接说", "揭晓",
                  "确诊", "诊断是什么", "是什么病"]

# PBL 讨论维度（轮换提示，保证“不断的展开”）
DIMENSIONS = [
    "除了疾病本身，这个案例里有没有心理或社会因素值得讨论？（家庭、学业压力、就医行为）",
    "我们从时间线上再过一遍：每个症状出现的先后顺序，本身是不是线索？",
    "如果这是急诊场景，哪些表现提示“危险”，需要优先处理？",
    "患者的年龄、性别、职业这些基本信息，对你们的假设有什么影响？",
    "有没有哪些假设，是目前信息根本无法区分的？区分它们各自需要什么证据？",
    "如果你们的假设是对的，接下来应该出现什么表现？怎么验证？",
    "反过来想：如果它不是这个病，案例里哪条信息最有力地反驳？",
    "这些症状之间有没有因果关系？还是彼此独立？",
    "把案例里的信息分成“支持”和“反对”两栏，逐条对照你们的假设。",
    "有没有我们还没注意到、但教材会强调的关键细节？再读一遍案例。",
]


class DiscussionCoach:
    """讨论催化剂：引导 PBL 小组讨论走向深入。"""

    def __init__(self, case, config=None, kb=None):
        self.case = case
        self.config = config
        self.kb = kb or []
        self.use_api = is_api_mode(config)

        self.turns = 0                       # 学生发言计数
        self._dimension_ptr = 0              # 维度提示轮换指针
        self._evidence_count = 0             # 依据性发言计数（控制唱反调节奏）
        self._last_dx = None                 # 最近提到的诊断
        self._reviewed_acts = set()          # 已回顾的幕次

        # 助教参考的检索索引（用于判断假设是否触及期望方向）
        self._tutor_docs = []
        self._tutor_meta = []
        for act in case["acts"]:
            notes = act.get("tutor_notes", {})
            for h in notes.get("expected_hypotheses", []):
                self._tutor_docs.append(tokenize(h + " " + act["title"]))
                self._tutor_meta.append(("hypothesis", act["act"], h))

        self._kb_docs = [tokenize(k["content"] + " " + " ".join(k["tags"]))
                         for k in self.kb]

    # ---------------- 工具 ----------------

    def _detect_dx(self, text):
        t = text.lower()
        found = []
        for dx, aliases in DX_ALIASES.items():
            if any(a in t for a in aliases):
                found.append(dx)
        return found

    def _retrieve_kb(self, query):
        q = tokenize(query)
        if not q or not self.kb:
            return None
        scores = bm25_scores(q, self._kb_docs)
        idx = scores.index(max(scores))
        if scores[idx] > 1.2:
            return self.kb[idx]
        return None

    def _match_expected(self, text):
        """学生发言是否触及助教参考的期望假设（BM25）。"""
        q = tokenize(text)
        if not q or not self._tutor_docs:
            return None
        scores = bm25_scores(q, self._tutor_docs)
        idx = scores.index(max(scores))
        if scores[idx] > 2.0:
            kind, act_no, text_h = self._tutor_meta[idx]
            return (kind, act_no, text_h)
        return None

    # ---------------- 系统提示词（API 轨） ----------------

    def _build_system_prompt(self, current_act_no, board_summary, issues_summary):
        acts_brief = "\n".join(
            "第{}幕《{}》助教期望假设：{}；期望学习议题：{}".format(
                a["act"], a["title"],
                "、".join(a.get("tutor_notes", {}).get("expected_hypotheses", [])),
                "、".join(a.get("tutor_notes", {}).get("expected_issues", [])))
            for a in self.case["acts"]
        )
        return (
            "你是 PBL（问题导向学习）小组讨论的导师（tutor）。"
            "学生小组正在讨论病例，目前已发放到第 {} 幕。\n\n"
            "【本案例助教参考（你心里有数即可，严禁直接告诉学生）】\n{}\n\n"
            "【当前假设板】{}\n【当前学习议题】{}\n\n"
            "【导师行为准则 —— 必须严格遵守】\n"
            "1. 你绝不讲解医学知识、绝不给答案、绝不评价对错，只提问和引导。\n"
            "2. 学生提出观点/假设时，追问依据：'你们的依据是案例里哪条信息？'\n"
            "3. 适时唱反调激发怀疑：'有没有另一种疾病也能解释这些表现？'\n"
            "4. 学生讨论陷入停顿时，从这些维度选一个提示：时间线、危险信号、"
            "心理社会因素、假设间如何区分、还需什么证据。\n"
            "5. 发现学生提到某个疾病推测但假设板上没有，提示他们记录下来。\n"
            "6. 发现学生提出'为什么/机制'类问题，提示列入学习议题。\n"
            "7. 每次回复不超过3句话，语气是引导式的讨论主持人，不是讲师。\n"
            "8. 学生直接问答案时，把问题抛回小组。"
        ).format(current_act_no, acts_brief, board_summary or "（空）", issues_summary or "（空）")

    # ---------------- 对外主入口 ----------------

    def on_act_released(self, act):
        """新一幕发放时的开场引导。"""
        prompts = act.get("discussion_prompts", [])
        if not prompts:
            return "新的一幕发放了。先各自读一遍，然后说说：你们注意到了什么？"
        head = "**【{}】** 发放了。先给大家一分钟读一遍。\n\n讨论可以从这里开始：\n".format(act["title"])
        body = "\n".join("- {}".format(p) for p in prompts[:3])
        return head + body

    def on_student_message(self, text, board=None, issues=None):
        """
        学生一条发言 → 返回 (催化回复, actions)
        actions: [{"type": "add_hypothesis"|"add_issue", "text": ...}] UI 提示
        """
        text = (text or "").strip()
        if not text:
            return "继续说，我在听。", []

        self.turns += 1
        actions = []

        # 兼容两种输入：字符串列表 / 字典列表（含 status_cn）
        def _txt(x):
            return x if isinstance(x, str) else (x.get("text") or "")

        def _st(x):
            if isinstance(x, str):
                return ""
            return x.get("status_cn") or STATUS_LABELS.get(x.get("status", ""), x.get("status", ""))

        board_texts = [_txt(h) for h in (board or [])]

        # ---- API 轨 ----
        if self.use_api:
            board_summary = "；".join(
                "{}（{}）".format(_txt(h), _st(h)) for h in (board or [])) or "（空）"
            issues_summary = "；".join(_txt(i) for i in (issues or [])) or "（空）"
            reply, mode = chat(
                self._build_system_prompt(
                    self._current_act_no(board), board_summary, issues_summary),
                "学生发言：" + text,
                self.config,
            )
            if reply:
                # API 模式下仍给出显式化建议（规则层抽取）
                for dx in self._detect_dx(text):
                    if not any(dx in bt for bt in board_texts):
                        actions.append({"type": "add_hypothesis", "text": dx})
                return reply, actions

        # ---- 离线轨：规则催化 ----

        # 1) 直接要答案 → 抛回小组
        if any(w in text for w in ANSWER_SEEKING):
            return ("这个问题现在没有标准答案——这正是要讨论的原因。"
                    "先用案例里已有的信息，说出你们倾向哪个方向、为什么？"), []

        # 2) 提到疾病推测
        dxs = self._detect_dx(text)
        new_dx = [d for d in dxs if not any(d in bt for bt in board_texts)]
        for d in new_dx:
            actions.append({"type": "add_hypothesis", "text": d})

        # 3) 疑问句 → 学习议题
        if any(w in text for w in QUESTION_WORDS):
            issue_text = self._extract_issue(text)
            if issue_text:
                actions.append({"type": "add_issue", "text": issue_text})
                return ("你们刚才问到了“{}”——这正是一个学习议题。"
                        "先把它列进议题清单，课后查清楚再回来验证假设。").format(issue_text), actions

        # 4) 在给依据 → 追问 + 唱反调（每3次依据性发言唱一次反调）
        if any(w in text for w in EVIDENCE_WORDS) and dxs:
            self._last_dx = dxs[0]
            self._evidence_count += 1
            if self._evidence_count % 3 == 0:
                return ("记下了。那我们当一回“反对者”：有没有另一种疾病，"
                        "也能解释目前这些表现？它和「{}」怎么区分？").format(dxs[0]), actions
            return ("你们提到「{}」。支持这个推测的，是案例里的哪几条信息？"
                    "有没有哪条信息其实不支持它？").format(dxs[0]), actions

        # 5) 新推测但没给依据
        if new_dx:
            return ("要把「{}」记录到假设板上吗？记下来，才能追踪它"
                    "在新信息面前是变强还是变弱。").format(new_dx[0]), actions

        # 6) 触及助教期望假设但不自知 → 肯定方向 + 推进（不说答案）
        hit = self._match_expected(text)
        if hit and self.turns % 2 == 0:
            return ("这个方向有道理。别停在直觉上——把支持它的案例信息逐条列出来，"
                    "再看看有没有反证。"), actions

        # 7) 兜底：轮换维度提示，保持“不断的展开”
        hint = DIMENSIONS[self._dimension_ptr % len(DIMENSIONS)]
        self._dimension_ptr += 1
        return hint, actions

    # ---------------- 新幕回顾（假设再审视） ----------------

    def review_old_hypotheses(self, act_no, board):
        """新一幕发放时，提示小组重新审视旧假设。"""
        old = [h for h in (board or []) if h.get("status") in ("proposed", "supported")]
        if not old:
            return None
        top = old[:2]
        texts = "、".join("「{}」".format(h["text"]) for h in top)
        return ("新信息到了，别急着下结论——先看看老假设：{}。"
                "结合这一幕的发现，它们各自是更可能了，还是更不可能了？").format(texts)

    # ---------------- 辅助 ----------------

    def _current_act_no(self, board):
        return getattr(self, "_act_no_hint", 1)

    def _extract_issue(self, text):
        """从发言中提取议题（取疑问短语，限长）。"""
        for w in QUESTION_WORDS:
            idx = text.find(w)
            if idx >= 0:
                seg = text[max(0, idx - 6): idx + 24].strip("，。？！、 ")
                if len(seg) >= 4:
                    return seg[:40]
        return None
