from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .image_cache import ensure_cached
from .image_layout_patch import _display_image_info
from .models import ChatImage

LOCK_FILE_SUFFIX = ".locks.json"
THUMBNAIL_SIZE = 112
THUMBNAIL_GRID_WIDTH = 148
THUMBNAIL_GRID_HEIGHT = 158
THUMBNAIL_WORKERS = 4


class StickerPreviewBridge(QObject):
    preview_ready = Signal(str, str, int)
    preview_failed = Signal(str, int)


def install_sticker_library_feature(ui_module: Any, sticker_module: Any) -> None:
    """增加可锁定的表情包记忆库，以及主窗口中的表情包库管理入口。"""
    _install_lockable_memory(sticker_module)
    _install_library_button(ui_module)


def _install_lockable_memory(sticker_module: Any) -> None:
    memory_cls = sticker_module.StickerMemory
    if getattr(memory_cls, "_lockable_memory_installed", False):
        return

    original_init = memory_cls.__init__
    original_load = memory_cls.load
    original_remember = memory_cls.remember_from_event

    def init_with_locks(self: Any, *args: Any, **kwargs: Any) -> None:
        if args:
            memory_path = Path(args[0])
        else:
            memory_path = Path(kwargs.get("path", sticker_module.STICKER_MEMORY_PATH))
        self.lock_path = memory_path.with_name(memory_path.stem + LOCK_FILE_SUFFIX)
        self.locked_ids = _read_lock_ids(self.lock_path)
        original_init(self, *args, **kwargs)
        _cleanup_stale_locks(self)

    def load_with_locks(self: Any) -> None:
        original_load(self)
        _cleanup_stale_locks(self)

    def remember_with_actual_count(self: Any, event: dict[str, Any]) -> int:
        before = set(self.records)
        original_remember(self, event)
        return len(set(self.records) - before)

    def prune_without_locked_records(self: Any) -> None:
        locked = set(getattr(self, "locked_ids", set()))
        while len(self.records) > self.limit:
            candidates = [record for record in self.records.values() if record.id not in locked]
            if not candidates:
                break
            victim = min(
                candidates,
                key=lambda record: (record.use_count, record.last_used_at or "", record.created_at or ""),
            )
            self.records.pop(victim.id, None)

    def set_locked(self: Any, sticker_id: str, locked: bool) -> bool:
        if sticker_id not in self.records:
            return False
        if locked:
            self.locked_ids.add(sticker_id)
        else:
            self.locked_ids.discard(sticker_id)
        _write_lock_ids(self.lock_path, self.locked_ids)
        return True

    def is_locked(self: Any, sticker_id: str) -> bool:
        return sticker_id in self.locked_ids

    def delete_record(self: Any, sticker_id: str) -> bool:
        if sticker_id not in self.records:
            return False
        self.records.pop(sticker_id, None)
        self.locked_ids.discard(sticker_id)
        self.save()
        _write_lock_ids(self.lock_path, self.locked_ids)
        return True

    def all_records(self: Any) -> list[Any]:
        return list(self.records.values())

    memory_cls.__init__ = init_with_locks
    memory_cls.load = load_with_locks
    memory_cls.remember_from_event = remember_with_actual_count
    memory_cls._prune = prune_without_locked_records
    memory_cls.set_locked = set_locked
    memory_cls.is_locked = is_locked
    memory_cls.delete_record = delete_record
    memory_cls.all_records = all_records
    memory_cls._lockable_memory_installed = True


