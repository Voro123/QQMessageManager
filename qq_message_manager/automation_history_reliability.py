from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any


_COMPACT_TIME_FORMATS = (
    (re.compile(r"^\d{14}$"), "%Y%m%d%H%M%S"),
    (re.compile(r"^\d{12}$"), "%Y%m%d%H%M"),
)


def install_automation_history_reliability(
    automation_module: Any,
    napcat_module: Any,
) -> None:
    """Make scheduled history filtering tolerant of NapCat timestamp variants."""

    _install_timestamp_parser(napcat_module)
    _install_history_filter_diagnostics(automation_module)


def _install_timestamp_parser(napcat_module: Any) -> None:
    if getattr(napcat_module, "_automation_history_timestamp_fix_installed", False):
        return

    original_history_item = napcat_module._history_item_to_message

    def event_time(value: Any) -> datetime:
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
        # Preserve the live-message behavior for a genuinely absent time value.
        # Historical items are handled below and never silently receive the
        # response time, because that can place them after the task cutoff.
        return datetime.now().replace(microsecond=0)

    def history_item_with_reliable_time(
        item: dict[str, Any],
        session_id: str,
        kind: str,
    ) -> Any:
        message = original_history_item(item, session_id, kind)
        if message is None:
            return None
        raw_time = _first_present(
            item.get("time"),
            item.get("msgTime"),
            item.get("timestamp"),
            item.get("sendTime"),
            item.get("send_time"),
            item.get("msg_time"),
        )
        parsed = _parse_timestamp(raw_time)
        if parsed is None:
            # Do not use datetime.now() for malformed historical timestamps.
            # A response arriving one second after cutoff would otherwise make
            # a real message look newer than the scheduled execution window.
            message.timestamp = datetime.min
            if isinstance(message.raw_event, dict):
                message.raw_event["_qqmm_invalid_history_time"] = str(raw_time or "")
        else:
            message.timestamp = parsed
        return message

    napcat_module._event_time = event_time
    napcat_module._history_item_to_message = history_item_with_reliable_time
    napcat_module._automation_history_timestamp_fix_installed = True


def _install_history_filter_diagnostics(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_history_filter_diagnostics_installed", False):
        return

    original_handler = automation_module._handle_automation_payload

    def handle_with_filter_diagnostics(
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
            task = (
                automation_module.task_by_id(
                    getattr(window, "automation_tasks", []),
                    str(getattr(context, "task_id", "") or ""),
                )
                if context is not None
                else None
            )
            if context is not None and task is not None:
                fetched = [
                    message
                    for message in payload.get("messages", [])
                    if isinstance(message, automation_module.ChatMessage)
                ]
                current = [
                    message
                    for message in getattr(window, "messages", {}).get(task.target_session_id, [])
                    if isinstance(message, automation_module.ChatMessage)
                ]
                merged: dict[str, Any] = {}
                for message in [*fetched, *current]:
                    merged[automation_module.message_key(message)] = message
                candidates = [
                    message
                    for message in sorted(merged.values(), key=lambda item: item.timestamp)
                    if context.checkpoint <= message.timestamp <= context.cutoff
                ]
                keys = [automation_module.message_key(message) for message in candidates]
                processed = window.automation_state.processed_keys(task.task_id, keys)
                pending_count = sum(
                    1
                    for message in candidates
                    if automation_module.message_key(message) not in processed
                )
                invalid_count = sum(
                    1
                    for message in fetched
                    if isinstance(getattr(message, "raw_event", None), dict)
                    and "_qqmm_invalid_history_time" in message.raw_event
                )
                details = (
                    f"定时任务“{task.name}”历史筛选：接口返回 {len(fetched)} 条，"
                    f"本地缓存 {len(current)} 条，合并后 {len(merged)} 条；"
                    f"时间范围内 {len(candidates)} 条，已处理 {len(processed)} 条，"
                    f"待处理 {pending_count} 条"
                )
                if invalid_count:
                    details += f"，另有 {invalid_count} 条时间戳无法解析"
                window.append_log(details)

        original_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_with_filter_diagnostics
    automation_module._automation_history_filter_diagnostics_installed = True


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_local_naive(value)
    if value is None or isinstance(value, bool):
        return None

    text = str(value).strip()
    if not text or text in {"0", "0.0"}:
        return None

    for pattern, fmt in _COMPACT_TIME_FORMATS:
        if pattern.fullmatch(text):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                return None

    # Some endpoints expose compact YYYYMMDDHHMMSS plus milliseconds.
    if re.fullmatch(r"\d{17}", text) and text.startswith(("19", "20")):
        try:
            base = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
            return base.replace(microsecond=int(text[14:17]) * 1000)
        except ValueError:
            return None

    normalized_iso = text.replace("Z", "+00:00")
    try:
        return _to_local_naive(datetime.fromisoformat(normalized_iso))
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:
        numeric = float(text)
    except ValueError:
        return None
    if not math.isfinite(numeric) or numeric == 0:
        return None

    absolute = abs(numeric)
    if absolute >= 1e17:
        seconds = numeric / 1_000_000_000
    elif absolute >= 1e14:
        seconds = numeric / 1_000_000
    elif absolute >= 1e11:
        seconds = numeric / 1_000
    else:
        seconds = numeric

    try:
        parsed = datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None
    return parsed if 1970 <= parsed.year <= 2200 else None


def _to_local_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value == 0:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped in {"0", "0.0"}:
                continue
        return value
    return None
