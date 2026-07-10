from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
)

MAX_STICKER_SUMMARY_LENGTH = 120
MAX_STICKER_USAGE_HINT_LENGTH = 360


def install_sticker_metadata_editor(sticker_module: Any, library_module: Any) -> None:
    """让表情包摘要和使用时机可以在表情包库中编辑并持久化。"""
    _install_memory_update(sticker_module)
    _install_dialog_editor(library_module)


def _install_memory_update(sticker_module: Any) -> None:
    memory_cls = sticker_module.StickerMemory
    if getattr(memory_cls, "_metadata_editor_installed", False):
        return

    def update_metadata(
        self: Any,
        sticker_id: str,
        summary: str,
        usage_hint: str,
    ) -> bool:
        record = self.records.get(sticker_id)
        if record is None:
            return False

        normalized_summary = " ".join(summary.strip().split())[:MAX_STICKER_SUMMARY_LENGTH]
        normalized_summary = normalized_summary or "表情包"
        normalized_usage_hint = usage_hint.strip()[:MAX_STICKER_USAGE_HINT_LENGTH]
        if not normalized_usage_hint:
            normalized_usage_hint = (
                f"适合表达“{normalized_summary}”或相近情绪、语气时使用。"
            )

        record.summary = normalized_summary
        record.usage_hint = normalized_usage_hint
        self.save()
        return True

    memory_cls.update_metadata = update_metadata
    memory_cls._metadata_editor_installed = True


def _install_dialog_editor(library_module: Any) -> None:
    dialog_cls = library_module.StickerLibraryDialog
    if getattr(dialog_cls, "_metadata_editor_installed", False):
        return

    original_init = dialog_cls.__init__
    original_item_changed = dialog_cls._on_current_item_changed
    original_clear_details = dialog_cls._clear_details

    def init_with_metadata_editor(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)

        self.grid_tip.setText(
            "直接点击缩略图查看详情；可编辑摘要和使用时机；🔒 表示不会被自动替换。"
        )
        self.summary_edit = QLineEdit(self)
        self.summary_edit.setMaxLength(MAX_STICKER_SUMMARY_LENGTH)
        self.summary_edit.setPlaceholderText("例如：震惊、无语、开心庆祝")
        self.summary_edit.setToolTip(
            "这段摘要会直接提供给 AI，用来快速识别表情包表达的情绪和含义。"
        )

        self.usage_edit = QPlainTextEdit(self)
        self.usage_edit.setMaximumHeight(88)
        self.usage_edit.setPlaceholderText(
            "例如：适合对方说出离谱内容时表达震惊；不要在严肃道歉场景使用。"
        )
        self.usage_edit.setToolTip(
            "描述适合使用和不适合使用的聊天场景，AI 会依据这段文字判断是否发送。"
        )

        detail_form = _find_form_containing(self, self.summary_value)
        if detail_form is not None:
            _replace_form_field(
                detail_form,
                self.summary_value,
                self.summary_edit,
                "摘要（可编辑）",
            )
            _replace_form_field(
                detail_form,
                self.usage_value,
                self.usage_edit,
                "使用时机（可编辑）",
            )

        self.save_metadata_button = QPushButton("保存描述")
        self.save_metadata_button.setEnabled(False)
        self.save_metadata_button.setToolTip("保存后，AI 下次选择表情包时会使用新的摘要和使用时机。")
        self.save_metadata_button.clicked.connect(lambda: _save_current_metadata(self))
        self.metadata_status_label = QLabel("")
        self.metadata_status_label.setStyleSheet("color:#2e7d32;")

        action_layout = _find_layout_containing_in_dialog(self, self.lock_button)
        if action_layout is not None:
            action_layout.insertWidget(0, self.save_metadata_button)
            action_layout.insertWidget(1, self.metadata_status_label)

        self.summary_edit.textChanged.connect(
            lambda _text: _update_save_button_state(self)
        )
        self.usage_edit.textChanged.connect(
            lambda: _update_save_button_state(self)
        )

        _sync_editor_from_current(self)

    def item_changed_with_metadata_editor(
        self: Any,
        current: Any,
        previous: Any,
    ) -> None:
        original_item_changed(self, current, previous)
        _sync_editor_from_current(self)

    def clear_details_with_metadata_editor(self: Any, preview_text: str) -> None:
        original_clear_details(self, preview_text)
        summary_edit = getattr(self, "summary_edit", None)
        usage_edit = getattr(self, "usage_edit", None)
        save_button = getattr(self, "save_metadata_button", None)
        status_label = getattr(self, "metadata_status_label", None)
        if summary_edit is not None:
            summary_edit.clear()
            summary_edit.setEnabled(False)
        if usage_edit is not None:
            usage_edit.clear()
            usage_edit.setEnabled(False)
        if save_button is not None:
            save_button.setEnabled(False)
        if status_label is not None:
            status_label.clear()

    dialog_cls.__init__ = init_with_metadata_editor
    dialog_cls._on_current_item_changed = item_changed_with_metadata_editor
    dialog_cls._clear_details = clear_details_with_metadata_editor
    dialog_cls._metadata_editor_installed = True


