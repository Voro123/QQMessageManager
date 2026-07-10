from __future__ import annotations

import asyncio
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from .automation_models import SCHEDULE_DAILY

TARGET_GROUPS_PREFIX = "automation_target_groups"
TARGET_FRIENDS_PREFIX = "automation_target_friends"
MANUAL_TARGET_VALUE = "__manual_target__"


def install_automation_editor_usability(
    automation_module: Any,
    napcat_module: Any,
    ui_module: Any,
) -> None:
    """提供可刷新的目标会话下拉框，并按调度方式隐藏无关字段。"""
    _install_target_discovery(napcat_module)
    _install_window_target_state(automation_module, ui_module)
    _install_target_payload_handler(automation_module)
    _install_task_editor(automation_module)


def _install_target_discovery(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_automation_target_discovery_installed", False):
        return

    original_handle = worker_cls._handle_action_response

    def request_automation_target_lists(self: Any) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            self.history_messages_received.emit(
                {
                    "automation_target_list": True,
                    "kind": "all",
                    "ok": False,
                    "targets": [],
                    "error": "当前未连接 NapCatQQ",
                }
            )
            return

        def schedule() -> None:
            asyncio.create_task(
                self._send_action(
                    "get_group_list",
                    {"no_cache": False},
                    self._next_echo(TARGET_GROUPS_PREFIX),
                )
            )
            asyncio.create_task(
                self._send_action(
                    "get_friend_list",
                    {},
                    self._next_echo(TARGET_FRIENDS_PREFIX),
                )
            )

        loop.call_soon_threadsafe(schedule)

    def handle_action_response(self: Any, event: dict[str, Any]) -> bool:
        echo_text = str(event.get("echo") or "")
        ok = event.get("status") == "ok" or event.get("retcode") == 0
        if echo_text.startswith(TARGET_GROUPS_PREFIX):
            targets = _normalize_groups(event.get("data")) if ok else []
            self.history_messages_received.emit(
                {
                    "automation_target_list": True,
                    "kind": "group",
                    "ok": ok,
                    "targets": targets,
                    "error": "" if ok else _action_error(napcat_module, event),
                }
            )
            return True
        if echo_text.startswith(TARGET_FRIENDS_PREFIX):
            targets = _normalize_friends(event.get("data")) if ok else []
            self.history_messages_received.emit(
                {
                    "automation_target_list": True,
                    "kind": "private",
                    "ok": ok,
                    "targets": targets,
                    "error": "" if ok else _action_error(napcat_module, event),
                }
            )
            return True
        return original_handle(self, event)

    def thread_request_automation_target_lists(self: Any) -> None:
        self.worker.request_automation_target_lists()

    worker_cls.request_automation_target_lists = request_automation_target_lists
    worker_cls._handle_action_response = handle_action_response
    thread_cls.request_automation_target_lists = thread_request_automation_target_lists
    worker_cls._automation_target_discovery_installed = True


def _install_window_target_state(automation_module: Any, ui_module: Any) -> None:
    del automation_module
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_target_state_installed", False):
        return

    original_init = main_window_cls.__init__
    original_start = main_window_cls.start

    def init_with_target_state(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.automation_available_targets: dict[str, dict[str, str]] = {}
        self.automation_active_edit_dialog = None

    def start_with_target_discovery(self: Any) -> None:
        original_start(self)
        client = getattr(self, "client_thread", None)
        if client is None:
            return
        if not getattr(self, "_automation_target_connected", False):
            client.connected.connect(lambda: client.request_automation_target_lists())
            self._automation_target_connected = True

    main_window_cls.__init__ = init_with_target_state
    main_window_cls.start = start_with_target_discovery
    main_window_cls._automation_target_state_installed = True


def _install_target_payload_handler(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_target_payload_installed", False):
        return
    original_handler = automation_module._handle_automation_payload

    def handle_target_payload(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("automation_target_list"):
            if payload.get("ok"):
                kind = str(payload.get("kind") or "")
                if kind in {"group", "private"}:
                    for session_id in list(window.automation_available_targets):
                        if session_id.startswith(kind + ":"):
                            window.automation_available_targets.pop(session_id, None)
                for target in payload.get("targets") or []:
                    if not isinstance(target, dict):
                        continue
                    session_id = str(target.get("session_id") or "")
                    if not session_id.startswith(("group:", "private:")):
                        continue
                    window.automation_available_targets[session_id] = {
                        "session_id": session_id,
                        "kind": str(target.get("kind") or session_id.split(":", 1)[0]),
                        "name": str(target.get("name") or session_id),
                    }
                window.append_log(
                    f"定时任务已刷新 {kind == 'group' and '群聊' or '私聊'}目标："
                    f"{len(payload.get('targets') or [])} 个"
                )
            else:
                window.append_log(
                    f"定时任务刷新目标会话失败：{payload.get('error') or '未知错误'}"
                )
            dialog = getattr(window, "automation_active_edit_dialog", None)
            refresh = getattr(dialog, "_reload_target_choices", None)
            if callable(refresh):
                refresh()
            return
        original_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_target_payload
    automation_module._automation_target_payload_installed = True


def _install_task_editor(automation_module: Any) -> None:
    dialog_cls = automation_module.AutomationTaskEditDialog
    if getattr(dialog_cls, "_automation_editor_usability_installed", False):
        return

    original_init = dialog_cls.__init__
    original_sync = dialog_cls._sync_controls

    def init_with_target_selector(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.target_input.setEditable(False)
        self.target_input.setMinimumContentsLength(28)
        self.target_input.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)

        self.target_manual_input = QLineEdit()
        self.target_manual_input.setPlaceholderText("group:群号 或 private:QQ号")
        self.target_refresh_button = QPushButton("刷新会话")
        self.target_refresh_button.setToolTip("从 NapCat 重新读取完整群列表和好友列表")
        self.target_refresh_button.clicked.connect(lambda: _request_targets(self.window))

        target_form = _form_for_widget(self.layout(), self.target_input)
        if target_form is not None:
            row, _role = target_form.getWidgetPosition(self.target_input)
            target_form.removeWidget(self.target_input)
            selector = QWidget(self)
            selector_layout = QHBoxLayout(selector)
            selector_layout.setContentsMargins(0, 0, 0, 0)
            selector_layout.setSpacing(6)
            selector_layout.addWidget(self.target_input, 1)
            selector_layout.addWidget(self.target_refresh_button)
            target_form.setWidget(row, QFormLayout.ItemRole.FieldRole, selector)
            target_form.insertRow(row + 1, "手动会话 ID", self.target_manual_input)
        self._automation_target_form = target_form

        self._automation_schedule_form = _form_for_widget(self.layout(), self.interval_input)
        self.target_input.currentIndexChanged.connect(self._sync_controls)
        self._reload_target_choices = lambda: _reload_target_choices(self)
        self._reload_target_choices()
        self._sync_controls()

        self.window.automation_active_edit_dialog = self
        self.destroyed.connect(lambda *_args: _clear_active_dialog(self))
        _request_targets(self.window)

    def sync_with_visibility(self: Any) -> None:
        original_sync(self)
        daily = self.schedule_type_input.currentData() == SCHEDULE_DAILY
        schedule_form = getattr(self, "_automation_schedule_form", None)
        _set_form_row_visible(schedule_form, self.interval_input, not daily)
        _set_form_row_visible(schedule_form, self.daily_time_input, daily)

        manual = self.target_input.currentData() == MANUAL_TARGET_VALUE
        target_form = getattr(self, "_automation_target_form", None)
        _set_form_row_visible(target_form, self.target_manual_input, manual)
        self.target_manual_input.setEnabled(manual)

    def target_value(self: Any) -> tuple[str, str]:
        data = str(self.target_input.currentData() or "")
        if data == MANUAL_TARGET_VALUE:
            raw = self.target_manual_input.text().strip()
            return raw, raw
        if data.startswith(("group:", "private:")):
            target = getattr(self.window, "automation_available_targets", {}).get(data) or {}
            name = str(target.get("name") or "")
            if not name:
                session = getattr(self.window, "sessions", {}).get(data)
                name = str(getattr(session, "name", "") or data)
            return data, name
        return "", ""

    dialog_cls.__init__ = init_with_target_selector
    dialog_cls._sync_controls = sync_with_visibility
    dialog_cls._target_value = target_value
    dialog_cls._automation_editor_usability_installed = True


def _reload_target_choices(dialog: Any) -> None:
    combo = dialog.target_input
    selected = str(combo.currentData() or "")
    manual_text = dialog.target_manual_input.text().strip()
    if selected not in {MANUAL_TARGET_VALUE, ""}:
        desired = selected
    else:
        desired = str(getattr(dialog.task, "target_session_id", "") or "")

    targets: dict[str, dict[str, str]] = {}
    for session in getattr(dialog.window, "sessions", {}).values():
        session_id = str(getattr(session, "session_id", "") or "")
        kind = str(getattr(session, "kind", "") or "")
        if kind not in {"group", "private"} or not session_id:
            continue
        targets[session_id] = {
            "session_id": session_id,
            "kind": kind,
            "name": str(getattr(session, "name", "") or session_id),
        }
    targets.update(getattr(dialog.window, "automation_available_targets", {}))

    combo.blockSignals(True)
    combo.clear()
    combo.addItem("请选择目标群聊或私聊", "")
    current_session_id = str(getattr(dialog.window, "current_session_id", "") or "")
    ordered = sorted(
        targets.values(),
        key=lambda item: (
            0 if item.get("session_id") == current_session_id else 1,
            0 if item.get("kind") == "group" else 1,
            str(item.get("name") or "").casefold(),
            str(item.get("session_id") or ""),
        ),
    )
    for target in ordered:
        session_id = str(target.get("session_id") or "")
        kind = str(target.get("kind") or "")
        name = str(target.get("name") or session_id)
        target_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        kind_label = "群聊" if kind == "group" else "私聊"
        current_label = "当前 · " if session_id == current_session_id else ""
        combo.addItem(f"{current_label}{kind_label} · {name} · {target_id}", session_id)
    combo.addItem("手动填写会话 ID……", MANUAL_TARGET_VALUE)

    index = combo.findData(desired)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif desired.startswith(("group:", "private:")):
        combo.setCurrentIndex(combo.findData(MANUAL_TARGET_VALUE))
        dialog.target_manual_input.setText(desired)
    elif selected == MANUAL_TARGET_VALUE:
        combo.setCurrentIndex(combo.findData(MANUAL_TARGET_VALUE))
        dialog.target_manual_input.setText(manual_text)
    else:
        combo.setCurrentIndex(0)
    combo.blockSignals(False)
    dialog._sync_controls()


def _request_targets(window: Any) -> None:
    client = getattr(window, "client_thread", None)
    if client is None or not hasattr(client, "request_automation_target_lists"):
        window.append_log("定时任务无法刷新目标会话：当前未连接 NapCatQQ")
        return
    window.append_log("正在从 NapCat 刷新定时任务目标群聊和好友……")
    client.request_automation_target_lists()


def _clear_active_dialog(dialog: Any) -> None:
    window = getattr(dialog, "window", None)
    if window is not None and getattr(window, "automation_active_edit_dialog", None) is dialog:
        window.automation_active_edit_dialog = None


def _form_for_widget(layout: Any, widget: QWidget) -> QFormLayout | None:
    if layout is None:
        return None
    if isinstance(layout, QFormLayout) and layout.indexOf(widget) >= 0:
        return layout
    for index in range(layout.count()):
        item = layout.itemAt(index)
        child_layout = item.layout() if item is not None else None
        found = _form_for_widget(child_layout, widget)
        if found is not None:
            return found
        child_widget = item.widget() if item is not None else None
        if child_widget is not None:
            found = _form_for_widget(child_widget.layout(), widget)
            if found is not None:
                return found
    return None


def _set_form_row_visible(form: QFormLayout | None, field: QWidget, visible: bool) -> None:
    if form is None:
        field.setVisible(visible)
        return
    try:
        form.setRowVisible(field, visible)
        return
    except (AttributeError, TypeError):
        pass
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)
    field.setVisible(visible)


def _normalize_groups(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _extract_list(value):
        if not isinstance(item, dict):
            continue
        group_id = str(
            item.get("group_id")
            or item.get("groupId")
            or item.get("group_uin")
            or item.get("groupUin")
            or item.get("uin")
            or ""
        ).strip()
        if not group_id or group_id in seen:
            continue
        seen.add(group_id)
        name = str(
            item.get("group_remark")
            or item.get("group_name")
            or item.get("groupName")
            or item.get("name")
            or f"群聊 {group_id}"
        ).strip()
        result.append(
            {
                "session_id": f"group:{group_id}",
                "kind": "group",
                "name": name,
            }
        )
    return result


def _normalize_friends(value: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _extract_list(value):
        if not isinstance(item, dict):
            continue
        user_id = str(
            item.get("user_id")
            or item.get("userId")
            or item.get("uin")
            or item.get("qq")
            or ""
        ).strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        name = str(
            item.get("remark")
            or item.get("nickname")
            or item.get("nick")
            or item.get("name")
            or f"QQ {user_id}"
        ).strip()
        result.append(
            {
                "session_id": f"private:{user_id}",
                "kind": "private",
                "name": name,
            }
        )
    return result


def _extract_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in ("data", "groups", "friends", "list", "items", "rows"):
        nested = value.get(key)
        if isinstance(nested, list):
            return nested
        if isinstance(nested, dict):
            result = _extract_list(nested)
            if result:
                return result
    return []


def _action_error(napcat_module: Any, event: dict[str, Any]) -> str:
    try:
        return str(napcat_module._action_error(event))
    except Exception:  # noqa: BLE001
        return str(event.get("wording") or event.get("message") or "未知错误")
