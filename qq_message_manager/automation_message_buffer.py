from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer

from .models import ChatMessage


@dataclass(slots=True)
class BufferedMessage:
    sequence: int
    received_at: datetime
    message: ChatMessage


class AutomationMessageBuffer:
    """Messages received from the same realtime signal used by the chat UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._next_sequence = 0
        self._messages: dict[str, list[BufferedMessage]] = defaultdict(list)
        self._seen_keys: set[str] = set()

    def record(self, message: Any) -> int | None:
        if not isinstance(message, ChatMessage):
            return None
        if message.historical or message.session_kind not in {"group", "private"}:
            return None
        if str(message.sender_id or "") == "scheduled_task":
            return None

        key = _message_identity(message)
        received_at = datetime.now().replace(tzinfo=None)
        with self._lock:
            if key in self._seen_keys:
                return None
            self._seen_keys.add(key)
            self._next_sequence += 1
            sequence = self._next_sequence
            self._messages[str(message.session_id)].append(
                BufferedMessage(
                    sequence=sequence,
                    received_at=received_at,
                    message=_copy_for_automation(message, received_at, sequence),
                )
            )
            return sequence

    def read_after(
        self,
        session_id: str,
        sequence: int,
        limit: int,
    ) -> tuple[list[ChatMessage], int, int]:
        safe_limit = max(20, min(int(limit), 5000))
        with self._lock:
            entries = list(self._messages.get(str(session_id), []))
        selected = [entry for entry in entries if entry.sequence > int(sequence)]
        if len(selected) > safe_limit:
            raise RuntimeError(
                f"内存消息缓存中有 {len(selected)} 条待处理消息，超过任务上限 {safe_limit}；"
                "请提高任务的消息读取上限，任务游标尚未推进"
            )
        upper_sequence = entries[-1].sequence if entries else int(sequence)
        return [entry.message for entry in selected], upper_sequence, len(selected)

    def session_size(self, session_id: str) -> int:
        with self._lock:
            return len(self._messages.get(str(session_id), []))


def install_automation_message_buffer(
    automation_module: Any,
    ui_module: Any,
) -> None:
    """Use an in-memory per-session buffer as the scheduled-task message source."""

    _install_window_buffer(automation_module, ui_module)
    _install_buffer_payload_dispatch(automation_module, ui_module)
    _install_success_cursor_commit(automation_module)
    _install_failure_cleanup(automation_module)


def _install_window_buffer(automation_module: Any, ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_message_buffer_installed", False):
        return

    original_init = main_window_cls.__init__
    original_start = main_window_cls.start

    def init_with_message_buffer(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.automation_message_buffer = AutomationMessageBuffer()
        self.automation_buffer_cursors: dict[tuple[str, str], int] = {}
        self.automation_buffer_pending: dict[str, tuple[str, int]] = {}
        # mark_success is called by every successful path, including confirmed
        # daily file delivery. Give it access to this window's in-memory cursor.
        self.automation_state._automation_buffer_window = self

    def start_with_message_buffer(self: Any, *args: Any, **kwargs: Any) -> None:
        original_start(self, *args, **kwargs)
        client = getattr(self, "client_thread", None)
        if client is None:
            return

        if getattr(self, "_automation_buffer_signal_client", None) is not client:
            client.message_received.connect(self.automation_message_buffer.record)
            self._automation_buffer_signal_client = client

        def request_from_message_buffer(request_id: str, session_id: str, count: int) -> None:
            context = getattr(self, "automation_pending", {}).get(str(request_id))
            if context is None:
                return
            cursor_key = (str(context.task_id), str(session_id))
            cursor = int(self.automation_buffer_cursors.get(cursor_key, 0))
            try:
                messages, upper_sequence, total = self.automation_message_buffer.read_after(
                    str(session_id),
                    cursor,
                    int(count),
                )
                self.automation_buffer_pending[str(context.task_id)] = (
                    str(session_id),
                    int(upper_sequence),
                )
                payload = {
                    "automation_history": True,
                    "automation_message_buffer": True,
                    "request_id": str(request_id),
                    "session_id": str(session_id),
                    "messages": messages,
                    "buffer_cursor": cursor,
                    "buffer_upper_sequence": int(upper_sequence),
                    "buffer_total": int(total),
                    "error": "",
                }
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "automation_history": True,
                    "automation_message_buffer": True,
                    "request_id": str(request_id),
                    "session_id": str(session_id),
                    "messages": [],
                    "buffer_cursor": cursor,
                    "buffer_upper_sequence": cursor,
                    "buffer_total": 0,
                    "error": str(exc),
                }
            # Keep the request asynchronous just like the old NapCat history call.
            QTimer.singleShot(0, lambda value=payload: client.history_messages_received.emit(value))

        client.request_automation_history = request_from_message_buffer
        self.append_log("定时任务使用实时消息内存缓存：收到并回显的消息会按会话直接进入缓存")

    main_window_cls.__init__ = init_with_message_buffer
    main_window_cls.start = start_with_message_buffer
    main_window_cls._automation_message_buffer_installed = True


def _install_buffer_payload_dispatch(automation_module: Any, ui_module: Any) -> None:
    if getattr(automation_module, "_automation_message_buffer_dispatch_installed", False):
        return
    original_handler = automation_module._handle_automation_payload

    def handle_with_message_buffer(
        window: Any,
        ui_module_arg: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict) or not payload.get("automation_message_buffer"):
            original_handler(window, ui_module_arg, ai_module, payload)
            return
        _handle_buffer_payload(
            automation_module,
            window,
            ui_module_arg,
            ai_module,
            payload,
        )

    automation_module._handle_automation_payload = handle_with_message_buffer
    automation_module._automation_message_buffer_dispatch_installed = True


def _handle_buffer_payload(
    automation_module: Any,
    window: Any,
    ui_module: Any,
    ai_module: Any,
    payload: dict[str, Any],
) -> None:
    request_id = str(payload.get("request_id") or "")
    context = getattr(window, "automation_pending", {}).pop(request_id, None)
    if context is None:
        return
    task = automation_module.task_by_id(
        getattr(window, "automation_tasks", []),
        str(getattr(context, "task_id", "") or ""),
    )
    if task is None:
        automation_module._finish_task(window, str(getattr(context, "task_id", "") or ""))
        return

    error = str(payload.get("error") or "")
    if error:
        automation_module._fail_run(window, task, context, error)
        return

    fetched = [
        message
        for message in payload.get("messages", [])
        if isinstance(message, automation_module.ChatMessage)
    ]
    keys = [automation_module.message_key(message) for message in fetched]
    processed = window.automation_state.processed_keys(task.task_id, keys)
    messages = [
        message
        for message in fetched
        if automation_module.message_key(message) not in processed
    ]
    cursor = int(payload.get("buffer_cursor") or 0)
    upper = int(payload.get("buffer_upper_sequence") or cursor)
    window.append_log(
        f"定时任务“{task.name}”实时消息缓存：会话 {task.target_session_id}，"
        f"游标 {cursor} → {upper}，读取 {len(fetched)} 条，"
        f"已处理 {len(processed)} 条，待处理 {len(messages)} 条"
    )

    if messages:
        window.append_log(
            f"定时任务“{task.name}”已取得 {len(messages)} 条未处理消息，正在调用 AI"
        )
    elif not context.delivery:
        window.append_log(
            f"定时任务“{task.name}”本轮没有新消息，仍按计划调用 AI 执行指令"
        )
    else:
        window.append_log(
            f"定时任务“{task.name}”没有新增消息，正在准备每日文件归档"
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
            result = None
            if messages or not context.delivery:
                result = automation_module.generate_scheduled_result(
                    ai_module,
                    config,
                    task,
                    messages,
                    automation_module.records_for_ai(existing),
                    checkpoint_time=context.checkpoint,
                    cutoff_time=context.cutoff,
                )
            stats = {"inserted": 0, "updated": 0, "ignored": 0}
            if task.file_enabled:
                if result is not None:
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
                    "messages": messages,
                    "message_keys": [
                        automation_module.message_key(message) for message in messages
                    ],
                    "checkpoint_message_id": messages[-1].message_id if messages else "",
                    "text": result.text if result is not None else "",
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


def _install_success_cursor_commit(automation_module: Any) -> None:
    state_cls = automation_module.AutomationStateStore
    if getattr(state_cls, "_automation_buffer_cursor_commit_installed", False):
        return
    original_mark_success = state_cls.mark_success

    def mark_success_and_commit_buffer(
        self: Any,
        task_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_mark_success(self, task_id, *args, **kwargs)
        window = getattr(self, "_automation_buffer_window", None)
        if window is None:
            return
        pending = getattr(window, "automation_buffer_pending", {}).pop(str(task_id), None)
        if not pending:
            return
        session_id, sequence = pending
        key = (str(task_id), str(session_id))
        current = int(getattr(window, "automation_buffer_cursors", {}).get(key, 0))
        window.automation_buffer_cursors[key] = max(current, int(sequence))

    state_cls.mark_success = mark_success_and_commit_buffer
    state_cls._automation_buffer_cursor_commit_installed = True


def _install_failure_cleanup(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_buffer_failure_cleanup_installed", False):
        return
    original_fail = automation_module._fail_run

    def fail_and_keep_cursor(
        window: Any,
        task: Any,
        context: Any,
        error: str,
    ) -> None:
        getattr(window, "automation_buffer_pending", {}).pop(str(task.task_id), None)
        original_fail(window, task, context, error)

    automation_module._fail_run = fail_and_keep_cursor
    automation_module._automation_buffer_failure_cleanup_installed = True


def _copy_for_automation(
    message: ChatMessage,
    received_at: datetime,
    sequence: int,
) -> ChatMessage:
    raw_event = dict(message.raw_event) if isinstance(message.raw_event, dict) else {}
    raw_event["_qqmm_received_at"] = received_at.isoformat(timespec="microseconds")
    raw_event["_qqmm_buffer_sequence"] = sequence
    return ChatMessage(
        session_id=str(message.session_id),
        session_name=str(message.session_name),
        session_kind=message.session_kind,
        sender_id=str(message.sender_id),
        sender_name=str(message.sender_name),
        text=str(message.text or ""),
        timestamp=received_at,
        raw_event=raw_event,
        outgoing=bool(message.outgoing),
        historical=False,
        message_id=str(message.message_id or ""),
        images=list(message.images),
    )


def _message_identity(message: ChatMessage) -> str:
    message_id = str(message.message_id or "").strip()
    if message_id:
        return f"id:{message_id}"
    event_time = getattr(message, "timestamp", None)
    event_time_text = event_time.isoformat() if isinstance(event_time, datetime) else ""
    raw = "|".join(
        [
            str(message.session_id or ""),
            str(message.sender_id or ""),
            event_time_text,
            str(message.text or ""),
            "1" if message.outgoing else "0",
        ]
    )
    return "sha:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
