from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import ai_client as ai_module
from . import napcat_client as napcat_module
from . import ui as ui_module
from .ai_context_limit_patch import install_ai_context_message_limit
from .ai_rules_cleanup import install_ai_rules_cleanup
from .ai_typing_delay import install_ai_typing_delay
from .button_position_patch import install_summary_send_button_swap
from .chat_summary_feature import install_chat_summary_feature
from .image_layout_patch import install_image_layout_fix
from .ui import QQMessageManagerApp, SETTINGS_APPLICATION, SETTINGS_ORGANIZATION


# 先安装会给 AI 设置追加控件的补丁，再统一重排设置界面。
install_ai_typing_delay(ui_module)
install_ai_context_message_limit(ui_module)
install_ai_rules_cleanup(ui_module, ai_module)
install_chat_summary_feature(ui_module, napcat_module)
install_summary_send_button_swap(ui_module)
install_image_layout_fix(ui_module)


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
