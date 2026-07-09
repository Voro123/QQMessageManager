from __future__ import annotations

import asyncio
import random
import threading
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any

from PySide6.QtCore import QDateTime, QObject, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
)

from .ai_summary import generate_chat_summary
from .models import ChatMessage

SUMMARY_DEFAULT_MAX_MESSAGES = 200
SUMMARY_MAX_MESSAGES_LIMIT = 1000


@dataclass(slots=True)
class SummaryRequest:
    request_id: str
    session_id: str
    session_name: str
    session_kind: str
    start_time: datetime | None
    end_time: datetime | None
    max_messages: int


class SummaryBridge(QObject):
    summary_ready = Signal(str, str, int)
    summary_failed = Signal(str, str)


def install_chat_summary_feature(ui_module: Any, napcat_module: Any) -> None:
    _install_napcat_summary_request(napcat_module)
    _install_main_window_summary_ui(ui_module)


def _install_napcat_summary_request(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_chat_summary_installed", False):
        return

    original_handle_action_response = worker_cls._handle_action_response

    def request_summary_history(self: Any, request_id: str, session_id: str, count: int) -> None:
        count = max(1, min(int(count), SUMMARY_MAX_MESSAGES_LIMIT))
        if self._loop is None or not self._loop.is_running():
            self.log.emit("当前未连接，无法请求总结历史消息")
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._request_summary_history(request_id, session_id, count))
        )

    async def _request_summary_history(self: Any, request_id: str, session_id: str, count: int) -> None:
        kind, target_id = napcat_module._split_session_id(session_id)
        if not target_id:
            self.history_messages_received.emit(
                {"summary_history": True, "request_id": request_id, "session_id": session_id, "messages": [], "error": "无效会话"}
            )
            return
        if kind == "group":
            action = "get_group_msg_history"
            params = {"group_id": napcat_module._onebot_id(target_id), "count": count}
        elif kind == "private":
            action = "get_friend_msg_history"
            params = {"user_id": napcat_module._onebot_id(target_id), "count": count}
        else:
            self.history_messages_received.emit(
                {"summary_history": True, "request_id": request_id, "session_id": session_id, "messages": [], "error": "该会话类型不支持总结"}
            )
            return
        await self._send_action(action, params, self._next_echo(f"summary_history:{request_id}:{session_id}"))

    def handle_action_response(self: Any, event: dict[str, Any]) -> bool:
        echo = event.get("echo")
        echo_text = str(echo) if echo else ""
        if echo_text.startswith("summary_history:"):
            request_id, session_id = _parse_summary_echo(echo_text)
            ok = event.get("status") == "ok" or event.get("retcode") == 0
            if ok:
                kind, _ = napcat_module._split_session_id(session_id)
                messages = napcat_module._extract_history_messages(event.get("data"), session_id, kind)
                self.history_messages_received.emit(
                    {"summary_history": True, "request_id": request_id, "session_id": session_id, "messages": messages, "error": ""}
                )
                self.log.emit(f"已为总结请求读取 {session_id} 的 {len(messages)} 条历史消息")
            else:
                error = f"获取总结历史失败：{napcat_module._action_error(event)}"
                self.history_messages_received.emit(
                    {"summary_history": True, "request_id": request_id, "session_id": session_id, "messages": [], "error": error}
                )
                self.log.emit(error)
            return True
        return original_handle_action_response(self, event)

    def thread_request_summary_history(self: Any, request_id: str, session_id: str, count: int) -> None:
        self.worker.request_summary_history(request_id, session_id, count)

    worker_cls.request_summary_history = request_summary_history
    worker_cls._request_summary_history = _request_summary_history
    worker_cls._handle_action_response = handle_action_response
    thread_cls.request_summary_history = thread_request_summary_history
    worker_cls._chat_summary_installed = True


