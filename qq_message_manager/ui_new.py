"""
现代化 UI 组件 - 按重构方案实现
"""
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
    QGroupBox,
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

from .ai_client import AI_PROVIDERS, AI_PROVIDER_MINIMAX_M3, AiReplyConfig, generate_ai_reply, test_ai_connection
from .image_cache import ensure_cached, to_data_uri, supported_format, short_id
from pathlib import Path
from .models import ChatImage, ChatMessage, ChatSession
from .napcat_client import NapCatClientThread
from .styles import COLORS, SPACING, get_stylesheet

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "3002"
DEFAULT_PATH = ""
DEFAULT_URL = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}{DEFAULT_PATH}"
SETTINGS_ORGANIZATION = "QQMessageManager"
SETTINGS_APPLICATION = "QQMessageManager"
PINNED_SESSIONS_KEY = "chat/pinned_sessions"
AI_MANAGED_SESSIONS_KEY = "ai/managed_sessions"
PINNED_BACKGROUND = QColor("#fff3cd")
NORMAL_BACKGROUND = QColor("#ffffff")


class LoginWindow(QWidget):
    """登录窗口 - 保持原有逻辑不变，仅优化样式"""
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
        connect_button.setObjectName("primaryButton")
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


class TopBar(QWidget):
    """顶部导航栏"""
    toggle_log = Signal()
    open_settings = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")

        # 左侧：应用名称
        self.title_label = QLabel("QQMessageManager")
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        self.title_label.setFont(font)

        # 中间：连接状态
        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet(f"color: {COLORS['text_weak']}; font-size: 16px;")
        self.status_text = QLabel("未连接")
        self.status_text.setStyleSheet(f"color: {COLORS['text_secondary']};")

        # 右侧：操作按钮
        self.log_button = QPushButton("日志")
        self.log_button.setFixedSize(60, 32)
        self.log_button.clicked.connect(self.toggle_log.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING["page_margin"], 8, SPACING["page_margin"], 8)

        layout.addWidget(self.title_label)
        layout.addSpacing(24)
        layout.addWidget(self.status_indicator)
        layout.addWidget(self.status_text)
        layout.addStretch(1)
        layout.addWidget(self.log_button)

    def update_status(self, connected: bool, url: str = "") -> None:
        """更新连接状态"""
        if connected:
            self.status_indicator.setStyleSheet(f"color: {COLORS['success']}; font-size: 16px;")
            self.status_text.setText(f"已连接 {url}")
        else:
            self.status_indicator.setStyleSheet(f"color: {COLORS['error']}; font-size: 16px;")
            self.status_text.setText("未连接")


class Sidebar(QWidget):
    """左侧会话栏"""
    session_selected = Signal(str)
    session_context_menu = Signal(str, object)  # session_id, position

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("leftPanel")
        self.setFixedWidth(280)

        # 标题
        title = QLabel("消息")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)
        title.setFixedHeight(48)
        title.setContentsMargins(16, 0, 0, 0)

        # 搜索框
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索会话...")
        self.search_input.setFixedHeight(36)

        # 会话列表
        self.session_list = QListWidget()
        self.session_list.currentItemChanged.connect(self._on_session_changed)
        self.session_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._show_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(title)
        layout.addWidget(self.search_input)
        layout.addWidget(self.session_list)

    def _on_session_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        del previous
        if current:
            session_id = current.data(Qt.ItemDataRole.UserRole)
            self.session_selected.emit(session_id)

    def _show_context_menu(self, position: Any) -> None:
        item = self.session_list.itemAt(position)
        if not item:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        session = self.parent().window().sessions.get(session_id) if self.parent() else None
        if not session:
            return

        menu = QMenu(self)
        action_text = "取消置顶" if session.pinned else "置顶会话"
        pin_action = menu.addAction(action_text)
        pin_action.triggered.connect(lambda: self.session_context_menu.emit(session_id, "toggle_pin"))
        menu.exec(self.session_list.mapToGlobal(position))

    def add_session_item(self, session_id: str, item: QListWidgetItem) -> None:
        """添加会话项"""
        self.session_list.addItem(item)

    def clear_selection(self) -> None:
        """清除选择"""
        self.session_list.setCurrentItem(None)


