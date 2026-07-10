from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from typing import Any

ANCHOR_PREFIX = "automation_history_anchor|"
HISTORY_PREFIX = "automation_history|"
RECENT_CONTACT_MIN = 50
RECENT_CONTACT_MAX = 200


def install_automation_history_anchor(
    automation_module: Any,
    napcat_module: Any,
) -> None:
    """Anchor scheduled history at the real latest message before filtering."""

    _install_latest_message_anchor(napcat_module)
    _install_anchored_first_cursor_migration(automation_module)


def _install_latest_message_anchor(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    if getattr(worker_cls, "_automation_latest_history_anchor_installed", False):
        return

    original_request = worker_cls.request_automation_history
    original_handle = worker_cls._handle_action_response

    def request_automation_history(
        self: Any,
        request_id: str,
        session_id: str,
        count: int,
    ) -> None:
        count = max(20, min(int(count), 5000))
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            original_request(self, request_id, session_id, count)
            return

        requests = _anchor_requests(self)
        requests[str(request_id)] = {
            "request_id": str(request_id),
            "session_id": str(session_id),
            "count": count,
            "anchored": False,
            "fallback": False,
            "anchor_message_seq": "",
            "anchor_real_seq": "",
            "anchor_time": "",
        }
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._request_automation_history_anchor(
                    str(request_id),
                    str(session_id),
                    count,
                )
            )
        )

    async def request_history_anchor(
        self: Any,
        request_id: str,
        session_id: str,
        count: int,
    ) -> None:
        recent_count = max(
            RECENT_CONTACT_MIN,
            min(RECENT_CONTACT_MAX, int(count)),
        )
        await self._send_action(
            "get_recent_contact",
            {"count": recent_count},
            self._next_echo(f"{ANCHOR_PREFIX}{request_id}|{session_id}"),
        )

    async def request_history_from_anchor(
        self: Any,
        request_id: str,
        session_id: str,
        count: int,
        message_seq: str,
    ) -> None:
        kind, target_id = napcat_module._split_session_id(session_id)
        if not target_id or kind not in {"group", "private"}:
            _fallback_to_unanchored(
                self,
                original_request,
                request_id,
                session_id,
                count,
                "目标会话无效，改用无锚点历史读取",
            )
            return

        common_params = {
            "message_seq": str(message_seq),
            "count": max(20, min(int(count), 5000)),
            "disable_get_url": True,
            "parse_mult_msg": False,
            "quick_reply": True,
            "reverse_order": False,
        }
        if kind == "group":
            action = "get_group_msg_history"
            params = {
                "group_id": napcat_module._onebot_id(target_id),
                **common_params,
            }
        else:
            action = "get_friend_msg_history"
            params = {
                "user_id": napcat_module._onebot_id(target_id),
                **common_params,
            }

        await self._send_action(
            action,
            params,
            self._next_echo(f"{HISTORY_PREFIX}{request_id}|{session_id}"),
        )

    def handle_action_response(self: Any, event: dict[str, Any]) -> bool:
        echo_text = str(event.get("echo") or "")
        ok = event.get("status") == "ok" or event.get("retcode") == 0

        if echo_text.startswith(ANCHOR_PREFIX):
            request_id, session_id = _parse_echo(echo_text, ANCHOR_PREFIX)
            state = _anchor_requests(self).get(request_id)
            if state is None:
                return True

            if not ok:
                _fallback_to_unanchored(
                    self,
                    original_request,
                    request_id,
                    session_id,
                    int(state.get("count") or 20),
                    "获取最近消息锚点失败",
                )
                return True

            item = _find_recent_contact(
                event.get("data"),
                session_id,
                napcat_module,
            )
            anchor = _recent_contact_anchor(item)
            if not anchor["message_seq"]:
                _fallback_to_unanchored(
                    self,
                    original_request,
                    request_id,
                    session_id,
                    int(state.get("count") or 20),
                    "最近会话中没有找到目标的最新消息锚点",
                )
                return True

            state.update(anchor)
            state["anchored"] = True
            self.log.emit(
                f"定时任务历史已锚定 {session_id} 的最新消息："
                f"message_seq={anchor['message_seq']}"
                + (
                    f"，real_seq={anchor['real_seq']}"
                    if anchor["real_seq"]
                    else ""
                )
                + (
                    f"，msgTime={anchor['time']}"
                    if anchor["time"]
                    else ""
                )
            )
            asyncio.create_task(
                self._request_automation_history_from_anchor(
                    request_id,
                    session_id,
                    int(state.get("count") or 20),
                    str(anchor["message_seq"]),
                )
            )
            return True

        if echo_text.startswith(HISTORY_PREFIX):
            request_id, session_id = _parse_echo(echo_text, HISTORY_PREFIX)
            requests = _anchor_requests(self)
            state = requests.get(request_id)
            if state is None:
                return original_handle(self, event)

            if not ok and state.get("anchored") and not state.get("fallback"):
                _fallback_to_unanchored(
                    self,
                    original_request,
                    request_id,
                    session_id,
                    int(state.get("count") or 20),
                    "按最新消息锚点读取历史失败",
                )
                return True

            requests.pop(request_id, None)
            if ok:
                kind, _target_id = napcat_module._split_session_id(session_id)
                messages = napcat_module._extract_history_messages(
                    event.get("data"),
                    session_id,
                    kind,
                )
                self.history_messages_received.emit(
                    {
                        "automation_history": True,
                        "request_id": request_id,
                        "session_id": session_id,
                        "messages": messages,
                        "error": "",
                        "history_anchored": bool(state.get("anchored")),
                        "history_anchor_message_seq": str(
                            state.get("anchor_message_seq") or ""
                        ),
                        "history_anchor_real_seq": str(
                            state.get("anchor_real_seq") or ""
                        ),
                        "history_anchor_time": str(
                            state.get("anchor_time") or ""
                        ),
                        "history_fallback": bool(state.get("fallback")),
                    }
                )
                source = "最新消息锚点" if state.get("anchored") else "无锚点回退"
                self.log.emit(
                    f"定时任务已通过{source}读取 {session_id} 的 "
                    f"{len(messages)} 条历史消息"
                )
            else:
                error = (
                    "定时任务读取历史失败："
                    f"{napcat_module._action_error(event)}"
                )
                self.history_messages_received.emit(
                    {
                        "automation_history": True,
                        "request_id": request_id,
                        "session_id": session_id,
                        "messages": [],
                        "error": error,
                        "history_anchored": bool(state.get("anchored")),
                        "history_fallback": bool(state.get("fallback")),
                    }
                )
            return True

        return original_handle(self, event)

    worker_cls.request_automation_history = request_automation_history
    worker_cls._request_automation_history_anchor = request_history_anchor
    worker_cls._request_automation_history_from_anchor = request_history_from_anchor
    worker_cls._handle_action_response = handle_action_response
    worker_cls._automation_latest_history_anchor_installed = True