def _install_main_window_summary_ui(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_chat_summary_ui_installed", False):
        return

    original_init = main_window_cls.__init__
    original_start = main_window_cls.start
    original_disconnect = main_window_cls.disconnect_from_server

    def init_with_summary(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.chat_summary_pending: dict[str, SummaryRequest] = {}
        self.chat_summary_bridge = SummaryBridge(self)
        self.chat_summary_bridge.summary_ready.connect(lambda title, summary, count: _show_summary_result(self, title, summary, count))
        self.chat_summary_bridge.summary_failed.connect(lambda title, error: _show_summary_error(self, title, error))
        self.summary_button = QPushButton("总结")
        self.summary_button.setToolTip("总结当前群聊/私聊在指定时间区间内发生的内容")
        self.summary_button.clicked.connect(lambda: _open_summary_dialog(self, ui_module))
        send_bar = self.message_input.parentWidget()
        layout = send_bar.layout() if send_bar is not None else None
        if layout is not None:
            layout.insertWidget(max(0, layout.count() - 3), self.summary_button)

    def start_with_summary(self: Any) -> None:
        original_start(self)
        if self.client_thread is not None and not getattr(self, "_chat_summary_signal_connected", False):
            self.client_thread.history_messages_received.connect(lambda payload: _handle_summary_history(self, ui_module, payload))
            self._chat_summary_signal_connected = True

    def disconnect_with_summary_clear(self: Any) -> None:
        pending = getattr(self, "chat_summary_pending", None)
        if pending is not None:
            pending.clear()
        original_disconnect(self)

    main_window_cls.__init__ = init_with_summary
    main_window_cls.start = start_with_summary
    main_window_cls.disconnect_from_server = disconnect_with_summary_clear
    main_window_cls._chat_summary_ui_installed = True


def _open_summary_dialog(window: Any, ui_module: Any) -> None:
    session_id = getattr(window, "current_session_id", None)
    if not session_id or session_id not in window.sessions:
        QMessageBox.information(window, "请选择会话", "请先选择一个群聊或私聊。")
        return
    session = window.sessions[session_id]
    if session.kind not in {"group", "private"}:
        QMessageBox.information(window, "无法总结", "当前会话类型不支持总结。")
        return
    if window.client_thread is None:
        QMessageBox.warning(window, "未连接", "当前未连接 NapCatQQ，无法读取历史消息。")
        return
    config = ui_module.load_ai_config(window.settings).normalized()
    if not config.api_key:
        QMessageBox.warning(window, "缺少 API Key", "请先在 AI 设置中填写 API Key。")
        return

    dialog = SummarySettingsDialog(window.settings, session.name, session.kind, window)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    start_time, end_time, max_messages = dialog.values()
    request_id = _new_request_id()
    request = SummaryRequest(
        request_id=request_id,
        session_id=session_id,
        session_name=session.name,
        session_kind=session.kind,
        start_time=start_time,
        end_time=end_time,
        max_messages=max_messages,
    )
    window.chat_summary_pending[request_id] = request
    window.append_log(f"正在读取 {session.name} 的历史消息用于总结，最多 {max_messages} 条")
    window.client_thread.request_summary_history(request_id, session_id, max_messages)


def _handle_summary_history(window: Any, ui_module: Any, payload: Any) -> None:
    if not isinstance(payload, dict) or not payload.get("summary_history"):
        return
    request_id = str(payload.get("request_id") or "")
    request = window.chat_summary_pending.pop(request_id, None)
    if request is None:
        return
    error = str(payload.get("error") or "")
    if error:
        window.chat_summary_bridge.summary_failed.emit(request.session_name, error)
        return

    fetched_messages = payload.get("messages") or []
    messages = _merge_messages(fetched_messages, window.messages.get(request.session_id, []))
    messages = _filter_summary_messages(messages, request.start_time, request.end_time, request.max_messages)
    if not messages:
        window.chat_summary_bridge.summary_failed.emit(request.session_name, "指定范围内没有可总结的消息。")
        return

    config = ui_module.load_ai_config(window.settings).normalized()
    window.append_log(f"已读取 {len(messages)} 条消息，正在调用 AI 总结")

    def worker() -> None:
        try:
            summary = generate_chat_summary(
                config,
                session_name=request.session_name,
                session_kind=request.session_kind,
                messages=messages,
                start_time=request.start_time,
                end_time=request.end_time,
            )
            window.chat_summary_bridge.summary_ready.emit(request.session_name, summary, len(messages))
        except Exception as exc:  # noqa: BLE001
            window.chat_summary_bridge.summary_failed.emit(request.session_name, f"AI 总结失败：{exc}")

    threading.Thread(target=worker, daemon=True).start()


def _merge_messages(*message_lists: list[ChatMessage]) -> list[ChatMessage]:
    merged: dict[str, ChatMessage] = {}
    for messages in message_lists:
        for message in messages:
            if not isinstance(message, ChatMessage):
                continue
            key = message.message_id or f"{message.session_id}:{message.sender_id}:{int(message.timestamp.timestamp())}:{message.text}"
            merged[key] = message
    return sorted(merged.values(), key=lambda item: item.timestamp)


def _filter_summary_messages(
    messages: list[ChatMessage],
    start_time: datetime | None,
    end_time: datetime | None,
    max_messages: int,
) -> list[ChatMessage]:
    filtered = []
    for message in messages:
        if start_time is not None and message.timestamp < start_time:
            continue
        if end_time is not None and message.timestamp > end_time:
            continue
        filtered.append(message)
    if len(filtered) > max_messages:
        filtered = filtered[-max_messages:]
    return filtered


def _show_summary_result(window: Any, title: str, summary: str, count: int) -> None:
    window.append_log(f"已完成 {title} 的聊天总结（{count} 条消息）")
    dialog = SummaryResultDialog(title, summary, count, window)
    dialog.exec()


def _show_summary_error(window: Any, title: str, error: str) -> None:
    window.append_log(f"聊天总结失败：{error}")
    QMessageBox.warning(window, f"总结失败 · {title}", error)


def _parse_summary_echo(echo_text: str) -> tuple[str, str]:
    body = echo_text[len("summary_history:") :]
    parts = body.split(":")
    if len(parts) < 3:
        return "", ""
    request_id = parts[0]
    session_id = ":".join(parts[1:-1])
    return request_id, session_id


def _new_request_id() -> str:
    return f"sum_{int(datetime.now().timestamp() * 1000)}_{random.randint(1000, 9999)}"


class SummarySettingsDialog(QDialog):
    def __init__(self, settings: Any, session_name: str, session_kind: str, parent: Any = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("总结当前会话")
        self.setMinimumWidth(520)
        kind_label = "群聊" if session_kind == "group" else "私聊"

        self.start_enabled = QCheckBox("限制开始时间")
        self.end_enabled = QCheckBox("限制结束时间")
        now = QDateTime.currentDateTime()
        self.start_input = QDateTimeEdit(now.addDays(-1))
        self.end_input = QDateTimeEdit(now)
        for widget in (self.start_input, self.end_input):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.start_input.setEnabled(False)
        self.end_input.setEnabled(False)
        self.start_enabled.toggled.connect(self.start_input.setEnabled)
        self.end_enabled.toggled.connect(self.end_input.setEnabled)

        self.max_messages = QSpinBox()
        self.max_messages.setRange(1, SUMMARY_MAX_MESSAGES_LIMIT)
        self.max_messages.setValue(_setting_int(settings, "summary/max_messages", SUMMARY_DEFAULT_MAX_MESSAGES))

        form = QFormLayout()
        form.addRow("会话", QLabel(f"{kind_label} · {session_name}"))
        form.addRow("开始", self.start_enabled)
        form.addRow("开始时间", self.start_input)
        form.addRow("结束", self.end_enabled)
        form.addRow("结束时间", self.end_input)
        form.addRow("消息最大数量", self.max_messages)
        tip = QLabel("默认不限制时间区间；程序会主动向 NapCat 读取历史消息，而不是只总结窗口里已经显示的消息。")
        tip.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(tip)
        layout.addWidget(buttons)

    def values(self) -> tuple[datetime | None, datetime | None, int]:
        max_messages = self.max_messages.value()
        self.settings.setValue("summary/max_messages", max_messages)
        self.settings.sync()
        start_time = _qdatetime_to_datetime(self.start_input.dateTime()) if self.start_enabled.isChecked() else None
        end_time = _qdatetime_to_datetime(self.end_input.dateTime()) if self.end_enabled.isChecked() else None
        return start_time, end_time, max_messages


class SummaryResultDialog(QDialog):
    def __init__(self, title: str, summary: str, count: int, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"聊天总结 · {title}")
        self.resize(760, 620)
        heading = QLabel(f"{title} · 已总结 {count} 条消息")
        heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        browser = QTextBrowser()
        browser.setMarkdown(summary)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(browser)
        layout.addWidget(buttons)


def _qdatetime_to_datetime(value: QDateTime) -> datetime:
    return datetime.fromtimestamp(value.toSecsSinceEpoch())


def _setting_int(settings: Any, key: str, default: int) -> int:
    value = settings.value(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
