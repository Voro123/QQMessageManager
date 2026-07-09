from __future__ import annotations

import json
import random
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QSettings, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .ai_client import AI_PROVIDER_MINIMAX_M3, AiReplyConfig, generate_ai_reply
from .models import ChatMessage, ChatSession
from .napcat_client import NapCatClientThread

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "3001"
DEFAULT_PATH = ""
DEFAULT_URL = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}{DEFAULT_PATH}"
SETTINGS_ORGANIZATION = "QQMessageManager"
SETTINGS_APPLICATION = "QQMessageManager"
PINNED_SESSIONS_KEY = "chat/pinned_sessions"
AI_MANAGED_SESSIONS_KEY = "ai/managed_sessions"
PINNED_BACKGROUND = QColor("#fff3cd")
NORMAL_BACKGROUND = QColor("#ffffff")


class LoginWindow(QWidget):
    login_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)
        self.setWindowTitle("QQMessageManager - 连接 NapCatQQ")
        self.setMinimumWidth(560)

        self.url_input = QLineEdit(self._setting_text("login/url", DEFAULT_URL))
        self.host_input = QLineEdit(self._setting_text("login/host", DEFAULT_HOST))
        self.port_input = QLineEdit(self._setting_text("login/port", DEFAULT_PORT))
        self.path_input = QLineEdit(self._setting_text("login/path", DEFAULT_PATH))
        self.path_input.setPlaceholderText("正向 WS 通常留空；有自定义路径时再填写")
        self.token_input = QLineEdit(self._setting_text("login/token", ""))
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("没有配置 Token 可留空；连接后会记住上次输入")

        self.use_full_url = QCheckBox("优先使用完整 WebSocket 地址")
        self.use_full_url.setChecked(self._setting_bool("login/use_full_url", True))
        self.use_full_url.toggled.connect(self._sync_form_state)

        connect_button = QPushButton("连接")
        connect_button.clicked.connect(self._emit_login)

        title = QLabel("QQMessageManager")
        font = QFont()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        subtitle = QLabel("通过 NapCatQQ 正向 WebSocket 实时接收并统一展示 QQ 私聊和群聊消息")
        subtitle.setObjectName("subtitle")

        form = QFormLayout()
        form.addRow("完整地址", self.url_input)
        form.addRow("Host", self.host_input)
        form.addRow("Port", self.port_input)
        form.addRow("Path", self.path_input)
        form.addRow("Token", self.token_input)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(12)
        layout.addWidget(self.use_full_url)
        layout.addLayout(form)
        layout.addWidget(connect_button, alignment=Qt.AlignmentFlag.AlignRight)

        self.setStyleSheet(
            """
            QWidget { font-size: 14px; }
            #subtitle { color: #666; }
            QLineEdit { padding: 8px; border: 1px solid #d5d5d5; border-radius: 6px; }
            QPushButton { padding: 8px 22px; border-radius: 6px; background: #12b7f5; color: white; }
            QPushButton:hover { background: #0aa4df; }
            """
        )
        self._sync_form_state()

    def _sync_form_state(self) -> None:
        use_full = self.use_full_url.isChecked()
        self.url_input.setEnabled(use_full)
        self.host_input.setEnabled(not use_full)
        self.port_input.setEnabled(not use_full)
        self.path_input.setEnabled(not use_full)

    def _emit_login(self) -> None:
        websocket_url = self._build_url()
        if not websocket_url:
            QMessageBox.warning(self, "连接信息不完整", "请输入有效的 WebSocket 地址。")
            return
        self._save_login_settings(websocket_url)
        self.login_requested.emit(websocket_url, self.token_input.text().strip())

    def _build_url(self) -> str:
        if self.use_full_url.isChecked():
            url = self.url_input.text().strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
                return ""
            return url

        host = self.host_input.text().strip() or DEFAULT_HOST
        port = self.port_input.text().strip() or DEFAULT_PORT
        path = self.path_input.text().strip()
        if path and not path.startswith("/"):
            path = f"/{path}"
        return f"ws://{host}:{port}{path}"

    def _save_login_settings(self, websocket_url: str) -> None:
        self.settings.setValue("login/url", websocket_url)
        self.settings.setValue("login/host", self.host_input.text().strip() or DEFAULT_HOST)
        self.settings.setValue("login/port", self.port_input.text().strip() or DEFAULT_PORT)
        self.settings.setValue("login/path", self.path_input.text().strip())
        self.settings.setValue("login/token", self.token_input.text().strip())
        self.settings.setValue("login/use_full_url", self.use_full_url.isChecked())
        self.settings.sync()

    def _setting_text(self, key: str, default: str) -> str:
        value = self.settings.value(key, default)
        if value is None:
            return default
        return str(value)

    def _setting_bool(self, key: str, default: bool) -> bool:
        value = self.settings.value(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}


class AiSettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("AI 代管设置")
        self.setMinimumWidth(620)

        config = load_ai_config(settings)

        self.provider_input = QComboBox()
        self.provider_input.addItem(AI_PROVIDER_MINIMAX_M3)
        self.provider_input.setCurrentText(config.provider)

        self.api_key_input = QLineEdit(config.api_key)
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Minimax API Key")

        self.prompt_input = QTextEdit(config.prompt)
        self.prompt_input.setPlaceholderText("提供给 AI 的已知信息/人设/规则，默认为空")
        self.prompt_input.setMinimumHeight(120)

        self.timed_enabled = QCheckBox("收到非自己发言后自动发送")
        self.timed_enabled.setChecked(config.timed_enabled)
        self.timed_min = _spin(config.timed_min_seconds, 1, 3600)
        self.timed_max = _spin(config.timed_max_seconds, 1, 3600)

        self.require_recent_enabled = QCheckBox("如果最近没有其他人发言，则不发送新信息")
        self.require_recent_enabled.setChecked(config.require_recent_non_self_enabled)
        self.recent_seconds = _spin(config.recent_non_self_seconds, 1, 3600)

        self.context_count = _spin(config.context_message_count, 1, 100)

        self.mention_enabled = QCheckBox("被艾特时自动回复")
        self.mention_enabled.setChecked(config.mention_enabled)
        self.mention_min = _spin(config.mention_min_seconds, 1, 3600)
        self.mention_max = _spin(config.mention_max_seconds, 1, 3600)

        self.prevent_self_follow = QCheckBox("如果上一条发言人是自己，不发送信息")
        self.prevent_self_follow.setChecked(config.prevent_self_follow_enabled)

        self.allow_ai_skip = QCheckBox("允许 AI 自主判断本次不发送信息")
        self.allow_ai_skip.setChecked(config.allow_ai_skip_enabled)

        form = QFormLayout()
        form.addRow("AI 服务商", self.provider_input)
        form.addRow("API Key", self.api_key_input)
        form.addRow("Prompt", self.prompt_input)
        form.addRow("规则 2", self.timed_enabled)
        form.addRow("收到后发送延迟", _range_widget(self.timed_min, self.timed_max, "秒"))
        form.addRow("规则 3", self.require_recent_enabled)
        form.addRow("最近其他人发言窗口", _single_spin_widget(self.recent_seconds, "秒"))
        form.addRow("规则 4：参考消息数", self.context_count)
        form.addRow("规则 5", self.mention_enabled)
        form.addRow("被艾特回复延迟", _range_widget(self.mention_min, self.mention_max, "秒"))
        form.addRow("规则 6", self.prevent_self_follow)
        form.addRow("自主判断", self.allow_ai_skip)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def config(self) -> AiReplyConfig:
        return AiReplyConfig(
            provider=self.provider_input.currentText(),
            api_key=self.api_key_input.text(),
            prompt=self.prompt_input.toPlainText(),
            timed_enabled=self.timed_enabled.isChecked(),
            timed_min_seconds=self.timed_min.value(),
            timed_max_seconds=self.timed_max.value(),
            require_recent_non_self_enabled=self.require_recent_enabled.isChecked(),
            recent_non_self_seconds=self.recent_seconds.value(),
            context_message_count=self.context_count.value(),
            mention_enabled=self.mention_enabled.isChecked(),
            mention_min_seconds=self.mention_min.value(),
            mention_max_seconds=self.mention_max.value(),
            prevent_self_follow_enabled=self.prevent_self_follow.isChecked(),
            allow_ai_skip_enabled=self.allow_ai_skip.isChecked(),
        ).normalized()

    def accept(self) -> None:  # noqa: D102
        save_ai_config(self.settings, self.config())
        super().accept()