def _sync_editor_from_current(dialog: Any) -> None:
    summary_edit = getattr(dialog, "summary_edit", None)
    usage_edit = getattr(dialog, "usage_edit", None)
    save_button = getattr(dialog, "save_metadata_button", None)
    status_label = getattr(dialog, "metadata_status_label", None)
    if summary_edit is None or usage_edit is None or save_button is None:
        return

    record = dialog.memory.get(dialog.current_sticker_id) if dialog.current_sticker_id else None
    summary_edit.blockSignals(True)
    usage_edit.blockSignals(True)
    if record is None:
        summary_edit.clear()
        usage_edit.clear()
        summary_edit.setEnabled(False)
        usage_edit.setEnabled(False)
        save_button.setEnabled(False)
    else:
        summary_edit.setText(record.summary or "表情包")
        usage_edit.setPlainText(record.usage_hint or "")
        summary_edit.setEnabled(True)
        usage_edit.setEnabled(True)
        save_button.setEnabled(True)
    summary_edit.blockSignals(False)
    usage_edit.blockSignals(False)
    if status_label is not None:
        status_label.clear()


def _update_save_button_state(dialog: Any) -> None:
    button = getattr(dialog, "save_metadata_button", None)
    summary_edit = getattr(dialog, "summary_edit", None)
    if button is None or summary_edit is None:
        return
    button.setEnabled(
        bool(dialog.current_sticker_id)
        and summary_edit.isEnabled()
        and bool(summary_edit.text().strip())
    )
    status_label = getattr(dialog, "metadata_status_label", None)
    if status_label is not None:
        status_label.clear()


def _save_current_metadata(dialog: Any) -> None:
    sticker_id = dialog.current_sticker_id
    if not sticker_id:
        return
    summary = dialog.summary_edit.text().strip()
    usage_hint = dialog.usage_edit.toPlainText().strip()
    if not summary:
        dialog.metadata_status_label.setStyleSheet("color:#c62828;")
        dialog.metadata_status_label.setText("摘要不能为空")
        return

    if not dialog.memory.update_metadata(sticker_id, summary, usage_hint):
        dialog.metadata_status_label.setStyleSheet("color:#c62828;")
        dialog.metadata_status_label.setText("保存失败：记录不存在")
        return

    dialog._refresh_records(sticker_id)
    dialog.metadata_status_label.setStyleSheet("color:#2e7d32;")
    dialog.metadata_status_label.setText("已保存，AI 将使用新描述")


def _replace_form_field(
    form: QFormLayout,
    old_widget: Any,
    new_widget: Any,
    label_text: str,
) -> None:
    row, _role = form.getWidgetPosition(old_widget)
    if row < 0:
        return
    label = form.labelForField(old_widget)
    if label is not None:
        label.setText(label_text)
    form.removeWidget(old_widget)
    old_widget.hide()
    form.setWidget(row, QFormLayout.ItemRole.FieldRole, new_widget)


def _find_form_containing(dialog: Any, widget: Any) -> QFormLayout | None:
    for form in dialog.findChildren(QFormLayout):
        row, _role = form.getWidgetPosition(widget)
        if row >= 0:
            return form
    return None


def _find_layout_containing_in_dialog(dialog: Any, widget: Any) -> QLayout | None:
    candidates: list[QLayout] = []
    root = dialog.layout()
    if isinstance(root, QLayout):
        candidates.append(root)
    candidates.extend(dialog.findChildren(QLayout))
    for layout in candidates:
        for index in range(layout.count()):
            if layout.itemAt(index).widget() is widget:
                return layout
    return None
