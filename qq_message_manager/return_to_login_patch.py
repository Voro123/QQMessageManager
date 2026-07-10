from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QPushButton


def install_return_to_login(ui_module: Any) -> None:
    """点击“断开连接”后销毁当前聊天窗口并返回登录窗口。"""
    main_window_cls = ui_module.MainWindow
    manager_cls = ui_module.QQMessageManagerApp
    if getattr(main_window_cls, "_return_to_login_installed", False):
        return

    original_disconnect = main_window_cls.disconnect_from_server
    original_login = manager_cls._login

    def disconnect_and_maybe_return(self: Any) -> None:
        # closeEvent 也会调用 disconnect_from_server；只有按钮点击才返回登录页，
        # 避免用户关闭主窗口时又把登录窗口弹出来。
        manual_disconnect = isinstance(self.sender(), QPushButton)
        original_disconnect(self)
        if not manual_disconnect:
            return
        callback: Callable[[Any], None] | None = getattr(self, "_return_to_login_callback", None)
        if callback is not None:
            callback(self)

    def login_with_return_callback(self: Any, websocket_url: str, token: str) -> None:
        original_login(self, websocket_url, token)
        window = self.main_window
        if window is None:
            return

        def return_to_login(disconnected_window: Any) -> None:
            if self.main_window is not disconnected_window:
                return
            disconnected_window.hide()
            disconnected_window.deleteLater()
            self.main_window = None
            self.login_window.show()
            self.login_window.raise_()
            self.login_window.activateWindow()

        window._return_to_login_callback = return_to_login

    main_window_cls.disconnect_from_server = disconnect_and_maybe_return
    manager_cls._login = login_with_return_callback
    main_window_cls._return_to_login_installed = True
