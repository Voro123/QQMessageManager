from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
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
    _deduplicate_inserts_by_source_message(automation_module)
    _execute_empty_interval_tasks(automation_module)
    _retry_upload_without_reprocessing(automation_module)
    _guard_results_after_disconnect(automation_module)
    _recover_interrupted_tasks(automation_module, ui_module)
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


def _deduplicate_inserts_by_source_message(automation_module: Any) -> None:
    if getattr(automation_module, "_source_message_dedup_installed", False):
        return
    original_apply = automation_module.apply_operations

    def apply_with_source_dedup(task: Any, records: list[dict[str, Any]], operations: list[dict[str, Any]]) -> Any:
        source_to_record: dict[str, str] = {}
        for record in records:
            record_id = str(record.get("record_id") or "")
            for source_id in record.get("source_message_ids", []):
                if record_id and str(source_id):
                    source_to_record[str(source_id)] = record_id

        normalized: list[dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict):
                normalized.append(operation)
                continue
            action = str(operation.get("action") or "").strip().lower()
            source_ids = [str(value) for value in operation.get("source_message_ids", []) if str(value)]
            if action == "insert":
                existing_id = next((source_to_record[value] for value in source_ids if value in source_to_record), "")
                if existing_id:
                    converted = dict(operation)
                    converted["action"] = "update"
                    converted["record_id"] = existing_id
                    normalized.append(converted)
                    continue
            normalized.append(operation)
        return original_apply(task, records, normalized)

    automation_module.apply_operations = apply_with_source_dedup
    automation_module._source_message_dedup_installed = True


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


def _retry_upload_without_reprocessing(automation_module: Any) -> None:
    if getattr(automation_module, "_upload_only_retry_installed", False):
        return
    original_upload_handler = automation_module._handle_upload_result
    original_tick = automation_module._automation_tick

    def upload_handler(window: Any, payload: dict[str, Any]) -> None:
        if payload.get("ok"):
            original_upload_handler(window, payload)
            return
        upload_id = str(payload.get("upload_id") or "")
        upload = getattr(window, "automation_uploads", {}).pop(upload_id, None)
        if upload is None:
            return
        task = task_by_id(getattr(window, "automation_tasks", []), upload.run.task_id)
        if task is None:
            automation_module._finish_task(window, upload.run.task_id)
            return
        attempt = upload.run.attempt + 1
        error = f"文件发送失败：{payload.get('error') or '未知错误'}"
        window.automation_state.mark_failure(task.task_id, error, attempt)
        window.append_log(f"定时任务“{task.name}”失败：{error}")
        automation_module._finish_task(window, task.task_id)
        if attempt <= len(automation_module.AUTOMATION_RETRY_DELAYS) and task.enabled:
            delay = automation_module.AUTOMATION_RETRY_DELAYS[attempt - 1]
            window.automation_retries[task.task_id] = {
                "due": datetime.now() + timedelta(seconds=delay),
                "delivery": True,
                "attempt": attempt,
                "upload_only": True,
                "path": upload.path,
                "message_keys": list(upload.message_keys),
                "checkpoint_message_id": upload.checkpoint_message_id,
                "cutoff": upload.run.cutoff,
                "checkpoint": upload.run.checkpoint,
            }
            window.append_log(f"定时任务“{task.name}”将在 {delay} 秒后仅重试文件上传（{attempt}/3）")

    def tick_with_upload_retry(window: Any, ui_module: Any, ai_module: Any) -> None:
        now = datetime.now().replace(microsecond=0)
        for task_id, retry in list(getattr(window, "automation_retries", {}).items()):
            if not retry.get("upload_only"):
                continue
            due = retry.get("due")
            if not isinstance(due, datetime) or due > now or task_id in window.automation_running:
                continue
            task = task_by_id(window.automation_tasks, task_id)
            window.automation_retries.pop(task_id, None)
            if task is None or not task.enabled:
                continue
            path = Path(str(retry.get("path") or ""))
            recipient = automation_module._resolve_recipient(window, task)
            if not path.is_file() or not recipient or window.client_thread is None:
                context = automation_module.AutomationRunContext(
                    request_id=f"upload_retry_{uuid.uuid4().hex[:12]}",
                    task_id=task.task_id,
                    cutoff=retry.get("cutoff") if isinstance(retry.get("cutoff"), datetime) else now,
                    checkpoint=retry.get("checkpoint") if isinstance(retry.get("checkpoint"), datetime) else now,
                    delivery=True,
                    manual=False,
                    attempt=int(retry.get("attempt") or 0),
                )
                automation_module._fail_run(window, task, context, "重试上传时文件、接收人或连接不可用")
                continue

            context = automation_module.AutomationRunContext(
                request_id=f"upload_retry_{uuid.uuid4().hex[:12]}",
                task_id=task.task_id,
                cutoff=retry.get("cutoff") if isinstance(retry.get("cutoff"), datetime) else now,
                checkpoint=retry.get("checkpoint") if isinstance(retry.get("checkpoint"), datetime) else now,
                delivery=True,
                manual=False,
                attempt=int(retry.get("attempt") or 0),
            )
            upload_id = f"upload_{uuid.uuid4().hex[:18]}"
            window.automation_running.add(task.task_id)
            window.automation_state.mark_started(task.task_id)
            window.automation_uploads[upload_id] = automation_module.AutomationUploadContext(
                upload_id=upload_id,
                run=context,
                path=str(path),
                message_keys=[str(value) for value in retry.get("message_keys", []) if str(value)],
                checkpoint_message_id=str(retry.get("checkpoint_message_id") or ""),
            )
            window.append_log(f"定时任务“{task.name}”正在仅重试文件上传：{path.name}")
            window.client_thread.upload_automation_file(upload_id, recipient, str(path), path.name)

        original_tick(window, ui_module, ai_module)

    automation_module._handle_upload_result = upload_handler
    automation_module._automation_tick = tick_with_upload_retry
    automation_module._upload_only_retry_installed = True


