from __future__ import annotations

import re
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .folder_access_agent import FolderAccessAgent
from .folder_access_models import DEFAULT_TEXT_EXTENSIONS, FolderGrant
from .folder_access_service import FolderAccessService
from .folder_access_store import FolderGrantStore

FILE_INTENT_RE = re.compile(
    r"(?:文件夹|目录|文件|项目|代码|README|readme|列出|读取|查看|搜索|查找|修改|写入|新建|创建|保存|更新)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FolderRoute:
    alias: str = ""
    immediate_reply: str = ""


class FolderFeatureBridge(QObject):
    reply_ready = Signal(str, str)
    log_ready = Signal(str)


class FolderAccessController:
    def __init__(self, window: Any, ui_module: Any, ai_module: Any, skill_module: Any) -> None:
        self._window_ref = weakref.ref(window)
        self.ui_module = ui_module
        self.ai_module = ai_module
        self.skill_module = skill_module
        self.bridge = FolderFeatureBridge(window)
        self.bridge.reply_ready.connect(window.ai_reply_ready.emit)
        self.bridge.log_ready.connect(window.append_log)
        self.service = FolderAccessService(
            self._load_grants,
            log_callback=self.bridge.log_ready.emit,
        )
        self.agent = FolderAccessAgent(
            self.service,
            ai_module.generate_raw_completion,
            skill_enabled=self.skill_enabled,
        )

    def skill_enabled(self) -> bool:
        settings = self._new_settings()
        enabled = self.skill_module.is_skill_enabled(
            settings,
            self.skill_module.FOLDER_ACCESS_SKILL_ID,
        )
        if not enabled and hasattr(self, "agent"):
            self.agent.pending_actions.clear()
        return enabled

    def _new_settings(self) -> QSettings:
        # QSettings is reentrant only when each thread owns its own instance.
        # Never pass MainWindow.settings into an AI/file worker thread.
        return QSettings(
            self.ui_module.SETTINGS_ORGANIZATION,
            self.ui_module.SETTINGS_APPLICATION,
        )

    def _load_grants(self) -> list[FolderGrant]:
        return FolderGrantStore(self._new_settings()).load()

    def route(self, message: Any) -> FolderRoute | None:
        if not self.skill_enabled() or message.historical or message.outgoing:
            return None
        text = str(message.text or "").strip()
        if not text or not FILE_INTENT_RE.search(text):
            return None
        grants = [grant for grant in self._load_grants() if grant.enabled]
        if not grants:
            return None
        folded = text.casefold()
        matched = [grant for grant in grants if grant.alias.casefold() in folded]
        if len(matched) > 1:
            self._audit_denied(message, "", "ambiguous_alias")
            return FolderRoute(immediate_reply="这条消息可能同时涉及多个关联项目，请明确只操作其中一个。")
        if not matched:
            allowed = [grant for grant in grants if str(message.sender_id) in grant.allowed_sender_ids]
            if not allowed:
                self._audit_denied(message, "", "sender_not_allowed")
                return FolderRoute(immediate_reply="你没有操作受控文件夹的权限。")
            aliases = "、".join(grant.alias for grant in allowed)
            return FolderRoute(immediate_reply=f"请明确要操作哪个关联项目：{aliases}。")
        grant = matched[0]
        if str(message.sender_id) not in grant.allowed_sender_ids:
            self._audit_denied(message, grant.alias, "sender_not_allowed")
            return FolderRoute(immediate_reply=f"你没有操作 {grant.alias} 文件夹的权限。")
        return FolderRoute(alias=grant.alias)

    def _audit_denied(self, message: Any, alias: str, error_code: str) -> None:
        service = getattr(self, "service", None)
        if service is not None:
            service.audit_denied_request(
                session_id=str(getattr(message, "session_id", "")),
                sender_id=str(getattr(message, "sender_id", "")),
                alias=alias,
                error_code=error_code,
            )

    def generate(self, config: Any, message: Any, route: FolderRoute) -> str:
        if route.immediate_reply:
            return route.immediate_reply
        return self.agent.run(
            config,
            user_text=str(message.text or ""),
            session_id=str(message.session_id),
            sender_id=str(message.sender_id),
            required_alias=route.alias,
        )

    def consume_confirmation(self, message: Any) -> bool:
        if not self.skill_enabled() or message.historical or message.outgoing:
            return False
        window = self._window_ref()
        if window is None or str(message.session_id) not in window.ai_managed_sessions:
            return False
        action_id = self.agent.confirmation_action_id(str(message.text or ""))
        if not action_id:
            return False

        def worker() -> None:
            reply = self.agent.confirm(
                action_id,
                session_id=str(message.session_id),
                sender_id=str(message.sender_id),
            )
            self.bridge.reply_ready.emit(str(message.session_id), reply)

        threading.Thread(target=worker, daemon=True).start()
        return True


def install_folder_access_feature(
    ui_module: Any,
    ai_module: Any,
    skill_module: Any,
) -> None:
    """Install one UI/configuration entry and one explicit runtime controller."""
    _install_main_window_controller(ui_module, ai_module, skill_module)
    _install_settings_entry(ui_module)


def _install_main_window_controller(ui_module: Any, ai_module: Any, skill_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_folder_access_feature_installed", False):
        return
    original_init = main_window_cls.__init__

    def init_with_folder_access(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.folder_access_controller = FolderAccessController(self, ui_module, ai_module, skill_module)

    main_window_cls.__init__ = init_with_folder_access
    main_window_cls._folder_access_feature_installed = True


def _install_settings_entry(ui_module: Any) -> None:
    dialog_cls = ui_module.AiSettingsDialog
    if getattr(dialog_cls, "_folder_access_settings_installed", False):
        return
    original_init = dialog_cls.__init__

    def init_with_folder_settings(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        callbacks = dict(getattr(self, "skill_configuration_callbacks", {}))
        settings = self.settings

        def configure_folder_access(parent: QWidget) -> None:
            FolderGrantManagerDialog(settings, parent).exec()

        callbacks["folder_access"] = configure_folder_access
        self.skill_configuration_callbacks = callbacks

    dialog_cls.__init__ = init_with_folder_settings
    dialog_cls._folder_access_settings_installed = True


class FolderGrantManagerDialog(QDialog):
    HEADERS = ("关联名", "文件夹", "读取", "写入", "写入确认", "状态")

    def __init__(self, settings: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = FolderGrantStore(settings)
        self.grants = self.store.load()
        self.setWindowTitle("受控文件夹")
        self.resize(980, 520)

        warning = QLabel("安全提示：允许操作的 QQ 号为空时，任何群成员都不能执行文件操作。请只填写可信 QQ。")
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#b54708;")
        self.table = QTableWidget(0, len(self.HEADERS), self)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        actions = QHBoxLayout()
        for label, callback in (
            ("添加", self._add), ("编辑", self._edit), ("删除授权", self._delete),
            ("打开文件夹", self._open), ("测试权限", self._test),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(warning)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self) -> None:
        self.table.setRowCount(len(self.grants))
        for row, grant in enumerate(self.grants):
            values = (
                grant.alias, grant.root_path, "是" if grant.read_enabled else "否",
                "是" if grant.write_enabled else "否",
                "是" if grant.write_confirmation_required else "否",
                "启用" if grant.enabled else "停用",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def _selected_index(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _add(self) -> None:
        dialog = FolderGrantEditDialog(None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.grants.append(dialog.grant())
            self._refresh()

    def _edit(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        dialog = FolderGrantEditDialog(self.grants[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.grants[index] = dialog.grant()
            self._refresh()

    def _delete(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        grant = self.grants[index]
        answer = QMessageBox.question(
            self, "删除授权", f"只删除“{grant.alias}”的授权配置，不会删除实际文件夹。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.grants.pop(index)
            self._refresh()

    def _open(self) -> None:
        index = self._selected_index()
        if index >= 0:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.grants[index].root_path))

    def _test(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        ok, message = FolderAccessService.test_grant_access(self.grants[index])
        (QMessageBox.information if ok else QMessageBox.warning)(self, "权限测试", message)

    def _save(self) -> None:
        try:
            self.store.save(self.grants)
        except ValueError as exc:
            QMessageBox.warning(self, "无法保存", str(exc))
            return
        self.accept()


class FolderGrantEditDialog(QDialog):
    def __init__(self, source: FolderGrant | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = source or FolderGrant()
        self.setWindowTitle("编辑受控文件夹" if source else "添加受控文件夹")
        self.setMinimumWidth(680)
        self.alias = QLineEdit(self.source.alias)
        self.description = QTextEdit(self.source.description)
        self.description.setMaximumHeight(80)
        self.root_path = QLineEdit(self.source.root_path)
        self.root_path.setReadOnly(True)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.root_path, 1)
        path_layout.addWidget(browse)
        self.enabled = QCheckBox("启用")
        self.enabled.setChecked(self.source.enabled)
        self.read_enabled = QCheckBox("允许读取")
        self.read_enabled.setChecked(self.source.read_enabled)
        self.write_enabled = QCheckBox("允许写入")
        self.write_enabled.setChecked(self.source.write_enabled)
        self.confirm = QCheckBox("写入前需要确认")
        self.confirm.setChecked(self.source.write_confirmation_required)
        self.senders = QLineEdit(", ".join(self.source.allowed_sender_ids))
        self.senders.setPlaceholderText("可信 QQ 号，多个用逗号分隔；留空则任何人都不能操作")
        self.extensions = QLineEdit(", ".join(self.source.allowed_extensions))
        self.extensions.setPlaceholderText(".txt, .md, .json, .py")
        form = QFormLayout()
        form.addRow("关联名", self.alias)
        form.addRow("描述", self.description)
        form.addRow("文件夹", path_row)
        form.addRow(self.enabled)
        form.addRow(self.read_enabled)
        form.addRow(self.write_enabled)
        form.addRow(self.confirm)
        form.addRow("允许操作的 QQ", self.senders)
        form.addRow("允许扩展名", self.extensions)
        tip = QLabel("默认只读、禁止写入、写入需确认。真实路径不会发送给 AI。")
        tip.setStyleSheet("color:#667085;")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(tip)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择授权文件夹", self.root_path.text())
        if selected:
            self.root_path.setText(selected)

    def grant(self) -> FolderGrant:
        senders = re.split(r"[,，;；\s]+", self.senders.text().strip())
        extensions = re.split(r"[,，;；\s]+", self.extensions.text().strip())
        return FolderGrant(
            grant_id=self.source.grant_id,
            alias=self.alias.text(),
            description=self.description.toPlainText(),
            root_path=self.root_path.text(),
            enabled=self.enabled.isChecked(),
            read_enabled=self.read_enabled.isChecked(),
            write_enabled=self.write_enabled.isChecked(),
            write_confirmation_required=self.confirm.isChecked(),
            allowed_sender_ids=[value for value in senders if value],
            allowed_extensions=[value for value in extensions if value] or list(DEFAULT_TEXT_EXTENSIONS),
        ).normalized()

    def _validate_accept(self) -> None:
        grant = self.grant()
        if not grant.alias:
            QMessageBox.warning(self, "配置不完整", "关联名不能为空。")
            return
        ok, message = FolderAccessService.test_grant_access(grant)
        if not ok:
            QMessageBox.warning(self, "配置不完整", message)
            return
        self.accept()