def _fallback_to_unanchored(
    worker: Any,
    original_request: Any,
    request_id: str,
    session_id: str,
    count: int,
    reason: str,
) -> None:
    state = _anchor_requests(worker).get(str(request_id))
    if state is None:
        return
    state["anchored"] = False
    state["fallback"] = True
    worker.log.emit(f"{reason}：{session_id}")
    original_request(worker, str(request_id), str(session_id), int(count))


def _anchor_requests(worker: Any) -> dict[str, dict[str, Any]]:
    requests = getattr(worker, "_qqmm_automation_history_anchors", None)
    if not isinstance(requests, dict):
        requests = {}
        worker._qqmm_automation_history_anchors = requests
    return requests


def _parse_echo(echo_text: str, prefix: str) -> tuple[str, str]:
    stable = echo_text.rsplit(":", 1)[0]
    body = stable[len(prefix) :]
    request_id, separator, session_id = body.partition("|")
    if not separator:
        return "", ""
    return request_id, session_id


def _find_recent_contact(
    data: Any,
    session_id: str,
    napcat_module: Any,
) -> dict[str, Any] | None:
    for item in _recent_items(data):
        kind = str(napcat_module._contact_kind(item) or "")
        target_id = str(
            napcat_module._contact_target_id(item, kind) or ""
        ).strip()
        if kind and target_id and f"{kind}:{target_id}" == session_id:
            return item
    return None


def _recent_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "data",
        "items",
        "list",
        "contacts",
        "recent",
        "changedList",
        "rows",
    ):
        value = data.get(key)
        items = _recent_items(value)
        if items:
            return items
    return []


