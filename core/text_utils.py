# -*- coding: utf-8 -*-
"""
床边教练 → PBL 课堂讨论助手 —— 文本工具
=======================================
中文分词（字符二元组）+ 极简 BM25，供讨论催化剂与知识库检索使用。
（从原 sp_engine.py 迁移，保持离线确定性行为。）
"""

import math
import re

# 中英文标点 + 空白
_PUNCT_RE = re.compile(r"[\s，。？！、；：,\.\?\!;:（）()\"'“”‘’\-—_/]+")


def tokenize(text):
    """中文字符二元组 + 英文数字词。"""
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    latin = re.findall(r"[a-z0-9]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    bigrams = []
    for seg in cjk:
        if len(seg) == 1:
            bigrams.append(seg)
        else:
            bigrams.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return latin + bigrams


def bm25_scores(query_tokens, doc_tokens_list, k1=1.5, b=0.75):
    """极简 BM25（无外部依赖差异，离线确定性行为）"""
    n = len(doc_tokens_list)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in doc_tokens_list) / n

    df = {}
    for doc in doc_tokens_list:
        for tok in set(doc):
            df[tok] = df.get(tok, 0) + 1

    idf = {}
    for tok, cnt in df.items():
        idf[tok] = math.log(1 + (n - cnt + 0.5) / (cnt + 0.5))

    scores = []
    q_set = set(query_tokens)
    for doc in doc_tokens_list:
        tf = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        dl = len(doc)
        s = 0.0
        for tok in q_set:
            if tok in tf:
                f = tf[tok]
                s += idf.get(tok, 0.0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (avgdl + 1e-9)))
        scores.append(s)
    return scores
