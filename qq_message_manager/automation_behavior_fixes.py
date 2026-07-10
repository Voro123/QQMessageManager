from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QDialog

GENERATED_SUFFIXES = {".xlsx", ".csv", ".json", ".md"}


def install_automation_behavior_fixes(automation_module: Any) -> None:
    """Fix scheduled-task delivery deletion control and empty-message runs."""

    _install_delete_after_send_control(automation_module)
    _install_retained_delivery_archiving(automation_module)
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
        delivery_toggle = getattr(self, "delivery_enabled_input", None)
        if delivery_toggle is not None:
            delivery_toggle.setText("每天把当前文件私聊发送")
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


def _install_retained_delivery_archiving(automation_module: Any) -> None:
    """Keep successfully sent files without merging and resending them later."""

    if getattr(automation_module, "_automation_retained_archive_fix_installed", False):
        return

    original_handler = automation_module._handle_upload_result

    def handle_with_retained_archive(window: Any, payload: dict[str, Any]) -> None:
        upload_id = str(payload.get("upload_id") or "")
        upload = getattr(window, "automation_uploads", {}).get(upload_id)
        task = (
            automation_module.task_by_id(
                getattr(window, "automation_tasks", []),
                upload.run.task_id,
            )
            if upload is not None
            else None
        )
        should_retain = bool(
            payload.get("ok")
            and upload is not None
            and task is not None
            and not bool(getattr(task, "delete_after_send", True))
        )
        retained_path = Path(str(upload.path)).expanduser() if should_retain else None
        cutoff = upload.run.cutoff if should_retain else None

        original_handler(window, payload)

        if not should_retain or retained_path is None or cutoff is None or task is None:
            return
        try:
            archive_label = _archive_sent_bundles(
                automation_module,
                task,
                retained_path,
                cutoff,
                upload_id,
            )
            if archive_label:
                window.append_log(
                    f"定时任务“{task.name}”文件发送成功，旧文件已保留到 {archive_label}"
                )
        except OSError as exc:
            # Delivery has already succeeded and the checkpoint has advanced.
            # A retention move failure must be visible, but must not retry or
            # send the same successful archive again.
            window.append_log(
                f"定时任务“{task.name}”文件已发送，但保留旧文件时失败：{exc}"
            )

    automation_module._handle_upload_result = handle_with_retained_archive
    automation_module._automation_retained_archive_fix_installed = True


def _archive_sent_bundles(
    automation_module: Any,
    task: Any,
    delivered_path: Path,
    cutoff: Any,
    upload_id: str,
) -> str:
    delivered_path = delivered_path.resolve()
    parent = delivered_path.parent
    if not parent.is_dir():
        return ""

    # The base success handler creates the next active file before returning.
    # Keep that file in the workspace root and move all previously generated
    # artifacts into sent/, so the archive merge code will not pick them up on
    # the next day.
    active_path = automation_module.artifact_path(task, cutoff.date()).resolve()
    active_sidecar = Path(str(active_path) + ".records.json")
    delivered_sidecar = Path(str(delivered_path) + ".records.json")
    safe_upload_id = "".join(char for char in upload_id if char.isalnum() or char in "_-")[-24:]
    folder_name = f"{cutoff:%Y-%m-%d_%H%M%S}_{safe_upload_id or 'sent'}"
    destination = parent / "sent" / folder_name

    # A template without {date} resolves the delivered archive and the new
    # active file to the same path. Preserve a copy first, then reset the root
    # file to an empty workbook/table for the next period.
    same_active_path = delivered_path == active_path
    if same_active_path and delivered_path.is_file():
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(delivered_path, destination / delivered_path.name)
        if delivered_sidecar.is_file():
            shutil.copy2(delivered_sidecar, destination / delivered_sidecar.name)
        automation_module.write_artifact(task, cutoff.date(), [])

    candidates: list[Path] = []
    for item in parent.iterdir():
        if not item.is_file():
            continue
        is_sidecar = item.name.endswith(".records.json")
        is_artifact = item.suffix.lower() in GENERATED_SUFFIXES
        if not is_sidecar and not is_artifact:
            continue
        resolved = item.resolve()
        if resolved in {active_path, active_sidecar}:
            continue
        candidates.append(item)

    if not candidates and not same_active_path:
        return ""
    destination.mkdir(parents=True, exist_ok=True)
    for source in candidates:
        target = destination / source.name
        if target.exists():
            target = destination / f"{source.stem}_{safe_upload_id}{source.suffix}"
        shutil.move(str(source), str(target))
    return f"sent/{folder_name}/"


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