def _recent_contact_anchor(item: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(item, dict):
        return {
            "message_seq": "",
            "real_seq": "",
            "time": "",
            "anchor_message_seq": "",
            "anchor_real_seq": "",
            "anchor_time": "",
        }

    latest = None
    for key in (
        "lastestMsg",
        "latestMsg",
        "latest_message",
        "last_message",
        "message",
    ):
        value = item.get(key)
        if isinstance(value, dict):
            latest = value
            break
    latest = latest or {}

    message_seq = _first_nonzero_text(
        latest.get("message_id"),
        latest.get("messageId"),
        latest.get("message_seq"),
        latest.get("messageSeq"),
        item.get("message_id"),
        item.get("messageId"),
        item.get("msgId"),
        item.get("msg_id"),
    )
    real_seq = _first_nonzero_text(
        latest.get("real_seq"),
        latest.get("realSeq"),
        latest.get("msgSeq"),
        latest.get("msg_seq"),
        item.get("real_seq"),
        item.get("realSeq"),
        item.get("msgSeq"),
        item.get("msg_seq"),
    )
    raw_time = _first_nonzero_text(
        item.get("msgTime"),
        item.get("time"),
        latest.get("time"),
        latest.get("msgTime"),
        latest.get("timestamp"),
    )
    return {
        "message_seq": message_seq,
        "real_seq": real_seq,
        "time": raw_time,
        "anchor_message_seq": message_seq,
        "anchor_real_seq": real_seq,
        "anchor_time": raw_time,
    }


def _install_anchored_first_cursor_migration(
    automation_module: Any,
) -> None:
    if getattr(
        automation_module,
        "_automation_anchored_first_cursor_migration_installed",
        False,
    ):
        return

    original_handler = automation_module._handle_automation_payload

    def handle_with_anchored_migration(
        window: Any,
        ui_module: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if (
            isinstance(payload, dict)
            and payload.get("automation_history")
            and payload.get("history_anchored")
            and not payload.get("error")
        ):
            request_id = str(payload.get("request_id") or "")
            context = getattr(window, "automation_pending", {}).get(request_id)
            task = (
                automation_module.task_by_id(
                    getattr(window, "automation_tasks", []),
                    str(getattr(context, "task_id", "") or ""),
                )
                if context is not None
                else None
            )
            if context is not None and task is not None:
                state = window.automation_state.state(task.task_id)
                cursor = _sequence_number(state.get("checkpoint_real_seq"))
                messages = [
                    message
                    for message in payload.get("messages", [])
                    if isinstance(message, automation_module.ChatMessage)
                ]
                sequence_messages = [
                    (sequence, message)
                    for message in messages
                    for sequence in [_message_sequence(message)]
                    if sequence is not None
                ]
                in_window = [
                    message
                    for _sequence, message in sequence_messages
                    if context.checkpoint
                    <= message.timestamp
                    <= context.cutoff
                ]
                if cursor is None and sequence_messages and not in_window:
                    _place_sequence_batch_inside_window(
                        sequence_messages,
                        context.checkpoint,
                        context.cutoff,
                    )
                    window.append_log(
                        f"定时任务“{task.name}”已用最新消息锚点建立"
                        f"序号基线：首次补处理 {len(sequence_messages)} 条，"
                        f"序号 {_sequence_span(sequence_messages)}"
                    )

        original_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_with_anchored_migration
    automation_module._automation_anchored_first_cursor_migration_installed = True


def _place_sequence_batch_inside_window(
    sequence_messages: list[tuple[int, Any]],
    checkpoint: datetime,
    cutoff: datetime,
) -> None:
    ordered = sorted(sequence_messages, key=lambda item: item[0])
    count = len(ordered)
    start = cutoff - timedelta(microseconds=max(count, 1))
    if start < checkpoint:
        start = checkpoint
    for index, (sequence, message) in enumerate(ordered, start=1):
        raw_event = getattr(message, "raw_event", None)
        if isinstance(raw_event, dict):
            raw_event["_qqmm_anchor_time_override"] = str(sequence)
            raw_event["_qqmm_anchor_original_timestamp"] = message.timestamp.isoformat()
        message.timestamp = min(
            start + timedelta(microseconds=index),
            cutoff,
        )


def _sequence_span(
    sequence_messages: list[tuple[int, Any]],
) -> str:
    values = [sequence for sequence, _message in sequence_messages]
    return f"{min(values)}～{max(values)}" if values else "未知"


def _message_sequence(message: Any) -> int | None:
    raw_event = getattr(message, "raw_event", None)
    if not isinstance(raw_event, dict):
        return None
    for key in (
        "real_seq",
        "realSeq",
        "message_seq",
        "messageSeq",
        "msgSeq",
        "msg_seq",
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


def _first_nonzero_text(*values: Any) -> str:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text and text not in {"0", "0.0"}:
            return text
    return ""
