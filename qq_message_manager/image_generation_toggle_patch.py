from __future__ import annotations

import random
import re
import threading
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QFormLayout, QGroupBox

IMAGE_GENERATION_SETTINGS_KEY = "ai/image_generation_enabled"

NEGATIVE_REQUEST_RE = re.compile(
    r"(?:不要|不用|别|禁止|无需)\s*(?:生成|画|绘制|创作|制作|做|出)",
    re.IGNORECASE,
)
DRAW_REQUEST_RE = re.compile(
    r"(?:帮我|给我|请|麻烦|能不能|可以)?\s*"
    r"(?:画|绘制|画出|绘出)\s*(?:一(?:张|幅|个|只|位)?|个|张|幅|只|位)?\s*\S+",
    re.IGNORECASE,
)
GENERATION_MEDIA_RE = re.compile(
    r"(?:生成|创作|制作|做|出)\s*.{0,80}?"
    r"(?:图片|图像|图|插画|海报|头像|壁纸|表情包|照片|画面|封面|立绘)",
    re.IGNORECASE,
)
MEDIA_GENERATION_RE = re.compile(
    r"(?:图片|图像|插画|海报|头像|壁纸|表情包|照片|画面|封面|立绘)"
    r".{0,30}?(?:生成|创作|制作|画|绘制)",
    re.IGNORECASE,
)
POLITE_PREFIX_RE = re.compile(r"^(?:帮我|给我|请|麻烦|能不能|可以|请你|麻烦你)\s*", re.IGNORECASE)
LEADING_ACTION_RE = re.compile(
    r"^(?:生成|创作|制作|做|画出|绘出|画|绘制|出)\s*"
    r"(?:一(?:张|幅|个|只|位)?|一个|一幅|一张|个|张|幅|只|位|一下)?\s*",
    re.IGNORECASE,
)
TRAILING_MEDIA_RE = re.compile(
    r"\s*(?:图片|图像|插画|海报|头像|壁纸|表情包|照片|画面|封面|立绘)\s*$",
    re.IGNORECASE,
)


def install_image_generation_toggle(ui_module: Any, generation_module: Any, ai_module: Any) -> None:
    """给图片生成 Skill 增加默认关闭的开关，并允许非 @ 的明确画图请求触发。"""
    _install_setting(ui_module)
    _install_trigger(ui_module, generation_module, ai_module)


