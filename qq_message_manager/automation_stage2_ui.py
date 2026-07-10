from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


def install_automation_stage2_ui(
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
) -> None:
    """给定时任务管理器增加工作区导入、预览和打开目录入口。"""
    dialog_cls = automation_module.AutomationTaskManagerDialog
    if getattr(dialog_cls, "_stage2_file_ui_installed", False):
        return

    original_init = dialog_cls.__init__

    def init_with_file_tools(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)

        preview_button = QPushButton("查看数据")
        preview_button.setToolTip("查看当前任务文件中已被程序识别的记录")
        import_button = QPushButton("导入文件")
        import_button.setToolTip("由用户选择现有文件并复制到当前任务专属工作区")
        workspace_button = QPushButton("打开工作区")
        workspace_button.setToolTip("打开当前任务的专属文件目录")

        preview_button.clicked.connect(
            lambda: _preview_selected(self, automation_module, file_import_module, storage_module)
        )
        import_button.clicked.connect(
            lambda: _import_selected(self, automation_module, file_import_module, storage_module)
        )
        workspace_button.clicked.connect(lambda: _open_workspace(self, storage_module))

        tools = QHBoxLayout()
        tools.addWidget(QLabel("文件工具"))
        tools.addWidget(preview_button)
        tools.addWidget(import_button)
        tools.addWidget(workspace_button)
        tools.addStretch(1)
        self.layout().addLayout(tools)

    dialog_cls.__init__ = init_with_file_tools
    dialog_cls._stage2_file_ui_installed = True


def _selected_file_task(dialog: Any) -> Any | None:
    task = dialog._selected_task()  # noqa: SLF001
    if task is None:
        QMessageBox.information(dialog, "请选择任务", "请先在列表中选择一个定时任务。")
        return None
    if not task.file_enabled:
        QMessageBox.information(
            dialog,
            "未启用文件工作区",
            "该任务没有启用“本地文件工作区 Skill”。",
        )
        return None
    return task


def _preview_selected(
    dialog: Any,
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
) -> None:
    task = _selected_file_task(dialog)
    if task is None:
        return

    path = file_import_module.latest_task_artifact(task, automation_module, storage_module)
    if path is None:
        path = automation_module.artifact_path(task, date.today())
        try:
            automation_module.ensure_empty_artifact(task, date.today())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(dialog, "无法创建文件", str(exc))
            return

    try:
        records = automation_module.load_records(path)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(dialog, "无法读取文件", str(exc))
        return
    DataPreviewDialog(task, Path(path), records, dialog).exec()


def _import_selected(
    dialog: Any,
    automation_module: Any,
    file_import_module: Any,
    storage_module: Any,
) -> None:
    task = _selected_file_task(dialog)
    if task is None:
        return

    filters = {
        "xlsx": "Excel 工作簿 (*.xlsx)",
        "csv": "CSV 文件 (*.csv)",
        "json": "JSON 文件 (*.json)",
        "md": "Markdown 文件 (*.md)",
    }
    selected, _filter = QFileDialog.getOpenFileName(
        dialog,
        "导入现有任务文件",
        "",
        filters.get(task.file_format, "支持的文件 (*.xlsx *.csv *.json *.md)"),
    )
    if not selected:
        return

    target = automation_module.artifact_path(task, date.today())
    if target.exists():
        answer = QMessageBox.question(
            dialog,
            "覆盖当前文件",
            f"当前任务已存在文件“{target.name}”。\n导入会覆盖该文件并重新建立记录索引，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

    try:
        imported_path, count = file_import_module.import_user_file(
            task,
            Path(selected),
            target,
            automation_module,
            storage_module,
        )
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(dialog, "导入失败", str(exc))
        return

    dialog.window.append_log(
        f"定时任务“{task.name}”已由用户导入 {imported_path.name}，识别 {count} 条记录"
    )
    QMessageBox.information(
        dialog,
        "导入完成",
        f"文件已复制到任务专属工作区，并识别 {count} 条记录。\n"
        "后续定时任务会在这些记录基础上新增或更新。",
    )


def _open_workspace(dialog: Any, storage_module: Any) -> None:
    task = _selected_file_task(dialog)
    if task is None:
        return
    try:
        workspace = storage_module.task_workspace(task.task_id)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(dialog, "无法打开工作区", str(exc))
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(workspace))):
        QMessageBox.warning(dialog, "无法打开工作区", f"系统无法打开目录：{workspace}")


class DataPreviewDialog(QDialog):
    def __init__(
        self,
        task: Any,
        path: Path,
        records: list[dict[str, Any]],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"任务数据预览 · {task.name}")
        self.resize(1100, 680)
        self.setMinimumSize(760, 480)

        columns = [column.name for column in task.columns]
        shown = records[-1000:]
        title = QLabel(f"{path.name} · 共 {len(records)} 条记录")
        title.setStyleSheet("font-size:17px;font-weight:600;")
        tip = QLabel(
            "这里显示程序实际提供给定时任务的数据。最多预览最后 1000 条；"
            "记录 ID 用于后续回答更新原行。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#667085;")

        table = QTableWidget(len(shown), len(columns) + 1, self)
        table.setHorizontalHeaderLabels(["记录 ID", *columns])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)

        for row_index, record in enumerate(shown):
            record_id = str(record.get("record_id") or "")
            id_item = QTableWidgetItem(record_id)
            id_item.setToolTip(
                "来源消息 ID：" + "、".join(str(value) for value in record.get("source_message_ids", []))
            )
            table.setItem(row_index, 0, id_item)
            values = record.get("values") or {}
            for column_index, name in enumerate(columns, start=1):
                value = values.get(name, "")
                item = QTableWidgetItem("" if value is None else str(value))
                item.setToolTip(item.text())
                table.setItem(row_index, column_index, item)

        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 190)
        for index in range(1, min(table.columnCount(), 8)):
            header.resizeSection(index, 150)
        if table.columnCount() > 1:
            header.setStretchLastSection(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(tip)
        layout.addWidget(table, 1)
        layout.addWidget(buttons)