def _guard_results_after_disconnect(automation_module: Any) -> None:
    if getattr(automation_module, "_disconnect_result_guard_installed", False):
        return
    original_ready = automation_module._handle_execution_ready

    def ready_with_connection_guard(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        if isinstance(payload, dict):
            context = payload.get("context")
            task = task_by_id(getattr(window, "automation_tasks", []), str(payload.get("task_id") or ""))
            text = str(payload.get("text") or "").strip()
            needs_connection = bool(
                task is not None
                and context is not None
                and (getattr(context, "delivery", False) or (text and task.output_mode == "send_text"))
            )
            if needs_connection and getattr(window, "client_thread", None) is None:
                window.automation_state.mark_failure(task.task_id, "AI 完成后连接已断开，结果尚未发送", 0)
                window.append_log(f"定时任务“{task.name}”失败：AI 完成后连接已断开，结果尚未发送")
                automation_module._finish_task(window, task.task_id)
                return
        original_ready(window, ui_module, ai_module, payload)

    automation_module._handle_execution_ready = ready_with_connection_guard
    automation_module._disconnect_result_guard_installed = True


def _recover_interrupted_tasks(automation_module: Any, ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_recovery_installed", False):
        return
    original_init = main_window_cls.__init__

    def init_with_automation_recovery(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        now = datetime.now().replace(microsecond=0)
        changed = False
        for task in getattr(self, "automation_tasks", []):
            if not task.enabled:
                continue
            state = self.automation_state.state(task.task_id)
            if str(state.get("last_status") or "") not in {"running", "failed"}:
                continue
            task.next_run_at = now.isoformat()
            if task.daily_delivery_enabled:
                task.next_delivery_at = now.isoformat()
            changed = True
        if changed:
            automation_module.save_automation_tasks(self.settings, self.automation_tasks)

    main_window_cls.__init__ = init_with_automation_recovery
    main_window_cls._automation_recovery_installed = True


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
