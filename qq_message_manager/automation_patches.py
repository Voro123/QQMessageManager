from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer

from .automation_models import SCHEDULED_FILE_SKILL_ID, task_by_id
from .models import ChatMessage


def install_automation_patches(
    automation_module: Any,
    skill_library_module: Any,
    ui_module: Any,
) -> None:
    _hide_scheduled_file_skill_from_chat_library(skill_library_module)
    _execute_empty_interval_tasks(automation_module)
    _retry_login_info_after_connect(ui_module)


def _hide_scheduled_file_skill_from_chat_library(skill_library_module: Any) -> None:
    if getattr(skill_library_module, "_scheduled_skill_hidden", False):
        return
    original_available = skill_library_module.available_skills

    def available_without_scheduled_files(ai_module: Any) -> list[Any]:
        return [
            definition
            for definition in original_available(ai_module)
            if getattr(definition, "skill_id", "") != SCHEDULED_FILE_SKILL_ID
        ]

    skill_library_module.available_skills = available_without_scheduled_files
    skill_library_module._scheduled_skill_hidden = True


def _execute_empty_interval_tasks(automation_module: Any) -> None:
    if getattr(automation_module, "_empty_task_execution_installed", False):
        return
    original_handler = automation_module._handle_automation_payload

    def handle_with_empty_trigger(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        if (
            isinstance(payload, dict)
            and payload.get("automation_history")
            and not payload.get("error")
            and not payload.get("messages")
        ):
            request_id = str(payload.get("request_id") or "")
            context = getattr(window, "automation_pending", {}).get(request_id)
            task = task_by_id(getattr(window, "automation_tasks", []), context.task_id) if context is not None else None
            if context is not None and task is not None:
                synthetic = ChatMessage(
                    session_id=task.target_session_id,
                    session_name=task.target_session_name or task.target_session_id,
                    session_kind="group" if task.target_session_id.startswith("group:") else "private",
                    sender_id="scheduled_trigger",
                    sender_name="定时任务触发器",
                    text="[本轮时间范围内没有新的聊天消息，请仍按定时任务指令执行；不得把本句写入业务记录。]",
                    timestamp=context.cutoff,
                    message_id=f"scheduled_trigger:{request_id}",
                    historical=True,
                )
                payload = dict(payload)
                payload["messages"] = [synthetic]
        original_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_with_empty_trigger
    automation_module._empty_task_execution_installed = True


def _retry_login_info_after_connect(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_login_retry_installed", False):
        return
    original_start = main_window_cls.start

    def start_with_login_retry(self: Any) -> None:
        original_start(self)

        def request_again() -> None:
            client = getattr(self, "client_thread", None)
            if client is not None and not getattr(self, "automation_self_qq", ""):
                client.request_automation_login_info()

        QTimer.singleShot(2500, request_again)
        QTimer.singleShot(8000, request_again)

    main_window_cls.start = start_with_login_retry
    main_window_cls._automation_login_retry_installed = True
