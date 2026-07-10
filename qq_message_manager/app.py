from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import ai_client as ai_module
from . import image_generation_feature as image_generation_module
from . import napcat_client as napcat_module
from . import sticker_memory as sticker_module
from . import ui as ui_module
from .ai_context_limit_patch import install_ai_context_message_limit
from .ai_rules_cleanup import install_ai_rules_cleanup
from .ai_typing_delay import install_ai_typing_delay
from .button_position_patch import install_summary_send_button_swap
from .chat_summary_feature import install_chat_summary_feature
from .image_generation_feature import install_image_generation_feature
from .image_generation_toggle_patch import install_image_generation_toggle
from .image_layout_patch import install_image_layout_fix
from .return_to_login_patch import install_return_to_login
from .sticker_library_feature import install_sticker_library_feature
from .ui import QQMessageManagerApp, SETTINGS_APPLICATION, SETTINGS_ORGANIZATION
from .vision_input_patch import install_vision_input


# 先安装会给 AI 设置追加控件的补丁，再统一重排设置界面。
install_ai_typing_delay(ui_module)
install_ai_context_message_limit(ui_module)
install_ai_rules_cleanup(ui_module, ai_module)
install_chat_summary_feature(ui_module, napcat_module)
install_summary_send_button_swap(ui_module)
install_image_layout_fix(ui_module)
# 视觉输入必须在规则整理和图片裁剪之后安装，确保图片提示及裁剪预览进入最终请求。
install_vision_input(ui_module, ai_module)
# 先安装图片生成后端，再用默认关闭的设置开关覆盖旧的“必须 @”触发方式。
install_image_generation_feature(ui_module, ai_module, napcat_module)
install_image_generation_toggle(ui_module, image_generation_module, ai_module)
# 表情包库使用锁定侧车文件保存状态，并在发送栏中提供管理入口。
install_sticker_library_feature(ui_module, sticker_module)
install_return_to_login(ui_module)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setOrganizationName(SETTINGS_ORGANIZATION)
    app.setApplicationName(SETTINGS_APPLICATION)
    manager = QQMessageManagerApp(app)
    manager.show()
    return app.exec()
