from __future__ import annotations

from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtGui import QImageReader

IMAGE_MAX_WIDTH = 280
IMAGE_MAX_HEIGHT = 280


def install_image_layout_fix(ui_module: Any) -> None:
    """为 QTextBrowser 中的图片写入明确宽高，避免按原图尺寸预留大片空白。"""
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_image_layout_fix_installed", False):
        return

    def message_html(self: Any, message: Any) -> str:
        time_text = message.timestamp.strftime("%H:%M:%S")
        sender = escape(message.sender_name)
        align = "right" if message.outgoing else "left"
        bubble_background = "#dcf8c6" if message.outgoing else "#ffffff"
        bubble_border = "#c8e6a2" if message.outgoing else "#e5e5e5"
        text_color = "#000000"

        allow_images = ui_module.load_ai_config(self.settings).allow_image_read_enabled
        image_html = ""
        if allow_images and message.images:
            parts: list[str] = []
            for image in message.images[:3]:
                if image.local_path:
                    local_path = str(Path(image.local_path).resolve())
                    src = Path(local_path).as_uri()
                    width, height = _scaled_image_size(local_path)
                    parts.append(
                        f"<img src='{src}' width='{width}' height='{height}' "
                        "style='margin-top:6px;border-radius:10px;'>"
                    )
                elif image.load_failed:
                    parts.append("<span style='color:#c0392b;font-size:12px;'>[图片加载失败]</span>")
                else:
                    parts.append("<span style='color:#aaa;font-size:12px;'>[图片加载中]</span>")
            if parts:
                image_html = "<div style='margin-top:6px;'>" + "<br>".join(parts) + "</div>"

        text = message.text.strip()
        show_text = text and text != "[图片消息已过滤]"
        if image_html:
            body = (escape(text).replace("\n", "<br>") + image_html) if show_text else image_html
        elif show_text:
            body = escape(text).replace("\n", "<br>")
        elif message.images:
            body = "<span style='color:#aaa;font-size:12px;'>[图片消息已过滤]</span>"
        else:
            body = "<span style='color:#aaa;font-size:12px;'>[空消息]</span>"

        return (
            f"<div style='margin:16px 12px;text-align:{align};'>"
            f"<div style='color:#8e8e8e;font-size:11px;margin-bottom:4px;'>{time_text} · {sender}</div>"
            "<div style='display:inline-block;margin-top:2px;padding:10px 14px;"
            f"background:{bubble_background};border:1px solid {bubble_border};"
            "border-radius:12px;line-height:1.5;text-align:left;max-width:70%;"
            f"color:{text_color};'>"
            f"{body}</div></div>"
        )

    main_window_cls._message_html = message_html
    main_window_cls._image_layout_fix_installed = True


@lru_cache(maxsize=512)
def _scaled_image_size(path: str) -> tuple[int, int]:
    reader = QImageReader(path)
    size = reader.size()
    width = size.width()
    height = size.height()
    if width <= 0 or height <= 0:
        return IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT

    scale = min(IMAGE_MAX_WIDTH / width, IMAGE_MAX_HEIGHT / height, 1.0)
    scaled_width = max(1, round(width * scale))
    scaled_height = max(1, round(height * scale))
    return scaled_width, scaled_height
