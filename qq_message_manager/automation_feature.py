from __future__ import annotations

import copy
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTime, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .automation_ai import generate_scheduled_result
from .automation_models import (
    OUTPUT_SEND_TEXT,
    OUTPUT_SILENT,
    RECIPIENT_CONTACT,
    RECIPIENT_MANUAL,
    RECIPIENT_SELF,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    AutomationColumn,
    AutomationTask,
    load_automation_tasks,
    save_automation_tasks,
    task_by_id,
    task_work_date,
)
from .automation_napcat import install_automation_napcat
from .automation_storage import (
    AutomationStateStore,
    apply_operations,
    artifact_path,
    delete_artifact_bundle,
    ensure_empty_artifact,
    load_records,
    message_key,
    records_for_ai,
    save_records,
    write_artifact,
)
from .models import ChatMessage

AUTOMATION_TICK_MS = 10_000
AUTOMATION_RETRY_DELAYS = (60, 300, 900)
AUTOMATION_DEFAULT_HISTORY_LIMIT = 1000


@dataclass(slots=True)
class AutomationRunContext:
    request_id: str
    task_id: str
    cutoff: datetime
    checkpoint: datetime
    delivery: bool
    manual: bool
    attempt: int = 0


@dataclass(slots=True)
class AutomationUploadContext:
    upload_id: str
    run: AutomationRunContext
    path: str
    message_keys: list[str]
    checkpoint_message_id: str


class AutomationBridge(QObject):
    ready = Signal(object)
    failed = Signal(object)