class MainWindow(QMainWindow):
    ai_reply_ready = Signal(str, str)
    ai_reply_failed = Signal(str, str)

    def __init__(self, websocket_url: str, token: str = "") -> None:
        super().__init__()
        self.settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)
        self.pinned_sessions = self._load_string_set(PINNED_SESSIONS_KEY)
        self.ai_managed_sessions = self._load_string_set(AI_MANAGED_SESSIONS_KEY)
        self.websocket_url = websocket_url
        self.token = token
        self.client_thread: NapCatClientThread | None = None
        self.sessions: dict[str, ChatSession] = {}
        self.messages: dict[str, list[ChatMessage]] = defaultdict(list)
        self.session_items: dict[str, QListWidgetItem] = {}
        self.seen_message_keys: set[str] = set()
        self.current_session_id: str | None = None
        self.last_non_self_message_time: dict[str, datetime] = {}
        self.ai_timers: dict[str, QTimer] = {}
        self.ai_inflight_sessions: set[str] = set()
        self.ai_reply_ready.connect(self._handle_ai_reply_ready)
        self.ai_reply_failed.connect(self._handle_ai_reply_failed)

        self.setWindowTitle("QQMessageManager")
        self.resize(1080, 720)
        self.setMinimumSize(820, 560)

        self.session_list = QListWidget()
        self.session_list.currentItemChanged.connect(self._on_session_changed)
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._show_session_menu)

        self.header_label = QLabel("等待消息")
        self.header_label.setObjectName("chatHeader")
        self.chat_browser = QTextBrowser()
        self.empty_label = QLabel("连接成功后，收到的 QQ 私聊和群聊会显示在这里。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyLabel")
        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("选择一个私聊或群聊后输入消息，按 Enter 发送")
        self.message_input.returnPressed.connect(self.send_current_message)
        self.ai_managed_checkbox = QCheckBox("AI代管")
        self.ai_managed_checkbox.toggled.connect(self._toggle_current_ai_managed)
        self.ai_settings_button = QPushButton("AI设置")
        self.ai_settings_button.clicked.connect(self.open_ai_settings)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self.send_current_message)
        self.log_browser = QTextBrowser()
        self.log_browser.setMaximumHeight(120)

        disconnect_button = QPushButton("断开连接")
        disconnect_button.clicked.connect(self.disconnect_from_server)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_title = QLabel("消息")
        left_title.setObjectName("leftTitle")
        left_layout.addWidget(left_title)
        left_layout.addWidget(self.session_list)
        left_layout.addWidget(disconnect_button)

        send_bar = QWidget()
        send_layout = QHBoxLayout(send_bar)
        send_layout.setContentsMargins(0, 0, 0, 0)
        send_layout.addWidget(self.message_input)
        send_layout.addWidget(self.ai_managed_checkbox)
        send_layout.addWidget(self.ai_settings_button)
        send_layout.addWidget(self.send_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.header_label)
        right_layout.addWidget(self.chat_browser)
        right_layout.addWidget(self.empty_label)
        right_layout.addWidget(send_bar)
        right_layout.addWidget(QLabel("连接日志"))
        right_layout.addWidget(self.log_browser)
        self.chat_browser.hide()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([280, 800])

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(splitter)
        self.setCentralWidget(container)
        self.setStatusBar(QStatusBar())
        self._set_status("准备连接")
        self._sync_ai_control_state()

        self.setStyleSheet(
            """
            QMainWindow { background: #f5f7fa; }
            QWidget { font-size: 14px; }
            #leftTitle, #chatHeader { font-size: 18px; font-weight: 600; padding: 8px; }
            QListWidget, QTextBrowser { background: white; border: 1px solid #e6e8eb; border-radius: 8px; }
            QListWidget::item { padding: 10px; border-bottom: 1px solid #f0f0f0; }
            QListWidget::item:selected { background: #dff4ff; color: #111; }
            #emptyLabel { color: #999; background: white; border: 1px dashed #ddd; border-radius: 8px; }
            QLineEdit { padding: 8px; border: 1px solid #d5d5d5; border-radius: 6px; background: white; }
            QPushButton { padding: 8px 18px; border-radius: 6px; background: #eeeeee; }
            QPushButton:hover { background: #e0e0e0; }
            """
        )

    def start(self) -> None:
        self.client_thread = NapCatClientThread(self.websocket_url, self.token)
        self.client_thread.connected.connect(lambda: self._set_status("已连接"))
        self.client_thread.disconnected.connect(self._handle_disconnected)
        self.client_thread.message_received.connect(self.add_message)
        self.client_thread.history_messages_received.connect(self.add_history_messages)
        self.client_thread.session_name_updated.connect(self.update_session_name)
        self.client_thread.log.connect(self.append_log)
        self.client_thread.start()

    def disconnect_from_server(self) -> None:
        for timer in self.ai_timers.values():
            timer.stop()
        self.ai_timers.clear()
        if self.client_thread is not None:
            self.client_thread.stop()
            self.client_thread = None
        self._set_status("已断开")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.disconnect_from_server()
        event.accept()

    def add_history_messages(self, messages: Any) -> None:
        if not isinstance(messages, list):
            return
        for message in messages:
            if isinstance(message, ChatMessage):
                message.historical = True
                self.add_message(message)

    def add_message(self, message: Any) -> None:
        if not isinstance(message, ChatMessage):
            return

        message_key = self._message_key(message)
        if message_key in self.seen_message_keys:
            return
        self.seen_message_keys.add(message_key)

        session = self.sessions.get(message.session_id)
        if session is None:
            session = ChatSession(
                message.session_id,
                message.session_name,
                message.session_kind,
                last_message=message.text,
                last_time=message.timestamp,
                pinned=message.session_id in self.pinned_sessions,
            )
            self.sessions[message.session_id] = session
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, message.session_id)
            self.session_items[message.session_id] = item
            self.session_list.addItem(item)
        else:
            item = self.session_items[message.session_id]
            if _should_replace_session_name(session, message.session_name):
                session.name = message.session_name

        self.messages[message.session_id].append(message)
        self.messages[message.session_id].sort(key=lambda stored_message: stored_message.timestamp)
        if not session.last_message or message.timestamp >= session.last_time:
            session.last_message = message.text
            session.last_time = message.timestamp
        if not message.historical and not message.outgoing and self.current_session_id != message.session_id:
            session.unread_count += 1
        if not message.historical and not message.outgoing:
            self.last_non_self_message_time[message.session_id] = datetime.now()
            self._maybe_schedule_mention_reply(message)
            self._schedule_after_non_self_message_ai_reply(message.session_id)
        self._refresh_session_item(item, session)
        self._sort_sessions()

        if self.current_session_id == message.session_id:
            self._render_current_session()
        elif self.current_session_id is None:
            self.session_list.setCurrentItem(self.session_items[message.session_id])

    def send_current_message(self) -> None:
        text = self.message_input.text().strip()
        if not text:
            return
        if not self.current_session_id:
            QMessageBox.information(self, "请选择会话", "请先在左侧选择一个私聊或群聊。")
            return
        session = self.sessions.get(self.current_session_id)
        if session is None:
            QMessageBox.warning(self, "无法发送", "当前会话不存在。")
            return
        if session.kind not in {"group", "private"}:
            QMessageBox.warning(self, "无法发送", "当前会话类型不支持发送消息。")
            return
        if self.client_thread is None:
            QMessageBox.warning(self, "未连接", "当前未连接 NapCatQQ，无法发送消息。")
            return

        self.client_thread.send_text(self.current_session_id, text)
        self.message_input.clear()
        self.add_message(
            ChatMessage(
                session_id=session.session_id,
                session_name=session.name,
                session_kind=session.kind,
                sender_id="self",
                sender_name="我",
                text=text,
                outgoing=True,
            )
        )

    def update_session_name(self, session_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            return
        session = self.sessions.get(session_id)
        if session is None:
            return
        session.name = name
        item = self.session_items.get(session_id)
        if item is not None:
            self._refresh_session_item(item, session)
        if self.current_session_id == session_id:
            self._render_current_session()

    def open_ai_settings(self) -> None:
        dialog = AiSettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.append_log("AI 代管设置已保存")
            self._clear_all_ai_timers()

    def append_log(self, text: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.log_browser.append(f"[{now}] {escape(text)}")

    def _handle_disconnected(self, reason: str) -> None:
        self._set_status("连接断开，等待重连")
        self.append_log(reason)

    def _on_session_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        del previous
        if current is None:
            self.current_session_id = None
            self._sync_ai_control_state()
            self._render_current_session()
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        self.current_session_id = session_id
        session = self.sessions.get(session_id)
        if session:
            session.unread_count = 0
            self._refresh_session_item(current, session)
        self._sync_ai_control_state()
        self._render_current_session()

    def _show_session_menu(self, position: Any) -> None:
        item = self.session_list.itemAt(position)
        if item is None:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        session = self.sessions.get(session_id)
        if session is None:
            return

        menu = QMenu(self)
        action_text = "取消置顶" if session.pinned else "置顶会话"
        pin_action = menu.addAction(action_text)
        pin_action.triggered.connect(lambda: self._toggle_pin_session(session_id))
        menu.exec(self.session_list.mapToGlobal(position))

    def _toggle_pin_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        item = self.session_items.get(session_id)
        if session is None or item is None:
            return

        session.pinned = not session.pinned
        if session.pinned:
            self.pinned_sessions.add(session_id)
        else:
            self.pinned_sessions.discard(session_id)
        self._save_string_set(PINNED_SESSIONS_KEY, self.pinned_sessions)
        self._refresh_session_item(item, session)
        self._sort_sessions()
        if self.current_session_id == session_id:
            self.session_list.setCurrentItem(item)

    def _toggle_current_ai_managed(self, checked: bool) -> None:
        if not self.current_session_id:
            return
        session_id = self.current_session_id
        if checked:
            self.ai_managed_sessions.add(session_id)
            self.append_log(f"已开启 AI 代管：{self.sessions[session_id].name}")
        else:
            self.ai_managed_sessions.discard(session_id)
            self._stop_ai_timer(session_id)
            self.append_log(f"已关闭 AI 代管：{self.sessions[session_id].name}")
        self._save_string_set(AI_MANAGED_SESSIONS_KEY, self.ai_managed_sessions)
        self._sync_ai_control_state()

    def _sync_ai_control_state(self) -> None:
        has_session = bool(self.current_session_id and self.current_session_id in self.sessions)
        self.ai_managed_checkbox.blockSignals(True)
        self.ai_managed_checkbox.setEnabled(has_session)
        self.ai_managed_checkbox.setChecked(bool(self.current_session_id in self.ai_managed_sessions if self.current_session_id else False))
        self.ai_managed_checkbox.blockSignals(False)

    def _clear_all_ai_timers(self) -> None:
        for timer in self.ai_timers.values():
            timer.stop()
            timer.deleteLater()
        self.ai_timers.clear()

    def _schedule_after_non_self_message_ai_reply(self, session_id: str) -> None:
        self._stop_ai_timer(session_id)
        if session_id not in self.ai_managed_sessions or session_id not in self.sessions:
            return
        config = load_ai_config(self.settings).normalized()
        if not config.timed_enabled:
            return
        delay_ms = random.randint(config.timed_min_seconds, config.timed_max_seconds) * 1000
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda sid=session_id: self._handle_after_message_ai_timer(sid))
        self.ai_timers[session_id] = timer
        timer.start(delay_ms)

    def _stop_ai_timer(self, session_id: str) -> None:
        timer = self.ai_timers.pop(session_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _handle_after_message_ai_timer(self, session_id: str) -> None:
        self.ai_timers.pop(session_id, None)
        self._request_ai_reply(session_id, "收到非自己发言后延迟")

    def _maybe_schedule_mention_reply(self, message: ChatMessage) -> None:
        session_id = message.session_id
        if session_id not in self.ai_managed_sessions:
            return
        config = load_ai_config(self.settings).normalized()
        if not config.mention_enabled or not self._message_mentions_self(message):
            return
        delay_ms = random.randint(config.mention_min_seconds, config.mention_max_seconds) * 1000
        QTimer.singleShot(delay_ms, lambda sid=session_id: self._request_ai_reply(sid, "被艾特"))

    def _request_ai_reply(self, session_id: str, reason: str) -> None:
        if session_id not in self.ai_managed_sessions or session_id not in self.sessions:
            return
        if session_id in self.ai_inflight_sessions:
            return
        config = load_ai_config(self.settings).normalized()
        if not config.api_key:
            self.append_log("AI 代管未配置 API Key，已跳过自动回复")
            return
        if config.prevent_self_follow_enabled and self._last_speaker_is_self(session_id):
            self.append_log(f"AI 代管跳过：{reason}，上一条发言人是自己")
            return
        if config.require_recent_non_self_enabled and not self._has_recent_non_self_message(session_id, config.recent_non_self_seconds):
            self.append_log(f"AI 代管跳过：{reason}，最近没有其他人发言")
            return

        session = self.sessions[session_id]
        context = self._ai_context_messages(session_id, config.context_message_count)
        self.ai_inflight_sessions.add(session_id)

        def worker() -> None:
            try:
                reply = generate_ai_reply(
                    config,
                    session_name=session.name,
                    session_kind=session.kind,
                    context_messages=context,
                )
                self.ai_reply_ready.emit(session_id, reply)
            except Exception as exc:  # noqa: BLE001
                self.ai_reply_failed.emit(session_id, f"AI 代管回复失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _handle_ai_reply_ready(self, session_id: str, reply: str) -> None:
        self.ai_inflight_sessions.discard(session_id)
        reply = reply.strip()
        if not reply:
            if load_ai_config(self.settings).allow_ai_skip_enabled:
                self.append_log("AI 代管判断本次不需要回复")
            return
        if session_id not in self.ai_managed_sessions:
            return
        config = load_ai_config(self.settings).normalized()
        if config.prevent_self_follow_enabled and self._last_speaker_is_self(session_id):
            self.append_log("AI 代管跳过发送：上一条发言人是自己")
            return
        session = self.sessions.get(session_id)
        if session is None or self.client_thread is None:
            return
        self.client_thread.send_text(session_id, reply)
        self.add_message(
            ChatMessage(
                session_id=session.session_id,
                session_name=session.name,
                session_kind=session.kind,
                sender_id="self",
                sender_name="AI代管",
                text=reply,
                outgoing=True,
            )
        )

    def _handle_ai_reply_failed(self, session_id: str, error: str) -> None:
        self.ai_inflight_sessions.discard(session_id)
        self.append_log(error)

    def _message_mentions_self(self, message: ChatMessage) -> bool:
        event = message.raw_event or {}
        self_id = str(event.get("self_id") or event.get("selfId") or "")
        raw_message = str(event.get("raw_message") or event.get("rawMessage") or message.text or "")
        segments = event.get("message")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("type") != "at":
                    continue
                data = segment.get("data") or {}
                target = str(data.get("qq") or data.get("user_id") or data.get("userId") or "")
                if target == "all" or (self_id and target == self_id):
                    return True
        return bool(self_id and f"@{self_id}" in raw_message) or "@我" in raw_message

    def _last_speaker_is_self(self, session_id: str) -> bool:
        messages = self.messages.get(session_id) or []
        if not messages:
            return False
        latest = max(messages, key=lambda message: message.timestamp)
        return latest.outgoing or latest.sender_id == "self"

    def _has_recent_non_self_message(self, session_id: str, seconds: int) -> bool:
        last_time = self.last_non_self_message_time.get(session_id)
        if last_time is None:
            return False
        return datetime.now() - last_time <= timedelta(seconds=seconds)

    def _ai_context_messages(self, session_id: str, count: int) -> list[dict[str, str]]:
        messages = self.messages.get(session_id, [])[-count:]
        return [
            {
                "sender_name": message.sender_name,
                "text": message.text,
                "outgoing": "1" if message.outgoing else "0",
            }
            for message in messages
            if message.text.strip()
        ]

    def _render_current_session(self) -> None:
        if not self.current_session_id:
            self.header_label.setText("等待消息")
            self.chat_browser.clear()
            self.empty_label.show()
            self.chat_browser.hide()
            return

        session = self.sessions[self.current_session_id]
        pin_text = "📌 " if session.pinned else ""
        self.header_label.setText(f"{pin_text}{_kind_label(session.kind)} · {session.name}")
        self.empty_label.hide()
        self.chat_browser.show()
        should_scroll_to_bottom = self._chat_scroll_is_at_bottom()
        self.chat_browser.setHtml("".join(self._message_html(m) for m in self.messages[self.current_session_id]))
        if should_scroll_to_bottom:
            scrollbar = self.chat_browser.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _chat_scroll_is_at_bottom(self) -> bool:
        scrollbar = self.chat_browser.verticalScrollBar()
        return scrollbar.maximum() <= 0 or scrollbar.value() >= scrollbar.maximum() - 4

    @staticmethod
    def _message_html(message: ChatMessage) -> str:
        time_text = message.timestamp.strftime("%H:%M:%S")
        sender = escape(message.sender_name)
        body = escape(message.text).replace("\n", "<br>") or "<span style='color:#aaa;'>[空消息]</span>"
        align = "right" if message.outgoing else "left"
        bubble_background = "#d9fdd3" if message.outgoing else "#eef9ff"
        return (
            f"<div style='margin: 12px 0; text-align:{align};'>"
            f"<div style='color:#888;font-size:12px;'>{time_text} · {sender}</div>"
            "<div style='display:inline-block;margin-top:4px;padding:8px 10px;"
            f"background:{bubble_background};border-radius:8px;line-height:1.45;text-align:left;'>"
            f"{body}</div></div>"
        )

    def _refresh_session_item(self, item: QListWidgetItem, session: ChatSession) -> None:
        unread = f" [{session.unread_count}]" if session.unread_count else ""
        time_text = session.last_time.strftime("%H:%M")
        last = session.last_message.replace("\n", " ")
        if len(last) > 32:
            last = f"{last[:32]}..."
        pin_prefix = "📌 " if session.pinned else ""
        ai_suffix = " 🤖" if session.session_id in self.ai_managed_sessions else ""
        item.setText(f"{pin_prefix}{_kind_icon(session.kind)} {session.name}{ai_suffix}{unread}\n{time_text}  {last}")
        item.setToolTip(session.last_message)
        item.setBackground(PINNED_BACKGROUND if session.pinned else NORMAL_BACKGROUND)

    def _sort_sessions(self) -> None:
        ordered = sorted(
            self.sessions.values(),
            key=lambda session: (0 if session.pinned else 1, -session.last_time.timestamp()),
        )
        current_id = self.current_session_id
        self.session_list.blockSignals(True)
        while self.session_list.count():
            self.session_list.takeItem(0)
        for session in ordered:
            item = self.session_items[session.session_id]
            self.session_list.addItem(item)
            if session.session_id == current_id:
                self.session_list.setCurrentItem(item)
        self.session_list.blockSignals(False)

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(f"{text} · {self.websocket_url}")

    def _load_string_set(self, key: str) -> set[str]:
        raw = self.settings.value(key, "[]")
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError:
            return set()
        if not isinstance(value, list):
            return set()
        return {str(session_id) for session_id in value if str(session_id).strip()}

    def _save_string_set(self, key: str, values: set[str]) -> None:
        self.settings.setValue(key, json.dumps(sorted(values), ensure_ascii=False))
        self.settings.sync()

    @staticmethod
    def _message_key(message: ChatMessage) -> str:
        if message.message_id:
            return f"{message.session_id}:id:{message.message_id}"
        return ":".join(
            [
                message.session_id,
                message.sender_id,
                str(int(message.timestamp.timestamp())),
                message.text,
                "out" if message.outgoing else "in",
            ]
        )


def load_ai_config(settings: QSettings) -> AiReplyConfig:
    return AiReplyConfig(
        provider=_setting_text(settings, "ai/provider", AI_PROVIDER_MINIMAX_M3),
        api_key=_setting_text(settings, "ai/api_key", ""),
        prompt=_setting_text(settings, "ai/prompt", ""),
        timed_enabled=_setting_bool(settings, "ai/timed_enabled", False),
        timed_min_seconds=_setting_int(settings, "ai/timed_min_seconds", 10),
        timed_max_seconds=_setting_int(settings, "ai/timed_max_seconds", 20),
        require_recent_non_self_enabled=_setting_bool(settings, "ai/require_recent_non_self_enabled", True),
        recent_non_self_seconds=_setting_int(settings, "ai/recent_non_self_seconds", 15),
        context_message_count=_setting_int(settings, "ai/context_message_count", 10),
        mention_enabled=_setting_bool(settings, "ai/mention_enabled", True),
        mention_min_seconds=_setting_int(settings, "ai/mention_min_seconds", 3),
        mention_max_seconds=_setting_int(settings, "ai/mention_max_seconds", 6),
        prevent_self_follow_enabled=_setting_bool(settings, "ai/prevent_self_follow_enabled", True),
        allow_ai_skip_enabled=_setting_bool(settings, "ai/allow_ai_skip_enabled", False),
    ).normalized()


def save_ai_config(settings: QSettings, config: AiReplyConfig) -> None:
    normalized = config.normalized()
    settings.setValue("ai/provider", normalized.provider)
    settings.setValue("ai/api_key", normalized.api_key)
    settings.setValue("ai/prompt", normalized.prompt)
    settings.setValue("ai/timed_enabled", normalized.timed_enabled)
    settings.setValue("ai/timed_min_seconds", normalized.timed_min_seconds)
    settings.setValue("ai/timed_max_seconds", normalized.timed_max_seconds)
    settings.setValue("ai/require_recent_non_self_enabled", normalized.require_recent_non_self_enabled)
    settings.setValue("ai/recent_non_self_seconds", normalized.recent_non_self_seconds)
    settings.setValue("ai/context_message_count", normalized.context_message_count)
    settings.setValue("ai/mention_enabled", normalized.mention_enabled)
    settings.setValue("ai/mention_min_seconds", normalized.mention_min_seconds)
    settings.setValue("ai/mention_max_seconds", normalized.mention_max_seconds)
    settings.setValue("ai/prevent_self_follow_enabled", normalized.prevent_self_follow_enabled)
    settings.setValue("ai/allow_ai_skip_enabled", normalized.allow_ai_skip_enabled)
    settings.sync()


def _setting_text(settings: QSettings, key: str, default: str) -> str:
    value = settings.value(key, default)
    if value is None:
        return default
    return str(value)


def _setting_bool(settings: QSettings, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(settings: QSettings, key: str, default: int) -> int:
    value = settings.value(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _spin(value: int, minimum: int, maximum: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    return spin


def _range_widget(left: QSpinBox, right: QSpinBox, suffix: str) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(left)
    layout.addWidget(QLabel("~"))
    layout.addWidget(right)
    layout.addWidget(QLabel(suffix))
    layout.addStretch(1)
    return widget


def _single_spin_widget(spin: QSpinBox, suffix: str) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(spin)
    layout.addWidget(QLabel(suffix))
    layout.addStretch(1)
    return widget


def _should_replace_session_name(session: ChatSession, candidate: str) -> bool:
    if not candidate:
        return False
    if session.name.startswith("群聊 ") and not candidate.startswith("群聊 "):
        return True
    if session.name.startswith("QQ ") and not candidate.startswith("QQ "):
        return True
    return False


def _kind_label(kind: str) -> str:
    return {"group": "群聊", "private": "私聊", "system": "系统"}.get(kind, kind)


def _kind_icon(kind: str) -> str:
    return {"group": "群", "private": "私", "system": "系"}.get(kind, "聊")


class QQMessageManagerApp:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.login_window = LoginWindow()
        self.main_window: MainWindow | None = None
        self.login_window.login_requested.connect(self._login)

    def show(self) -> None:
        self.login_window.show()

    def _login(self, websocket_url: str, token: str) -> None:
        self.main_window = MainWindow(websocket_url, token)
        self.main_window.show()
        self.main_window.start()
        self.login_window.close()
