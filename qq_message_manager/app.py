from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from .ui import QQMessageManagerApp


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("QQMessageManager")
    manager = QQMessageManagerApp(app)
    manager.show()
    return app.exec()