def _install_setting(ui_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_image_generation_toggle_installed", False):
        return

    original_init = dialog_cls.__init__
    original_accept = dialog_cls.accept

    def init_with_image_generation_toggle(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.image_generation_enabled = QCheckBox("允许生成图片")
        self.image_generation_enabled.setChecked(
            _setting_bool(self.settings, IMAGE_GENERATION_SETTINGS_KEY, False)
        )
        self.image_generation_enabled.setToolTip(
            "默认关闭。开启后，当前已代管会话中的明确画图请求可直接触发图片生成，"
            "不再要求必须 @ 机器人；当前模型和接口必须支持图片生成。"
        )

        media_form = _find_group_form(self, "图片与表情包")
        if media_form is not None:
            media_form.addRow(self.image_generation_enabled)
        else:
            fallback = _find_first_form(self)
            if fallback is not None:
                fallback.addRow("图片生成", self.image_generation_enabled)

    def accept_with_image_generation_toggle(self: Any) -> None:
        checkbox = getattr(self, "image_generation_enabled", None)
        if checkbox is not None:
            self.settings.setValue(IMAGE_GENERATION_SETTINGS_KEY, checkbox.isChecked())
            self.settings.sync()
        original_accept(self)

    dialog_cls.__init__ = init_with_image_generation_toggle
    dialog_cls.accept = accept_with_image_generation_toggle
    dialog_cls._image_generation_toggle_installed = True


def _install_trigger(ui_module: Any, generation_module: Any, ai_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_image_generation_toggle_trigger_installed", False):
        return

    original_init = main_window_cls.__init__
    current_mention_handler = main_window_cls._maybe_schedule_mention_reply
    current_schedule_handler = main_window_cls._schedule_after_non_self_message_ai_reply

    def init_with_generation_consumed_state(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.image_generation_consumed_sessions: set[str] = set()

    def handle_message_for_image_generation(self: Any, message: Any) -> None:
        if not _looks_like_image_request(message):
            current_mention_handler(self, message)
            return

        enabled = _setting_bool(self.settings, IMAGE_GENERATION_SETTINGS_KEY, False)
        if not enabled:
            _schedule_normal_mention_reply(self, ui_module, message)
            return

        session_id = message.session_id
        if session_id not in self.ai_managed_sessions:
            _schedule_normal_mention_reply(self, ui_module, message)
            return

        # add_message 随后还会调用普通新消息调度；记录本次会话用于阻止重复文本回复。
        self.image_generation_consumed_sessions.add(session_id)
        prompt = _extract_image_prompt(message)
        if not prompt:
            generation_module._send_requester_text(  # noqa: SLF001
                self,
                session_id,
                message.sender_id,
                "请描述要生成的图片内容。",
            )
            return

        inflight = getattr(self, "image_generation_inflight_sessions", set())
        if session_id in inflight:
            generation_module._send_requester_text(  # noqa: SLF001
                self,
                session_id,
                message.sender_id,
                "当前会话已有图片正在生成，请稍等。",
            )
            return

        config = ui_module.load_ai_config(self.settings).normalized()
        try:
            backend = generation_module._resolve_backend(config, ai_module)  # noqa: SLF001
        except generation_module.ImageGenerationConfigurationError as exc:
            generation_module._send_requester_text(  # noqa: SLF001
                self,
                session_id,
                message.sender_id,
                str(exc),
            )
            self.append_log(f"图片生成 Skill 未执行：{exc}")
            return
        except generation_module.UnsupportedImageModel:
            model_name = generation_module._current_model_name(config, ai_module)  # noqa: SLF001
            generation_module._send_requester_text(  # noqa: SLF001
                self,
                session_id,
                message.sender_id,
                f"当前模型（{model_name}）不支持生成图片。",
            )
            self.append_log(f"图片生成 Skill 未执行：当前模型 {model_name} 不支持图片生成")
            return

        inflight.add(session_id)
        self.image_generation_inflight_sessions = inflight
        self.append_log(
            f"已触发图片生成 Skill：会话 {session_id}，模型 {backend.model}，等待生成结果"
        )

        def worker() -> None:
            try:
                image_path = generation_module.generate_image(backend, prompt)
                self.image_generation_bridge.generated.emit(
                    session_id,
                    message.sender_id,
                    image_path,
                    prompt,
                )
            except generation_module.UnsupportedImageModel:
                self.image_generation_bridge.failed.emit(
                    session_id,
                    message.sender_id,
                    f"UNSUPPORTED:{backend.model}",
                )
            except Exception as exc:  # noqa: BLE001
                self.image_generation_bridge.failed.emit(session_id, message.sender_id, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def schedule_without_generation_duplicate(self: Any, session_id: str) -> None:
        consumed = getattr(self, "image_generation_consumed_sessions", set())
        if session_id in consumed:
            consumed.discard(session_id)
            self._stop_ai_timer(session_id)
            return
        current_schedule_handler(self, session_id)

    main_window_cls.__init__ = init_with_generation_consumed_state
    main_window_cls._maybe_schedule_mention_reply = handle_message_for_image_generation
    main_window_cls._schedule_after_non_self_message_ai_reply = schedule_without_generation_duplicate
    main_window_cls._image_generation_toggle_trigger_installed = True


def _schedule_normal_mention_reply(window: Any, ui_module: Any, message: Any) -> None:
    session_id = message.session_id
    if session_id not in window.ai_managed_sessions:
        return
    config = ui_module.load_ai_config(window.settings).normalized()
    if not config.mention_enabled or not window._message_mentions_self(message):
        return
    delay_ms = random.randint(config.mention_min_seconds, config.mention_max_seconds) * 1000
    QTimer.singleShot(delay_ms, lambda sid=session_id: window._request_ai_reply(sid, "被艾特"))


def _looks_like_image_request(message: Any) -> bool:
    text = _request_text(message)
    if not text or NEGATIVE_REQUEST_RE.search(text):
        return False
    return bool(
        DRAW_REQUEST_RE.search(text)
        or GENERATION_MEDIA_RE.search(text)
        or MEDIA_GENERATION_RE.search(text)
    )


def _extract_image_prompt(message: Any) -> str:
    text = _request_text(message)
    text = POLITE_PREFIX_RE.sub("", text, count=1)
    text = LEADING_ACTION_RE.sub("", text, count=1)
    text = TRAILING_MEDIA_RE.sub("", text, count=1)
    text = text.strip(" ：:，,。.!！?？\t\r\n")
    return text[:4000]


def _request_text(message: Any) -> str:
    event = message.raw_event or {}
    segments = event.get("message")
    if isinstance(segments, list):
        parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data") or {}
            parts.append(str(data.get("text") or ""))
        text = "".join(parts)
    else:
        text = str(event.get("raw_message") or event.get("rawMessage") or message.text or "")
        text = re.sub(r"\[CQ:at,[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"@(?:all|\d+|我)\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _find_group_form(dialog: Any, title: str) -> QFormLayout | None:
    for group in dialog.findChildren(QGroupBox):
        if group.title() != title:
            continue
        layout = group.layout()
        if isinstance(layout, QFormLayout):
            return layout
    return None


def _find_first_form(dialog: Any) -> QFormLayout | None:
    for form in dialog.findChildren(QFormLayout):
        return form
    return None


def _setting_bool(settings: Any, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
