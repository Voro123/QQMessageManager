from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .ai_client import (
    AI_MODEL_MINIMAX_M3,
    AI_PROVIDER_MINIMAX_M3,
    AiProviderError,
    AiReplyConfig,
    MinimaxM3Client,
    OpenAICompatibleClient,
    THINK_TAG_RE,
    _extract_reply_text,
    _resolve_endpoint_and_model,
)
from .models import ChatMessage

SUMMARY_MAX_INPUT_CHARS = 48000
SUMMARY_TIMEOUT_TIP = "总结失败：AI 服务商没有返回可用文本"


def generate_chat_summary(
    config: AiReplyConfig,
    *,
    session_name: str,
    session_kind: str,
    messages: list[ChatMessage],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> str:
    normalized = config.normalized()
    if not normalized.api_key:
        raise AiProviderError("缺少 API Key，无法总结聊天记录")

    prompt_messages = _build_summary_messages(session_name, session_kind, messages, start_time, end_time)
    if normalized.provider == AI_PROVIDER_MINIMAX_M3:
        client = MinimaxM3Client(normalized.api_key)
        payload = {
            "model": AI_MODEL_MINIMAX_M3,
            "messages": prompt_messages,
            "temperature": 0.3,
            "stream": False,
            "max_completion_tokens": 1400,
            "thinking": {"type": "disabled"},
            "reasoning_split": False,
        }
        errors: list[str] = []
        for endpoint in client.endpoints:
            try:
                response = client._post_json(endpoint, payload)  # noqa: SLF001
                summary = _clean_summary(_extract_reply_text(response))
                if summary:
                    return summary
                errors.append(f"{endpoint}: {SUMMARY_TIMEOUT_TIP}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{endpoint}: {exc}")
        raise AiProviderError("MiniMax-M3 总结失败：" + "；".join(errors[-2:]))

    endpoint, model = _resolve_endpoint_and_model(normalized)
    client = OpenAICompatibleClient(api_key=normalized.api_key, endpoint=endpoint, model=model)
    payload = {
        "model": client.model,
        "messages": prompt_messages,
        "temperature": 0.3,
        "stream": False,
        "max_tokens": 1800,
    }
    response = client._post_json(client.endpoint, payload)  # noqa: SLF001
    summary = _clean_summary(_extract_reply_text(response))
    if not summary:
        raw_snippet = json.dumps(response, ensure_ascii=False)[:800]
        raise AiProviderError(f"{SUMMARY_TIMEOUT_TIP}；原始返回前 800 字：{raw_snippet}")
    return summary


def _build_summary_messages(
    session_name: str,
    session_kind: str,
    messages: list[ChatMessage],
    start_time: datetime | None,
    end_time: datetime | None,
) -> list[dict[str, str]]:
    kind_label = "群聊" if session_kind == "group" else "私聊"
    time_range = _format_time_range(start_time, end_time)
    transcript = _format_transcript(messages)
    system_prompt = (
        "你是聊天记录总结助手。你需要根据给定的 QQ 聊天记录，总结这个会话在指定时间区间内发生了什么。"
        "不要编造聊天记录中没有的信息。不要输出思考过程。"
        "如果记录里有图片、表情包、语音、文件等占位，只能根据占位和上下文推断，不能假装看到了具体内容。"
        "请使用中文，结构清晰，尽量保留重要人名、事项、结论和待办。"
    )
    user_prompt = (
        f"会话类型：{kind_label}\n"
        f"会话名称：{session_name}\n"
        f"时间范围：{time_range}\n"
        f"消息数量：{len(messages)}\n\n"
        "请按下面格式总结：\n"
        "1. 总览：用 2~4 句话概括这段聊天主要发生了什么。\n"
        "2. 主要话题：按要点列出重要话题，每点说明参与者和内容。\n"
        "3. 结论 / 决定 / 待办：如有就列出；没有就写“无明确待办”。\n"
        "4. 氛围与关系变化：概括聊天语气、冲突、玩笑或情绪变化。\n"
        "5. 可能需要回看原文的点：列出不确定或需要确认的内容。\n\n"
        "聊天记录如下：\n"
        f"{transcript}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _format_transcript(messages: list[ChatMessage]) -> str:
    lines: list[str] = []
    total = 0
    for message in sorted(messages, key=lambda item: item.timestamp):
        text = (message.text or "").strip() or "[空消息]"
        if len(text) > 500:
            text = text[:500] + "..."
        line = f"[{message.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {message.sender_name}: {text}"
        if total + len(line) > SUMMARY_MAX_INPUT_CHARS:
            lines.append("[后续消息因输入长度限制被截断]")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _format_time_range(start_time: datetime | None, end_time: datetime | None) -> str:
    if start_time is None and end_time is None:
        return "不限"
    start = start_time.strftime("%Y-%m-%d %H:%M:%S") if start_time else "不限开始"
    end = end_time.strftime("%Y-%m-%d %H:%M:%S") if end_time else "不限结束"
    return f"{start} ~ {end}"


def _clean_summary(text: str) -> str:
    summary = THINK_TAG_RE.sub("", text or "")
    summary = summary.replace("</think>", "")
    return summary.strip().strip('"“”')
