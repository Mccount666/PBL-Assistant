# -*- coding: utf-8 -*-
"""
PBL 语音讨论助手 —— 语音通道适配层
===================================
本模块把"语音怎么接"与"PBL 逻辑"解耦。

当前两种模式：

  1) browser（默认，开箱即用）
     - 前端用浏览器原生 Web Speech API 做语音识别（STT）
     - AI 回复用浏览器 SpeechSynthesis 播报（TTS）
     - 后端只处理文本（/api/discuss）
     - 优点：零配置、零成本、localStorage 隐私安全
     - 局限：识别延迟 1-2 秒；Safari/Firefox 支持不完整

  2) realtalk（接入实时语音 API 后启用）
     - 前端通过 WebRTC/WebSocket 直连语音 API，实现低延迟双向对话
     - 后端只负责：签发临时凭证、注入 PBL system prompt、记录对话
     - 需要 API 文档中的以下信息（见 RealTalkConfig 字段说明）

RealTalk 接入清单（把文档里的值填进 RealTalkConfig 即可）：
  [ ] 连接方式：WebSocket URL / WebRTC SDP 交换端点
  [ ] 鉴权方式：是否支持服务端签发 ephemeral token（主 Key 不能进前端）
  [ ] 音频格式：PCM16 / Opus，采样率（16k / 24k）
  [ ] 事件协议：音频帧如何发送、转写结果如何返回、回复音频如何接收
  [ ] system prompt 注入：是否支持 instructions 参数或首条消息注入
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RealTalkConfig:
    """RealTalk 语音 API 配置（等文档到位后填写）。"""

    # ---- 连接 ----
    ws_url: str = ""                 # 例如 wss://api.example.com/v1/realtime
    api_base: str = ""               # HTTP 端点（用于签发临时凭证）

    # ---- 鉴权 ----
    api_key: str = ""                # 主 Key（仅后端使用，绝不返回前端）
    token_endpoint: str = ""         # 签发 ephemeral token 的接口路径
    token_auth_header: str = "Authorization"   # 鉴权头名称
    token_scheme: str = "Bearer"     # 鉴权前缀

    # ---- 音频 ----
    input_format: str = "pcm16"      # 输入音频编码
    output_format: str = "pcm16"     # 输出音频编码
    sample_rate: int = 24000

    # ---- 协议 ----
    session_update_event: str = ""   # 会话配置事件名（如 session.update）
    audio_append_event: str = ""     # 音频追加事件名（如 input_audio_buffer.append）
    transcript_event: str = ""       # 转写结果事件名
    response_audio_event: str = ""   # 回复音频事件名

    # ---- 行为 ----
    supports_instructions: bool = False   # 是否支持 system prompt 注入


def build_pbl_instructions(case: dict, act_no: int, board: list, issues: list) -> str:
    """
    组装 PBL 导师的 system prompt（注入语音会话）。
    与 core/discussion_coach.py 的提示词保持一致。
    """
    act = case["acts"][min(act_no, len(case["acts"])) - 1]
    expected = act.get("tutor_notes", {})
    board_text = "；".join("{}（{}）".format(b["text"], b.get("status", "")) for b in board) or "（空）"
    issue_text = "；".join(i["text"] for i in issues) or "（空）"

    return (
        "你是 PBL（问题导向学习）小组讨论的导师（tutor）。学生小组正在围坐讨论病例，"
        "目前已发放到第 {} 幕《{}》。\n\n"
        "【助教参考（你心里有数即可，严禁直接告诉学生）】\n"
        "期望讨论出的假设：{}\n期望学习议题：{}\n\n"
        "【当前假设板】{}\n【当前学习议题】{}\n\n"
        "【行为准则】\n"
        "1. 绝不讲解医学知识、绝不给答案、绝不评价对错，只提问和引导。\n"
        "2. 学生提出观点时追问依据：'你们的依据是案例里哪条信息？'\n"
        "3. 适时唱反调：'有没有另一种疾病也能解释这些表现？'\n"
        "4. 讨论停顿时从这些维度提示：时间线、危险信号、心理社会因素、假设间如何区分。\n"
        "5. 学生提到疾病推测但假设板没有，提示记录下来。\n"
        "6. 学生提出'为什么/机制'类问题，提示列入学习议题。\n"
        "7. 每次发言不超过 2-3 句，口语化，适合语音播报。\n"
        "8. 不要每句都插话——只在值得介入时开口。"
    ).format(
        act_no,
        act["title"],
        "、".join(expected.get("expected_hypotheses", [])) or "—",
        "、".join(expected.get("expected_issues", [])) or "—",
        board_text,
        issue_text,
    )


class VoiceProvider:
    """语音通道基类。"""

    name = "base"

    def create_session(self, case: dict, act_no: int, board: list, issues: list) -> dict:
        """
        创建语音会话。
        browser 模式：返回 {"mode": "browser", "instructions": <提示词>}
        realtalk 模式：返回 {"mode": "realtime", "ws_url": ..., "token": ..., "instructions": ...}
        """
        raise NotImplementedError

    def is_available(self) -> bool:
        raise NotImplementedError


class BrowserSpeechProvider(VoiceProvider):
    """浏览器原生语音（默认，无需任何配置）。"""

    name = "browser-webkit-speech"

    def create_session(self, case, act_no, board, issues):
        return {
            "mode": "browser",
            "instructions": build_pbl_instructions(case, act_no, board, issues),
            "stt": "Web Speech API (zh-CN)",
            "tts": "SpeechSynthesis",
        }

    def is_available(self) -> bool:
        return True


class RealTalkProvider(VoiceProvider):
    """
    RealTalk 实时语音（待接入）。
    填入 RealTalkConfig 并实现 _issue_ephemeral_token / 前端协议对接后启用。
    """

    name = "realtalk-realtime"

    def __init__(self, config: Optional[RealTalkConfig] = None):
        self.config = config or RealTalkConfig()

    def is_available(self) -> bool:
        c = self.config
        return bool(c.ws_url and c.api_key)

    def create_session(self, case, act_no, board, issues):
        if not self.is_available():
            raise RuntimeError(
                "RealTalk 未配置：请在 voice_adapter.py 的 RealTalkConfig 中填入 "
                "ws_url / api_key / token_endpoint（参考文件顶部的接入清单）"
            )
        instructions = build_pbl_instructions(case, act_no, board, issues)
        # TODO(接入时): 调用 token_endpoint 签发临时凭证（主 Key 留在后端）
        #   POST {api_base}{token_endpoint}  Authorization: {scheme} {api_key}
        #   → 返回 ephemeral token / client_secret
        # ephemeral = self._issue_ephemeral_token()
        return {
            "mode": "realtime",
            "provider": self.name,
            "ws_url": self.config.ws_url,
            "token": "TODO-ephemeral-token",
            "audio": {
                "input": self.config.input_format,
                "output": self.config.output_format,
                "sample_rate": self.config.sample_rate,
            },
            "instructions": instructions,
            "events": {
                "session_update": self.config.session_update_event,
                "audio_append": self.config.audio_append_event,
                "transcript": self.config.transcript_event,
                "response_audio": self.config.response_audio_event,
            },
        }


def get_provider(mode: str = "browser") -> VoiceProvider:
    """按模式取语音提供方（前端可切换）。"""
    if mode == "realtalk":
        return RealTalkProvider()
    return BrowserSpeechProvider()