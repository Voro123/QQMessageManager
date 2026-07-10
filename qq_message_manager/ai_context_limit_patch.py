from __future__ import annotations

from typing import Any

AI_CONTEXT_MESSAGE_LIMIT = 999


def install_ai_context_message_limit(ui_module: Any) -> None:
    """将 AI 设置中的上下文参考消息数上限提升到 999。"""
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_ai_context_message_limit_installed", False):
        return

    original_init = dialog_cls.__init__

    def init_with_context_limit(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        context_count = getattr(self, "context_count", None)
        if context_count is not None:
            context_count.setMaximum(AI_CONTEXT_MESSAGE_LIMIT)

    dialog_cls.__init__ = init_with_context_limit
    dialog_cls._ai_context_message_limit_installed = True