class ChatPanel(QWidget):
    """中间聊天区域"""
    send_message = Signal(str, str)  # session_id, text
    ai_toggle = Signal(str, bool)  # session_id, enabled

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.current_session_id: str | None = None

        # 顶部会话信息栏
        self.header_label = QLabel("等待消息")
        self.header_label.setFixedHeight(56)
        self.header_label.setContentsMargins(16, 0, 16, 0)

        # 消息列表
        self.chat_browser = QTextBrowser()
        self.chat_browser.setOpenExternalLinks(True)

        # 空状态提示
        self.empty_label = QLabel("连接成功后，收到的 QQ 私聊和群聊会显示在这里。")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet(f"color: {COLORS['text_weak']}; padding: 40px;")

        # 输入区
        self.message_input = QTextEdit()
        self.message_input.setPlaceholderText("输入消息，Enter 发送，Shift + Enter 换行")
        self.message_input.setMaximumHeight(120)
        self.message_input.setMinimumHeight(88)

        self.ai_managed_checkbox = QCheckBox("AI 托管")
        self.ai_managed_checkbox.toggled.connect(self._on_ai_toggle)

        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primaryButton")
        self.send_button.clicked.connect(self._send_message)

        input_layout = QHBoxLayout()
        input_layout.addWidget(self.message_input)
        input_layout.addWidget(self.ai_managed_checkbox)
        input_layout.addWidget(self.send_button)

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header_label)
        layout.addWidget(self.chat_browser)
        layout.addWidget(self.empty_label)
        layout.addLayout(input_layout)
        layout.addSpacing(8)

        self.chat_browser.hide()

    def _on_ai_toggle(self, checked: bool) -> None:
        if self.current_session_id:
            self.ai_toggle.emit(self.current_session_id, checked)

    def _send_message(self) -> None:
        text = self.message_input.toPlainText().strip()
        if not text or not self.current_session_id:
            return
        self.send_message.emit(self.current_session_id, text)
        self.message_input.clear()

    def set_session(self, session_id: str, session_name: str, session_kind: str) -> None:
        """设置当前会话"""
        self.current_session_id = session_id
        pin_text = ""  # 暂时不显示置顶标记在标题
        kind_label = "群聊" if session_kind == "group" else "私聊"
        self.header_label.setText(f"{kind_label} · {session_name}")
        self.empty_label.hide()
        self.chat_browser.show()

    def clear_session(self) -> None:
        """清除当前会话"""
        self.current_session_id = None
        self.header_label.setText("等待消息")
        self.chat_browser.clear()
        self.empty_label.show()
        self.chat_browser.hide()

    def append_message(self, html: str) -> None:
        """添加消息到聊天区域"""
        should_scroll = self._is_at_bottom()
        self.chat_browser.append(html)
        if should_scroll:
            self._scroll_to_bottom()

    def _is_at_bottom(self) -> bool:
        scrollbar = self.chat_browser.verticalScrollBar()
        return scrollbar.maximum() <= 0 or scrollbar.value() >= scrollbar.maximum() - 4

    def _scroll_to_bottom(self) -> None:
        scrollbar = self.chat_browser.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class AiSettingsPanel(QWidget):
    """右侧 AI 设置面板"""
    config_changed = Signal()
    test_connection = Signal()

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aiPanel")
        self.setFixedWidth(380)
        self.settings = settings

        # 标题
        title = QLabel("AI 托管设置")
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)

        subtitle = QLabel("配置自动回复、模型和安全规则")
        subtitle.setObjectName("subtitle")

        # 服务配置卡片
        self.provider_input = QComboBox()
        self.provider_input.addItems(AI_PROVIDERS)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("API Key")

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("API 地址")

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("模型名称")

        # 测试连接按钮
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self.test_connection.emit)
        self.test_result_label = QLabel("")
        self.test_result_label.setWordWrap(True)

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(8)

        # 服务配置组
        service_group = self._create_group("服务配置")
        service_group.layout().addWidget(QLabel("AI 服务商"))
        service_group.layout().addWidget(self.provider_input)
        service_group.layout().addWidget(QLabel("API Key"))
        service_group.layout().addWidget(self.api_key_input)
        service_group.layout().addWidget(QLabel("API 地址"))
        service_group.layout().addWidget(self.base_url_input)
        service_group.layout().addWidget(QLabel("模型名称"))
        service_group.layout().addWidget(self.model_input)
        service_group.layout().addWidget(self.test_button)
        service_group.layout().addWidget(self.test_result_label)

        layout.addWidget(service_group)
        layout.addStretch(1)

        self._load_config()

    def _create_group(self, title: str) -> QGroupBox:
        """创建设置分组"""
        group = QGroupBox(title)
        group.setLayout(QVBoxLayout())
        group.layout().setContentsMargins(12, 12, 12, 12)
        group.layout().setSpacing(8)
        return group

    def _load_config(self) -> None:
        """加载配置"""
        config = load_ai_config(self.settings)
        self.provider_input.setCurrentText(config.provider)
        self.api_key_input.setText(config.api_key)
        self.base_url_input.setText(config.base_url)
        self.model_input.setText(config.model)

    def get_config(self) -> AiReplyConfig:
        """获取当前配置"""
        return AiReplyConfig(
            provider=self.provider_input.currentText(),
            api_key=self.api_key_input.text(),
            base_url=self.base_url_input.text(),
            model=self.model_input.text(),
        ).normalized()

    def set_test_result(self, success: bool, message: str) -> None:
        """设置测试结果"""
        color = COLORS["success"] if success else COLORS["error"]
        self.test_result_label.setText(f'<font color="{color}">{message}</font>')


