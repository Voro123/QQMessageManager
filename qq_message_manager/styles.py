from PySide6.QtGui import QColor, QFont
from PySide6.QtCore import Qt

# 颜色规范
COLORS = {
    # 主色
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "primary_light": "#E0F2FE",

    # 状态色
    "success": "#16A34A",
    "warning": "#F59E0B",
    "error": "#DC2626",

    # 背景色
    "bg_primary": "#F8FAFC",
    "bg_panel": "#FFFFFF",
    "bg_hover": "#F1F5F9",
    "bg_selected": "#E0F2FE",

    # 边框色
    "border": "#E5E7EB",

    # 文字色
    "text_primary": "#111827",
    "text_secondary": "#6B7280",
    "text_weak": "#9CA3AF",

    # 特殊
    "unread_badge": "#DC2626",
    "pinned_bg": "#fff3cd",
}

# 圆角规范
RADIUS = {
    "window": 12,
    "button": 8,
    "input": 8,
    "bubble": 12,
    "image": 10,
    "card": 10,
}

# 字体规范
FONTS = {
    "title": QFont("Microsoft YaHei UI", 18, QFont.Weight.Bold),
    "subtitle": QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold),
    "body": QFont("Microsoft YaHei UI", 14),
    "caption": QFont("Microsoft YaHei UI", 12),
    "button": QFont("Microsoft YaHei UI", 14),
}

# 间距规范
SPACING = {
    "page_margin": 16,
    "card_padding": 16,
    "component": 12,
    "small": 8,
}

# 左侧会话栏宽度
SIDEBAR_WIDTH = 280

# 右侧AI设置面板宽度
AI_PANEL_WIDTH = 380

# 顶部栏高度
TOPBAR_HEIGHT = 48

# 消息气泡最大宽度比例
BUBBLE_MAX_WIDTH_RATIO = 0.7

# 图片缩略图尺寸
IMAGE_THUMBNAIL_MAX_WIDTH = 280
IMAGE_THUMBNAIL_MAX_HEIGHT = 280

# 会话卡片高度
SESSION_CARD_HEIGHT = 72


def get_stylesheet() -> str:
    """返回全局样式表"""
    return f"""
        /* 全局 */
        QWidget {{
            font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
            font-size: 14px;
            color: {COLORS["text_primary"]};
        }}

        /* 主窗口背景 */
        QMainWindow {{
            background: {COLORS["bg_primary"]};
        }}

        /* 面板背景 */
        #leftPanel, #chatPanel, #aiPanel {{
            background: {COLORS["bg_panel"]};
            border: none;
        }}

        #leftPanel {{
            border-right: 1px solid {COLORS["border"]};
        }}

        #aiPanel {{
            border-left: 1px solid {COLORS["border"]};
        }}

        /* 顶部栏 */
        #topBar {{
            background: {COLORS["bg_panel"]};
            border-bottom: 1px solid {COLORS["border"]};
            min-height: {TOPBAR_HEIGHT}px;
            max-height: {TOPBAR_HEIGHT}px;
        }}

        /* 会话列表 */
        QListWidget {{
            background: {COLORS["bg_primary"]};
            border: none;
            outline: none;
        }}

        QListWidget::item {{
            padding: 0px;
            border-bottom: 1px solid {COLORS["border"]};
            min-height: {SESSION_CARD_HEIGHT}px;
        }}

        QListWidget::item:selected {{
            background: {COLORS["bg_selected"]};
            color: {COLORS["text_primary"]};
        }}

        QListWidget::item:hover {{
            background: {COLORS["bg_hover"]};
        }}

        /* 按钮 */
        QPushButton {{
            padding: 8px 18px;
            border-radius: {RADIUS["button"]}px;
            background: {COLORS["bg_panel"]};
            border: 1px solid {COLORS["border"]};
            color: {COLORS["text_primary"]};
        }}

        QPushButton:hover {{
            background: {COLORS["bg_hover"]};
        }}

        QPushButton#primaryButton {{
            background: {COLORS["primary"]};
            color: white;
            border: none;
        }}

        QPushButton#primaryButton:hover {{
            background: {COLORS["primary_hover"]};
        }}

        /* 输入框 */
        QLineEdit, QTextEdit, QPlainTextEdit {{
            padding: 8px 12px;
            border: 1px solid {COLORS["border"]};
            border-radius: {RADIUS["input"]}px;
            background: {COLORS["bg_panel"]};
        }}

        QLineEdit:focus, QTextEdit:focus {{
            border: 2px solid {COLORS["primary"]};
        }}

        /* 复选框 */
        QCheckBox {{
            spacing: 8px;
        }}

        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            border-radius: 4px;
            border: 2px solid {COLORS["border"]};
            background: {COLORS["bg_panel"]};
        }}

        QCheckBox::indicator:checked {{
            background: {COLORS["primary"]};
            border-color: {COLORS["primary"]};
        }}

        /* 组合框 */
        QComboBox {{
            padding: 8px 12px;
            border: 1px solid {COLORS["border"]};
            border-radius: {RADIUS["input"]}px;
            background: {COLORS["bg_panel"]};
        }}

        QComboBox:hover {{
            border-color: {COLORS["primary"]};
        }}

        QComboBox::drop-down {{
            border: none;
            width: 20px;
        }}

        /* 滚动条 */
        QScrollBar:vertical {{
            background: transparent;
            width: 8px;
            margin: 0px;
        }}

        QScrollBar::handle:vertical {{
            background: {COLORS["text_weak"]};
            border-radius: 4px;
            min-height: 30px;
        }}

        QScrollBar::handle:vertical:hover {{
            background: {COLORS["text_secondary"]};
        }}

        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}

        /* 分割线 */
        QSplitter::handle {{
            background: {COLORS["border"]};
            width: 1px;
        }}

        /* 状态栏 */
        QStatusBar {{
            background: {COLORS["bg_panel"]};
            border-top: 1px solid {COLORS["border"]};
            padding: 4px 12px;
        }}

        /* 标签 */
        QLabel#subtitle {{
            color: {COLORS["text_secondary"]};
            font-size: 12px;
        }}

        QLabel#pageTitle {{
            font-size: 18px;
            font-weight: bold;
        }}
    """
