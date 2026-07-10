from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .models import ChatMessage


class LocalMessageLogError(RuntimeError):
    pass


class LocalMessageLogBridge(QObject):
    payload_ready = Signal(object)


class LocalMessageLogStore:
    """Persist realtime QQ messages using the local receipt time as truth."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS realtime_message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    session_name TEXT NOT NULL DEFAULT '',
                    session_kind TEXT NOT NULL DEFAULT '',
                    message_key TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    event_time TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL,
                    outgoing INTEGER NOT NULL DEFAULT 0,
                    raw_event_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(session_id, message_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_realtime_message_log_range
                ON realtime_message_log(session_id, received_at, id)
                """
            )

    def append(self, message: ChatMessage, received_at: datetime | None = None) -> bool:
        if message.historical or message.session_kind not in {"group", "private"}:
            return False
        if str(message.sender_id or "") == "scheduled_task":
            return False

        received = (received_at or datetime.now()).replace(tzinfo=None)
        received_text = received.isoformat(timespec="microseconds")
        source_id = _source_message_id(message, received)
        message_key = f"id:{source_id}"
        raw_event = message.raw_event if isinstance(message.raw_event, dict) else {}
        try:
            raw_json = json.dumps(raw_event, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_json = "{}"

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO realtime_message_log(
                    session_id, session_name, session_kind, message_key,
                    message_id, sender_id, sender_name, text,
                    event_time, received_at, outgoing, raw_event_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(message.session_id or ""),
                    str(message.session_name or ""),
                    str(message.session_kind or ""),
                    message_key,
                    source_id,
                    str(message.sender_id or ""),
                    str(message.sender_name or ""),
                    str(message.text or ""),
                    _datetime_text(getattr(message, "timestamp", None)),
                    received_text,
                    1 if message.outgoing else 0,
                    raw_json,
                ),
            )
            return cursor.rowcount > 0

    def read_range(
        self,
        session_id: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[list[ChatMessage], int]:
        start_text = start.replace(tzinfo=None).isoformat(timespec="microseconds")
        end_text = end.replace(tzinfo=None).isoformat(timespec="microseconds")
        safe_limit = max(20, min(int(limit), 5000))

        with self._connect() as connection:
            total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM realtime_message_log
                    WHERE session_id = ? AND received_at > ? AND received_at <= ?
                    """,
                    (str(session_id), start_text, end_text),
                ).fetchone()[0]
            )
            if total > safe_limit:
                raise LocalMessageLogError(
                    f"本地实时日志在本轮范围内有 {total} 条消息，超过任务上限 {safe_limit}；"
                    "请提高任务的历史消息上限后重试，检查点尚未推进"
                )
            rows = connection.execute(
                """
                SELECT * FROM realtime_message_log
                WHERE session_id = ? AND received_at > ? AND received_at <= ?
                ORDER BY received_at ASC, id ASC
                """,
                (str(session_id), start_text, end_text),
            ).fetchall()

        return [_row_to_message(row) for row in rows], total


def install_automation_local_message_log(
    automation_module: Any,
    storage_module: Any,
    ui_module: Any,
) -> None:
    """Make the local realtime log the only scheduled-task message source."""

    _install_window_log(ui_module, storage_module)
    _install_local_history_resolver(ui_module)
    _install_local_payload_dispatch(automation_module, ui_module)


def _install_window_log(ui_module: Any, storage_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_local_message_log_installed", False):
        return

    original_init = main_window_cls.__init__
    original_add_message = main_window_cls.add_message

    def init_with_local_log(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.automation_message_log = LocalMessageLogStore(storage_module.AUTOMATION_STATE_DB)
        self.automation_local_log_bridge = LocalMessageLogBridge(self)

    def add_message_with_local_log(self: Any, message: Any) -> None:
        if isinstance(message, ChatMessage):
            try:
                self.automation_message_log.append(message, datetime.now())
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"写入本地实时消息日志失败：{exc}")
        original_add_message(self, message)

    main_window_cls.__init__ = init_with_local_log
    main_window_cls.add_message = add_message_with_local_log
    main_window_cls._automation_local_message_log_installed = True


def _install_local_history_resolver(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_local_history_resolver_installed", False):
        return

    original_start = main_window_cls.start

    def start_with_local_resolver(self: Any, *args: Any, **kwargs: Any) -> None:
        original_start(self, *args, **kwargs)
        bridge = getattr(self, "automation_local_log_bridge", None)
        if bridge is None:
            return
        if not getattr(self, "_automation_local_bridge_connected", False):
            bridge.payload_ready.connect(
                lambda payload: _dispatch_local_payload(self, payload)
            )
            self._automation_local_bridge_connected = True

        client = getattr(self, "client_thread", None)
        if client is None:
            return

        def resolve(request_id: str, session_id: str, count: int) -> None:
            context = getattr(self, "automation_pending", {}).get(str(request_id))
            if context is None:
                return

            def worker() -> None:
                try:
                    messages, total = self.automation_message_log.read_range(
                        str(session_id),
                        context.checkpoint,
                        context.cutoff,
                        int(count),
                    )
                    payload = {
                        "automation_history": True,
                        "automation_local_log": True,
                        "request_id": str(request_id),
                        "session_id": str(session_id),
                        "messages": messages,
                        "local_total": total,
                        "error": "",
                    }
                except Exception as exc:  # noqa: BLE001
                    payload = {
                        "automation_history": True,
                        "automation_local_log": True,
                        "request_id": str(request_id),
                        "session_id": str(session_id),
                        "messages": [],
                        "local_total": 0,
                        "error": str(exc),
                    }
                bridge.payload_ready.emit(payload)

            threading.Thread(target=worker, daemon=True).start()

        client._automation_local_history_resolver = resolve
        original_request = client.request_automation_history

        def request_from_local_log(request_id: str, session_id: str, count: int) -> None:
            resolver = getattr(client, "_automation_local_history_resolver", None)
            if callable(resolver):
                resolver(str(request_id), str(session_id), int(count))
                return
            original_request(request_id, session_id, count)

        client.request_automation_history = request_from_local_log
        self.append_log("定时任务已切换为本地实时消息日志，不再调用 NapCat 历史接口")

    main_window_cls.start = start_with_local_resolver
    main_window_cls._automation_local_history_resolver_installed = True


def _install_local_payload_dispatch(automation_module: Any, ui_module: Any) -> None:
    if getattr(automation_module, "_automation_local_payload_dispatch_installed", False):
        return

    original_handler = automation_module._handle_automation_payload

    def handle_with_local_dispatch(
        window: Any,
        ui_module_arg: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if not isinstance(payload, dict) or not payload.get("automation_local_log"):
            original_handler(window, ui_module_arg, ai_module, payload)
            return
        _handle_local_history_payload(
            automation_module,
            window,
            ui_module_arg,
            ai_module,
            payload,
        )

    automation_module._handle_automation_payload = handle_with_local_dispatch
    automation_module._automation_local_payload_dispatch_installed = True


def _dispatch_local_payload(window: Any, payload: Any) -> None:
    from . import ai_client as ai_module
    from . import automation_feature as automation_module
    from . import ui as ui_module

    automation_module._handle_automation_payload(window, ui_module, ai_module, payload)


def _handle_local_history_payload(
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

    detail = (
        f"定时任务“{task.name}”本地实时日志："
        f"读取 {len(fetched)} 条，已处理 {len(processed)} 条，待处理 {len(messages)} 条"
    )
    if fetched:
        detail += (
            f"；本机接收时间 {fetched[0].timestamp:%Y-%m-%d %H:%M:%S.%f}"
            f" ～ {fetched[-1].timestamp:%Y-%m-%d %H:%M:%S.%f}"
        )
    window.append_log(detail)

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


def _row_to_message(row: sqlite3.Row) -> ChatMessage:
    received_at = _parse_datetime(str(row["received_at"] or "")) or datetime.now()
    try:
        raw_event = json.loads(str(row["raw_event_json"] or "{}"))
    except json.JSONDecodeError:
        raw_event = {}
    if not isinstance(raw_event, dict):
        raw_event = {}
    raw_event["_qqmm_received_at"] = received_at.isoformat(timespec="microseconds")
    raw_event["_qqmm_original_event_time"] = str(row["event_time"] or "")
    raw_event["_qqmm_local_message_log_id"] = int(row["id"])
    return ChatMessage(
        session_id=str(row["session_id"] or ""),
        session_name=str(row["session_name"] or ""),
        session_kind=str(row["session_kind"] or "group"),
        sender_id=str(row["sender_id"] or ""),
        sender_name=str(row["sender_name"] or ""),
        text=str(row["text"] or ""),
        timestamp=received_at,
        raw_event=raw_event,
        outgoing=bool(row["outgoing"]),
        historical=False,
        message_id=str(row["message_id"] or ""),
    )


def _source_message_id(message: ChatMessage, received_at: datetime) -> str:
    message_id = str(message.message_id or "").strip()
    if message_id:
        return message_id
    raw = "|".join(
        [
            str(message.session_id or ""),
            str(message.sender_id or ""),
            received_at.isoformat(timespec="microseconds"),
            str(message.text or ""),
            "1" if message.outgoing else "0",
        ]
    )
    return "local_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _datetime_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="microseconds")
    return ""


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None
