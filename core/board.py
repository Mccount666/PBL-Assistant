# -*- coding: utf-8 -*-
"""
PBL 课堂讨论助手 —— 假设追踪板 & 学习议题清单
==============================================
PBL 的两大标志性认知工具：

  假设追踪板（Hypothesis Board）
    把小组口头提出的疾病推测显式记录下来，随着逐幕发放的新信息，
    持续更新每个假设的状态，让“怀疑→验证→修正”的过程可见。
    状态：proposed（提出）/ supported（支持增强）/ refuted（推翻）/ pending（待定）

  学习议题清单（Learning Issues）
    讨论中冒出的“为什么/机制/是什么”类问题，课后自学后回来验证假设。
    状态：open（待查）/ resolved（已解决）

两个对象都是纯内存数据结构，由 app.py 在 st.session_state 中持有。
"""

import time


# ============================================================
# 假设追踪板
# ============================================================

STATUS_LABELS = {
    "proposed": "已提出",
    "supported": "支持增强",
    "refuted": "已推翻",
    "pending": "待定",
}

STATUS_ICONS = {
    "proposed": "🔵",
    "supported": "🟢",
    "refuted": "🔴",
    "pending": "⚪",
}


class HypothesisBoard:
    """假设追踪板：记录并追踪小组提出的疾病推测。"""

    def __init__(self):
        self.items = []          # [{id, text, status, evidence[], created_act, history[]}]
        self._seq = 0

    def add(self, text, act_no=1):
        text = (text or "").strip()
        if not text:
            return None
        # 去重（同文本已存在则不再新增）
        for it in self.items:
            if it["text"] == text:
                return it
        self._seq += 1
        item = {
            "id": "H{}".format(self._seq),
            "text": text,
            "status": "proposed",
            "evidence": [],
            "created_act": act_no,
            "created_at": time.time(),
            "history": [{"act": act_no, "status": "proposed", "note": "提出假设"}],
        }
        self.items.append(item)
        return item

    def set_status(self, hyp_id, status, act_no=1, note=""):
        if status not in STATUS_LABELS:
            return False
        for it in self.items:
            if it["id"] == hyp_id:
                it["status"] = status
                it["history"].append({"act": act_no, "status": status, "note": note})
                return True
        return False

    def add_evidence(self, hyp_id, note, act_no=1):
        for it in self.items:
            if it["id"] == hyp_id:
                it["evidence"].append({"act": act_no, "note": note})
                return True
        return False

    def remove(self, hyp_id):
        self.items = [it for it in self.items if it["id"] != hyp_id]

    def get(self, hyp_id):
        for it in self.items:
            if it["id"] == hyp_id:
                return it
        return None

    def summary(self):
        """供催化剂系统提示词使用的摘要。"""
        if not self.items:
            return "（空）"
        return "；".join("{}（{}）".format(it["text"], STATUS_LABELS[it["status"]])
                         for it in self.items)

    def timeline(self):
        """按时间展开的假设演变记录（供纪要导出）。"""
        events = []
        for it in self.items:
            for h in it["history"]:
                events.append({
                    "act": h["act"],
                    "hyp": it["text"],
                    "status": h["status"],
                    "status_cn": STATUS_LABELS[h["status"]],
                    "note": h.get("note", ""),
                })
        events.sort(key=lambda e: (e["act"], e["hyp"]))
        return events


# ============================================================
# 学习议题清单
# ============================================================

class IssueTracker:
    """学习议题清单：讨论中冒出的待查问题。"""

    def __init__(self):
        self.items = []          # [{id, text, status, source_act, notes[], created_at}]
        self._seq = 0

    def add(self, text, act_no=1):
        text = (text or "").strip()
        if not text:
            return None
        for it in self.items:
            if it["text"] == text:
                return it
        self._seq += 1
        item = {
            "id": "I{}".format(self._seq),
            "text": text,
            "status": "open",
            "source_act": act_no,
            "notes": [],
            "created_at": time.time(),
        }
        self.items.append(item)
        return item

    def set_status(self, issue_id, status):
        if status not in ("open", "resolved"):
            return False
        for it in self.items:
            if it["id"] == issue_id:
                it["status"] = status
                return True
        return False

    def add_note(self, issue_id, note):
        for it in self.items:
            if it["id"] == issue_id:
                it["notes"].append(note)
                return True
        return False

    def remove(self, issue_id):
        self.items = [it for it in self.items if it["id"] != issue_id]

    def get(self, issue_id):
        for it in self.items:
            if it["id"] == issue_id:
                return it
        return None

    def summary(self):
        if not self.items:
            return "（空）"
        return "；".join(it["text"] for it in self.items)

    def open_count(self):
        return sum(1 for it in self.items if it["status"] == "open")
