from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QWidget,
)

MIN_SPEECH_INTERVAL_ENABLED_KEY = "ai/min_speech_interval_enabled"
MIN_SPEECH_INTERVAL_SECONDS_KEY = "ai/min_speech_interval_seconds"
DEFAULT_MIN_SPEECH_INTERVAL_SECONDS = 60
MAX_MIN_SPEECH_INTERVAL_SECONDS = 24 * 60 * 60


def install_ai_min_speech_interval(
    ui_module: Any,
    typing_delay_module: Any,
    image_generation_module: Any,
    summary_skill_module: Any,
) -> None:
    """增加按会话生效的 AI 发言最小间隔，并覆盖所有 AI 自动发送路径。"""
    _install_setting(ui_module)
    _install_window_state_and_guards(ui_module)
    _install_typing_payload_guard(typing_delay_module)
    _install_image_generation_guards(image_generation_module)
    _install_summary_guards(summary_skill_module)


def _install_setting(ui_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_min_speech_interval_setting_installed", False):
        return

    original_init = dialog_cls.__init__
    original_accept = dialog_cls.accept

    def init_with_min_speech_interval(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.min_speech_interval_enabled = QCheckBox("启用发言最小间隔")
        self.min_speech_interval_enabled.setChecked(
            _setting_bool(self.settings, MIN_SPEECH_INTERVAL_ENABLED_KEY, False)
        )
        self.min_speech_interval_enabled.setToolTip(
            "默认关闭。开启后，每个群聊/私聊分别计算：AI 上次实际发言后的指定时间内，"
            "普通回复、表情包、图片生成结果和聊天总结都不会再次发送；手动发送不计入。"
        )

        self.min_speech_interval_seconds = QSpinBox()
        self.min_speech_interval_seconds.setRange(1, MAX_MIN_SPEECH_INTERVAL_SECONDS)
        self.min_speech_interval_seconds.setValue(
            _setting_int(
                self.settings,
                MIN_SPEECH_INTERVAL_SECONDS_KEY,
                DEFAULT_MIN_SPEECH_INTERVAL_SECONDS,
            )
        )
        self.min_speech_interval_seconds.setToolTip("同一会话中两次 AI 发言之间至少等待的秒数。")

        seconds_row = QWidget(self)
        seconds_layout = QHBoxLayout(seconds_row)
        seconds_layout.setContentsMargins(0, 0, 0, 0)
        seconds_layout.addWidget(self.min_speech_interval_seconds)
        seconds_layout.addWidget(QLabel("秒"))
        seconds_layout.addStretch(1)

        form = getattr(self, "behavior_form", None)
        if not isinstance(form, QFormLayout):
            form = _find_group_form(self, "回复前检查") or _find_first_form(self)
        if form is not None:
            form.addRow(self.min_speech_interval_enabled)
            form.addRow("最小发言间隔", seconds_row)

        self.min_speech_interval_enabled.toggled.connect(
            self.min_speech_interval_seconds.setEnabled
        )
        self.min_speech_interval_seconds.setEnabled(
            self.min_speech_interval_enabled.isChecked()
        )

    def accept_with_min_speech_interval(self: Any) -> None:
        checkbox = getattr(self, "min_speech_interval_enabled", None)
        seconds = getattr(self, "min_speech_interval_seconds", None)
        if checkbox is not None and seconds is not None:
            self.settings.setValue(
                MIN_SPEECH_INTERVAL_ENABLED_KEY,
                checkbox.isChecked(),
            )
            self.settings.setValue(
                MIN_SPEECH_INTERVAL_SECONDS_KEY,
                seconds.value(),
            )
            self.settings.sync()
        original_accept(self)

    dialog_cls.__init__ = init_with_min_speech_interval
    dialog_cls.accept = accept_with_min_speech_interval
    dialog_cls._min_speech_interval_setting_installed = True


def _install_window_state_and_guards(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_min_speech_interval_guard_installed", False):
        return

    original_init = main_window_cls.__init__
    original_add_message = main_window_cls.add_message
    current_mention_handler = main_window_cls._maybe_schedule_mention_reply
    current_schedule_handler = main_window_cls._schedule_after_non_self_message_ai_reply
    current_request_ai_reply = main_window_cls._request_ai_reply
    current_disconnect = main_window_cls.disconnect_from_server

    def init_with_min_speech_interval_state(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.ai_last_spoken_monotonic: dict[str, float] = {}

    def add_message_with_ai_speech_tracking(self: Any, message: Any) -> None:
        original_add_message(self, message)
        if (
            getattr(message, "outgoing", False)
            and str(getattr(message, "sender_name", "")) == "AI代管"
            and str(getattr(message, "session_id", ""))
        ):
            self._mark_ai_spoken(str(message.session_id))

    def min_interval_seconds(self: Any) -> int:
        return max(
            1,
            _setting_int(
                self.settings,
                MIN_SPEECH_INTERVAL_SECONDS_KEY,
                DEFAULT_MIN_SPEECH_INTERVAL_SECONDS,
            ),
        )

    def min_interval_remaining(self: Any, session_id: str) -> int:
        if not _setting_bool(self.settings, MIN_SPEECH_INTERVAL_ENABLED_KEY, False):
            return 0
        last_spoken = getattr(self, "ai_last_spoken_monotonic", {}).get(session_id)
        if last_spoken is None:
            return 0
        remaining = self._ai_min_interval_seconds() - (time.monotonic() - last_spoken)
        return max(0, math.ceil(remaining))

    def can_ai_speak(self: Any, session_id: str, source: str = "AI 回复", *, log: bool = True) -> bool:
        remaining = self._ai_min_interval_remaining(session_id)
        if remaining <= 0:
            return True
        if log:
            self.append_log(f"{source}已跳过：发言最小间隔尚余 {remaining} 秒")
        return False

    def mark_ai_spoken(self: Any, session_id: str) -> None:
        if not session_id:
            return
        self.ai_last_spoken_monotonic[session_id] = time.monotonic()

    def mention_handler_with_min_interval(self: Any, message: Any) -> None:
        session_id = str(getattr(message, "session_id", ""))
        if (
            session_id in self.ai_managed_sessions
            and not self._ai_min_interval_can_speak(session_id, "AI 自动响应")
        ):
            return
        current_mention_handler(self, message)

    def schedule_with_min_interval(self: Any, session_id: str) -> None:
        if (
            session_id in self.ai_managed_sessions
            and not self._ai_min_interval_can_speak(
                session_id,
                "AI 自动回复",
                log=False,
            )
        ):
            self._stop_ai_timer(session_id)
            return
        current_schedule_handler(self, session_id)

    def request_ai_reply_with_min_interval(self: Any, session_id: str, reason: str) -> None:
        if (
            session_id in self.ai_managed_sessions
            and not self._ai_min_interval_can_speak(session_id, f"AI 代管：{reason}")
        ):
            return
        current_request_ai_reply(self, session_id, reason)

    def disconnect_with_min_interval_clear(self: Any) -> None:
        getattr(self, "ai_last_spoken_monotonic", {}).clear()
        current_disconnect(self)

    main_window_cls.__init__ = init_with_min_speech_interval_state
    main_window_cls.add_message = add_message_with_ai_speech_tracking
    main_window_cls._ai_min_interval_seconds = min_interval_seconds
    main_window_cls._ai_min_interval_remaining = min_interval_remaining
    main_window_cls._ai_min_interval_can_speak = can_ai_speak
    main_window_cls._mark_ai_spoken = mark_ai_spoken
    main_window_cls._maybe_schedule_mention_reply = mention_handler_with_min_interval
    main_window_cls._schedule_after_non_self_message_ai_reply = schedule_with_min_interval
    main_window_cls._request_ai_reply = request_ai_reply_with_min_interval
    main_window_cls.disconnect_from_server = disconnect_with_min_interval_clear
    main_window_cls._min_speech_interval_guard_installed = True


def _install_typing_payload_guard(typing_delay_module: Any) -> None:
    if getattr(typing_delay_module, "_min_speech_interval_payload_guard_installed", False):
        return
    original_send_payload = typing_delay_module._send_ai_reply_payload

    def send_payload_with_min_interval(
        window: Any,
        ui_module: Any,
        session_id: str,
        reply_text: str,
        sticker_id: str,
        sticker_code: str,
    ) -> None:
        if not window._ai_min_interval_can_speak(session_id, "AI 回复发送"):
            window.ai_inflight_sessions.discard(session_id)
            return
        original_send_payload(
            window,
            ui_module,
            session_id,
            reply_text,
            sticker_id,
            sticker_code,
        )

    typing_delay_module._send_ai_reply_payload = send_payload_with_min_interval
    typing_delay_module._min_speech_interval_payload_guard_installed = True


def _install_image_generation_guards(image_generation_module: Any) -> None:
    if getattr(image_generation_module, "_min_speech_interval_guards_installed", False):
        return
    original_handle_generated_image = image_generation_module._handle_generated_image
    original_send_requester_text = image_generation_module._send_requester_text

    def handle_generated_image_with_min_interval(
        window: Any,
        session_id: str,
        sender_id: str,
        image_path: str,
        prompt: str,
    ) -> None:
        if not window._ai_min_interval_can_speak(session_id, "图片生成结果"):
            getattr(window, "image_generation_inflight_sessions", set()).discard(session_id)
            try:
                Path(image_path).unlink(missing_ok=True)
            except OSError:
                pass
            return
        original_handle_generated_image(window, session_id, sender_id, image_path, prompt)

    def send_requester_text_with_min_interval(
        window: Any,
        session_id: str,
        sender_id: str,
        text: str,
    ) -> None:
        if not window._ai_min_interval_can_speak(session_id, "图片生成提示"):
            return
        original_send_requester_text(window, session_id, sender_id, text)

    image_generation_module._handle_generated_image = handle_generated_image_with_min_interval
    image_generation_module._send_requester_text = send_requester_text_with_min_interval
    image_generation_module._min_speech_interval_guards_installed = True


def _install_summary_guards(summary_skill_module: Any) -> None:
    if getattr(summary_skill_module, "_min_speech_interval_guards_installed", False):
        return
    original_start_summary = summary_skill_module._start_summary_request
    original_deliver_summary = summary_skill_module._deliver_summary
    original_deliver_error = summary_skill_module._deliver_summary_error

    def start_summary_with_min_interval(
        window: Any,
        ui_module: Any,
        summary_module: Any,
        **kwargs: Any,
    ) -> None:
        session_id = str(kwargs.get("session_id") or "")
        delivery = kwargs.get("delivery")
        if not window._ai_min_interval_can_speak(session_id, "聊天总结 Skill"):
            if getattr(delivery, "source", "") == "button":
                remaining = window._ai_min_interval_remaining(session_id)
                QMessageBox.information(
                    window,
                    "发言间隔限制",
                    f"AI 在当前会话还需等待约 {remaining} 秒才能再次发言。",
                )
            return
        original_start_summary(window, ui_module, summary_module, **kwargs)

    def deliver_summary_with_min_interval(
        window: Any,
        session_id: str,
        title: str,
        summary: str,
        count: int,
        people_label: str,
        source: str,
    ) -> None:
        if not window._ai_min_interval_can_speak(session_id, "聊天总结发送"):
            return
        original_deliver_summary(
            window,
            session_id,
            title,
            summary,
            count,
            people_label,
            source,
        )

    def deliver_error_with_min_interval(
        window: Any,
        session_id: str,
        title: str,
        error: str,
        source: str,
    ) -> None:
        if source != "button" and not window._ai_min_interval_can_speak(
            session_id,
            "聊天总结错误提示",
        ):
            return
        original_deliver_error(window, session_id, title, error, source)

    summary_skill_module._start_summary_request = start_summary_with_min_interval
    summary_skill_module._deliver_summary = deliver_summary_with_min_interval
    summary_skill_module._deliver_summary_error = deliver_error_with_min_interval
    summary_skill_module._min_speech_interval_guards_installed = True


def _find_group_form(dialog: Any, title: str) -> QFormLayout | None:
    for group in dialog.findChildren(QGroupBox):
        if group.title() != title:
            continue
        layout = group.layout()
        if isinstance(layout, QFormLayout):
            return layout
    return None


def _find_first_form(dialog: Any) -> QFormLayout | None:
    forms = dialog.findChildren(QFormLayout)
    return forms[0] if forms else None


def _setting_bool(settings: Any, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(settings: Any, key: str, default: int) -> int:
    value = settings.value(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
