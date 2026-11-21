# -*- coding: utf-8 -*-
"""
床边教练 Bedside Coach —— LLM 客户端
=====================================
支持两种运行模式（与《产品说明文档》中"可切换本地模型"对应）：

  1) API 模式（默认）：调用 OpenAI 兼容接口
     - 豆包（火山方舟 ARK）：https://ark.cn-beijing.volces.com/api/v3
     - DeepSeek：          https://api.deepseek.com/v1
     - 任何 OpenAI 兼容服务均可通过环境变量/配置切换
  2) 离线模式（offline）：无需任何 API Key，
     使用本地规则 + BM25 检索的"本地模型"降级引擎，
     保证演示、断网、考场环境全流程可用。

安全说明：
  为防止 SSRF，API 地址仅允许 https 协议，且须命中 LLM 服务商白名单，
  同时解析后阻断私网/环回/链路本地地址。
  默认模式为 offline（本地运行，数据不出本机，符合活动隐私要求）。

配置优先级：环境变量 > .bedside_config.json > 默认值
"""

import os
import json
import time
import socket
import threading
import ipaddress
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

DEFAULT_CONFIG = {
    # 运行模式: "api" 或 "offline"
    "mode": "offline",
    # API 模式参数（OpenAI 兼容）
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "",
    "model": "doubao-pro-32k",  # 豆包模型名 / 或 deepseek-chat
    # 辅助任务模型（知识点提取、检索式翻译等）：留空则复用主模型
    "utility_model": "",
    # 辅助模型失败时的候选列表（按顺序尝试）
    "utility_fallback_models": [],
    # 实时语音可用模型（可配置；留空用内置默认）
    "realtime_models": [],
    "temperature": 0.4,
    "max_tokens": 600,
    "timeout": 30,
}

# LLM 服务商域名白名单（防 SSRF：仅允许已知大模型服务商）
ALLOWED_API_HOSTS = {
    "ark.cn-beijing.volces.com",        # 豆包 / 火山方舟
    "api.deepseek.com",                 # DeepSeek
    "api.openai.com",                   # OpenAI
    "api.moonshot.cn",                  # Kimi
    "dashscope.aliyuncs.com",           # 通义千问
    "open.bigmodel.cn",                 # 智谱 GLM
    "api.siliconflow.cn",               # 硅基流动
    "openrouter.ai",                    # OpenRouter
    "api.groq.com",                     # Groq
    "api.mistral.ai",                   # Mistral
    "api.together.xyz",                 # Together
    "api.anthropic.com",                # Anthropic（OpenAI 兼容网关）
    "api.stepfun.com",                  # 阶跃星辰（文本 + Realtime 语音）
    "localhost",                        # 本地 Ollama / LM Studio
    "127.0.0.1",
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".bedside_config.json")

# token 用量记录（JSONL，线程安全追加）
USAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "llm_usage.jsonl")
_usage_lock = threading.Lock()

_BUILTIN_REALTIME_MODELS = [
    "stepaudio-3-realtime-preview",
    "stepaudio-2.5-realtime",
    "step-1o-audio",
    "step-audio-2",
    "step-audio-2-mini",
    "step-audio-r1.5",
]


def realtime_models(config=None):
    """实时语音可用模型：配置优先，内置默认兜底。"""
    cfg = config or load_config()
    models = [m.strip() for m in (cfg.get("realtime_models") or []) if m and m.strip()]
    return models or list(_BUILTIN_REALTIME_MODELS)


