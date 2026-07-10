from __future__ import annotations

from typing import Any

from .models import ChatMessage


def install_sticker_current_session_guard(ui_module: Any) -> None:
    """Only remember stickers carried by messages in the active conversation.

    ``MainWindow.add_message`` still handles every realtime message so background
    sessions can update unread counts and previews.  For a background session we
    temporarily replace this window's sticker-memory entry point with a no-op;
    the rest of the message handling path is left unchanged.
    """

    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_sticker_current_session_guard_installed", False):
        return

    original_add_message = main_window_cls.add_message

    def add_message_with_sticker_scope(self: Any, message: Any) -> None:
        if _should_remember_from_message(
            str(getattr(self, "current_session_id", "") or ""),
            message,
        ):
            original_add_message(self, message)
            return

        # Historical/outgoing/non-chat messages are already ignored by the base
        # handler.  Only suppress the memory call for a live background message.
        if not _is_live_incoming_chat_message(message):
            original_add_message(self, message)
            return

        memory = getattr(self, "sticker_memory", None)
        original_remember = getattr(memory, "remember_from_event", None)
        if memory is None or not callable(original_remember):
            original_add_message(self, message)
            return

        memory.remember_from_event = lambda _event: 0
        try:
            original_add_message(self, message)
        finally:
            memory.remember_from_event = original_remember

    main_window_cls.add_message = add_message_with_sticker_scope
    main_window_cls._sticker_current_session_guard_installed = True


def _should_remember_from_message(current_session_id: str, message: Any) -> bool:
    return (
        _is_live_incoming_chat_message(message)
        and bool(current_session_id)
        and current_session_id == str(message.session_id)
    )


def _is_live_incoming_chat_message(message: Any) -> bool:
    return (
        isinstance(message, ChatMessage)
        and message.session_kind in {"group", "private"}
        and not message.historical
        and not message.outgoing
    )