def _install_library_button(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_sticker_library_button_installed", False):
        return

    original_init = main_window_cls.__init__

    def init_with_sticker_library(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.sticker_library_button = QPushButton("表情包库")
        self.sticker_library_button.setToolTip("以缩略图网格查看、锁定或删除 AI 已记忆的表情包")
        self.sticker_library_button.clicked.connect(lambda: _open_sticker_library(self))

        send_bar = self.message_input.parentWidget()
        layout = send_bar.layout() if send_bar is not None else None
        if layout is None:
            return
        summary_button = getattr(self, "summary_button", None)
        summary_index = layout.indexOf(summary_button) if summary_button is not None else -1
        if summary_index >= 0:
            layout.insertWidget(summary_index, self.sticker_library_button)
        else:
            layout.addWidget(self.sticker_library_button)

    main_window_cls.__init__ = init_with_sticker_library
    main_window_cls._sticker_library_button_installed = True


def _open_sticker_library(window: Any) -> None:
    dialog = StickerLibraryDialog(window.sticker_memory, window.token, window)
    dialog.exec()


class StickerLibraryDialog(QDialog):
    def __init__(self, memory: Any, token: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.memory = memory
        self.token = token
        self.current_sticker_id = ""
        self.item_by_id: dict[str, QListWidgetItem] = {}
        self.preview_paths: dict[str, str] = {}
        self.thumbnail_generation = 0

        self.preview_bridge = StickerPreviewBridge(self)
        self.preview_bridge.preview_ready.connect(self._on_preview_ready)
        self.preview_bridge.preview_failed.connect(self._on_preview_failed)

        self.setWindowTitle("AI 表情包库")
        self.resize(1100, 700)
        self.setMinimumSize(900, 560)

        self.count_label = QLabel()
        self.grid_tip = QLabel("直接点击缩略图查看详情；🔒 表示该表情包不会被自动替换。")
        self.grid_tip.setStyleSheet("color:#777;")

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListView.ViewMode.IconMode)
        self.list_widget.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_widget.setMovement(QListView.Movement.Static)
        self.list_widget.setLayoutMode(QListView.LayoutMode.Batched)
        self.list_widget.setBatchSize(20)
        self.list_widget.setWrapping(True)
        self.list_widget.setWordWrap(True)
        self.list_widget.setSpacing(8)
        self.list_widget.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.list_widget.setGridSize(QSize(THUMBNAIL_GRID_WIDTH, THUMBNAIL_GRID_HEIGHT))
        self.list_widget.setMinimumWidth(560)
        self.list_widget.setStyleSheet(
            "QListWidget { background:#f6f7f9; border:1px solid #d9dde3; border-radius:8px; }"
            "QListWidget::item { background:white; border:1px solid #e1e4e8; border-radius:8px; padding:6px; }"
            "QListWidget::item:selected { background:#e8f3ff; border:2px solid #409eff; color:#111; }"
        )
        self.list_widget.currentItemChanged.connect(self._on_current_item_changed)

        self.preview_label = QLabel("点击左侧任意缩略图查看大图")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(320, 320)
        self.preview_label.setStyleSheet(
            "QLabel { background:white; border:1px solid #d9dde3; border-radius:8px; color:#888; }"
        )

        self.id_value = _selectable_label("-")
        self.summary_value = _selectable_label("-")
        self.usage_value = _selectable_label("-")
        self.type_value = _selectable_label("-")
        self.use_count_value = _selectable_label("-")
        self.lock_value = _selectable_label("-")
        self.created_value = _selectable_label("-")
        self.last_used_value = _selectable_label("-")

        detail_form = QFormLayout()
        detail_form.addRow("ID", self.id_value)
        detail_form.addRow("类型", self.type_value)
        detail_form.addRow("摘要", self.summary_value)
        detail_form.addRow("用途", self.usage_value)
        detail_form.addRow("使用次数", self.use_count_value)
        detail_form.addRow("锁定状态", self.lock_value)
        detail_form.addRow("记录时间", self.created_value)
        detail_form.addRow("最近使用", self.last_used_value)

        self.lock_button = QPushButton("锁定")
        self.lock_button.setEnabled(False)
        self.lock_button.clicked.connect(self._toggle_lock)
        self.delete_button = QPushButton("删除")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_selected)
        refresh_button = QPushButton("刷新缩略图")
        refresh_button.clicked.connect(self._refresh_records)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self.lock_button)
        action_layout.addWidget(self.delete_button)
        action_layout.addWidget(refresh_button)
        action_layout.addStretch(1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.count_label)
        left_layout.addWidget(self.grid_tip)
        left_layout.addWidget(self.list_widget, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignHCenter)
        right_layout.addLayout(detail_form)
        right_layout.addLayout(action_layout)
        right_layout.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([700, 380])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)

        self._refresh_records()

    def _refresh_records(self, keep_id: str = "") -> None:
        self.memory.load()
        selected_id = keep_id or self.current_sticker_id
        self.thumbnail_generation += 1
        generation = self.thumbnail_generation

        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.item_by_id.clear()

        records = sorted(
            self.memory.all_records(),
            key=lambda record: (
                0 if self.memory.is_locked(record.id) else 1,
                -int(record.use_count),
                record.summary or record.id,
            ),
        )
        valid_ids = {record.id for record in records}
        self.preview_paths = {
            sticker_id: path
            for sticker_id, path in self.preview_paths.items()
            if sticker_id in valid_ids and Path(path).exists()
        }

        selected_item: QListWidgetItem | None = None
        missing_records: list[Any] = []
        for record in records:
            locked = self.memory.is_locked(record.id)
            item = QListWidgetItem(_thumbnail_text(record, locked))
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setSizeHint(QSize(THUMBNAIL_GRID_WIDTH, THUMBNAIL_GRID_HEIGHT))
            item.setToolTip(
                f"{record.summary or '表情包'}\n"
                f"用途：{record.usage_hint or '-'}\n"
                f"使用次数：{record.use_count}\n"
                f"状态：{'已锁定' if locked else '未锁定'}"
            )
            self.list_widget.addItem(item)
            self.item_by_id[record.id] = item

            cached_preview = self.preview_paths.get(record.id)
            if cached_preview:
                self._set_thumbnail_icon(item, cached_preview)
            else:
                missing_records.append(record)

            if record.id == selected_id:
                selected_item = item

        locked_count = sum(1 for record in records if self.memory.is_locked(record.id))
        self.count_label.setText(
            f"已记录 {len(records)} / {self.memory.limit} 个表情包，其中锁定 {locked_count} 个"
        )
        self.list_widget.blockSignals(False)

        if selected_item is not None:
            self.list_widget.setCurrentItem(selected_item)
        elif self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)
        else:
            self.current_sticker_id = ""
            self._clear_details("当前还没有记忆任何表情包")

        if missing_records:
            self._load_thumbnail_batch(missing_records, generation)

    def _load_thumbnail_batch(self, records: list[Any], generation: int) -> None:
        token = self.token

        def coordinator() -> None:
            with ThreadPoolExecutor(max_workers=THUMBNAIL_WORKERS) as pool:
                futures = {
                    pool.submit(_prepare_record_preview, record, token): record.id
                    for record in records
                }
                for future in as_completed(futures):
                    sticker_id = futures[future]
                    try:
                        path = future.result()
                    except Exception:  # noqa: BLE001
                        path = ""
                    if path:
                        self.preview_bridge.preview_ready.emit(sticker_id, path, generation)
                    else:
                        self.preview_bridge.preview_failed.emit(sticker_id, generation)

        threading.Thread(target=coordinator, daemon=True).start()

    def _on_current_item_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        del previous
        if current is None:
            self.current_sticker_id = ""
            self._clear_details("点击左侧任意缩略图查看大图")
            return
        sticker_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self.current_sticker_id = sticker_id
        record = self.memory.get(sticker_id)
        if record is None:
            self._clear_details("记录不存在")
            return

        locked = self.memory.is_locked(sticker_id)
        self.id_value.setText(record.id)
        self.type_value.setText(record.source_type or "-")
        self.summary_value.setText(record.summary or "-")
        self.usage_value.setText(record.usage_hint or "-")
        self.use_count_value.setText(str(record.use_count))
        self.lock_value.setText("已锁定，不参与自动淘汰" if locked else "未锁定")
        self.created_value.setText(record.created_at or "-")
        self.last_used_value.setText(record.last_used_at or "从未使用")
        self.lock_button.setText("解除锁定" if locked else "锁定")
        self.lock_button.setEnabled(True)
        self.delete_button.setEnabled(True)

        path = self.preview_paths.get(sticker_id)
        if path:
            self._show_large_preview(path)
        else:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("正在加载预览…")

    def _on_preview_ready(self, sticker_id: str, path: str, generation: int) -> None:
        if generation != self.thumbnail_generation:
            return
        self.preview_paths[sticker_id] = path
        item = self.item_by_id.get(sticker_id)
        if item is not None:
            self._set_thumbnail_icon(item, path)
        if sticker_id == self.current_sticker_id:
            self._show_large_preview(path)

    def _on_preview_failed(self, sticker_id: str, generation: int) -> None:
        if generation != self.thumbnail_generation:
            return
        item = self.item_by_id.get(sticker_id)
        record = self.memory.get(sticker_id)
        if record is not None and not self.memory.is_locked(sticker_id):
            # A record without a decodable local/remote image is not a usable
            # sticker.  Remove old bad entries as they are discovered, while
            # preserving the explicit no-eviction guarantee for locked ones.
            self.memory.delete_record(sticker_id)
            self.preview_paths.pop(sticker_id, None)
            self.item_by_id.pop(sticker_id, None)
            if item is not None:
                row = self.list_widget.row(item)
                self.list_widget.takeItem(row)
            if sticker_id == self.current_sticker_id:
                self.current_sticker_id = ""
                if self.list_widget.count():
                    self.list_widget.setCurrentRow(0)
                else:
                    self._clear_details("当前还没有记忆任何表情包")
            records = self.memory.all_records()
            locked_count = sum(1 for current in records if self.memory.is_locked(current.id))
            self.count_label.setText(
                f"已记录 {len(records)} / {self.memory.limit} 个表情包，其中锁定 {locked_count} 个"
            )
            return
        if item is not None:
            item.setToolTip(item.toolTip() + "\n预览：无法读取")
        if sticker_id == self.current_sticker_id:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("当前记录没有可读取的图片预览")

    def _set_thumbnail_icon(self, item: QListWidgetItem, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        scaled = pixmap.scaled(
            THUMBNAIL_SIZE,
            THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        item.setIcon(QIcon(scaled))

    def _show_large_preview(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("预览读取失败")
            return
        scaled = pixmap.scaled(
            310,
            310,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setText("")
        self.preview_label.setPixmap(scaled)

    def _toggle_lock(self) -> None:
        sticker_id = self.current_sticker_id
        if not sticker_id:
            return
        locked = not self.memory.is_locked(sticker_id)
        self.memory.set_locked(sticker_id, locked)
        self._refresh_records(sticker_id)

    def _delete_selected(self) -> None:
        sticker_id = self.current_sticker_id
        record = self.memory.get(sticker_id)
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "删除表情包记录",
            f"确定删除“{record.summary or sticker_id}”吗？\n只删除 AI 记忆记录，不会删除 QQ 中的原表情包。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.memory.delete_record(sticker_id)
        self.current_sticker_id = ""
        self.preview_paths.pop(sticker_id, None)
        self._refresh_records()

    def _clear_details(self, preview_text: str) -> None:
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText(preview_text)
        for label in (
            self.id_value,
            self.summary_value,
            self.usage_value,
            self.type_value,
            self.use_count_value,
            self.lock_value,
            self.created_value,
            self.last_used_value,
        ):
            label.setText("-")
        self.lock_button.setEnabled(False)
        self.delete_button.setEnabled(False)


def _prepare_record_preview(record: Any, token: str) -> str:
    image = ChatImage(
        url=record.url,
        path=record.path,
        file=record.file,
        file_id=record.file_id,
        file_unique=record.file_unique,
    )
    local_path = ensure_cached(image, token=token)
    if not local_path:
        return ""
    display_path, _width, _height = _display_image_info(local_path)
    return display_path


def _thumbnail_text(record: Any, locked: bool) -> str:
    summary = (record.summary or "表情包").strip()
    if len(summary) > 12:
        summary = summary[:11] + "…"
    prefix = "🔒 " if locked else ""
    return f"{prefix}{summary}\n使用 {record.use_count} 次"


def _selectable_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _read_lock_ids(path: Path) -> set[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(value) for value in raw if str(value).strip()}


def _write_lock_ids(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(values), ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_stale_locks(memory: Any) -> None:
    locked = set(getattr(memory, "locked_ids", set()))
    valid = locked.intersection(memory.records)
    if valid != locked:
        memory.locked_ids = valid
        _write_lock_ids(memory.lock_path, valid)
