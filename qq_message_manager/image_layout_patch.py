from __future__ import annotations

import hashlib
import math
import tempfile
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QImageReader

IMAGE_MAX_WIDTH = 280
IMAGE_MAX_HEIGHT = 280
ANALYSIS_MAX_SIDE = 512
ALPHA_THRESHOLD = 12
WHITE_THRESHOLD = 246
PREVIEW_VERSION = "white-crop-v2"
PREVIEW_DIR = Path(tempfile.gettempdir()) / "qq_message_manager" / "display_previews"


def install_image_layout_fix(ui_module: Any) -> None:
    """修复 QTextBrowser 图片占位，并裁掉透明或纯白画布边缘。"""
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
                    display_path, width, height = _display_image_info(local_path)
                    src = Path(display_path).as_uri()
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


def _display_image_info(path: str) -> tuple[str, int, int]:
    try:
        stat = Path(path).stat()
        return _prepare_display_image(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        width, height = _scaled_image_size(path)
        return path, width, height


@lru_cache(maxsize=512)
def _prepare_display_image(
    path: str,
    modified_ns: int,
    file_size: int,
) -> tuple[str, int, int]:
    del modified_ns, file_size
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        width, height = _scaled_image_size(path)
        return path, width, height

    crop_rect = _transparent_content_bounds(image)
    if crop_rect is None or not _is_meaningful_crop(image.rect(), crop_rect):
        white_crop = _nonwhite_content_bounds(image)
        crop_rect = white_crop if _is_safe_white_crop(image.rect(), white_crop) else None

    display_path = path
    display_image = image
    if crop_rect is not None:
        crop_rect = _add_crop_padding(crop_rect, image.rect())
        display_image = image.copy(crop_rect)
        preview_path = _preview_path(path, crop_rect)
        try:
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            if not preview_path.exists():
                display_image.save(str(preview_path), "PNG")
            if preview_path.exists():
                display_path = str(preview_path)
        except OSError:
            display_path = path
            display_image = image

    width, height = _scaled_size(display_image.width(), display_image.height())
    return display_path, width, height


def _transparent_content_bounds(image: QImage) -> QRect | None:
    if not image.hasAlphaChannel():
        return None
    return _content_bounds(image, lambda color: color.alpha() > ALPHA_THRESHOLD)


def _nonwhite_content_bounds(image: QImage) -> QRect | None:
    def is_content(color: QColor) -> bool:
        if color.alpha() <= ALPHA_THRESHOLD:
            return False
        return not (
            color.red() >= WHITE_THRESHOLD
            and color.green() >= WHITE_THRESHOLD
            and color.blue() >= WHITE_THRESHOLD
        )

    return _content_bounds(image, is_content)


def _content_bounds(image: QImage, is_content: Callable[[QColor], bool]) -> QRect | None:
    analysis = image
    if max(image.width(), image.height()) > ANALYSIS_MAX_SIDE:
        analysis = image.scaled(
            ANALYSIS_MAX_SIDE,
            ANALYSIS_MAX_SIDE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

    width = analysis.width()
    height = analysis.height()
    if width <= 0 or height <= 0:
        return None

    left = width
    top = height
    right = -1
    bottom = -1
    for y in range(height):
        for x in range(width):
            if not is_content(analysis.pixelColor(x, y)):
                continue
            left = min(left, x)
            top = min(top, y)
            right = max(right, x)
            bottom = max(bottom, y)

    if right < left or bottom < top:
        return None

    scale_x = image.width() / width
    scale_y = image.height() / height
    original_left = max(0, math.floor(left * scale_x))
    original_top = max(0, math.floor(top * scale_y))
    original_right = min(image.width() - 1, math.ceil((right + 1) * scale_x) - 1)
    original_bottom = min(image.height() - 1, math.ceil((bottom + 1) * scale_y) - 1)
    return QRect(
        original_left,
        original_top,
        original_right - original_left + 1,
        original_bottom - original_top + 1,
    )


def _is_meaningful_crop(full_rect: QRect, crop_rect: QRect | None) -> bool:
    if crop_rect is None:
        return False
    removed_width = full_rect.width() - crop_rect.width()
    removed_height = full_rect.height() - crop_rect.height()
    width_threshold = max(12, round(full_rect.width() * 0.08))
    height_threshold = max(12, round(full_rect.height() * 0.08))
    return removed_width >= width_threshold or removed_height >= height_threshold


def _is_safe_white_crop(full_rect: QRect, crop_rect: QRect | None) -> bool:
    """只在白边足够大时启用强制裁剪，避免误裁普通截图。"""
    if crop_rect is None or not _is_meaningful_crop(full_rect, crop_rect):
        return False

    left_gap = crop_rect.left() - full_rect.left()
    top_gap = crop_rect.top() - full_rect.top()
    right_gap = full_rect.right() - crop_rect.right()
    bottom_gap = full_rect.bottom() - crop_rect.bottom()

    large_vertical_gap = max(top_gap, bottom_gap) >= max(20, round(full_rect.height() * 0.14))
    large_horizontal_gap = (left_gap + right_gap) >= max(28, round(full_rect.width() * 0.22))
    full_area = max(1, full_rect.width() * full_rect.height())
    crop_area = max(1, crop_rect.width() * crop_rect.height())
    removed_ratio = 1.0 - crop_area / full_area

    return removed_ratio >= 0.12 and (large_vertical_gap or large_horizontal_gap)


def _add_crop_padding(crop_rect: QRect, full_rect: QRect) -> QRect:
    padding = max(3, min(12, round(min(crop_rect.width(), crop_rect.height()) * 0.025)))
    return crop_rect.adjusted(-padding, -padding, padding, padding).intersected(full_rect)


def _preview_path(source_path: str, crop_rect: QRect) -> Path:
    try:
        stat = Path(source_path).stat()
        signature = (
            f"{PREVIEW_VERSION}|{source_path}|{stat.st_mtime_ns}|{stat.st_size}|"
            f"{crop_rect.x()}:{crop_rect.y()}:{crop_rect.width()}:{crop_rect.height()}"
        )
    except OSError:
        signature = (
            f"{PREVIEW_VERSION}|{source_path}|"
            f"{crop_rect.x()}:{crop_rect.y()}:{crop_rect.width()}:{crop_rect.height()}"
        )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
    return PREVIEW_DIR / f"{digest}.png"


@lru_cache(maxsize=512)
def _scaled_image_size(path: str) -> tuple[int, int]:
    reader = QImageReader(path)
    size = reader.size()
    return _scaled_size(size.width(), size.height())


def _scaled_size(width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT
    scale = min(IMAGE_MAX_WIDTH / width, IMAGE_MAX_HEIGHT / height, 1.0)
    return max(1, round(width * scale)), max(1, round(height * scale))
