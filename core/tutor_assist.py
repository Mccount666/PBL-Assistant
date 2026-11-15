# -*- coding: utf-8 -*-
"""
PBL 助手型导师 —— 主题脚手架 + 文献推荐
==========================================
学生提到某个临床主题（症状/体征/疾病）并提问时，给出：
  1. 该主题应展开的关键推理点（结合案例的思考框架）；
  2. 推荐可查的文献（调用已接入的文献库，真实检索）。

离线（无 API Key）也能给出结构化关键点；文献检索本身走公开学术 API，
优先用预置英文检索式，失败再退回 LLM 翻译。
"""

from core.discussion_coach import DX_ALIASES
from core.literature import search_literature, search_for_topic


# 症状/体征别名（疾病别名复用 discussion_coach.DX_ALIASES）
SYMPTOM_ALIASES = {
    "腹痛": ["腹痛", "肚子疼", "腹部疼痛", "肚子痛", "转移性右下腹痛"],
    "发热": ["发热", "发烧", "体温升高", "高热"],
    "恶心呕吐": ["恶心", "呕吐", "想吐"],
    "腹泻": ["腹泻", "拉肚子", "稀便"],
    "咳嗽咳痰": ["咳嗽", "咳痰", "铁锈色痰"],
    "胸痛": ["胸痛", "胸口疼"],
    "呼吸困难": ["呼吸困难", "气促", "喘不上气"],
    "头痛": ["头痛", "头疼"],
    "黄疸": ["黄疸", "皮肤发黄"],
    "血尿": ["血尿", "尿血"],
}

# 主题 → 关键推理点 + 英文检索式
SCAFFOLDS = {
    "腹痛": {
        "en": "acute abdominal pain differential diagnosis",
        "points": [
            "部位与转移：起病在哪、是否转移、最终固定在哪（脐周→右下腹高度提示阑尾）",
            "性质与程度：隐痛/绞痛/胀痛/刀割样，持续还是阵发",
            "时间与诱因：起病急缓，与进食、油腻、饮酒的关系",
            "伴随症状：发热、恶心呕吐、腹泻、是否停止排气排便",
            "加重缓解因素：咳嗽或颠簸加重提示腹膜刺激征",
            "鉴别方向：阑尾炎、胃肠炎、溃疡穿孔、尿路结石、胰腺炎",
        ],
    },
    "阑尾炎": {
        "en": "acute appendicitis diagnosis clinical",
        "points": [
            "典型病程：转移性右下腹痛（内脏痛→躯体痛）",
            "关键体征：麦氏点固定压痛、反跳痛、肌紧张",
            "辅助试验：结肠充气试验、腰大肌试验、闭孔内肌试验",
            "实验室：WBC 与中性粒细胞、CRP 升高的意义与局限",
            "影像选择：超声/CT 的适用场景与穿孔风险评估",
            "鉴别：胃肠炎、溃疡穿孔、尿路结石、妇科急症",
        ],
    },
    "肺炎": {
        "en": "community acquired pneumonia diagnosis",
        "points": [
            "典型表现：急起发热、寒战、咳嗽咳痰（铁锈色痰）、胸膜性胸痛",
            "肺实变体征：语颤增强、叩诊浊音、支气管呼吸音、细湿啰音",
            "影像：叶/段性分布致密影",
            "病原学：肺炎链球菌最常见，经验性抗感染思路",
            "严重度评估：CURB-65 等，决定门诊还是住院",
            "鉴别：肺结核、肺脓肿、肺癌、肺栓塞",
        ],
    },
    "发热": {
        "en": "fever of unknown origin differential diagnosis",
        "points": [
            "热型与病程：稽留热/弛张热/间歇热，急性还是慢性",
            "伴随症状：定位感染灶（呼吸/消化/泌尿/神经）",
            "感染 vs 非感染：肿瘤、自身免疫、药物热",
            "关键查体与初步检查：血常规、CRP、影像",
            "危险信号：意识改变、瘀斑、低血压",
        ],
    },
    "恶心呕吐": {
        "en": "nausea vomiting differential diagnosis",
        "points": [
            "与腹痛的关系：呕吐后腹痛是否缓解（缓解多偏胃肠炎，不缓解警惕外科急腹症）",
            "呕吐物性状：胃内容物/胆汁/粪样/咖啡渣",
            "伴随症状：腹泻、发热、头痛、停止排气排便",
            "颅内压增高 vs 反射性 vs 梗阻性",
        ],
    },
    "咳嗽咳痰": {
        "en": "cough sputum differential diagnosis",
        "points": [
            "痰的性状：铁锈色（肺炎链球菌）、脓臭（厌氧/脓肿）、粉红泡沫（肺水肿）",
            "病程与热型：急性感染 vs 慢性",
            "伴随：胸痛、咯血、呼吸困难",
            "查体与影像：实变体征、胸片/CT",
        ],
    },
    "胸痛": {
        "en": "chest pain differential diagnosis emergency",
        "points": [
            "先排危重：心梗、肺栓塞、主动脉夹层、气胸",
            "性质与放射：压榨样放射至左臂（心源性）、撕裂样（夹层）、胸膜性（呼吸相关）",
            "伴随：呼吸困难、大汗、晕厥",
            "关键检查：心电图、肌钙蛋白、D-二聚体、影像",
        ],
    },
    "呼吸困难": {
        "en": "dyspnea differential diagnosis",
        "points": [
            "起病急缓与诱因：突发（气胸/栓塞）vs 渐进",
            "心源性 vs 肺源性：端坐呼吸、湿啰音 vs 哮鸣音",
            "伴随：胸痛、发热、咯血、下肢水肿",
            "严重度：血氧、呼吸频率、辅助呼吸肌参与",
        ],
    },
}

