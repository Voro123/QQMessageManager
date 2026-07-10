from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .automation_models import (
    RECIPIENT_SELF,
    TRANSFER_AUTO,
    TRANSFER_LOCAL,
    TRANSFER_STREAM,
    task_by_id,
)


def install_automation_stage3_feature(
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
    ui_module: Any,
) -> None:
    """安装文件发送测试、好友探测、发送历史和归档校验。"""
    _install_delivery_history_store(automation_module)
    _install_window_capability_state(automation_module, ui_module)
    _install_payload_and_delivery_tracking(automation_module)
    _install_delivery_file_validation(automation_module, file_import_module, storage_module)
    _install_task_transfer_editor(automation_module)
    _install_task_manager_delivery_tools(automation_module, file_import_module, storage_module)


def _install_delivery_history_store(automation_module: Any) -> None:
    state_cls = automation_module.AutomationStateStore
    if getattr(state_cls, "_stage3_delivery_history_installed", False):
        return
    original_initialize = state_cls._initialize

    def initialize_with_delivery_history(self: Any) -> None:
        original_initialize(self)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    recipient_qq TEXT NOT NULL DEFAULT '',
                    file_name TEXT NOT NULL DEFAULT '',
                    transfer_mode TEXT NOT NULL DEFAULT '',
                    delivery_kind TEXT NOT NULL DEFAULT 'scheduled',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    ok INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_history_task_time "
                "ON delivery_history(task_id, created_at DESC)"
            )

    def record_delivery(
        self: Any,
        task_id: str,
        recipient_qq: str,
        file_name: str,
        transfer_mode: str,
        delivery_kind: str,
        file_size: int,
        ok: bool,
        error: str = "",
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_history(
                    task_id, created_at, recipient_qq, file_name,
                    transfer_mode, delivery_kind, file_size, ok, error
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(task_id),
                    datetime.now().replace(microsecond=0).isoformat(),
                    str(recipient_qq),
                    str(file_name)[:260],
                    str(transfer_mode)[:30],
                    str(delivery_kind)[:30],
                    max(0, int(file_size or 0)),
                    1 if ok else 0,
                    str(error or "")[:2000],
                ),
            )

    def delivery_history(self: Any, task_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if task_id:
                rows = connection.execute(
                    "SELECT * FROM delivery_history WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM delivery_history ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    state_cls._initialize = initialize_with_delivery_history
    state_cls.record_delivery = record_delivery
    state_cls.delivery_history = delivery_history
    state_cls._stage3_delivery_history_installed = True


def _install_window_capability_state(automation_module: Any, ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_stage3_capabilities_installed", False):
        return
    original_init = main_window_cls.__init__
    original_start = main_window_cls.start

    def init_with_delivery_capabilities(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.automation_friends: dict[str, str] = {}
        self.automation_napcat_version = ""
        self.automation_test_uploads: dict[str, dict[str, Any]] = {}
        self.automation_manager_dialog = None

    def start_with_delivery_capabilities(self: Any) -> None:
        original_start(self)
        client = getattr(self, "client_thread", None)
        if client is None:
            return
        _sync_transfer_modes(self)
        if not getattr(self, "_automation_stage3_connected", False):
            client.connected.connect(lambda: _request_capabilities(self))
            self._automation_stage3_connected = True

    main_window_cls.__init__ = init_with_delivery_capabilities
    main_window_cls.start = start_with_delivery_capabilities
    main_window_cls._automation_stage3_capabilities_installed = True


def _install_payload_and_delivery_tracking(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_stage3_payload_installed", False):
        return
    original_payload_handler = automation_module._handle_automation_payload

    def handle_stage3_payload(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("automation_friend_list"):
            if payload.get("ok"):
                friends = payload.get("friends") or []
                window.automation_friends = {
                    str(item.get("user_id") or ""): str(item.get("name") or "")
                    for item in friends
                    if isinstance(item, dict) and str(item.get("user_id") or "")
                }
                window.append_log(f"定时任务已刷新 {len(window.automation_friends)} 个好友")
            else:
                window.append_log(f"定时任务刷新好友失败：{payload.get('error') or '未知错误'}")
            _refresh_manager_status(window)
            return

        if isinstance(payload, dict) and payload.get("automation_version_info"):
            if payload.get("ok"):
                window.automation_napcat_version = str(payload.get("version") or "")
                if window.automation_napcat_version:
                    window.append_log(f"定时任务检测到 NapCat 版本：{window.automation_napcat_version}")
            else:
                window.append_log(f"定时任务获取 NapCat 版本失败：{payload.get('error') or '未知错误'}")
            _refresh_manager_status(window)
            return

        if isinstance(payload, dict) and payload.get("automation_upload"):
            upload_id = str(payload.get("upload_id") or "")
            test = getattr(window, "automation_test_uploads", {}).pop(upload_id, None)
            if isinstance(test, dict):
                ok = bool(payload.get("ok"))
                error = str(payload.get("error") or "")
                _record_delivery(
                    window,
                    test.get("task_id", ""),
                    test.get("recipient", ""),
                    test.get("path", ""),
                    test.get("mode", TRANSFER_AUTO),
                    "test",
                    ok,
                    error,
                )
                task_name = str(test.get("task_name") or "定时任务")
                if ok:
                    window.append_log(f"定时任务“{task_name}”测试发送成功")
                    QMessageBox.information(
                        window,
                        "测试发送成功",
                        "NapCat 已确认文件上传成功。\n"
                        "本次测试不会推进检查点、删除文件或创建新一天文件。",
                    )
                else:
                    window.append_log(f"定时任务“{task_name}”测试发送失败：{error or '未知错误'}")
                    suffix = "\n机器人向自己发送失败时，请改选另一个好友 QQ。" if test.get("recipient_is_self") else ""
                    QMessageBox.warning(window, "测试发送失败", (error or "未知错误") + suffix)
                _refresh_manager_status(window)
                return

            upload = getattr(window, "automation_uploads", {}).get(upload_id)
            task = task_by_id(
                getattr(window, "automation_tasks", []),
                upload.run.task_id if upload is not None else "",
            )
            snapshot = None
            if upload is not None and task is not None:
                snapshot = {
                    "task_id": task.task_id,
                    "recipient": automation_module._resolve_recipient(window, task),
                    "path": upload.path,
                    "mode": _effective_transfer_mode(window, task),
                }
            original_payload_handler(window, ui_module, ai_module, payload)
            if snapshot is not None:
                _record_delivery(
                    window,
                    snapshot["task_id"],
                    snapshot["recipient"],
                    snapshot["path"],
                    snapshot["mode"],
                    "scheduled",
                    bool(payload.get("ok")),
                    str(payload.get("error") or ""),
                )
                _refresh_manager_status(window)
            return

        original_payload_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_stage3_payload
    automation_module._automation_stage3_payload_installed = True


def _install_delivery_file_validation(
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
) -> None:
    if getattr(automation_module, "_automation_stage3_validation_installed", False):
        return
    original_ready = automation_module._handle_execution_ready

    def ready_with_file_validation(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        if isinstance(payload, dict):
            context = payload.get("context")
            task = task_by_id(
                getattr(window, "automation_tasks", []),
                str(payload.get("task_id") or ""),
            )
            if task is not None:
                _sync_transfer_modes(window)
            if task is not None and context is not None and bool(getattr(context, "delivery", False)) and task.file_enabled:
                path = Path(str(payload.get("path") or ""))
                try:
                    _validate_delivery_file(task, path, file_import_module, storage_module)
                except Exception as exc:  # noqa: BLE001
                    automation_module._fail_run(window, task, context, f"归档文件校验失败：{exc}")
                    return
        original_ready(window, ui_module, ai_module, payload)

    automation_module._handle_execution_ready = ready_with_file_validation
    automation_module._automation_stage3_validation_installed = True


def _install_task_transfer_editor(automation_module: Any) -> None:
    dialog_cls = automation_module.AutomationTaskEditDialog
    if getattr(dialog_cls, "_automation_stage3_transfer_editor_installed", False):
        return
    original_init = dialog_cls.__init__
    original_validate = dialog_cls._validate_and_accept

    def init_with_transfer_mode(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.file_transfer_mode_input = QComboBox()
        self.file_transfer_mode_input.addItem("自动（本机用路径，远程用 Stream API）", TRANSFER_AUTO)
        self.file_transfer_mode_input.addItem("NapCat 本地路径", TRANSFER_LOCAL)
        self.file_transfer_mode_input.addItem("Stream API（跨设备）", TRANSFER_STREAM)
        index = self.file_transfer_mode_input.findData(getattr(self.task, "file_transfer_mode", TRANSFER_AUTO))
        self.file_transfer_mode_input.setCurrentIndex(max(0, index))

        note = QLabel(
            "自动模式会根据 WebSocket 主机判断：127.0.0.1/localhost 使用本地路径，其他地址使用 Stream API。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#667085;")
        group = QGroupBox("文件传输")
        form = QFormLayout(group)
        form.addRow("传输模式", self.file_transfer_mode_input)
        form.addRow("", note)
        root = self.layout()
        root.insertWidget(max(0, root.count() - 1), group)

        known = getattr(self.window, "automation_friends", {})
        for qq, name in sorted(known.items(), key=lambda item: item[1] or item[0]):
            if self.recipient_input.findData(qq) < 0:
                self.recipient_input.addItem(f"{name or ('QQ ' + qq)} · {qq}", qq)

    def validate_with_transfer_mode(self: Any) -> None:
        self.task.file_transfer_mode = str(self.file_transfer_mode_input.currentData() or TRANSFER_AUTO)
        original_validate(self)

    dialog_cls.__init__ = init_with_transfer_mode
    dialog_cls._validate_and_accept = validate_with_transfer_mode
    dialog_cls._automation_stage3_transfer_editor_installed = True


def _install_task_manager_delivery_tools(
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
) -> None:
    dialog_cls = automation_module.AutomationTaskManagerDialog
    if getattr(dialog_cls, "_automation_stage3_manager_tools_installed", False):
        return
    original_init = dialog_cls.__init__

    def init_with_delivery_tools(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.window.automation_manager_dialog = self
        self.stage3_status = QLabel()
        self.stage3_status.setWordWrap(True)
        self.stage3_status.setStyleSheet("color:#667085;")

        refresh_button = QPushButton("刷新好友/版本")
        test_button = QPushButton("测试发送文件")
        history_button = QPushButton("发送记录")
        refresh_button.clicked.connect(lambda: _request_capabilities(self.window))
        test_button.clicked.connect(
            lambda: _test_selected_delivery(
                self,
                automation_module,
                file_import_module,
                storage_module,
            )
        )
        history_button.clicked.connect(lambda: _show_delivery_history(self, automation_module))

        row = QHBoxLayout()
        row.addWidget(QLabel("发送工具"))
        row.addWidget(refresh_button)
        row.addWidget(test_button)
        row.addWidget(history_button)
        row.addStretch(1)
        self.layout().addWidget(self.stage3_status)
        self.layout().addLayout(row)
        self._refresh_stage3_status = lambda: _set_manager_status(self)
        _set_manager_status(self)
        _request_capabilities(self.window)

    dialog_cls.__init__ = init_with_delivery_tools
    dialog_cls._automation_stage3_manager_tools_installed = True


def _request_capabilities(window: Any) -> None:
    client = getattr(window, "client_thread", None)
    if client is None:
        window.append_log("定时任务无法刷新好友/版本：当前未连接 NapCatQQ")
        return
    _sync_transfer_modes(window)
    client.request_automation_friend_list()
    client.request_automation_version_info()
    client.request_automation_login_info()


def _test_selected_delivery(
    dialog: Any,
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
) -> None:
    task = dialog._selected_task()  # noqa: SLF001
    if task is None:
        QMessageBox.information(dialog, "请选择任务", "请先选择一个定时任务。")
        return
    if not task.file_enabled:
        QMessageBox.information(dialog, "未启用文件", "该任务没有启用本地文件工作区。")
        return
    client = getattr(dialog.window, "client_thread", None)
    if client is None:
        QMessageBox.warning(dialog, "未连接", "当前未连接 NapCatQQ，无法测试发送。")
        return
    recipient = automation_module._resolve_recipient(dialog.window, task)
    if not recipient:
        QMessageBox.warning(dialog, "接收人不可用", "未配置接收 QQ，或尚未取得机器人自己的 QQ。")
        return

    path = file_import_module.latest_task_artifact(task, automation_module, storage_module)
    if path is None:
        try:
            path = automation_module.ensure_empty_artifact(task, date.today())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(dialog, "无法准备测试文件", str(exc))
            return
    path = Path(path)
    try:
        _validate_delivery_file(task, path, file_import_module, storage_module)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(dialog, "文件校验失败", str(exc))
        return

    mode = _effective_transfer_mode(dialog.window, task)
    if mode == TRANSFER_STREAM and dialog.window.automation_napcat_version:
        if not _version_at_least(dialog.window.automation_napcat_version, (4, 8, 115)):
            QMessageBox.warning(
                dialog,
                "NapCat 版本可能不支持",
                f"当前检测到版本 {dialog.window.automation_napcat_version}，"
                "Stream API 需要 NapCat v4.8.115 或更高版本。",
            )
            return

    upload_id = f"test_upload_{uuid.uuid4().hex[:18]}"
    dialog.window.automation_test_uploads[upload_id] = {
        "task_id": task.task_id,
        "task_name": task.name,
        "recipient": recipient,
        "recipient_is_self": task.recipient_mode == RECIPIENT_SELF,
        "path": str(path),
        "mode": mode,
    }
    dialog.window.append_log(
        f"定时任务“{task.name}”正在测试发送 {path.name} 给 QQ {recipient}，传输模式：{mode}"
    )
    client.upload_automation_file(
        upload_id,
        recipient,
        str(path),
        path.name,
        task.file_transfer_mode,
    )


def _show_delivery_history(dialog: Any, automation_module: Any) -> None:
    task = dialog._selected_task()  # noqa: SLF001
    task_id = task.task_id if task is not None else ""
    rows = dialog.window.automation_state.delivery_history(task_id, 300)
    DeliveryHistoryDialog(rows, task.name if task is not None else "全部任务", dialog).exec()


class DeliveryHistoryDialog(QDialog):
    def __init__(self, rows: list[dict[str, Any]], title_name: str, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"文件发送记录 · {title_name}")
        self.resize(1050, 600)
        self.setMinimumSize(760, 420)
        columns = ["时间", "类型", "结果", "接收 QQ", "传输", "文件", "大小", "错误"]
        table = QTableWidget(len(rows), len(columns), self)
        table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        for row_index, row in enumerate(rows):
            values = [
                str(row.get("created_at") or "").replace("T", " "),
                "测试" if row.get("delivery_kind") == "test" else "定时归档",
                "成功" if int(row.get("ok") or 0) else "失败",
                str(row.get("recipient_qq") or ""),
                str(row.get("transfer_mode") or ""),
                str(row.get("file_name") or ""),
                _format_size(int(row.get("file_size") or 0)),
                str(row.get("error") or ""),
            ]
            for column_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row_index, column_index, item)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 150)
        header.resizeSection(1, 80)
        header.resizeSection(2, 70)
        header.resizeSection(3, 120)
        header.resizeSection(4, 100)
        header.resizeSection(5, 220)
        header.resizeSection(6, 90)
        header.setStretchLastSection(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"最近 {len(rows)} 条发送结果"))
        layout.addWidget(table, 1)
        layout.addWidget(buttons)


def _validate_delivery_file(task: Any, path: Path, file_import_module: Any, storage_module: Any) -> None:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("归档文件不存在")
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("归档文件为空")
    expected = f".{str(task.file_format).lower()}"
    if path.suffix.lower() != expected:
        raise RuntimeError(f"文件扩展名 {path.suffix} 与任务格式 {expected} 不一致")
    _records, recognized = file_import_module.read_artifact_records(path, task, storage_module)
    if not recognized:
        raise RuntimeError("文件内容无法按当前任务结构读取")


def _sync_transfer_modes(window: Any) -> None:
    client = getattr(window, "client_thread", None)
    if client is None:
        return
    client.automation_transfer_modes = {
        task.task_id: getattr(task, "file_transfer_mode", TRANSFER_AUTO)
        for task in getattr(window, "automation_tasks", [])
    }


def _effective_transfer_mode(window: Any, task: Any) -> str:
    configured = str(getattr(task, "file_transfer_mode", TRANSFER_AUTO) or TRANSFER_AUTO)
    if configured in {TRANSFER_LOCAL, TRANSFER_STREAM}:
        return configured
    client = getattr(window, "client_thread", None)
    worker = getattr(client, "worker", None)
    websocket_url = str(getattr(worker, "websocket_url", ""))
    host = (urlparse(websocket_url).hostname or "").lower()
    return TRANSFER_LOCAL if host in {"127.0.0.1", "::1", "localhost", "localhost.localdomain"} else TRANSFER_STREAM


def _record_delivery(
    window: Any,
    task_id: str,
    recipient: str,
    path: str,
    mode: str,
    kind: str,
    ok: bool,
    error: str,
) -> None:
    try:
        file_path = Path(path)
        size = file_path.stat().st_size if file_path.is_file() else 0
        window.automation_state.record_delivery(
            task_id,
            recipient,
            file_path.name,
            mode,
            kind,
            size,
            ok,
            error,
        )
    except (OSError, sqlite3.Error) as exc:
        window.append_log(f"保存文件发送记录失败：{exc}")


def _set_manager_status(dialog: Any) -> None:
    version = dialog.window.automation_napcat_version or "未知"
    friends = len(getattr(dialog.window, "automation_friends", {}))
    self_qq = dialog.window.automation_self_qq or "尚未取得"
    dialog.stage3_status.setText(
        f"NapCat 版本：{version} · 已读取好友：{friends} · 机器人 QQ：{self_qq}"
    )


def _refresh_manager_status(window: Any) -> None:
    dialog = getattr(window, "automation_manager_dialog", None)
    callback = getattr(dialog, "_refresh_stage3_status", None)
    if callable(callback):
        callback()


def _version_at_least(text: str, minimum: tuple[int, int, int]) -> bool:
    numbers = [int(value) for value in re.findall(r"\d+", str(text))[:3]]
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3]) >= minimum


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
