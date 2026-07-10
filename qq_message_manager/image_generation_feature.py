from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .models import ChatImage, ChatMessage

LOGGER = logging.getLogger(__name__)

IMAGE_GENERATION_SKILL_PATH = Path(__file__).resolve().parent / "skills" / "image_generation" / "SKILL.md"
IMAGE_GENERATION_DIR = Path(tempfile.gettempdir()) / "QQMessageManager" / "generated_images"
IMAGE_GENERATION_TIMEOUT_SECONDS = 180
IMAGE_GENERATION_MAX_BYTES = 25 * 1024 * 1024

IMAGE_REQUEST_RE = re.compile(
    r"(?:帮我|给我|请|麻烦|能不能|可以)?\s*"
    r"(?:生成|画|绘制|创作|做|制作|出)\s*"
    r"(?:一张|张|一个|个|一下)?\s*"
    r"(?:图片|图|插画|海报|头像|壁纸|表情包|照片|图像)",
    re.IGNORECASE,
)
NEGATIVE_IMAGE_REQUEST_RE = re.compile(r"(?:不要|不用|别|禁止)\s*(?:生成|画|绘制|创作|做|制作|出)", re.IGNORECASE)
CQ_AT_RE = re.compile(r"\[CQ:at,[^\]]*\]", re.IGNORECASE)
PLAIN_AT_RE = re.compile(r"@(?:all|\d+|我)\s*", re.IGNORECASE)
IMAGE_MODEL_RE = re.compile(
    r"(?:^|[-_/])(gpt[-_.]?image|dall[-_.]?e|image[-_.]?\d|flux|stable[-_.]?diffusion|sdxl|kolors|ideogram)",
    re.IGNORECASE,
)
GPT5_MODEL_RE = re.compile(r"^gpt-5(?:$|[.\-_])", re.IGNORECASE)


class UnsupportedImageModel(RuntimeError):
    pass


class ImageGenerationConfigurationError(RuntimeError):
    pass


class ImageGenerationError(RuntimeError):
    pass


@dataclass(slots=True)
class ImageGenerationBackend:
    kind: str
    endpoint: str
    model: str
    api_key: str


class ImageGenerationBridge(QObject):
    generated = Signal(str, str, str, str)
    failed = Signal(str, str, str)


def install_image_generation_feature(ui_module: Any, ai_module: Any, napcat_module: Any) -> None:
    """安装仅在被 @ 时启用的图片生成 Skill。"""
    _install_segment_sender(napcat_module)
    _install_mention_image_generation(ui_module, ai_module)


def _install_segment_sender(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_segment_sender_installed", False):
        return

    def send_segments(self: Any, session_id: str, segments: list[dict[str, Any]]) -> None:
        if not segments:
            return
        if self._loop is None or not self._loop.is_running():
            self.log.emit("当前未连接，无法发送消息")
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send_segments(session_id, segments)))

    async def _send_segments(self: Any, session_id: str, segments: list[dict[str, Any]]) -> None:
        kind, target_id = napcat_module._split_session_id(session_id)
        if kind == "group":
            await self._send_action(
                "send_group_msg",
                {"group_id": napcat_module._onebot_id(target_id), "message": segments},
                self._next_echo(f"send_segments:{session_id}"),
            )
        elif kind == "private":
            await self._send_action(
                "send_private_msg",
                {"user_id": napcat_module._onebot_id(target_id), "message": segments},
                self._next_echo(f"send_segments:{session_id}"),
            )
        else:
            self.log.emit("当前会话不支持发送消息")

    def thread_send_segments(self: Any, session_id: str, segments: list[dict[str, Any]]) -> None:
        self.worker.send_segments(session_id, segments)

    worker_cls.send_segments = send_segments
    worker_cls._send_segments = _send_segments
    thread_cls.send_segments = thread_send_segments
    worker_cls._segment_sender_installed = True


