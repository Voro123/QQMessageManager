from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import napcat_client as napcat_module
from . import ui as ui_module
from .ai_typing_delay import install_ai_typing_delay
from .chat_summary_feature import install_chat_summary_feature
from .ui import QQMessageManagerApp, SETTINGS_APPLICATION, SETTINGS_ORGANIZATION


install_ai_typing_delay(ui_module)
install_chat_summary_feature(ui_module, napcat_module)


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
