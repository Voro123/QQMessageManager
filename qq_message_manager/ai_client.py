from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

AI_PROVIDER_MINIMAX_M3 = "Minimax-m3"
AI_MODEL_MINIMAX_M3 = "MiniMax-M3"
AI_REPLY_TIMEOUT_SECONDS = 45
MINIMAX_CHAT_ENDPOINTS = (
    "https://api.minimax.chat/v1/chat/completions",
    "https://api.minimaxi.chat/v1/chat/completions",
    "https://api.minimax.chat/v1/text/chatcompletion_v2",
    "https://api.minimaxi.chat/v1/text/chatcompletion_v2",
)


@dataclass(slots=True)
class AiReplyConfig:
    provider: str = AI_PROVIDER_MINIMAX_M3
    api_key: str = ""
    prompt: str = ""
    timed_enabled: bool = False
    timed_min_seconds: int = 10
    timed_max_seconds: int = 20
    require_recent_non_self_enabled: bool = True
    recent_non_self_seconds: int = 15
    context_message_count: int = 10
    mention_enabled: bool = True
    mention_min_seconds: int = 3
    mention_max_seconds: int = 6
    prevent_self_follow_enabled: bool = True

    def normalized(self) -> "AiReplyConfig":
        timed_min = max(1, int(self.timed_min_seconds))
        timed_max = max(timed_min, int(self.timed_max_seconds))
        mention_min = max(1, int(self.mention_min_seconds))
        mention_max = max(mention_min, int(self.mention_max_seconds))
        return AiReplyConfig(
            provider=self.provider or AI_PROVIDER_MINIMAX_M3,
            api_key=self.api_key.strip(),
            prompt=self.prompt.strip(),
            timed_enabled=bool(self.timed_enabled),
            timed_min_seconds=timed_min,
            timed_max_seconds=timed_max,
            require_recent_non_self_enabled=bool(self.require_recent_non_self_enabled),
            recent_non_self_seconds=max(1, int(self.recent_non_self_seconds)),
            context_message_count=max(1, int(self.context_message_count)),
            mention_enabled=bool(self.mention_enabled),
            mention_min_seconds=mention_min,
            mention_max_seconds=mention_max,
            prevent_self_follow_enabled=bool(self.prevent_self_follow_enabled),
        )


class AiProviderError(RuntimeError):
    pass


class MinimaxM3Client:
    def __init__(self, api_key: str, endpoints: tuple[str, ...] = MINIMAX_CHAT_ENDPOINTS) -> None:
        self.api_key = api_key.strip()
        self.endpoints = endpoints

    def generate_reply(
        self,
        *,
        session_name: str,
        session_kind: str,
        known_prompt: str,
        context_messages: list[dict[str, str]],
    ) -> str:
        if not self.api_key:
            raise AiProviderError("缺少 Minimax API Key")

        messages = self._build_messages(session_name, session_kind, known_prompt, context_messages)
        payload = {
            "model": AI_MODEL_MINIMAX_M3,
            "messages": messages,
            "temperature": 0.8,
            "stream": False,
        }

        errors: list[str] = []
        for endpoint in self.endpoints:
            try:
                response = self._post_json(endpoint, payload)
                reply = _extract_reply_text(response)
                if reply:
                    return _clean_reply(reply)
                errors.append(f"{endpoint}: 响应中没有可用文本")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{endpoint}: {exc}")

        raise AiProviderError("MiniMax-M3 调用失败：" + "；".join(errors[-2:]))

    @staticmethod
    def _build_messages(
        session_name: str,
        session_kind: str,
        known_prompt: str,
        context_messages: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        kind_label = "群聊" if session_kind == "group" else "私聊"
        system_prompt = (
            "你正在代管一个 QQ 聊天会话。"
            "请根据上下文自然回复一条即将发送到聊天里的中文消息。"
            "只输出要发送的消息正文，不要解释，不要加引号，不要暴露你是 AI。"
            "回复尽量简短、像真实聊天，不要超过 120 个字。"
            f"当前会话类型：{kind_label}；会话名称：{session_name}。"
        )
        if known_prompt:
            system_prompt += "\n已知信息/人设/规则：\n" + known_prompt

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for item in context_messages:
            sender_name = item.get("sender_name", "对方")
            text = item.get("text", "").strip()
            if not text:
                continue
            role = "assistant" if item.get("outgoing") == "1" else "user"
            prefix = "我" if role == "assistant" else sender_name
            messages.append({"role": role, "content": f"{prefix}: {text}"})
        messages.append({"role": "user", "content": "请生成下一条要发送的回复。"})
        return messages

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=AI_REPLY_TIMEOUT_SECONDS) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AiProviderError(f"HTTP {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise AiProviderError(str(exc.reason)) from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AiProviderError(f"接口返回不是 JSON：{body[:500]}") from exc
        if not isinstance(parsed, dict):
            raise AiProviderError("接口返回格式不是对象")
        return parsed


def generate_ai_reply(
    config: AiReplyConfig,
    *,
    session_name: str,
    session_kind: str,
    context_messages: list[dict[str, str]],
) -> str:
    normalized = config.normalized()
    if normalized.provider != AI_PROVIDER_MINIMAX_M3:
        raise AiProviderError(f"暂不支持的 AI 服务商：{normalized.provider}")
    client = MinimaxM3Client(normalized.api_key)
    return client.generate_reply(
        session_name=session_name,
        session_kind=session_kind,
        known_prompt=normalized.prompt,
        context_messages=context_messages[-normalized.context_message_count :],
    )


def _extract_reply_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
            text = first.get("text")
            if isinstance(text, str):
                return text

    for key in ("reply", "content", "text", "answer", "output"):
        value = response.get(key)
        if isinstance(value, str):
            return value

    data = response.get("data")
    if isinstance(data, dict):
        return _extract_reply_text(data)
    return ""


def _clean_reply(text: str) -> str:
    reply = text.strip().strip('"“”')
    if not reply:
        return ""
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    return "\n".join(lines[:3]).strip()