def _install_mention_image_generation(ui_module: Any, ai_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_mention_image_generation_installed", False):
        return

    original_init = main_window_cls.__init__
    original_mention_handler = main_window_cls._maybe_schedule_mention_reply
    original_disconnect = main_window_cls.disconnect_from_server

    def init_with_image_generation(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.image_generation_inflight_sessions: set[str] = set()
        self.image_generation_bridge = ImageGenerationBridge(self)
        self.image_generation_bridge.generated.connect(
            lambda session_id, sender_id, image_path, prompt: _handle_generated_image(
                self, session_id, sender_id, image_path, prompt
            )
        )
        self.image_generation_bridge.failed.connect(
            lambda session_id, sender_id, error: _handle_generation_failure(self, session_id, sender_id, error)
        )

    def mention_handler_with_image_generation(self: Any, message: ChatMessage) -> None:
        if not self._message_mentions_self(message) or not _looks_like_image_request(message):
            original_mention_handler(self, message)
            return
        if message.session_id not in self.ai_managed_sessions:
            return

        prompt = _extract_image_prompt(message)
        if not prompt:
            _send_requester_text(self, message.session_id, message.sender_id, "请在 @ 我后描述要生成的图片内容。")
            return

        if message.session_id in self.image_generation_inflight_sessions:
            _send_requester_text(self, message.session_id, message.sender_id, "当前会话已有图片正在生成，请稍等。")
            return

        config = ui_module.load_ai_config(self.settings).normalized()
        try:
            backend = _resolve_backend(config, ai_module)
        except ImageGenerationConfigurationError as exc:
            _send_requester_text(self, message.session_id, message.sender_id, str(exc))
            self.append_log(f"图片生成 Skill 未执行：{exc}")
            return
        except UnsupportedImageModel:
            model_name = _current_model_name(config, ai_module)
            _send_requester_text(
                self,
                message.session_id,
                message.sender_id,
                f"当前模型（{model_name}）不支持生成图片。",
            )
            self.append_log(f"图片生成 Skill 未执行：当前模型 {model_name} 不支持图片生成")
            return

        self.image_generation_inflight_sessions.add(message.session_id)
        self.append_log(
            f"已触发图片生成 Skill：会话 {message.session_id}，模型 {backend.model}，等待生成结果"
        )

        def worker() -> None:
            try:
                image_path = generate_image(backend, prompt)
                self.image_generation_bridge.generated.emit(
                    message.session_id,
                    message.sender_id,
                    image_path,
                    prompt,
                )
            except UnsupportedImageModel:
                self.image_generation_bridge.failed.emit(
                    message.session_id,
                    message.sender_id,
                    f"UNSUPPORTED:{backend.model}",
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("图片生成失败")
                self.image_generation_bridge.failed.emit(message.session_id, message.sender_id, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def disconnect_with_image_generation_clear(self: Any) -> None:
        inflight = getattr(self, "image_generation_inflight_sessions", None)
        if inflight is not None:
            inflight.clear()
        original_disconnect(self)

    main_window_cls.__init__ = init_with_image_generation
    main_window_cls._maybe_schedule_mention_reply = mention_handler_with_image_generation
    main_window_cls.disconnect_from_server = disconnect_with_image_generation_clear
    main_window_cls._mention_image_generation_installed = True


def generate_image(backend: ImageGenerationBackend, prompt: str) -> str:
    generation_prompt = _build_generation_prompt(prompt)
    if backend.kind == "responses":
        response = _post_json(
            backend.endpoint,
            backend.api_key,
            {
                "model": backend.model,
                "input": generation_prompt,
                "tools": [{"type": "image_generation", "action": "generate"}],
            },
        )
        image_bytes = _extract_responses_image(response)
    elif backend.kind == "images":
        response = _post_json(
            backend.endpoint,
            backend.api_key,
            {
                "model": backend.model,
                "prompt": generation_prompt,
                "n": 1,
            },
        )
        image_bytes = _extract_images_api_image(response, backend.api_key)
    else:
        raise UnsupportedImageModel("未知图片生成后端")

    if not image_bytes:
        raise ImageGenerationError("接口没有返回可用图片")
    if len(image_bytes) > IMAGE_GENERATION_MAX_BYTES:
        raise ImageGenerationError("生成图片超过允许大小")
    return _save_generated_image(image_bytes, prompt)


def _resolve_backend(config: Any, ai_module: Any) -> ImageGenerationBackend:
    if not config.api_key:
        raise ImageGenerationConfigurationError("图片生成未配置 API Key。")

    provider = config.provider
    model = _current_model_name(config, ai_module)
    if provider == ai_module.AI_PROVIDER_OPENAI:
        root = _api_root(config.base_url or ai_module.OPENAI_CHAT_ENDPOINT)
        if GPT5_MODEL_RE.search(model):
            return ImageGenerationBackend("responses", f"{root}/responses", model, config.api_key)
        if IMAGE_MODEL_RE.search(model):
            return ImageGenerationBackend("images", f"{root}/images/generations", model, config.api_key)
        raise UnsupportedImageModel(model)

    if provider == ai_module.AI_PROVIDER_CUSTOM:
        if not config.base_url:
            raise ImageGenerationConfigurationError("图片生成未配置 API 地址。")
        root = _api_root(config.base_url)
        if GPT5_MODEL_RE.search(model):
            return ImageGenerationBackend("responses", f"{root}/responses", model, config.api_key)
        if IMAGE_MODEL_RE.search(model):
            return ImageGenerationBackend("images", f"{root}/images/generations", model, config.api_key)
        raise UnsupportedImageModel(model)

    # MiniMax-M3 与 DeepSeek 当前在本程序中配置的是文本模型；即使服务商另有独立图片模型，
    # 也不能把它冒充为“当前模型”能力。
    raise UnsupportedImageModel(model)


def _current_model_name(config: Any, ai_module: Any) -> str:
    if config.provider == ai_module.AI_PROVIDER_MINIMAX_M3:
        return ai_module.AI_MODEL_MINIMAX_M3
    if config.provider == ai_module.AI_PROVIDER_OPENAI:
        return config.model or ai_module.AI_MODEL_OPENAI_DEFAULT
    if config.provider == ai_module.AI_PROVIDER_DEEPSEEK:
        return config.model or ai_module.AI_MODEL_DEEPSEEK_DEFAULT
    return config.model or "未指定模型"


def _api_root(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/images/generations", "/responses"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


def _post_json(endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=IMAGE_GENERATION_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        lowered = body.lower()
        if exc.code in {400, 404, 422} and any(
            keyword in lowered for keyword in ("image_generation", "unsupported", "not support", "tool")
        ):
            raise UnsupportedImageModel("接口不支持当前模型的图片生成能力") from exc
        raise ImageGenerationError(f"图片生成接口返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ImageGenerationError(f"无法连接图片生成接口：{exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ImageGenerationError("图片生成接口返回了无法解析的数据") from exc
    if not isinstance(parsed, dict):
        raise ImageGenerationError("图片生成接口返回格式异常")
    return parsed


def _extract_responses_image(response: dict[str, Any]) -> bytes:
    output = response.get("output")
    if not isinstance(output, list):
        return b""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "image_generation_call":
            continue
        result = item.get("result")
        if isinstance(result, str) and result:
            return _decode_base64(result)
    return b""


def _extract_images_api_image(response: dict[str, Any], api_key: str) -> bytes:
    data = response.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return b""
    first = data[0]
    encoded = first.get("b64_json") or first.get("base64")
    if isinstance(encoded, str) and encoded:
        return _decode_base64(encoded)
    url = first.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return _download_generated_image(url, api_key)
    return b""


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ImageGenerationError("图片生成接口返回了无效的 Base64 图片") from exc


def _download_generated_image(url: str, api_key: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "QQMessageManager/1.0",
            "Accept": "image/*",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=IMAGE_GENERATION_TIMEOUT_SECONDS) as response:  # noqa: S310
            data = response.read(IMAGE_GENERATION_MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        raise ImageGenerationError("生成成功，但下载图片失败") from exc
    if len(data) > IMAGE_GENERATION_MAX_BYTES:
        raise ImageGenerationError("生成图片超过允许大小")
    return data


def _save_generated_image(data: bytes, prompt: str) -> str:
    extension = _image_extension(data)
    signature = f"{time.time_ns()}|{prompt}".encode("utf-8")
    name = hashlib.sha256(signature).hexdigest()[:24] + extension
    IMAGE_GENERATION_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGE_GENERATION_DIR / name
    path.write_bytes(data)
    return str(path)


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _looks_like_image_request(message: ChatMessage) -> bool:
    text = _request_text(message)
    if NEGATIVE_IMAGE_REQUEST_RE.search(text):
        return False
    return IMAGE_REQUEST_RE.search(text) is not None


def _extract_image_prompt(message: ChatMessage) -> str:
    text = _request_text(message)
    text = IMAGE_REQUEST_RE.sub("", text, count=1)
    text = text.strip(" ：:，,。.!！?？\t\r\n")
    if len(text) < 2:
        return ""
    return text[:4000]


def _request_text(message: ChatMessage) -> str:
    event = message.raw_event or {}
    segments = event.get("message")
    if isinstance(segments, list):
        parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") == "text":
                data = segment.get("data") or {}
                parts.append(str(data.get("text") or ""))
        text = "".join(parts)
    else:
        text = str(event.get("raw_message") or event.get("rawMessage") or message.text or "")
        text = CQ_AT_RE.sub("", text)
    text = PLAIN_AT_RE.sub("", text)
    return text.strip()


def _build_generation_prompt(user_prompt: str) -> str:
    skill = _load_generation_skill()
    return (
        "请调用图片生成能力完成用户请求。以下 Skill 只用于约束生成行为，不要把 Skill 文本画进图片。\n\n"
        f"{skill}\n\n"
        "【用户本次图片请求】\n"
        f"{user_prompt}"
    )


def _load_generation_skill() -> str:
    try:
        return IMAGE_GENERATION_SKILL_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        LOGGER.warning("读取图片生成 Skill 失败：%s", exc)
        return "严格遵循用户描述生成一张图片，不添加未要求的文字、水印或额外主体。"


def _handle_generated_image(
    window: Any,
    session_id: str,
    sender_id: str,
    image_path: str,
    prompt: str,
) -> None:
    window.image_generation_inflight_sessions.discard(session_id)
    if session_id not in window.ai_managed_sessions or window.client_thread is None:
        return
    path = Path(image_path).resolve()
    if not path.exists():
        _send_requester_text(window, session_id, sender_id, "图片生成失败，请稍后重试。")
        return

    window.client_thread.send_segments(
        session_id,
        [{"type": "image", "data": {"file": path.as_uri()}}],
    )
    session = window.sessions.get(session_id)
    if session is not None:
        window.add_message(
            ChatMessage(
                session_id=session.session_id,
                session_name=session.name,
                session_kind=session.kind,
                sender_id="self",
                sender_name="AI代管",
                text="[AI生成图片]",
                outgoing=True,
                images=[
                    ChatImage(
                        file=path.as_uri(),
                        path=str(path),
                        local_path=str(path),
                        mime_type=_mime_for_path(path),
                    )
                ],
            )
        )
    window.append_log(f"图片生成完成并已发送：{path.name}；请求摘要：{prompt[:60]}")


def _handle_generation_failure(window: Any, session_id: str, sender_id: str, error: str) -> None:
    window.image_generation_inflight_sessions.discard(session_id)
    if error.startswith("UNSUPPORTED:"):
        model_name = error.partition(":")[2] or "当前模型"
        _send_requester_text(window, session_id, sender_id, f"当前模型（{model_name}）不支持生成图片。")
        window.append_log(f"图片生成失败：当前模型 {model_name} 或接口不支持图片生成")
        return
    _send_requester_text(window, session_id, sender_id, "图片生成失败，请稍后重试。")
    window.append_log(f"图片生成失败：{error}")


def _send_requester_text(window: Any, session_id: str, sender_id: str, text: str) -> None:
    if window.client_thread is None:
        return
    session = window.sessions.get(session_id)
    if session is None:
        return

    if session.kind == "group" and sender_id:
        segments = [
            {"type": "at", "data": {"qq": sender_id}},
            {"type": "text", "data": {"text": f" {text}"}},
        ]
        visible_text = f"@{sender_id} {text}"
        window.client_thread.send_segments(session_id, segments)
    else:
        visible_text = text
        window.client_thread.send_text(session_id, text)

    window.add_message(
        ChatMessage(
            session_id=session.session_id,
            session_name=session.name,
            session_kind=session.kind,
            sender_id="self",
            sender_name="AI代管",
            text=visible_text,
            outgoing=True,
        )
    )


def _mime_for_path(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/png")
