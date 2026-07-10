from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QDialog


def install_automation_behavior_fixes(automation_module: Any) -> None:
    """Fix scheduled-task delivery deletion control and empty-message runs."""

    _install_delete_after_send_control(automation_module)
    _install_empty_message_execution(automation_module)


def _install_delete_after_send_control(automation_module: Any) -> None:
    dialog_cls = automation_module.AutomationTaskEditDialog
    if getattr(dialog_cls, "_automation_delete_option_fix_installed", False):
        return

    original_init = dialog_cls.__init__
    original_sync = dialog_cls._sync_controls
    original_validate = dialog_cls._validate_and_accept

    def init_with_delete_option(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        checkbox = getattr(self, "delete_after_send_input", None)
        if checkbox is not None:
            checkbox.setChecked(bool(getattr(self.task, "delete_after_send", True)))
        self._sync_controls()

    def sync_with_delete_option(self: Any) -> None:
        original_sync(self)
        checkbox = getattr(self, "delete_after_send_input", None)
        file_toggle = getattr(self, "file_enabled_input", None)
        delivery_toggle = getattr(self, "delivery_enabled_input", None)
        if checkbox is None or file_toggle is None or delivery_toggle is None:
            return
        checkbox.setEnabled(file_toggle.isChecked() and delivery_toggle.isChecked())

    def validate_with_delete_option(self: Any) -> None:
        checkbox = getattr(self, "delete_after_send_input", None)
        desired = bool(checkbox.isChecked()) if checkbox is not None else bool(
            getattr(self.task, "delete_after_send", True)
        )
        original_validate(self)
        if self.result() == int(QDialog.DialogCode.Accepted):
            # The base dialog historically hard-coded this field to True.
            # Override it only after validation succeeds so the user's choice
            # is what the task manager persists.
            self.task.delete_after_send = desired

    dialog_cls.__init__ = init_with_delete_option
    dialog_cls._sync_controls = sync_with_delete_option
    dialog_cls._validate_and_accept = validate_with_delete_option
    dialog_cls._automation_delete_option_fix_installed = True


def _install_empty_message_execution(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_empty_message_execution_installed", False):
        return

    original_handler = automation_module._handle_automation_payload

    def handle_with_empty_message_execution(
        window: Any,
        ui_module: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict) or not payload.get("automation_history"):
            original_handler(window, ui_module, ai_module, payload)
            return

        request_id = str(payload.get("request_id") or "")
        context = getattr(window, "automation_pending", {}).get(request_id)
        if context is None or bool(getattr(context, "delivery", False)) or payload.get("error"):
            original_handler(window, ui_module, ai_module, payload)
            return

        task = automation_module.task_by_id(
            getattr(window, "automation_tasks", []),
            str(getattr(context, "task_id", "") or ""),
        )
        if task is None:
            original_handler(window, ui_module, ai_module, payload)
            return

        fetched = [
            message
            for message in payload.get("messages", [])
            if isinstance(message, automation_module.ChatMessage)
        ]
        current = [
            message
            for message in getattr(window, "messages", {}).get(task.target_session_id, [])
            if isinstance(message, automation_module.ChatMessage)
        ]
        merged: dict[str, Any] = {}
        for message in [*fetched, *current]:
            merged[automation_module.message_key(message)] = message
        candidates = [
            message
            for message in sorted(merged.values(), key=lambda item: item.timestamp)
            if context.checkpoint <= message.timestamp <= context.cutoff
        ]
        keys = [automation_module.message_key(message) for message in candidates]
        processed = window.automation_state.processed_keys(task.task_id, keys)
        unprocessed = [
            message
            for message in candidates
            if automation_module.message_key(message) not in processed
        ]
        if unprocessed:
            original_handler(window, ui_module, ai_module, payload)
            return

        # Consume the pending history request exactly as the base handler does,
        # but do not short-circuit merely because the transcript is empty.
        window.automation_pending.pop(request_id, None)
        window.append_log(
            f"定时任务“{task.name}”本轮没有新消息，仍按计划调用 AI 执行指令"
        )
        config = ui_module.load_ai_config(window.settings).normalized()

        def worker() -> None:
            try:
                work_date = automation_module.task_work_date(context.cutoff, context.delivery)
                path = (
                    automation_module.artifact_path(task, work_date)
                    if task.file_enabled
                    else None
                )
                existing = automation_module.load_records(path) if path is not None else []
                result = automation_module.generate_scheduled_result(
                    ai_module,
                    config,
                    task,
                    [],
                    automation_module.records_for_ai(existing),
                    checkpoint_time=context.checkpoint,
                    cutoff_time=context.cutoff,
                )
                stats = {"inserted": 0, "updated": 0, "ignored": 0}
                if task.file_enabled:
                    existing, stats = automation_module.apply_operations(
                        task,
                        existing,
                        result.operations,
                    )
                    path = automation_module.write_artifact(task, work_date, existing)
                window.automation_bridge.ready.emit(
                    {
                        "context": context,
                        "task_id": task.task_id,
                        "messages": [],
                        "message_keys": [],
                        "checkpoint_message_id": "",
                        "text": result.text,
                        "stats": stats,
                        "path": str(Path(path)) if path is not None else "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                window.automation_bridge.failed.emit(
                    {
                        "context": context,
                        "task_id": task.task_id,
                        "error": str(exc),
                    }
                )

        threading.Thread(target=worker, daemon=True).start()

    automation_module._handle_automation_payload = handle_with_empty_message_execution
    automation_module._automation_empty_message_execution_installed = True