# 通用症状推理框架（未预置主题时兜底）
GENERIC_POINTS = [
    "部位与范围：具体在哪、是否放射、是否转移",
    "性质与程度：什么样的感觉、有多重、持续还是阵发",
    "时间与演变：何时起病、如何变化、有无诱因",
    "伴随症状：把相关表现串起来看",
    "加重与缓解因素：什么情况下更重或更轻",
    "鉴别方向：列出几个能解释这些表现的可能，再逐一找支持/反对证据",
]

QUERY_HINTS = ["为什么", "怎么", "是什么", "什么", "哪些", "如何", "讲讲", "说说",
               "介绍", "相关", "机制", "原因", "诊断", "鉴别", "考虑", "？", "?",
               "展开", "思路", "要点", "了解", "想问", "请教", "看看", "查"]


def detect_topic(text):
    """从学生发言中识别临床主题（疾病优先，其次症状）。"""
    t = text or ""
    for dx, aliases in DX_ALIASES.items():
        if any(a in t for a in aliases):
            return dx
    for topic, aliases in SYMPTOM_ALIASES.items():
        if any(a in t for a in aliases):
            return topic
    return None


def is_topic_query(text):
    """是否在就某主题提问/请求展开（而非单纯陈述）。"""
    return any(w in (text or "") for w in QUERY_HINTS)


def scaffold_for(topic):
    sc = SCAFFOLDS.get(topic)
    if sc:
        return {"topic": topic, "points": sc["points"], "en": sc["en"]}
    return {"topic": topic, "points": GENERIC_POINTS, "en": ""}


def scaffold_text(topic):
    """离线/兜底用的关键点文本。"""
    sc = scaffold_for(topic)
    pts = "\n".join("- " + p for p in sc["points"][:6])
    return ("围绕「{}」，可以沿这几个点展开：\n{}\n\n"
            "先结合案例逐条对照、列出支持与反对证据；也可以查相关文献佐证。").format(topic, pts)


def literature_for(topic, cfg, limit=3):
    """优先用预置英文检索式（离线可用），失败退回 LLM 翻译检索。"""
    sc = scaffold_for(topic)
    en = sc.get("en")
    if en:
        items = search_literature(en, limit)
        if items:
            return {"query": en, "items": items}
    try:
        return search_for_topic(topic, cfg, limit)
    except Exception:
        return {"query": topic, "items": []}