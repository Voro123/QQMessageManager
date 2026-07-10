from __future__ import annotations

import hashlib
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any

from .automation_models import next_daily_time, next_schedule_time, task_by_id


def install_automation_hardening(automation_module: Any, ui_module: Any) -> None:
    """补充跨重启上传恢复、归档边界和结构化来源校验。"""
    _install_persistent_pending_delivery(automation_module)
    _install_delivery_cutoff_boundary(automation_module)
    _install_source_id_validation(automation_module)
    _install_manual_combo_parsing(automation_module)
    _install_recovery_after_existing_patches(automation_module, ui_module)


def _install_persistent_pending_delivery(automation_module: Any) -> None:
    state_cls = automation_module.AutomationStateStore
    if getattr(state_cls, "_pending_delivery_state_installed", False):
        return

    original_initialize = state_cls._initialize
    original_mark_success = state_cls.mark_success

    def initialize_with_pending_delivery(self: Any) -> None:
        original_initialize(self)
        additions = {
            "pending_delivery": "INTEGER NOT NULL DEFAULT 0",
            "pending_file_path": "TEXT NOT NULL DEFAULT ''",
            "pending_cutoff": "TEXT NOT NULL DEFAULT ''",
            "pending_checkpoint": "TEXT NOT NULL DEFAULT ''",
            "pending_message_keys": "TEXT NOT NULL DEFAULT '[]'",
            "pending_checkpoint_message_id": "TEXT NOT NULL DEFAULT ''",
        }
        with self._connect() as connection:
            existing = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(task_state)").fetchall()
            }
            for column, declaration in additions.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE task_state ADD COLUMN {column} {declaration}"
                    )

    def mark_pending_delivery(
        self: Any,
        task_id: str,
        file_path: str,
        cutoff: datetime,
        checkpoint: datetime,
        message_keys: list[str],
        checkpoint_message_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_state(
                    task_id, pending_delivery, pending_file_path,
                    pending_cutoff, pending_checkpoint, pending_message_keys,
                    pending_checkpoint_message_id, last_status
                ) VALUES(?, 1, ?, ?, ?, ?, ?, 'pending_upload')
                ON CONFLICT(task_id) DO UPDATE SET
                    pending_delivery = 1,
                    pending_file_path = excluded.pending_file_path,
                    pending_cutoff = excluded.pending_cutoff,
                    pending_checkpoint = excluded.pending_checkpoint,
                    pending_message_keys = excluded.pending_message_keys,
                    pending_checkpoint_message_id = excluded.pending_checkpoint_message_id,
                    last_status = 'pending_upload'
                """,
                (
                    task_id,
                    file_path,
                    cutoff.isoformat(),
                    checkpoint.isoformat(),
                    json.dumps(list(dict.fromkeys(message_keys)), ensure_ascii=False),
                    checkpoint_message_id,
                ),
            )

    def clear_pending_delivery(self: Any, task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE task_state SET
                    pending_delivery = 0,
                    pending_file_path = '',
                    pending_cutoff = '',
                    pending_checkpoint = '',
                    pending_message_keys = '[]',
                    pending_checkpoint_message_id = ''
                WHERE task_id = ?
                """,
                (task_id,),
            )

    def mark_success_and_clear(self: Any, *args: Any, **kwargs: Any) -> None:
        original_mark_success(self, *args, **kwargs)
        task_id = str(args[0] if args else kwargs.get("task_id") or "")
        if task_id:
            self.clear_pending_delivery(task_id)

    state_cls._initialize = initialize_with_pending_delivery
    state_cls.mark_pending_delivery = mark_pending_delivery
    state_cls.clear_pending_delivery = clear_pending_delivery
    state_cls.mark_success = mark_success_and_clear
    state_cls._pending_delivery_state_installed = True

    original_ready = automation_module._handle_execution_ready

    def ready_with_pending_delivery(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        if isinstance(payload, dict):
            context = payload.get("context")
            task = task_by_id(
                getattr(window, "automation_tasks", []),
                str(payload.get("task_id") or ""),
            )
            path = str(payload.get("path") or "")
            if (
                task is not None
                and context is not None
                and bool(getattr(context, "delivery", False))
                and task.file_enabled
                and path
            ):
                window.automation_state.mark_pending_delivery(
                    task.task_id,
                    path,
                    context.cutoff,
                    context.checkpoint,
                    [str(value) for value in payload.get("message_keys", []) if str(value)],
                    str(payload.get("checkpoint_message_id") or ""),
                )
        original_ready(window, ui_module, ai_module, payload)

    automation_module._handle_execution_ready = ready_with_pending_delivery
    automation_module._persistent_pending_delivery_installed = True


def _install_delivery_cutoff_boundary(automation_module: Any) -> None:
    if getattr(automation_module, "_delivery_cutoff_boundary_installed", False):
        return
    original_start = automation_module._start_task

    def start_with_delivery_boundary(
        window: Any,
        ui_module: Any,
        ai_module: Any,
        task: Any,
        *,
        delivery: bool,
        manual: bool,
        attempt: int,
        advance_schedule: bool,
    ) -> None:
        now = datetime.now().replace(microsecond=0)
        boundary = _latest_daily_boundary(task.delivery_time, now) if delivery else None
        original_start(
            window,
            ui_module,
            ai_module,
            task,
            delivery=delivery,
            manual=manual,
            attempt=attempt,
            advance_schedule=advance_schedule,
        )
        if boundary is None:
            return
        contexts = [
            context
            for context in getattr(window, "automation_pending", {}).values()
            if context.task_id == task.task_id and context.delivery
        ]
        if contexts:
            contexts[-1].cutoff = boundary

    automation_module._start_task = start_with_delivery_boundary
    automation_module._delivery_cutoff_boundary_installed = True


def _latest_daily_boundary(hhmm: str, now: datetime) -> datetime:
    try:
        hour_text, minute_text = str(hhmm).split(":", 1)
        boundary = datetime.combine(now.date(), time(int(hour_text), int(minute_text)))
    except (TypeError, ValueError):
        boundary = datetime.combine(now.date(), time(0, 0))
    if boundary > now:
        boundary = boundary.replace(day=boundary.day) - automation_timedelta_day()
    return boundary


def automation_timedelta_day() -> Any:
    from datetime import timedelta

    return timedelta(days=1)


def _install_source_id_validation(automation_module: Any) -> None:
    if getattr(automation_module, "_source_id_validation_installed", False):
        return
    original_generate = automation_module.generate_scheduled_result

    def generate_with_source_validation(
        ai_module: Any,
        config: Any,
        task: Any,
        messages: list[Any],
        existing_records: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        result = original_generate(
            ai_module,
            config,
            task,
            messages,
            existing_records,
            **kwargs,
        )
        allowed = {
            _stable_source_id(message)
            for message in messages
            if str(getattr(message, "sender_id", "")) != "scheduled_trigger"
        }
        for operation in getattr(result, "operations", []):
            if not isinstance(operation, dict):
                continue
            operation["source_message_ids"] = [
                str(value)
                for value in operation.get("source_message_ids", [])
                if str(value) in allowed
            ]
        return result

    automation_module.generate_scheduled_result = generate_with_source_validation
    automation_module._source_id_validation_installed = True


def _stable_source_id(message: Any) -> str:
    message_id = str(getattr(message, "message_id", "") or "")
    if message_id:
        return message_id
    timestamp = getattr(message, "timestamp", datetime.now())
    raw = "|".join(
        [
            str(getattr(message, "session_id", "")),
            str(getattr(message, "sender_id", "")),
            str(int(timestamp.timestamp())),
            str(getattr(message, "text", "")),
        ]
    )
    return "local_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _install_manual_combo_parsing(automation_module: Any) -> None:
    dialog_cls = automation_module.AutomationTaskEditDialog
    if getattr(dialog_cls, "_manual_combo_parsing_installed", False):
        return

    def target_value(self: Any) -> tuple[str, str]:
        text = self.target_input.currentText().strip()
        index = self.target_input.currentIndex()
        selected_text = self.target_input.itemText(index).strip() if index >= 0 else ""
        selected_data = str(self.target_input.itemData(index) or "") if index >= 0 else ""
        if selected_data and text == selected_text:
            parts = text.split(" · ")
            return selected_data, parts[1] if len(parts) >= 2 else selected_data
        return text, text

    def recipient_value(self: Any) -> tuple[str, str]:
        text = self.recipient_input.currentText().strip()
        index = self.recipient_input.currentIndex()
        selected_text = self.recipient_input.itemText(index).strip() if index >= 0 else ""
        selected_data = str(self.recipient_input.itemData(index) or "") if index >= 0 else ""
        if text == selected_text and selected_data == "__self__":
            return automation_module.RECIPIENT_SELF, ""
        if text == selected_text and selected_data:
            return automation_module.RECIPIENT_CONTACT, "".join(char for char in selected_data if char.isdigit())
        return automation_module.RECIPIENT_MANUAL, "".join(char for char in text if char.isdigit())

    dialog_cls._target_value = target_value
    dialog_cls._recipient_value = recipient_value
    dialog_cls._manual_combo_parsing_installed = True


def _install_recovery_after_existing_patches(automation_module: Any, ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_persistent_automation_recovery_installed", False):
        return
    original_init = main_window_cls.__init__

    def init_with_persistent_recovery(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        now = datetime.now().replace(microsecond=0)
        changed = False
        for task in getattr(self, "automation_tasks", []):
            if not task.enabled:
                continue
            state = self.automation_state.state(task.task_id)
            pending_delivery = bool(int(state.get("pending_delivery") or 0))
            pending_path = Path(str(state.get("pending_file_path") or ""))
            if pending_delivery and pending_path.is_file():
                try:
                    keys = json.loads(str(state.get("pending_message_keys") or "[]"))
                except json.JSONDecodeError:
                    keys = []
                cutoff = _parse_datetime(state.get("pending_cutoff"), now)
                checkpoint = _parse_datetime(state.get("pending_checkpoint"), now)
                self.automation_retries[task.task_id] = {
                    "due": now,
                    "delivery": True,
                    "attempt": int(state.get("retry_count") or 0),
                    "upload_only": True,
                    "path": str(pending_path),
                    "message_keys": [str(value) for value in keys if str(value)],
                    "checkpoint_message_id": str(state.get("pending_checkpoint_message_id") or ""),
                    "cutoff": cutoff,
                    "checkpoint": checkpoint,
                }
                task.next_run_at = next_schedule_time(task, now, include_now=False).isoformat()
                task.next_delivery_at = next_daily_time(task.delivery_time, now, include_now=False).isoformat()
                changed = True
                continue

            status = str(state.get("last_status") or "")
            if status in {"running", "failed"}:
                task.next_run_at = now.isoformat()
                if task.daily_delivery_enabled:
                    task.next_delivery_at = next_daily_time(task.delivery_time, now, include_now=False).isoformat()
                changed = True
        if changed:
            automation_module.save_automation_tasks(self.settings, self.automation_tasks)

    main_window_cls.__init__ = init_with_persistent_recovery
    main_window_cls._persistent_automation_recovery_installed = True


def _parse_datetime(value: Any, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return default