def install_automation_feature(ui_module: Any, ai_module: Any, napcat_module: Any) -> None:
    install_automation_napcat(napcat_module)
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_feature_installed", False):
        return

    original_init = main_window_cls.__init__
    original_start = main_window_cls.start
    original_disconnect = main_window_cls.disconnect_from_server

    def init_with_automation(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.automation_tasks = load_automation_tasks(self.settings)
        self.automation_state = AutomationStateStore()
        self.automation_running: set[str] = set()
        self.automation_pending: dict[str, AutomationRunContext] = {}
        self.automation_uploads: dict[str, AutomationUploadContext] = {}
        self.automation_retries: dict[str, dict[str, Any]] = {}
        self.automation_self_qq = ""
        self.automation_bridge = AutomationBridge(self)
        self.automation_bridge.ready.connect(lambda payload: _handle_execution_ready(self, ui_module, ai_module, payload))
        self.automation_bridge.failed.connect(lambda payload: _handle_execution_failed(self, payload))
        self.automation_timer = QTimer(self)
        self.automation_timer.setInterval(AUTOMATION_TICK_MS)
        self.automation_timer.timeout.connect(lambda: _automation_tick(self, ui_module, ai_module))

        self.automation_button = QPushButton("定时任务")
        self.automation_button.setToolTip("管理每天固定时间或按间隔执行的 AI 定时任务")
        self.automation_button.clicked.connect(lambda: _open_task_manager(self, ui_module, ai_module))
        send_bar = self.message_input.parentWidget()
        send_layout = send_bar.layout() if send_bar is not None else None
        if send_layout is not None:
            send_layout.addWidget(self.automation_button)

    def start_with_automation(self: Any) -> None:
        original_start(self)
        if self.client_thread is None:
            return
        if not getattr(self, "_automation_signal_connected", False):
            self.client_thread.history_messages_received.connect(lambda payload: _handle_automation_payload(self, ui_module, ai_module, payload))
            self.client_thread.connected.connect(lambda: _automation_connected(self, ui_module, ai_module))
            self._automation_signal_connected = True
        self.automation_timer.start()
        QTimer.singleShot(1500, lambda: _automation_tick(self, ui_module, ai_module))

    def disconnect_with_automation(self: Any) -> None:
        timer = getattr(self, "automation_timer", None)
        if timer is not None:
            timer.stop()
        getattr(self, "automation_running", set()).clear()
        getattr(self, "automation_pending", {}).clear()
        getattr(self, "automation_uploads", {}).clear()
        getattr(self, "automation_retries", {}).clear()
        original_disconnect(self)

    main_window_cls.__init__ = init_with_automation
    main_window_cls.start = start_with_automation
    main_window_cls.disconnect_from_server = disconnect_with_automation
    main_window_cls._automation_feature_installed = True


def _automation_connected(window: Any, ui_module: Any, ai_module: Any) -> None:
    if window.client_thread is not None:
        window.client_thread.request_automation_login_info()
    QTimer.singleShot(500, lambda: _automation_tick(window, ui_module, ai_module))


def _automation_tick(window: Any, ui_module: Any, ai_module: Any) -> None:
    if window.client_thread is None:
        return
    now = datetime.now().replace(microsecond=0)

    for task_id, retry in list(window.automation_retries.items()):
        due = retry.get("due")
        if isinstance(due, datetime) and due <= now and task_id not in window.automation_running:
            task = task_by_id(window.automation_tasks, task_id)
            window.automation_retries.pop(task_id, None)
            if task is not None and task.enabled:
                _start_task(
                    window,
                    ui_module,
                    ai_module,
                    task,
                    delivery=bool(retry.get("delivery")),
                    manual=False,
                    attempt=int(retry.get("attempt") or 0),
                    advance_schedule=False,
                )

    for task in list(window.automation_tasks):
        if not task.enabled or task.task_id in window.automation_running or task.task_id in window.automation_retries:
            continue
        delivery_due = task.daily_delivery_enabled and task.next_delivery_datetime is not None and task.next_delivery_datetime <= now
        run_due = task.next_run_datetime is not None and task.next_run_datetime <= now
        if delivery_due:
            _start_task(window, ui_module, ai_module, task, delivery=True, manual=False, attempt=0, advance_schedule=True)
        elif run_due:
            _start_task(window, ui_module, ai_module, task, delivery=False, manual=False, attempt=0, advance_schedule=True)


def _start_task(
    window: Any,
    ui_module: Any,
    ai_module: Any,
    task: AutomationTask,
    *,
    delivery: bool,
    manual: bool,
    attempt: int,
    advance_schedule: bool,
) -> None:
    del ai_module
    if task.task_id in window.automation_running:
        return
    if window.client_thread is None:
        window.append_log(f"定时任务“{task.name}”未执行：当前未连接 NapCatQQ")
        return
    if not task.target_session_id.startswith(("group:", "private:")):
        window.append_log(f"定时任务“{task.name}”未执行：目标会话无效")
        return
    config = ui_module.load_ai_config(window.settings).normalized()
    if not config.api_key:
        window.append_log(f"定时任务“{task.name}”未执行：未配置 AI API Key")
        return

    now = datetime.now().replace(microsecond=0)
    cutoff = now
    checkpoint = window.automation_state.checkpoint_time(task)
    request_id = f"auto_{uuid.uuid4().hex[:18]}"
    context = AutomationRunContext(
        request_id=request_id,
        task_id=task.task_id,
        cutoff=cutoff,
        checkpoint=checkpoint,
        delivery=delivery,
        manual=manual,
        attempt=attempt,
    )
    window.automation_running.add(task.task_id)
    window.automation_pending[request_id] = context
    window.automation_state.mark_started(task.task_id)
    if advance_schedule:
        if delivery:
            task.advance_delivery_after_start(now)
        else:
            task.advance_run_after_start(now)
        save_automation_tasks(window.settings, window.automation_tasks)

    mode = "每日归档" if delivery else ("立即执行" if manual else "定时执行")
    window.append_log(
        f"{mode}“{task.name}”：读取 {checkpoint:%Y-%m-%d %H:%M:%S} 至 {cutoff:%Y-%m-%d %H:%M:%S} 的聊天记录"
    )
    window.client_thread.request_automation_history(request_id, task.target_session_id, task.history_limit)


def _handle_automation_payload(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("automation_login_info"):
        if payload.get("ok"):
            window.automation_self_qq = str(payload.get("user_id") or "")
            if window.automation_self_qq:
                window.append_log(f"定时任务已识别机器人 QQ：{window.automation_self_qq}")
        return
    if payload.get("automation_upload"):
        _handle_upload_result(window, payload)
        return
    if not payload.get("automation_history"):
        return

    request_id = str(payload.get("request_id") or "")
    context = window.automation_pending.pop(request_id, None)
    if context is None:
        return
    task = task_by_id(window.automation_tasks, context.task_id)
    if task is None:
        _finish_task(window, context.task_id)
        return
    error = str(payload.get("error") or "")
    if error:
        _fail_run(window, task, context, error)
        return

    fetched = [message for message in payload.get("messages", []) if isinstance(message, ChatMessage)]
    current = [message for message in window.messages.get(task.target_session_id, []) if isinstance(message, ChatMessage)]
    merged: dict[str, ChatMessage] = {}
    for message in [*fetched, *current]:
        merged[message_key(message)] = message
    candidates = [
        message
        for message in sorted(merged.values(), key=lambda item: item.timestamp)
        if context.checkpoint <= message.timestamp <= context.cutoff
    ]
    keys = [message_key(message) for message in candidates]
    processed = window.automation_state.processed_keys(task.task_id, keys)
    messages = [message for message in candidates if message_key(message) not in processed]

    if not messages and not context.delivery:
        window.automation_state.mark_success(task.task_id, context.cutoff, "", [], status="no_new_messages")
        window.append_log(f"定时任务“{task.name}”没有新消息，本轮结束")
        _finish_task(window, task.task_id)
        return

    window.append_log(
        f"定时任务“{task.name}”已取得 {len(messages)} 条未处理消息，正在调用 AI"
        if messages
        else f"定时任务“{task.name}”没有新增消息，正在准备每日文件归档"
    )
    config = ui_module.load_ai_config(window.settings).normalized()

    def worker() -> None:
        try:
            work_date = task_work_date(context.cutoff, context.delivery)
            path = artifact_path(task, work_date) if task.file_enabled else None
            existing = load_records(path) if path is not None else []
            if messages:
                result = generate_scheduled_result(
                    ai_module,
                    config,
                    task,
                    messages,
                    records_for_ai(existing),
                    checkpoint_time=context.checkpoint,
                    cutoff_time=context.cutoff,
                )
            else:
                result = None
            stats = {"inserted": 0, "updated": 0, "ignored": 0}
            if task.file_enabled:
                if result is not None:
                    existing, stats = apply_operations(task, existing, result.operations)
                path = write_artifact(task, work_date, existing)
            payload_ready = {
                "context": context,
                "task_id": task.task_id,
                "messages": messages,
                "message_keys": [message_key(message) for message in messages],
                "checkpoint_message_id": messages[-1].message_id if messages else "",
                "text": result.text if result is not None else "",
                "stats": stats,
                "path": str(path) if path is not None else "",
            }
            window.automation_bridge.ready.emit(payload_ready)
        except Exception as exc:  # noqa: BLE001
            window.automation_bridge.failed.emit(
                {"context": context, "task_id": task.task_id, "error": str(exc)}
            )

    threading.Thread(target=worker, daemon=True).start()


def _handle_execution_ready(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
    del ui_module, ai_module
    if not isinstance(payload, dict):
        return
    context = payload.get("context")
    if not isinstance(context, AutomationRunContext):
        return
    task = task_by_id(window.automation_tasks, str(payload.get("task_id") or ""))
    if task is None:
        _finish_task(window, context.task_id)
        return
    stats = payload.get("stats") or {}
    text = str(payload.get("text") or "").strip()
    message_keys = [str(value) for value in payload.get("message_keys", []) if str(value)]
    checkpoint_message_id = str(payload.get("checkpoint_message_id") or "")

    if text and task.output_mode == OUTPUT_SEND_TEXT and not context.delivery:
        _send_task_text(window, task, text)

    if task.file_enabled:
        window.append_log(
            f"定时任务“{task.name}”已更新文件：新增 {stats.get('inserted', 0)}，"
            f"更新 {stats.get('updated', 0)}，忽略 {stats.get('ignored', 0)}"
        )

    if context.delivery and task.file_enabled:
        path = str(payload.get("path") or "")
        recipient = _resolve_recipient(window, task)
        if not recipient:
            _fail_run(window, task, context, "每日文件接收 QQ 未配置或尚未取得机器人自身 QQ")
            return
        upload_id = f"upload_{uuid.uuid4().hex[:18]}"
        window.automation_uploads[upload_id] = AutomationUploadContext(
            upload_id=upload_id,
            run=context,
            path=path,
            message_keys=message_keys,
            checkpoint_message_id=checkpoint_message_id,
        )
        window.append_log(f"定时任务“{task.name}”正在把 {Path(path).name} 私聊发送给 QQ {recipient}")
        window.client_thread.upload_automation_file(upload_id, recipient, path, Path(path).name)
        return

    window.automation_state.mark_success(
        task.task_id,
        context.cutoff,
        checkpoint_message_id,
        message_keys,
    )
    window.append_log(f"定时任务“{task.name}”执行完成")
    _finish_task(window, task.task_id)


def _handle_execution_failed(window: Any, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    context = payload.get("context")
    task = task_by_id(window.automation_tasks, str(payload.get("task_id") or ""))
    if isinstance(context, AutomationRunContext) and task is not None:
        _fail_run(window, task, context, str(payload.get("error") or "未知错误"))


def _handle_upload_result(window: Any, payload: dict[str, Any]) -> None:
    upload_id = str(payload.get("upload_id") or "")
    upload = window.automation_uploads.pop(upload_id, None)
    if upload is None:
        return
    task = task_by_id(window.automation_tasks, upload.run.task_id)
    if task is None:
        _finish_task(window, upload.run.task_id)
        return
    if not payload.get("ok"):
        _fail_run(window, task, upload.run, f"文件发送失败：{payload.get('error') or '未知错误'}")
        return

    window.automation_state.mark_success(
        task.task_id,
        upload.run.cutoff,
        upload.checkpoint_message_id,
        upload.message_keys,
        status="file_sent",
    )
    if task.delete_after_send:
        try:
            delete_artifact_bundle(Path(upload.path))
            window.append_log(f"定时任务“{task.name}”文件发送成功，旧文件已删除")
        except Exception as exc:  # noqa: BLE001
            window.append_log(f"定时任务“{task.name}”文件已发送，但删除旧文件失败：{exc}")
    try:
        ensure_empty_artifact(task, upload.run.cutoff.date())
        window.append_log(f"定时任务“{task.name}”已创建新一天的空文件")
    except Exception as exc:  # noqa: BLE001
        window.append_log(f"定时任务“{task.name}”创建新文件失败：{exc}")
    _finish_task(window, task.task_id)


def _fail_run(window: Any, task: AutomationTask, context: AutomationRunContext, error: str) -> None:
    attempt = context.attempt + 1
    window.automation_state.mark_failure(task.task_id, error, attempt)
    window.append_log(f"定时任务“{task.name}”失败：{error}")
    _finish_task(window, task.task_id)
    if attempt <= len(AUTOMATION_RETRY_DELAYS) and task.enabled:
        delay = AUTOMATION_RETRY_DELAYS[attempt - 1]
        window.automation_retries[task.task_id] = {
            "due": datetime.now() + timedelta(seconds=delay),
            "delivery": context.delivery,
            "attempt": attempt,
        }
        window.append_log(f"定时任务“{task.name}”将在 {delay} 秒后重试（{attempt}/3）")


def _finish_task(window: Any, task_id: str) -> None:
    window.automation_running.discard(task_id)
    for request_id, context in list(window.automation_pending.items()):
        if context.task_id == task_id:
            window.automation_pending.pop(request_id, None)


def _resolve_recipient(window: Any, task: AutomationTask) -> str:
    if task.recipient_mode == RECIPIENT_SELF:
        return str(window.automation_self_qq or "")
    return re.sub(r"\D", "", task.recipient_qq or "")


def _send_task_text(window: Any, task: AutomationTask, text: str) -> None:
    if window.client_thread is None or not text.strip():
        return
    session = window.sessions.get(task.target_session_id)
    window.client_thread.send_text(task.target_session_id, text[:8000])
    if session is not None:
        window.add_message(
            ChatMessage(
                session_id=session.session_id,
                session_name=session.name,
                session_kind=session.kind,
                sender_id="scheduled_task",
                sender_name="定时任务",
                text=text[:8000],
                outgoing=True,
            )
        )


def _open_task_manager(window: Any, ui_module: Any, ai_module: Any) -> None:
    dialog = AutomationTaskManagerDialog(window, ui_module, ai_module)
    dialog.exec()


class AutomationTaskManagerDialog(QDialog):
    def __init__(self, window: Any, ui_module: Any, ai_module: Any) -> None:
        super().__init__(window)
        self.window = window
        self.ui_module = ui_module
        self.ai_module = ai_module
        self.setWindowTitle("定时任务")
        self.resize(900, 620)
        self.setMinimumSize(760, 500)

        title = QLabel("定时任务")
        title.setStyleSheet("font-size:20px;font-weight:600;")
        tip = QLabel("任务只在程序运行并连接 NapCatQQ 时执行；错过的计划恢复后只补最近一次。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#667085;")
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._edit())

        add_button = QPushButton("新建")
        edit_button = QPushButton("编辑")
        copy_button = QPushButton("复制")
        delete_button = QPushButton("删除")
        run_button = QPushButton("立即执行")
        refresh_button = QPushButton("刷新")
        close_button = QPushButton("关闭")
        add_button.clicked.connect(self._add)
        edit_button.clicked.connect(self._edit)
        copy_button.clicked.connect(self._copy)
        delete_button.clicked.connect(self._delete)
        run_button.clicked.connect(self._run_now)
        refresh_button.clicked.connect(self._refresh)
        close_button.clicked.connect(self.accept)

        actions = QHBoxLayout()
        for button in (add_button, edit_button, copy_button, delete_button, run_button, refresh_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(tip)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(actions)
        self._refresh()

    def _selected_task(self) -> AutomationTask | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return task_by_id(self.window.automation_tasks, str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def _refresh(self) -> None:
        selected = self._selected_task()
        selected_id = selected.task_id if selected else ""
        self.list_widget.clear()
        for task in self.window.automation_tasks:
            state = self.window.automation_state.state(task.task_id)
            schedule = (
                f"每 {max(1, task.interval_seconds // 60)} 分钟"
                if task.schedule_type == SCHEDULE_INTERVAL
                else f"每天 {task.daily_time}"
            )
            enabled = "启用" if task.enabled else "停用"
            file_label = f" · {task.file_format.upper()}" if task.file_enabled else ""
            delivery = f" · 每天 {task.delivery_time} 发送文件" if task.daily_delivery_enabled else ""
            status = str(state.get("last_status") or "未运行")
            text = (
                f"{task.name}  [{enabled}]\n"
                f"{schedule}{delivery}{file_label} · {task.target_session_name or task.target_session_id or '未选择会话'}\n"
                f"下次执行：{_display_datetime(task.next_run_at)} · 上次状态：{status}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, task.task_id)
            item.setToolTip(str(state.get("last_error") or task.instruction))
            self.list_widget.addItem(item)
            if task.task_id == selected_id:
                self.list_widget.setCurrentItem(item)
        if self.list_widget.currentItem() is None and self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def _add(self) -> None:
        task = AutomationTask.create_default()
        dialog = AutomationTaskEditDialog(self.window, task, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.window.automation_tasks.append(dialog.task_value())
        save_automation_tasks(self.window.settings, self.window.automation_tasks)
        self._refresh()

    def _edit(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        dialog = AutomationTaskEditDialog(self.window, task, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.task_value()
        index = self.window.automation_tasks.index(task)
        self.window.automation_tasks[index] = updated
        save_automation_tasks(self.window.settings, self.window.automation_tasks)
        self._refresh()

    def _copy(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        copied = AutomationTask.from_dict(copy.deepcopy(task.to_dict()))
        copied.task_id = f"task_{uuid.uuid4().hex[:16]}"
        copied.name += " - 副本"
        copied.created_at = datetime.now().replace(microsecond=0).isoformat()
        copied.recalculate_next_times(datetime.now(), reset=True)
        self.window.automation_tasks.append(copied)
        save_automation_tasks(self.window.settings, self.window.automation_tasks)
        self._refresh()

    def _delete(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        answer = QMessageBox.question(
            self,
            "删除定时任务",
            f"确定删除“{task.name}”吗？\n任务工作区文件不会自动删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.window.automation_tasks.remove(task)
        self.window.automation_state.delete_task(task.task_id)
        save_automation_tasks(self.window.settings, self.window.automation_tasks)
        self._refresh()

    def _run_now(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        _start_task(
            self.window,
            self.ui_module,
            self.ai_module,
            task,
            delivery=False,
            manual=True,
            attempt=0,
            advance_schedule=False,
        )
        self._refresh()


class AutomationTaskEditDialog(QDialog):
    def __init__(self, window: Any, task: AutomationTask, parent: Any = None) -> None:
        super().__init__(parent)
        self.window = window
        self.task = AutomationTask.from_dict(copy.deepcopy(task.to_dict()))
        self.setWindowTitle("编辑定时任务")
        self.resize(780, 720)
        self.setMinimumSize(680, 600)

        self.name_input = QLineEdit(self.task.name)
        self.enabled_input = QCheckBox("启用任务")
        self.enabled_input.setChecked(self.task.enabled)
        self.target_input = QComboBox()
        self.target_input.setEditable(True)
        self.target_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.target_input.addItem("请选择群聊或私聊", "")
        for session in sorted(window.sessions.values(), key=lambda item: (item.kind, item.name)):
            if session.kind not in {"group", "private"}:
                continue
            kind = "群聊" if session.kind == "group" else "私聊"
            self.target_input.addItem(f"{kind} · {session.name} · {session.session_id}", session.session_id)
        self._select_combo_data(self.target_input, self.task.target_session_id, self.task.target_session_name)

        self.schedule_type_input = QComboBox()
        self.schedule_type_input.addItem("从任务创建时间开始，按固定间隔", SCHEDULE_INTERVAL)
        self.schedule_type_input.addItem("每天固定时间", SCHEDULE_DAILY)
        self._select_combo_data(self.schedule_type_input, self.task.schedule_type)
        self.interval_input = QSpinBox()
        self.interval_input.setRange(1, 31 * 24 * 60)
        self.interval_input.setValue(max(1, self.task.interval_seconds // 60))
        self.interval_input.setSuffix(" 分钟")
        self.daily_time_input = QTimeEdit(QTime.fromString(self.task.daily_time, "HH:mm"))
        self.daily_time_input.setDisplayFormat("HH:mm")
        self.instruction_input = QPlainTextEdit(self.task.instruction)
        self.instruction_input.setPlaceholderText("例如：检查上次执行以来的群聊，把用户问题和后续回答写入工作簿。")
        self.instruction_input.setMinimumHeight(140)
        self.output_mode_input = QComboBox()
        self.output_mode_input.addItem("静默执行，只写日志或文件", OUTPUT_SILENT)
        self.output_mode_input.addItem("把 AI 文本结果发送到目标会话", OUTPUT_SEND_TEXT)
        self._select_combo_data(self.output_mode_input, self.task.output_mode)
        self.history_limit_input = QSpinBox()
        self.history_limit_input.setRange(20, 5000)
        self.history_limit_input.setValue(self.task.history_limit)
        self.history_limit_input.setSuffix(" 条")

        base_form = QFormLayout()
        base_form.addRow("任务名称", self.name_input)
        base_form.addRow("状态", self.enabled_input)
        base_form.addRow("目标会话", self.target_input)
        base_form.addRow("调度方式", self.schedule_type_input)
        base_form.addRow("执行间隔", self.interval_input)
        base_form.addRow("每日时间", self.daily_time_input)
        base_form.addRow("最多读取历史", self.history_limit_input)
        base_form.addRow("结果处理", self.output_mode_input)
        base_form.addRow("执行指令", self.instruction_input)
        base_widget = QWidget()
        base_widget.setLayout(base_form)

        self.file_enabled_input = QCheckBox("启用本地文件工作区 Skill（仅定时任务可用）")
        self.file_enabled_input.setChecked(self.task.file_enabled)
        self.file_format_input = QComboBox()
        for value in ("xlsx", "csv", "json", "md"):
            self.file_format_input.addItem(value.upper(), value)
        self._select_combo_data(self.file_format_input, self.task.file_format)
        self.file_name_input = QLineEdit(self.task.file_name_template)
        self.file_name_input.setPlaceholderText("{date}_{task_name}.xlsx")
        self.sheet_name_input = QLineEdit(self.task.sheet_name)
        self.columns_input = QPlainTextEdit(_format_columns(self.task.columns))
        self.columns_input.setMinimumHeight(180)
        self.columns_input.setPlaceholderText("列名|类型|必填/可选|枚举值逗号分隔|默认值|可更新/只读")
        self.dedup_input = QLineEdit("、".join(self.task.dedup_fields))
        self.dedup_input.setPlaceholderText("用逗号分隔，例如：人员、内容")
        schema_tip = QLabel(
            "每行定义一列：列名|类型(text/number/datetime/boolean/enum)|必填或可选|枚举值|默认值|可更新或只读。"
        )
        schema_tip.setWordWrap(True)
        schema_tip.setStyleSheet("color:#667085;")

        self.delivery_enabled_input = QCheckBox("每天把当前文件私聊发送并在成功后删除旧文件")
        self.delivery_enabled_input.setChecked(self.task.daily_delivery_enabled)
        self.delivery_time_input = QTimeEdit(QTime.fromString(self.task.delivery_time, "HH:mm"))
        self.delivery_time_input.setDisplayFormat("HH:mm")
        self.recipient_input = QComboBox()
        self.recipient_input.setEditable(True)
        self.recipient_input.addItem("机器人自己的 QQ", "__self__")
        for session in sorted(window.sessions.values(), key=lambda item: item.name):
            if session.kind != "private":
                continue
            qq = session.session_id.split(":", 1)[1]
            self.recipient_input.addItem(f"{session.name} · {qq}", qq)
        if self.task.recipient_mode == RECIPIENT_SELF:
            self.recipient_input.setCurrentIndex(0)
        else:
            self._select_combo_data(self.recipient_input, self.task.recipient_qq, self.task.recipient_qq)
        self.delete_after_send_input = QCheckBox("文件发送成功后自动删除")
        self.delete_after_send_input.setChecked(True)
        self.delete_after_send_input.setEnabled(False)

        file_form = QFormLayout()
        file_form.addRow(self.file_enabled_input)
        file_form.addRow("文件格式", self.file_format_input)
        file_form.addRow("文件名模板", self.file_name_input)
        file_form.addRow("工作表名称", self.sheet_name_input)
        file_form.addRow("列结构", self.columns_input)
        file_form.addRow("", schema_tip)
        file_form.addRow("去重字段", self.dedup_input)
        file_form.addRow(self.delivery_enabled_input)
        file_form.addRow("每日发送时间", self.delivery_time_input)
        file_form.addRow("私聊接收人", self.recipient_input)
        file_form.addRow(self.delete_after_send_input)
        file_widget = QWidget()
        file_widget.setLayout(file_form)

        tabs = QTabWidget()
        tabs.addTab(base_widget, "基础与调度")
        tabs.addTab(file_widget, "本地文件工作区")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)

        self.schedule_type_input.currentIndexChanged.connect(self._sync_controls)
        self.file_enabled_input.toggled.connect(self._sync_controls)
        self.delivery_enabled_input.toggled.connect(self._sync_controls)
        self.file_format_input.currentIndexChanged.connect(self._sync_file_suffix)
        self._sync_controls()

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: str, fallback_text: str = "") -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif fallback_text:
            combo.addItem(fallback_text, value)
            combo.setCurrentIndex(combo.count() - 1)

    def _sync_controls(self) -> None:
        daily = self.schedule_type_input.currentData() == SCHEDULE_DAILY
        self.interval_input.setEnabled(not daily)
        self.daily_time_input.setEnabled(daily)
        file_enabled = self.file_enabled_input.isChecked()
        for widget in (
            self.file_format_input,
            self.file_name_input,
            self.sheet_name_input,
            self.columns_input,
            self.dedup_input,
            self.delivery_enabled_input,
        ):
            widget.setEnabled(file_enabled)
        delivery = file_enabled and self.delivery_enabled_input.isChecked()
        self.delivery_time_input.setEnabled(delivery)
        self.recipient_input.setEnabled(delivery)

    def _sync_file_suffix(self) -> None:
        suffix = str(self.file_format_input.currentData() or "xlsx")
        current = self.file_name_input.text().strip()
        if current:
            self.file_name_input.setText(str(Path(current).with_suffix(f".{suffix}")))

    def _target_value(self) -> tuple[str, str]:
        data = str(self.target_input.currentData() or "")
        if data:
            text = self.target_input.currentText()
            name = text.split(" · ")[1] if " · " in text and len(text.split(" · ")) >= 2 else data
            return data, name
        raw = self.target_input.currentText().strip()
        return raw, raw

    def _recipient_value(self) -> tuple[str, str]:
        data = str(self.recipient_input.currentData() or "")
        if data == "__self__":
            return RECIPIENT_SELF, ""
        qq = re.sub(r"\D", "", data or self.recipient_input.currentText())
        return (RECIPIENT_CONTACT if data else RECIPIENT_MANUAL), qq

    def _validate_and_accept(self) -> None:
        target_id, target_name = self._target_value()
        if not target_id.startswith(("group:", "private:")):
            QMessageBox.warning(self, "目标会话无效", "请选择已有会话，或手动填写 group:群号 / private:QQ号。")
            return
        instruction = self.instruction_input.toPlainText().strip()
        if not instruction:
            QMessageBox.warning(self, "缺少执行指令", "请填写定时任务要交给 AI 执行的指令。")
            return
        try:
            columns = _parse_columns(self.columns_input.toPlainText())
        except ValueError as exc:
            QMessageBox.warning(self, "列结构无效", str(exc))
            return
        recipient_mode, recipient_qq = self._recipient_value()
        if self.file_enabled_input.isChecked() and self.delivery_enabled_input.isChecked():
            if recipient_mode != RECIPIENT_SELF and not recipient_qq:
                QMessageBox.warning(self, "接收人无效", "请选择机器人自己、已有私聊好友，或手动填写 QQ 号。")
                return

        self.task.name = self.name_input.text().strip()
        self.task.enabled = self.enabled_input.isChecked()
        self.task.target_session_id = target_id
        self.task.target_session_name = target_name
        self.task.schedule_type = str(self.schedule_type_input.currentData())
        self.task.interval_seconds = self.interval_input.value() * 60
        self.task.daily_time = self.daily_time_input.time().toString("HH:mm")
        self.task.history_limit = self.history_limit_input.value()
        self.task.instruction = instruction
        self.task.output_mode = str(self.output_mode_input.currentData())
        self.task.file_enabled = self.file_enabled_input.isChecked()
        self.task.file_format = str(self.file_format_input.currentData())
        self.task.file_name_template = self.file_name_input.text().strip()
        self.task.sheet_name = self.sheet_name_input.text().strip()
        self.task.columns = columns
        self.task.dedup_fields = [
            item.strip()
            for item in re.split(r"[,，、;；]", self.dedup_input.text())
            if item.strip()
        ]
        self.task.daily_delivery_enabled = self.task.file_enabled and self.delivery_enabled_input.isChecked()
        self.task.delivery_time = self.delivery_time_input.time().toString("HH:mm")
        self.task.recipient_mode = recipient_mode
        self.task.recipient_qq = recipient_qq
        self.task.delete_after_send = True
        self.task.normalize()
        self.task.recalculate_next_times(datetime.now(), reset=True)
        self.accept()

    def task_value(self) -> AutomationTask:
        return self.task


def _parse_columns(text: str) -> list[AutomationColumn]:
    columns: list[AutomationColumn] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        name = parts[0] if parts else ""
        if not name:
            raise ValueError(f"第 {line_number} 行缺少列名")
        if name in seen:
            raise ValueError(f"列名“{name}”重复")
        seen.add(name)
        value_type = parts[1].lower() if len(parts) > 1 and parts[1] else "text"
        required = len(parts) > 2 and parts[2] in {"必填", "required", "是", "true", "1"}
        enum_values = [value.strip() for value in re.split(r"[,，]", parts[3])] if len(parts) > 3 and parts[3] else []
        default = parts[4] if len(parts) > 4 else ""
        ai_update = not (len(parts) > 5 and parts[5] in {"只读", "readonly", "否", "false", "0"})
        column = AutomationColumn(name, value_type, required, enum_values, default, ai_update).normalized()
        if column.name:
            columns.append(column)
    if not columns:
        raise ValueError("至少需要定义一列")
    return columns


def _format_columns(columns: list[AutomationColumn]) -> str:
    lines = []
    for column in columns:
        lines.append(
            "|".join(
                [
                    column.name,
                    column.value_type,
                    "必填" if column.required else "可选",
                    ",".join(column.enum_values),
                    column.default,
                    "可更新" if column.ai_update else "只读",
                ]
            )
        )
    return "\n".join(lines)


def _display_datetime(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "未设置"