class LogDrawer(QWidget):
    """底部日志抽屉"""
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("logDrawer")
        self.setVisible(False)  # 默认隐藏

        self.log_browser = QTextBrowser()
        self.log_browser.setMaximumHeight(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.addWidget(QLabel("连接日志"))
        layout.addWidget(self.log_browser)

    def append_log(self, text: str) -> None:
        """添加日志"""
        from datetime import datetime
        now = datetime.now().strftime("%H:%M:%S")
        self.log_browser.append(f"[{now}] {escape(text)}")

    def toggle(self) -> None:
        """切换显示/隐藏"""
        self.setVisible(not self.isVisible())


class MainWindow(QMainWindow):
    """主窗口 - 重构为三栏布局"""

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

        self.setWindowTitle("QQMessageManager")
        self.resize(1280, 800)
        self.setMinimumSize(1000, 600)

        self._init_ui()
        self._set_status("准备连接")
        self.setStyleSheet(get_stylesheet())

    def _init_ui(self) -> None:
        """初始化 UI"""
        # 顶部栏
        self.top_bar = TopBar()
        self.top_bar.toggle_log.connect(self._toggle_log)

        # 左侧会话栏
        self.sidebar = Sidebar()
        self.sidebar.session_selected.connect(self._on_session_selected)

        # 中间聊天区域
        self.chat_panel = ChatPanel()
        self.chat_panel.send_message.connect(self._send_message)
        self.chat_panel.ai_toggle.connect(self._toggle_ai_managed)

        # 右侧 AI 设置面板
        self.ai_panel = AiSettingsPanel(self.settings)
        self.ai_panel.test_connection.connect(self._test_ai_connection)

        # 底部日志抽屉
        self.log_drawer = LogDrawer()

        # 三栏布局
        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        content_splitter.addWidget(self.sidebar)
        content_splitter.addWidget(self.chat_panel)
        content_splitter.addWidget(self.ai_panel)
        content_splitter.setStretchFactor(0, 0)  # 左侧固定
        content_splitter.setStretchFactor(1, 1)  # 中间拉伸
        content_splitter.setStretchFactor(2, 0)  # 右侧固定

        # 主布局
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.top_bar)
        main_layout.addWidget(content_splitter)
        main_layout.addWidget(self.log_drawer)

        self.setCentralWidget(main_widget)
        self.setStatusBar(QStatusBar())

    def start(self) -> None:
        """启动 WebSocket 连接"""
        self.client_thread = NapCatClientThread(self.websocket_url, self.token)
        self.client_thread.connected.connect(lambda: self._on_connected())
        self.client_thread.disconnected.connect(self._on_disconnected)
        self.client_thread.message_received.connect(self.add_message)
        self.client_thread.history_messages_received.connect(self.add_history_messages)
        self.client_thread.session_name_updated.connect(self.update_session_name)
        self.client_thread.log.connect(self.append_log)
        self.client_thread.start()

    def _on_connected(self) -> None:
        """连接成功"""
        self.top_bar.update_status(True, self.websocket_url)
        self._set_status("已连接")

    def _on_disconnected(self, reason: str) -> None:
        """连接断开"""
        self.top_bar.update_status(False)
        self._set_status("连接断开，等待重连")
        self.append_log(reason)

    def _toggle_log(self) -> None:
        """切换日志显示"""
        self.log_drawer.toggle()

    def add_message(self, message: Any) -> None:
        """添加消息"""
        if not isinstance(message, ChatMessage):
            return

        message_key = self._message_key(message)
        if message_key in self.seen_message_keys:
            return
        self.seen_message_keys.add(message_key)

        # 创建或更新会话
        if message.session_id not in self.sessions:
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
            self.sidebar.add_session_item(message.session_id, item)
        else:
            session = self.sessions[message.session_id]
            item = self.session_items[message.session_id]

        # 添加消息
        self.messages[message.session_id].append(message)
        self.messages[message.session_id].sort(key=lambda m: m.timestamp)

        # 更新会话信息
        if not session.last_message or message.timestamp >= session.last_time:
            session.last_message = message.text
            session.last_time = message.timestamp

        # 更新未读数
        if not message.historical and not message.outgoing and self.current_session_id != message.session_id:
            session.unread_count += 1

        # 刷新会话项
        self._refresh_session_item(item, session)

        # 如果当前正在查看该会话，刷新聊天区域
        if self.current_session_id == message.session_id:
            self._render_current_session()

    def _on_session_selected(self, session_id: str) -> None:
        """会话被选中"""
        self.current_session_id = session_id
        session = self.sessions.get(session_id)
        if session:
            session.unread_count = 0
            item = self.session_items.get(session_id)
            if item:
                self._refresh_session_item(item, session)
            self.chat_panel.set_session(session_id, session.name, session.kind)
            self._render_current_session()

    def _render_current_session(self) -> None:
        """渲染当前会话的消息"""
        if not self.current_session_id:
            self.chat_panel.clear_session()
            return

        messages = self.messages.get(self.current_session_id, [])
        self.chat_panel.chat_browser.clear()
        for message in messages:
            html = self._message_html(message)
            self.chat_panel.append_message(html)

    def _message_html(self, message: ChatMessage) -> str:
        """生成消息 HTML"""
        time_text = message.timestamp.strftime("%H:%M:%S")
        sender = escape(message.sender_name)
        align = "right" if message.outgoing else "left"
        bubble_background = "#d9fdd3" if message.outgoing else "#eef9ff"

        return (
            f"<div style='margin: 12px 0; text-align:{align};'>"
            f"<div style='color:#888;font-size:12px;'>{time_text} · {sender}</div>"
            "<div style='display:inline-block;margin-top:4px;padding:8px 10px;"
            f"background:{bubble_background};border-radius:12px;line-height:1.45;text-align:left;max-width:70%;'>"
            f"{escape(message.text).replace(chr(10), '<br>')}</div></div>"
        )

    def _refresh_session_item(self, item: QListWidgetItem, session: ChatSession) -> None:
        """刷新会话项显示"""
        unread = f" [{session.unread_count}]" if session.unread_count else ""
        time_text = session.last_time.strftime("%H:%M")
        last = session.last_message.replace("\n", " ")
        if len(last) > 32:
            last = f"{last[:32]}..."

        item.setText(f"{'📌 ' if session.pinned else ''}{session.name}{unread}\n{time_text}  {last}")
        item.setBackground(PINNED_BACKGROUND if session.pinned else NORMAL_BACKGROUND)

    def _send_message(self, session_id: str, text: str) -> None:
        """发送消息"""
        if not self.client_thread:
            QMessageBox.warning(self, "未连接", "当前未连接 NapCatQQ，无法发送消息。")
            return

        session = self.sessions.get(session_id)
        if not session:
            return

        self.client_thread.send_text(session_id, text)
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

    def _toggle_ai_managed(self, session_id: str, enabled: bool) -> None:
        """切换 AI 托管状态"""
        if enabled:
            self.ai_managed_sessions.add(session_id)
        else:
            self.ai_managed_sessions.discard(session_id)
        self._save_string_set(AI_MANAGED_SESSIONS_KEY, self.ai_managed_sessions)

    def _test_ai_connection(self) -> None:
        """测试 AI 连接"""
        config = self.ai_panel.get_config()
        if not config.api_key:
            self.ai_panel.set_test_result(False, "请先填写 API Key")
            return

        self.ai_panel.set_test_result(True, "正在测试连接...")

        def worker() -> None:
            try:
                ok, msg = test_ai_connection(config)
                self.ai_panel.set_test_result(ok, msg)
            except Exception as exc:
                self.ai_panel.set_test_result(False, f"测试异常：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def append_log(self, text: str) -> None:
        """添加日志"""
        self.log_drawer.append_log(text)

    def _set_status(self, text: str) -> None:
        """设置状态栏"""
        self.statusBar().showMessage(f"{text} · {self.websocket_url}")

    def _load_string_set(self, key: str) -> set[str]:
        """加载字符串集合配置"""
        raw = self.settings.value(key, "[]")
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError:
            return set()
        if not isinstance(value, list):
            return set()
        return {str(v) for v in value if str(v).strip()}

    def _save_string_set(self, key: str, values: set[str]) -> None:
        """保存字符串集合配置"""
        self.settings.setValue(key, json.dumps(sorted(values), ensure_ascii=False))
        self.settings.sync()

    def _message_key(self, message: ChatMessage) -> str:
        """生成消息唯一键"""
        if message.message_id:
            return f"{message.session_id}:id:{message.message_id}"
        return ":".join([
            message.session_id,
            message.sender_id,
            str(int(message.timestamp.timestamp())),
            message.text,
            "out" if message.outgoing else "in",
        ])

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭窗口"""
        if self.client_thread:
            self.client_thread.stop()
        event.accept()

    def add_history_messages(self, messages: Any) -> None:
        """添加历史消息"""
        if not isinstance(messages, list):
            return
        for message in messages:
            if isinstance(message, ChatMessage):
                message.historical = True
                self.add_message(message)

    def update_session_name(self, session_id: str, name: str) -> None:
        """更新会话名称"""
        name = name.strip()
        if not name:
            return
        session = self.sessions.get(session_id)
        if session:
            session.name = name
            item = self.session_items.get(session_id)
            if item:
                self._refresh_session_item(item, session)


# 以下函数保持原有逻辑不变
def load_ai_config(settings: QSettings) -> AiReplyConfig:
    """加载 AI 配置"""
    return AiReplyConfig(
        provider=_setting_text(settings, "ai/provider", AI_PROVIDER_MINIMAX_M3),
        api_key=_setting_text(settings, "ai/api_key", ""),
        prompt=_setting_text(settings, "ai/prompt", ""),
        base_url=_setting_text(settings, "ai/base_url", ""),
        model=_setting_text(settings, "ai/model", ""),
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
        allow_image_read_enabled=_setting_bool(settings, "ai/allow_image_read_enabled", False),
    ).normalized()


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


class QQMessageManagerApp:
    """应用入口"""
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
