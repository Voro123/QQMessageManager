from __future__ import annotations

from typing import Any


def install_summary_send_button_swap(ui_module: Any) -> None:
    """交换发送栏中“发送”和“总结”按钮的位置。"""
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_summary_send_button_swap_installed", False):
        return

    original_init = main_window_cls.__init__

    def init_with_swapped_buttons(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        summary_button = getattr(self, "summary_button", None)
        send_button = getattr(self, "send_button", None)
        send_bar = self.message_input.parentWidget()
        layout = send_bar.layout() if send_bar is not None else None
        if layout is None or summary_button is None or send_button is None:
            return

        layout.removeWidget(send_button)
        layout.removeWidget(summary_button)

        # 原“总结”位于输入框之后；交换后让“发送”占据该位置。
        layout.insertWidget(1, send_button)
        # 原“发送”位于最右侧；交换后将“总结”放到最右侧。
        layout.addWidget(summary_button)

    main_window_cls.__init__ = init_with_swapped_buttons
    main_window_cls._summary_send_button_swap_installed = True
