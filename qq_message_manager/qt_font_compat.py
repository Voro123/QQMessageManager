from __future__ import annotations

import sys
from typing import Any, Callable

from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QFont

_PREVIOUS_HANDLER: Callable[[Any, Any, str], None] | None = None
_INSTALLED = False


def install_qt_font_compatibility() -> None:
    """Avoid the harmless DirectWrite warning caused by legacy Fixedsys.

    Some Windows installations still expose ``Fixedsys`` as the system fixed
    font even though DirectWrite cannot create a modern font face for it. Qt
    then falls back successfully, but prints a warning on every launch. Map the
    legacy family to Consolas and suppress only that one known warning while
    preserving all other Qt diagnostics.
    """

    global _INSTALLED, _PREVIOUS_HANDLER
    if _INSTALLED or sys.platform != "win32":
        return

    QFont.insertSubstitution("Fixedsys", "Consolas")
    _PREVIOUS_HANDLER = qInstallMessageHandler(_qt_message_handler)
    _INSTALLED = True


def _qt_message_handler(message_type: QtMsgType, context: Any, message: str) -> None:
    if (
        message_type == QtMsgType.QtWarningMsg
        and "DirectWrite: CreateFontFaceFromHDC() failed" in message
        and 'Family="Fixedsys"' in message
    ):
        return

    if _PREVIOUS_HANDLER is not None:
        _PREVIOUS_HANDLER(message_type, context, message)
        return

    # qInstallMessageHandler returns None when Qt was using its built-in
    # handler. Keep all non-filtered diagnostics visible instead of swallowing
    # them together with the one compatibility warning.
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
