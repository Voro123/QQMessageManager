from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader

from .image_cache import ensure_cached, short_id

LOGGER = logging.getLogger(__name__)

VISION_SKILL_PATH = Path(__file__).resolve().parent / "skills" / "vision" / "SKILL.md"
VISION_CACHE_DIR = Path(tempfile.gettempdir()) / "qq_message_manager" / "vision_inputs"
VISION_CACHE_VERSION = "vision-v2"
VISION_MAX_IMAGES_PER_MESSAGE = 3
VISION_MAX_SIDE = 1600
VISION_MAX_ENCODED_BYTES = 5 * 1024 * 1024
FILTERED_IMAGE_TEXTS = {"", "[图片消息已过滤]", "[图片]", "[表情包]"}


def install_vision_input(ui_module: Any, ai_module: Any) -> None:
    """修复图片进入 AI 请求的链路，并为真实视觉输入注入内部 Skill。"""
    _install_settings_hint(ui_module)
    _install_context_builder(ui_module)
    _install_multimodal_prompt(ai_module)
    _install_minimax_multimodal_attempt(ai_module)


def _install_settings_hint(ui_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_vision_input_hint_installed", False):
        return
    original_init = dialog_cls.__init__

    def init_with_vision_hint(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        checkbox = self.allow_image_read_enabled
        checkbox.setText("允许读取图片（需要视觉模型）")
        checkbox.setToolTip(
            "开启后，程序会把最近聊天中的图片下载、裁剪空白、转换成标准 PNG/JPEG，"
            "并作为真实多模态输入发送给当前模型。模型本身必须支持图片输入。"
        )

    dialog_cls.__init__ = init_with_vision_hint
    dialog_cls._vision_input_hint_installed = True


def _install_context_builder(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_vision_context_builder_installed", False):
        return

    def ai_context_messages(
        self: Any,
        session_id: str,
        count: int,
        allow_image_read_enabled: bool = False,
        token: str = "",
    ) -> list[dict[str, Any]]:
        messages = self.messages.get(session_id, [])[-count:]
        result: list[dict[str, Any]] = []
        prepared_count = 0
        failed_ids: list[str] = []

        for message in messages:
            text = (message.text or "").strip()
            item: dict[str, Any] = {
                "sender_name": message.sender_name,
                "text": text,
                "outgoing": "1" if message.outgoing else "0",
            }

            if allow_image_read_enabled and message.images and not message.outgoing:
                data_uris: list[str] = []
                for image in message.images[:VISION_MAX_IMAGES_PER_MESSAGE]:
                    local_path = ensure_cached(image, token=token)
                    if not local_path:
                        failed_ids.append(short_id(image))
                        continue
                    vision_path = _display_or_original_path(local_path)
                    data_uri = _prepare_vision_data_uri(vision_path)
                    if not data_uri:
                        failed_ids.append(short_id(image))
                        continue
                    data_uris.append(data_uri)

                if data_uris:
                    item["images"] = data_uris
                    item["image_count"] = len(data_uris)
                    prepared_count += len(data_uris)
                    if text in FILTERED_IMAGE_TEXTS:
                        item["text"] = f"[本条消息已附加 {len(data_uris)} 张图片，请直接查看图片内容]"
                    else:
                        item["text"] = f"{text}\n[本条消息另附 {len(data_uris)} 张图片，请结合图片理解]"
                elif text in FILTERED_IMAGE_TEXTS:
                    item["text"] = "[图片读取失败，当前请求未附带可查看的图片]"

            if not item.get("text") and not item.get("images"):
                continue
            result.append(item)

        if allow_image_read_enabled:
            if prepared_count:
                self.append_log(f"已为本次 AI 请求准备 {prepared_count} 张真实图片输入")
            elif any(message.images for message in messages):
                suffix = f"（{', '.join(failed_ids[:3])}）" if failed_ids else ""
                self.append_log(f"图片读取已开启，但本次没有成功准备图片{suffix}")
        return result

    main_window_cls._ai_context_messages = ai_context_messages
    main_window_cls._vision_context_builder_installed = True


def _install_multimodal_prompt(ai_module: Any) -> None:
    if getattr(ai_module, "_vision_prompt_installed", False):
        return
    original_builder = ai_module.build_chat_messages

    def build_chat_messages_with_vision(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        messages = original_builder(*args, **kwargs)
        allow_image_read_enabled = _builder_arg(args, kwargs, 6, "allow_image_read_enabled", False)
        context_messages = _builder_arg(args, kwargs, 5, "context_messages", []) or []
        image_count = sum(len(item.get("images") or []) for item in context_messages)
        if not allow_image_read_enabled or image_count <= 0:
            return messages

        skill_text = _load_vision_skill()
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = (
                str(messages[0].get("content") or "")
                + "\n\n【视觉输入状态】\n"
                + f"本次请求已经实际附带 {image_count} 张图片，不是文字占位。"
                + "你必须先查看图片，再结合文字和前后聊天回复。"
                + "不要在图片已传入时回答‘看不到图片’。图片确实模糊或无法辨认时，只说明具体看不清的部分。\n"
                + skill_text
            )

        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            attached_count = sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")
            if attached_count <= 0:
                continue
            marker = f"【视觉输入】本条消息已附加 {attached_count} 张真实图片。请先观察图片内容，再理解这条消息。\n"
            text_part = next(
                (part for part in content if isinstance(part, dict) and part.get("type") == "text"),
                None,
            )
            if text_part is None:
                content.insert(0, {"type": "text", "text": marker})
            else:
                text_part["text"] = marker + str(text_part.get("text") or "")

        if messages and messages[-1].get("role") == "user" and isinstance(messages[-1].get("content"), str):
            messages[-1]["content"] += " 上下文含真实图片时，必须依据图片中可见的信息回复，不要忽略视觉输入。"
        return messages

    ai_module.build_chat_messages = build_chat_messages_with_vision
    ai_module._vision_prompt_installed = True


def _install_minimax_multimodal_attempt(ai_module: Any) -> None:
    client_cls = ai_module.MinimaxM3Client
    if getattr(client_cls, "_vision_attempt_installed", False):
        return
    original_generate = client_cls.generate_reply

    def generate_reply_with_vision(
        self: Any,
        *,
        session_name: str,
        session_kind: str,
        known_prompt: str,
        selected_skill: str,
        allow_ai_skip: bool,
        context_messages: list[dict[str, Any]],
        allow_image_read_enabled: bool = False,
        allow_sticker_send_enabled: bool = False,
        sticker_options: list[dict[str, str]] | None = None,
    ) -> str:
        images_present = any(item.get("images") for item in context_messages)
        if not allow_image_read_enabled or not images_present:
            return original_generate(
                self,
                session_name=session_name,
                session_kind=session_kind,
                known_prompt=known_prompt,
                selected_skill=selected_skill,
                allow_ai_skip=allow_ai_skip,
                context_messages=context_messages,
                allow_image_read_enabled=allow_image_read_enabled,
                allow_sticker_send_enabled=allow_sticker_send_enabled,
                sticker_options=sticker_options,
            )
        if not self.api_key:
            raise ai_module.AiProviderError("缺少 Minimax API Key")

        messages = ai_module.build_chat_messages(
            session_name,
            session_kind,
            known_prompt,
            selected_skill,
            allow_ai_skip,
            context_messages,
            allow_image_read_enabled=True,
            allow_sticker_send_enabled=allow_sticker_send_enabled,
            sticker_options=sticker_options,
        )
        payload = {
            "model": ai_module.AI_MODEL_MINIMAX_M3,
            "messages": messages,
            "temperature": 0.8,
            "stream": False,
            "max_completion_tokens": 256,
            "thinking": {"type": "disabled"},
            "reasoning_split": False,
        }
        errors: list[str] = []
        for endpoint in self.endpoints:
            try:
                response = self._post_json(endpoint, payload)
                reply = ai_module._extract_reply_text(response)
                cleaned_reply = ai_module._clean_reply(reply)
                if cleaned_reply or ai_module._is_no_reply(reply):
                    return cleaned_reply
                errors.append(f"{endpoint}: 响应中没有可用文本")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{endpoint}: {exc}")

        raise ai_module.AiProviderError(
            "当前 MiniMax 模型或接口未接受图片输入；请确认所选模型具备视觉能力。"
            + "；".join(errors[-2:])
        )

    client_cls.generate_reply = generate_reply_with_vision
    client_cls._vision_attempt_installed = True


def _builder_arg(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str, default: Any) -> Any:
    if name in kwargs:
        return kwargs[name]
    if len(args) > index:
        return args[index]
    return default


def _load_vision_skill() -> str:
    try:
        return VISION_SKILL_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        LOGGER.warning("读取视觉 Skill 失败：%s", exc)
        return ""


def _display_or_original_path(local_path: str) -> str:
    try:
        from .image_layout_patch import _display_image_info

        display_path, _width, _height = _display_image_info(str(Path(local_path).resolve()))
        return display_path
    except Exception:  # noqa: BLE001
        return local_path


def _prepare_vision_data_uri(path: str) -> str | None:
    try:
        source = Path(path).resolve()
        stat = source.stat()
    except OSError:
        return None
    prepared = _prepare_vision_file(str(source), stat.st_mtime_ns, stat.st_size)
    if prepared is None:
        return None
    prepared_path, mime_type = prepared
    try:
        data = Path(prepared_path).read_bytes()
    except OSError:
        return None
    if not data or len(data) > VISION_MAX_ENCODED_BYTES:
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


@lru_cache(maxsize=256)
def _prepare_vision_file(path: str, modified_ns: int, file_size: int) -> tuple[str, str] | None:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        return None

    image = _scale_image(image, VISION_MAX_SIDE)
    prefer_png = image.hasAlphaChannel()
    extension = ".png" if prefer_png else ".jpg"
    mime_type = "image/png" if prefer_png else "image/jpeg"
    signature = f"{VISION_CACHE_VERSION}|{path}|{modified_ns}|{file_size}|{image.width()}x{image.height()}|{extension}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
    VISION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination = VISION_CACHE_DIR / f"{digest}{extension}"

    if not destination.exists():
        quality = -1 if prefer_png else 88
        data = _encode_image(image, "PNG" if prefer_png else "JPEG", quality)
        if data is None:
            return None
        if len(data) > VISION_MAX_ENCODED_BYTES:
            image = _scale_image(image, 1024)
            data = _encode_image(image, "JPEG", 82, flatten_alpha=True)
            destination = VISION_CACHE_DIR / f"{digest}.jpg"
            mime_type = "image/jpeg"
        if data is None or len(data) > VISION_MAX_ENCODED_BYTES:
            return None
        destination.write_bytes(data)
    return str(destination), mime_type


def _scale_image(image: QImage, max_side: int) -> QImage:
    if max(image.width(), image.height()) <= max_side:
        return image
    return image.scaled(
        max_side,
        max_side,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _encode_image(image: QImage, fmt: str, quality: int, flatten_alpha: bool = False) -> bytes | None:
    prepared = image
    if flatten_alpha and image.hasAlphaChannel():
        background = QImage(image.size(), QImage.Format.Format_RGB32)
        background.fill(Qt.GlobalColor.white)
        from PySide6.QtGui import QPainter

        painter = QPainter(background)
        painter.drawImage(0, 0, image)
        painter.end()
        prepared = background

    byte_array = QByteArray()
    buffer = QBuffer(byte_array)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return None
    try:
        if not prepared.save(buffer, fmt, quality):
            return None
        return bytes(byte_array)
    finally:
        buffer.close()
