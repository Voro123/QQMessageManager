from __future__ import annotations

from typing import Any

_PENDING_ANCHORS: dict[str, str] = {}


def install_automation_history_checkpoint_guard(automation_module: Any) -> None:
    """Never erase the NapCat message-id anchor on an empty successful run."""

    _capture_history_anchor(automation_module)
    _preserve_checkpoint_message_id(automation_module)


def _capture_history_anchor(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_history_anchor_capture_installed", False):
        return
    original_handler = automation_module._handle_automation_payload

    def handle_with_anchor_capture(
        window: Any,
        ui_module: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if (
            isinstance(payload, dict)
            and payload.get("automation_history")
            and not payload.get("error")
        ):
            request_id = str(payload.get("request_id") or "")
            context = getattr(window, "automation_pending", {}).get(request_id)
            task_id = str(getattr(context, "task_id", "") or "") if context is not None else ""
            anchor = str(
                payload.get("history_cursor_anchor_message_id")
                or payload.get("history_anchor_message_seq")
                or ""
            ).strip()
            if task_id and anchor:
                _PENDING_ANCHORS[task_id] = anchor
        original_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_with_anchor_capture
    automation_module._automation_history_anchor_capture_installed = True


def _preserve_checkpoint_message_id(automation_module: Any) -> None:
    state_cls = automation_module.AutomationStateStore
    if getattr(state_cls, "_automation_checkpoint_message_guard_installed", False):
        return
    original_mark_success = state_cls.mark_success

    def mark_success_preserving_anchor(
        self: Any,
        task_id: str,
        checkpoint_time: Any,
        checkpoint_message_id: str,
        message_keys: list[str],
        status: str = "success",
    ) -> None:
        resolved = str(checkpoint_message_id or "").strip()
        if not resolved:
            resolved = str(_PENDING_ANCHORS.get(str(task_id)) or "").strip()
        if not resolved:
            resolved = str(self.state(str(task_id)).get("checkpoint_message_id") or "").strip()
        original_mark_success(
            self,
            task_id,
            checkpoint_time,
            resolved,
            message_keys,
            status,
        )
        _PENDING_ANCHORS.pop(str(task_id), None)

    state_cls.mark_success = mark_success_preserving_anchor
    state_cls._automation_checkpoint_message_guard_installed = True
