from __future__ import annotations

import asyncio
import math
from typing import Any


def install_automation_history_direction_guard(
    cursor_module: Any,
    napcat_module: Any,
) -> None:
    """Retry cursor history with the opposite direction when needed.

    NapCat versions expose ``reverse_order`` but the effective query direction
    around a starting message has varied.  Do not guess: accept the first result
    when it contains a sequence newer than the committed cursor, otherwise probe
    the opposite direction once before emitting the history payload.
    """

    worker_cls = napcat_module.NapCatWorker
    if getattr(worker_cls, "_automation_history_direction_guard_installed", False):
        return
    original_handle = worker_cls._handle_action_response

    def handle_with_direction_probe(self: Any, event: dict[str, Any]) -> bool:
        echo_text = str(event.get("echo") or "")
        if not echo_text.startswith(cursor_module.CURSOR_HISTORY_PREFIX):
            return original_handle(self, event)

        request_id, session_id = _parse_echo(
            echo_text,
            cursor_module.CURSOR_HISTORY_PREFIX,
        )
        requests = getattr(self, "_qqmm_automation_cursor_history_requests", {})
        state = requests.get(request_id) if isinstance(requests, dict) else None
        ok = event.get("status") == "ok" or event.get("retcode") == 0
        if not isinstance(state, dict) or not ok:
            return original_handle(self, event)

        cursor = _sequence_number(state.get("cursor_real_seq"))
        kind, _target_id = napcat_module._split_session_id(session_id)
        messages = napcat_module._extract_history_messages(event.get("data"), session_id, kind)
        newer = [
            sequence
            for message in messages
            for sequence in [_message_sequence(message)]
            if sequence is not None and cursor is not None and sequence > cursor
        ]
        if newer or state.get("opposite_direction_tried"):
            return original_handle(self, event)

        state["opposite_direction_tried"] = True
        self.log.emit(
            f"定时任务游标 {cursor if cursor is not None else '?'} 的首次历史方向"
            "未返回更大消息序号，自动尝试相反方向"
        )
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return original_handle(self, event)
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                _request_opposite_direction(
                    self,
                    request_id,
                    session_id,
                    state,
                    napcat_module,
                    cursor_module.CURSOR_HISTORY_PREFIX,
                )
            )
        )
        return True

    worker_cls._handle_action_response = handle_with_direction_probe
    worker_cls._automation_history_direction_guard_installed = True


async def _request_opposite_direction(
    worker: Any,
    request_id: str,
    session_id: str,
    state: dict[str, Any],
    napcat_module: Any,
    prefix: str,
) -> None:
    kind, target_id = napcat_module._split_session_id(session_id)
    if kind not in {"group", "private"} or not target_id:
        return
    common = {
        "message_seq": str(state.get("anchor_message_id") or ""),
        "count": max(20, min(int(state.get("count") or 20), 5000)),
        # The cursor layer's first request uses True. Probe False exactly once.
        "reverse_order": False,
        "disable_get_url": True,
        "parse_mult_msg": False,
        "quick_reply": True,
    }
    if kind == "group":
        action = "get_group_msg_history"
        params = {"group_id": napcat_module._onebot_id(target_id), **common}
    else:
        action = "get_friend_msg_history"
        params = {"user_id": napcat_module._onebot_id(target_id), **common}
    await worker._send_action(
        action,
        params,
        worker._next_echo(f"{prefix}{request_id}|{session_id}"),
    )


def _parse_echo(echo_text: str, prefix: str) -> tuple[str, str]:
    stable = echo_text.rsplit(":", 1)[0]
    body = stable[len(prefix) :]
    request_id, separator, session_id = body.partition("|")
    return (request_id, session_id) if separator else ("", "")


def _message_sequence(message: Any) -> int | None:
    raw_event = getattr(message, "raw_event", None)
    if not isinstance(raw_event, dict):
        return None
    for key in (
        "real_seq",
        "realSeq",
        "msgSeq",
        "msg_seq",
        "message_seq",
        "messageSeq",
        "seq",
    ):
        value = _sequence_number(raw_event.get(key))
        if value is not None:
            return value
    return None


def _sequence_number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text in {"0", "0.0"}:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            numeric = float(text)
        except ValueError:
            return None
        if not math.isfinite(numeric):
            return None
        return int(numeric)
