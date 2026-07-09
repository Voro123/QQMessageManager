from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QSplitter,
    QStatusBar,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .models import ChatMessage, ChatSession
from .napcat_client import NapCatClientThread

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "3001"
DEFAULT_PATH = ""
DEFAULT_URL = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}{DEFAULT_PATH}"
SETTINGS_ORGANIZATION = "QQMessageManager"
SETTINGS_APPLICATION = "QQMessageManager"
PINNED_SESSIONS_KEY = "chat/pinned_sessions"
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


class MainWindow(QMainWindow):
    def __init__(self, websocket_url: str, token: str = "") -> None:
        super().__init__()
        self.settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)
        self.pinned_sessions = self._load_pinned_sessions()
        self.websocket_url = websocket_url
        self.token = token
        self.client_thread: NapCatClientThread | None = None
        self.sessions: dict[str, ChatSession] = {}
        self.messages: dict[str, list[ChatMessage]] = defaultdict(list)
        self.session_items: dict[str, QListWidgetItem] = {}
        self.seen_message_keys: set[str] = set()
        self.current_session_id: str | None = None

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
            self._render_current_session()
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        self.current_session_id = session_id
        session = self.sessions.get(session_id)
        if session:
            session.unread_count = 0
            self._refresh_session_item(current, session)
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
        self._save_pinned_sessions()
        self._refresh_session_item(item, session)
        self._sort_sessions()
        if self.current_session_id == session_id:
            self.session_list.setCurrentItem(item)

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
        self.chat_browser.setHtml("".join(self._message_html(m) for m in self.messages[self.current_session_id]))
        self.chat_browser.verticalScrollBar().setValue(self.chat_browser.verticalScrollBar().maximum())

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
        item.setText(f"{pin_prefix}{_kind_icon(session.kind)} {session.name}{unread}\n{time_text}  {last}")
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

    def _load_pinned_sessions(self) -> set[str]:
        raw = self.settings.value(PINNED_SESSIONS_KEY, "[]")
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError:
            return set()
        if not isinstance(value, list):
            return set()
        return {str(session_id) for session_id in value if str(session_id).strip()}

    def _save_pinned_sessions(self) -> None:
        self.settings.setValue(PINNED_SESSIONS_KEY, json.dumps(sorted(self.pinned_sessions), ensure_ascii=False))
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
