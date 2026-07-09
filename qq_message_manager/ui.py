from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
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


class LoginWindow(QWidget):
    login_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("QQMessageManager - 连接 NapCatQQ")
        self.setMinimumWidth(560)

        self.url_input = QLineEdit(DEFAULT_URL)
        self.host_input = QLineEdit(DEFAULT_HOST)
        self.port_input = QLineEdit(DEFAULT_PORT)
        self.path_input = QLineEdit(DEFAULT_PATH)
        self.path_input.setPlaceholderText("正向 WS 通常留空；有自定义路径时再填写")
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("没有配置 Token 可留空")

        self.use_full_url = QCheckBox("优先使用完整 WebSocket 地址")
        self.use_full_url.setChecked(True)
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


class MainWindow(QMainWindow):
    def __init__(self, websocket_url: str, token: str = "") -> None:
        super().__init__()
        self.websocket_url = websocket_url
        self.token = token
        self.client_thread: NapCatClientThread | None = None
        self.sessions: dict[str, ChatSession] = {}
        self.messages: dict[str, list[ChatMessage]] = defaultdict(list)
        self.session_items: dict[str, QListWidgetItem] = {}
        self.current_session_id: str | None = None

        self.setWindowTitle("QQMessageManager")
        self.resize(1080, 720)
        self.setMinimumSize(820, 560)

        self.session_list = QListWidget()
        self.session_list.currentItemChanged.connect(self._on_session_changed)

        self.header_label = QLabel("等待消息")
        self.header_label.setObjectName("chatHeader")
        self.chat_browser = QTextBrowser()
        self.empty_label = QLabel("连接成功后，收到的 QQ 私聊和群聊会显示在这里。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyLabel")
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

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.header_label)
        right_layout.addWidget(self.chat_browser)
        right_layout.addWidget(self.empty_label)
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
            QPushButton { padding: 8px 18px; border-radius: 6px; background: #eeeeee; }
            QPushButton:hover { background: #e0e0e0; }
            """
        )

    def start(self) -> None:
        self.client_thread = NapCatClientThread(self.websocket_url, self.token)
        self.client_thread.connected.connect(lambda: self._set_status("已连接"))
        self.client_thread.disconnected.connect(self._handle_disconnected)
        self.client_thread.message_received.connect(self.add_message)
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

    def add_message(self, message: Any) -> None:
        if not isinstance(message, ChatMessage):
            return

        session = self.sessions.get(message.session_id)
        if session is None:
            session = ChatSession(message.session_id, message.session_name, message.session_kind)
            self.sessions[message.session_id] = session
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, message.session_id)
            self.session_items[message.session_id] = item
            self.session_list.addItem(item)
        else:
            item = self.session_items[message.session_id]

        self.messages[message.session_id].append(message)
        session.last_message = message.text
        session.last_time = message.timestamp
        if self.current_session_id != message.session_id:
            session.unread_count += 1
        self._refresh_session_item(item, session)
        self._sort_sessions()

        if self.current_session_id == message.session_id:
            self._render_current_session()
        elif self.current_session_id is None:
            self.session_list.setCurrentItem(self.session_items[message.session_id])

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

    def _render_current_session(self) -> None:
        if not self.current_session_id:
            self.header_label.setText("等待消息")
            self.chat_browser.clear()
            self.empty_label.show()
            self.chat_browser.hide()
            return

        session = self.sessions[self.current_session_id]
        self.header_label.setText(f"{_kind_label(session.kind)} · {session.name}")
        self.empty_label.hide()
        self.chat_browser.show()
        self.chat_browser.setHtml("".join(self._message_html(m) for m in self.messages[self.current_session_id]))
        self.chat_browser.verticalScrollBar().setValue(self.chat_browser.verticalScrollBar().maximum())

    @staticmethod
    def _message_html(message: ChatMessage) -> str:
        time_text = message.timestamp.strftime("%H:%M:%S")
        sender = escape(message.sender_name)
        body = escape(message.text).replace("\n", "<br>") or "<span style='color:#aaa;'>[空消息]</span>"
        return (
            "<div style='margin: 12px 0;'>"
            f"<div style='color:#888;font-size:12px;'>{time_text} · {sender}</div>"
            "<div style='display:inline-block;margin-top:4px;padding:8px 10px;"
            "background:#eef9ff;border-radius:8px;line-height:1.45;'>"
            f"{body}</div></div>"
        )

    def _refresh_session_item(self, item: QListWidgetItem, session: ChatSession) -> None:
        unread = f" [{session.unread_count}]" if session.unread_count else ""
        time_text = session.last_time.strftime("%H:%M")
        last = session.last_message.replace("\n", " ")
        if len(last) > 32:
            last = f"{last[:32]}..."
        item.setText(f"{_kind_icon(session.kind)} {session.name}{unread}\n{time_text}  {last}")
        item.setToolTip(session.last_message)

    def _sort_sessions(self) -> None:
        ordered = sorted(self.sessions.values(), key=lambda session: session.last_time, reverse=True)
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
