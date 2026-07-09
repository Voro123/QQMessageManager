from __future__ import annotations

import math
import random
from typing import Any

from PySide6.QtCore import QTimer

from .models import ChatMessage
from .sticker_memory import parse_sticker_marker

MIN_DELAY_CHARS = 18
MS_PER_CHAR = 110
MIN_DELAY_MS = 1200
MAX_DELAY_MS = 18000
RANDOM_JITTER_MS = (300, 1200)


def install_ai_typing_delay(ui_module: Any) -> None:
    """给 MainWindow 安装 AI 回复后的模拟打字延迟。"""
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_ai_typing_delay_installed", False):
        return

    original_disconnect = main_window_cls.disconnect_from_server
    original_clear_timers = main_window_cls._clear_all_ai_timers

    def disconnect_with_pending_clear(self: Any) -> None:
        _clear_pending_ai_send_timers(self)
        original_disconnect(self)

    def clear_timers_with_pending_clear(self: Any) -> None:
        _clear_pending_ai_send_timers(self)
        original_clear_timers(self)

    def handle_ai_reply_ready(self: Any, session_id: str, reply: str) -> None:
        config = ui_module.load_ai_config(self.settings).normalized()
        reply_text, sticker_id = parse_sticker_marker(reply)
        sticker_code = ""
        if sticker_id and config.allow_sticker_send_enabled:
            record = self.sticker_memory.get(sticker_id)
            if record is None:
                self.append_log(f"AI 代管跳过表情包：未找到 {sticker_id}")
            else:
                sticker_code = record.to_cq_code()
                if not sticker_code:
                    self.append_log(f"AI 代管跳过表情包：{sticker_id} 缺少可发送字段")
        elif sticker_id:
            self.append_log("AI 代管忽略表情包标识：表情包发送未开启")

        reply_text = reply_text.strip()
        if not reply_text and not sticker_code:
            self.ai_inflight_sessions.discard(session_id)
            if config.allow_ai_skip_enabled:
                self.append_log("AI 代管判断本次不需要回复")
            return

        delay_ms = _ai_typing_delay_ms(reply_text)
        if delay_ms <= 0:
            _send_ai_reply_payload(self, ui_module, session_id, reply_text, sticker_id, sticker_code)
            return

        pending = _pending_ai_send_timers(self)
        old_timer = pending.pop(session_id, None)
        if old_timer is not None:
            old_timer.stop()
            old_timer.deleteLater()

        timer = QTimer(self)
        timer.setSingleShot(True)
        pending[session_id] = timer
        delay_seconds = math.ceil(delay_ms / 1000)
        self.append_log(f"AI 代管已生成回复，模拟打字 {delay_seconds} 秒后发送")
        timer.timeout.connect(lambda sid=session_id: _send_delayed_ai_reply(self, ui_module, sid, reply_text, sticker_id, sticker_code))
        timer.start(delay_ms)

    main_window_cls.disconnect_from_server = disconnect_with_pending_clear
    main_window_cls._clear_all_ai_timers = clear_timers_with_pending_clear
    main_window_cls._handle_ai_reply_ready = handle_ai_reply_ready
    main_window_cls._ai_typing_delay_installed = True


def _send_delayed_ai_reply(
    window: Any,
    ui_module: Any,
    session_id: str,
    reply_text: str,
    sticker_id: str,
    sticker_code: str,
) -> None:
    pending = _pending_ai_send_timers(window)
    timer = pending.pop(session_id, None)
    if timer is not None:
        timer.deleteLater()
    _send_ai_reply_payload(window, ui_module, session_id, reply_text, sticker_id, sticker_code)


def _send_ai_reply_payload(
    window: Any,
    ui_module: Any,
    session_id: str,
    reply_text: str,
    sticker_id: str,
    sticker_code: str,
) -> None:
    try:
        if session_id not in window.ai_managed_sessions:
            return
        config = ui_module.load_ai_config(window.settings).normalized()
        if config.prevent_self_follow_enabled and window._last_speaker_is_self(session_id):
            window.append_log("AI 代管跳过发送：上一条发言人是自己")
            return
        session = window.sessions.get(session_id)
        if session is None or window.client_thread is None:
            return

        if reply_text:
            window.client_thread.send_text(session_id, reply_text)
            window.add_message(
                ChatMessage(
                    session_id=session.session_id,
                    session_name=session.name,
                    session_kind=session.kind,
                    sender_id="self",
                    sender_name="AI代管",
                    text=reply_text,
                    outgoing=True,
                )
            )
        if sticker_code:
            window.client_thread.send_text(session_id, sticker_code)
            window.sticker_memory.mark_used(sticker_id)
            window.add_message(
                ChatMessage(
                    session_id=session.session_id,
                    session_name=session.name,
                    session_kind=session.kind,
                    sender_id="self",
                    sender_name="AI代管",
                    text="[表情包]",
                    outgoing=True,
                )
            )
    finally:
        window.ai_inflight_sessions.discard(session_id)


def _ai_typing_delay_ms(text: str) -> int:
    count = sum(1 for char in text.strip() if not char.isspace())
    if count <= MIN_DELAY_CHARS:
        return 0
    delay = count * MS_PER_CHAR + random.randint(*RANDOM_JITTER_MS)
    delay = max(MIN_DELAY_MS, delay)
    return min(MAX_DELAY_MS, delay)


def _pending_ai_send_timers(window: Any) -> dict[str, QTimer]:
    pending = getattr(window, "ai_pending_send_timers", None)
    if pending is None:
        pending = {}
        setattr(window, "ai_pending_send_timers", pending)
    return pending


def _clear_pending_ai_send_timers(window: Any) -> None:
    pending = getattr(window, "ai_pending_send_timers", None)
    if not pending:
        return
    inflight = getattr(window, "ai_inflight_sessions", set())
    for session_id, timer in list(pending.items()):
        timer.stop()
        timer.deleteLater()
        inflight.discard(session_id)
    pending.clear()
