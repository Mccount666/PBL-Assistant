# -*- coding: utf-8 -*-
"""
PBL 讨论助手 —— 文献检索模块
==============================
多源聚合 + 自动降级（国内网络对学术站点时有波动，单源不可靠）：

  1) Semantic Scholar（api.semanticscholar.org）—— 相关度最好、响应快，免费无 Key
  2) Europe PMC（ebi.ac.uk）—— 医学文献全，聚合 PubMed / PMC 全文
  3) Crossref（api.crossref.org）—— 元数据兜底

功能：
  - 中文医学概念 → LLM 转英文检索式（可选）→ 检索 → 统一格式
  - 内存缓存 1 小时
"""

import json
import re
import time
import urllib.parse
import urllib.request

SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/search"
EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF_WORKS = "https://api.crossref.org/works"

_cache = {}
_CACHE_TTL = 3600


# ------------------------------------------------------------
# 基础请求（固定 host + 参数编码 + 重试）
# ------------------------------------------------------------

def _fetch_json(url, timeout=12, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PBL-Assistant/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.8)
    return None


# ------------------------------------------------------------
# 检索式翻译
# ------------------------------------------------------------

def _clean_query(text):
    """从模型输出中提取干净的英文检索式（过滤中文思考/解释）。"""
    line = (text or "").strip().split("\n")[0]
    m = re.search(r"[A-Za-z][A-Za-z0-9 ,\"()\-]{3,}", line)
    if not m:
        return ""
    q = re.sub(r"\s+", " ", m.group(0)).strip(" ,")
    return q[:120] if len(q) >= 3 else ""


def _to_english_query(topic, cfg):
    """用 LLM 把中文医学概念转成英文检索式；失败返回原文。"""
    if cfg.get("mode") != "api" or not cfg.get("api_key"):
        return topic
    try:
        from core.llm_client import chat
        ecfg = dict(cfg)
        # 用非推理轻量模型（推理模型的思考过程会污染输出）
        ecfg["model"] = "step-1o-turbo-vision"
        ecfg["max_tokens"] = 80
        ecfg["temperature"] = 0
        reply, _ = chat(
            "You are a medical literature search assistant. Output only an English search query.",
            "Translate this Chinese medical concept into an English literature search query "
            "(2-4 keywords, MeSH terms preferred). Output only the query, nothing else:\n" + topic,
            ecfg,
        )
        q = _clean_query(reply) if reply else ""
        if q:
            return q
    except Exception:
        pass
    return topic


# ------------------------------------------------------------
# 各数据源
# ------------------------------------------------------------

def _search_semantic_scholar(query, limit):
    params = {
        "query": query,
        "limit": str(max(1, min(int(limit), 10))),
        "fields": "title,year,venue,authors,abstract,citationCount,externalIds,url",
    }
    url = SEMANTIC_SCHOLAR + "?" + urllib.parse.urlencode(params)
    data = _fetch_json(url)
    if not data:
        return []
    items = []
    for p in (data.get("data") or []):
        authors = [a.get("name", "") for a in (p.get("authors") or [])][:3]
        url_item = p.get("url") or ""
        ext = p.get("externalIds") or {}
        if not url_item and ext.get("DOI"):
            url_item = "https://doi.org/" + ext["DOI"]
        items.append({
            "title": (p.get("title") or "").strip(),
            "authors": ", ".join(authors),
            "journal": p.get("venue") or "",
            "year": str(p.get("year") or ""),
            "abstract": (p.get("abstract") or "").strip()[:320],
            "url": url_item,
            "cited": p.get("citationCount") or 0,
            "source": "Semantic Scholar",
        })
    return items


def _search_europe_pmc(query, limit):
    params = {
        "query": query,
        "format": "json",
        "pageSize": str(max(1, min(int(limit), 10))),
        "resultType": "core",
    }
    url = EUROPE_PMC_SEARCH + "?" + urllib.parse.urlencode(params)
    data = _fetch_json(url)
    if not data:
        return []
    items = []
    for it in ((data.get("resultList") or {}).get("result") or []):
        pmid = it.get("pmid") or ""
        doi = it.get("doi") or ""
        url_item = ""
        if pmid:
            url_item = "https://europepmc.org/article/MED/" + pmid
        elif doi:
            url_item = "https://doi.org/" + doi
        journal = ((it.get("journalInfo") or {}).get("journal") or {}).get("title") \
            or it.get("journalTitle") or ""
        items.append({
            "title": (it.get("title") or "").strip(),
            "authors": (it.get("authorString") or "")[:100],
            "journal": journal,
            "year": str(it.get("pubYear") or ""),
            "abstract": (it.get("abstractText") or "").strip()[:320],
            "url": url_item,
            "cited": it.get("citedByCount") or 0,
            "source": "Europe PMC",
        })
    return items


def _search_crossref(query, limit):
    params = {
        "query": query,
        "rows": str(max(1, min(int(limit), 10))),
        "select": "title,container-title,issued,DOI,author,is-referenced-by-count",
    }
    url = CROSSREF_WORKS + "?" + urllib.parse.urlencode(params)
    data = _fetch_json(url)
    if not data:
        return []
    items = []
    for it in ((data.get("message") or {}).get("items") or []):
        title = (it.get("title") or [""])[0]
        if not title:
            continue
        year = ""
        issued = it.get("issued") or {}
        if issued.get("date-parts"):
            year = str(issued["date-parts"][0][0] or "")
        doi = it.get("DOI") or ""
        authors = [a.get("family", "") for a in (it.get("author") or [])][:3]
        items.append({
            "title": title.strip(),
            "authors": ", ".join([a for a in authors if a]),
            "journal": (it.get("container-title") or [""])[0],
            "year": year,
            "abstract": "",
            "url": ("https://doi.org/" + doi) if doi else "",
            "cited": it.get("is-referenced-by-count") or 0,
            "source": "Crossref",
        })
    return items


# ------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------

SOURCES = [_search_semantic_scholar, _search_europe_pmc, _search_crossref]


def search_literature(query, limit=3):
    """多源检索，返回第一个有结果的源。"""
    query = (query or "").strip()
    if not query:
        return []

    key = query.lower()
    now = time.time()
    if key in _cache and now - _cache[key][0] < _CACHE_TTL:
        return _cache[key][1]

    items = []
    for fn in SOURCES:
        items = fn(query, limit)
        if items:
            break

    _cache[key] = (now, items)
    return items


def search_for_topic(topic, cfg, limit=3):
    """知识点 → 英文检索式 → 文献列表。"""
    q = _to_english_query(topic, cfg)
    items = search_literature(q, limit)
    return {"query": q, "items": items}