def _record_usage(model, usage):
    try:
        rec = {
            "ts": int(time.time() * 1000),
            "model": model,
            "prompt": usage.get("prompt_tokens"),
            "completion": usage.get("completion_tokens"),
            "total": usage.get("total_tokens"),
        }
        os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
        with _usage_lock:
            with open(USAGE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _is_safe_url(api_base):
    """校验 LLM API 地址：域名白名单；本地地址（Ollama/LM Studio）允许 http。"""
    try:
        parsed = urllib.parse.urlparse(api_base)
    except Exception:
        return False, "URL 解析失败"

    host = parsed.hostname
    if not host:
        return False, "缺少主机名"

    is_local = host in ("localhost", "127.0.0.1")

    if parsed.scheme != "https" and not (is_local and parsed.scheme == "http"):
        return False, "仅支持 https 协议（本地地址除外）"

    if host not in ALLOWED_API_HOSTS:
        return False, "域名不在 LLM 服务商白名单内: {}".format(host)

    # 本地推理服务（Ollama / LM Studio）跳过公网 IP 校验
    if is_local:
        return True, "OK"

    # 解析后 IP 校验（阻断 DNS rebinding 指向内网）
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return False, "域名解析失败"

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "解析到非公网地址: {}".format(ip)

    return True, "OK"


def load_config():
    """加载配置：环境变量优先，其次配置文件，最后默认值。"""
    cfg = dict(DEFAULT_CONFIG)

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass

    # 环境变量覆盖（便于比赛现场快速配置）
    env_map = {
        "BEDSIDE_MODE": "mode",
        "BEDSIDE_API_BASE": "api_base",
        "BEDSIDE_API_KEY": "api_key",
        "BEDSIDE_MODEL": "model",
        "BEDSIDE_UTILITY_MODEL": "utility_model",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val

    # 候选模型列表用逗号分隔
    fallback_env = os.environ.get("BEDSIDE_UTILITY_FALLBACK_MODELS")
    if fallback_env:
        cfg["utility_fallback_models"] = [
            m.strip() for m in fallback_env.split(",") if m.strip()
        ]

    # 实时语音模型：环境变量（逗号分隔）或文件里的字符串统一成列表
    rt_env = os.environ.get("BEDSIDE_REALTIME_MODELS")
    if rt_env:
        cfg["realtime_models"] = [m.strip() for m in rt_env.split(",") if m.strip()]
    if isinstance(cfg.get("realtime_models"), str):
        cfg["realtime_models"] = [
            m.strip() for m in cfg["realtime_models"].split(",") if m.strip()
        ]

    # 模式有效性
    if cfg.get("mode") not in ("api", "offline"):
        cfg["mode"] = "offline"
    # API 模式但无 key → 自动降级离线
    if cfg["mode"] == "api" and not cfg.get("api_key"):
        cfg["mode"] = "offline"
    # API 模式但地址不安全 → 自动降级离线
    if cfg["mode"] == "api":
        ok, _ = _is_safe_url(cfg["api_base"])
        if not ok:
            cfg["mode"] = "offline"

    return cfg


def utility_models(config=None):
    """
    辅助任务（知识点提取、检索式翻译等）的模型候选列表。
    配置优先，主模型兜底；去重、保序。
    """
    cfg = config or load_config()
    primary = (cfg.get("utility_model") or "").strip() or cfg.get("model", "")
    models = [primary] if primary else []
    fallback = cfg.get("utility_fallback_models") or []
    if isinstance(fallback, str):
        fallback = fallback.split(",")
    for m in fallback:
        m = (m or "").strip()
        if m and m not in models:
            models.append(m)
    return models


def chat(system_prompt, user_prompt, config=None):
    """
    统一的对话入口。返回 (text, mode_used)。
    - API 模式失败时自动降级为离线引擎（返回 None，由调用方走本地规则）
    - 离线模式直接返回 None
    """
    cfg = config or load_config()

    if cfg["mode"] != "api":
        return None, "offline"

    ok, reason = _is_safe_url(cfg["api_base"])
    if not ok:
        return None, "offline"

    url = cfg["api_base"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        try:
            _record_usage(cfg.get("model"), body.get("usage") or {})
        except Exception:
            pass
        msg = body["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        # 兼容推理模型：content 为空时取 reasoning（思考过程含最终答案）
        if not text and msg.get("reasoning"):
            text = (msg.get("reasoning") or "").strip()
        if not text:
            return None, "offline"
        return text, "api"
    except Exception:
        # 网络/鉴权失败 → 静默降级离线模式
        return None, "offline"


def is_api_mode(config=None):
    cfg = config or load_config()
    return cfg.get("mode") == "api"


def mask_key(key):
    """Key 打码显示（前端只显示是否已配置 + 尾 4 位）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:3] + "*" * (len(key) - 7) + key[-4:]


def save_config(updates):
    """
    保存配置到 .bedside_config.json（与现有配置合并）。
    仅接受白名单字段，避免写入任意键。
    """
    allowed = {"mode", "api_base", "api_key", "model", "utility_model",
               "utility_fallback_models", "realtime_models",
               "temperature", "max_tokens", "timeout"}
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            cfg = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    for k, v in (updates or {}).items():
        if k in allowed:
            cfg[k] = v
    Path(CONFIG_PATH).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def test_connection(config=None):
    """
    检测 API 连接：发一个最小请求。
    返回 {"ok": bool, "latency_ms": int, "reply": str, "error": str}
    """
    import time
    cfg = config or load_config()
    if cfg.get("mode") != "api" or not cfg.get("api_key"):
        return {"ok": False, "error": "未配置 API Key", "latency_ms": 0, "reply": ""}

    ok, reason = _is_safe_url(cfg["api_base"])
    if not ok:
        return {"ok": False, "error": reason, "latency_ms": 0, "reply": ""}

    url = cfg["api_base"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "temperature": 0,
        "max_tokens": 16,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 30)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latency = int((time.time() - start) * 1000)
        text = body["choices"][0]["message"]["content"].strip()
        return {"ok": True, "latency_ms": latency, "reply": text[:40], "error": ""}
    except urllib.error.HTTPError as e:
        latency = int((time.time() - start) * 1000)
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        hint = {401: "Key 无效或已过期", 403: "无权限", 404: "接口地址或模型名错误",
                429: "请求频率/额度超限"}.get(e.code, "")
        return {"ok": False, "latency_ms": latency, "reply": "",
                "error": "HTTP {} {} {}".format(e.code, hint, detail).strip()}
    except Exception as e:
        latency = int((time.time() - start) * 1000)
        return {"ok": False, "latency_ms": latency, "reply": "",
                "error": "{}: {}".format(type(e).__name__, e)}


def current_mode_label(config=None):
    cfg = config or load_config()
    if cfg.get("mode") == "api":
        return "API 模式（{}）".format(cfg.get("model"))
    return "离线模式（本地规则引擎）"
