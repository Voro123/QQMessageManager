from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any


_COMPACT_TIME_FORMATS = (
    (re.compile(r"^\d{14}$"), "%Y%m%d%H%M%S"),
    (re.compile(r"^\d{12}$"), "%Y%m%d%H%M"),
)
_SEQUENCE_TO_COMMIT: dict[str, str] = {}


def install_automation_history_reliability(
    automation_module: Any,
    napcat_module: Any,
) -> None:
    """Make scheduled history filtering tolerant of NapCat history quirks."""

    _install_timestamp_parser(napcat_module)
    _install_zero_session_filter(napcat_module)
    _install_sequence_state(automation_module)
    _install_history_filter_diagnostics(automation_module)
    # Install last so sequence-based timestamp repair runs before diagnostics
    # and before the existing empty-message execution wrapper.
    _install_sequence_cursor_filter(automation_module)


def _install_timestamp_parser(napcat_module: Any) -> None:
    if getattr(napcat_module, "_automation_history_timestamp_fix_installed", False):
        return

    original_history_item = napcat_module._history_item_to_message

    def event_time(value: Any) -> datetime:
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
        return datetime.now().replace(microsecond=0)

    def history_item_with_reliable_time(
        item: dict[str, Any],
        session_id: str,
        kind: str,
    ) -> Any:
        message = original_history_item(item, session_id, kind)
        if message is None:
            return None
        raw_time = _history_raw_time(item)
        parsed = _parse_timestamp(raw_time)
        if isinstance(message.raw_event, dict):
            message.raw_event["_qqmm_raw_history_time"] = str(raw_time or "")
        if parsed is None:
            # Do not use datetime.now() for malformed historical timestamps.
            # A response arriving after cutoff would otherwise make a real
            # message look newer than the scheduled execution window.
            message.timestamp = datetime.min
            if isinstance(message.raw_event, dict):
                message.raw_event["_qqmm_invalid_history_time"] = str(raw_time or "")
        else:
            message.timestamp = parsed
        return message

    napcat_module._event_time = event_time
    napcat_module._history_item_to_message = history_item_with_reliable_time
    napcat_module._automation_history_timestamp_fix_installed = True


def _install_zero_session_filter(napcat_module: Any) -> None:
    """Ignore recent-contact entries whose target id is the invalid QQ value 0."""

    if getattr(napcat_module, "_zero_recent_session_filter_installed", False):
        return
    original_target_id = napcat_module._contact_target_id

    def target_id_without_zero(item: dict[str, Any], kind: str) -> str:
        value = str(original_target_id(item, kind) or "").strip()
        return "" if value in {"", "0"} else value

    napcat_module._contact_target_id = target_id_without_zero
    napcat_module._zero_recent_session_filter_installed = True


def _install_sequence_state(automation_module: Any) -> None:
    state_cls = automation_module.AutomationStateStore
    if getattr(state_cls, "_automation_sequence_cursor_state_installed", False):
        return

    original_initialize = state_cls._initialize
    original_mark_success = state_cls.mark_success
    original_mark_pending = getattr(state_cls, "mark_pending_delivery", None)
    original_clear_pending = getattr(state_cls, "clear_pending_delivery", None)

    def initialize_with_sequence_cursor(self: Any) -> None:
        original_initialize(self)
        additions = {
            "checkpoint_real_seq": "TEXT NOT NULL DEFAULT ''",
            "pending_checkpoint_real_seq": "TEXT NOT NULL DEFAULT ''",
        }
        with self._connect() as connection:
            existing = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(task_state)").fetchall()
            }
            for column, declaration in additions.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE task_state ADD COLUMN {column} {declaration}"
                    )

    def mark_success_with_sequence(
        self: Any,
        task_id: str,
        checkpoint_time: datetime,
        checkpoint_message_id: str,
        message_keys: list[str],
        status: str = "success",
    ) -> None:
        state_before = self.state(task_id)
        sequence = (
            _SEQUENCE_TO_COMMIT.get(task_id, "")
            or str(state_before.get("pending_checkpoint_real_seq") or "")
        )
        original_mark_success(
            self,
            task_id,
            checkpoint_time,
            checkpoint_message_id,
            message_keys,
            status,
        )
        if sequence:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE task_state SET checkpoint_real_seq = ? WHERE task_id = ?",
                    (sequence, task_id),
                )

    state_cls._initialize = initialize_with_sequence_cursor
    state_cls.mark_success = mark_success_with_sequence

    if callable(original_mark_pending):
        def mark_pending_with_sequence(
            self: Any,
            task_id: str,
            file_path: str,
            cutoff: datetime,
            checkpoint: datetime,
            message_keys: list[str],
            checkpoint_message_id: str,
        ) -> None:
            original_mark_pending(
                self,
                task_id,
                file_path,
                cutoff,
                checkpoint,
                message_keys,
                checkpoint_message_id,
            )
            sequence = _SEQUENCE_TO_COMMIT.get(task_id, "")
            if sequence:
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE task_state
                        SET pending_checkpoint_real_seq = ?
                        WHERE task_id = ?
                        """,
                        (sequence, task_id),
                    )

        state_cls.mark_pending_delivery = mark_pending_with_sequence

    if callable(original_clear_pending):
        def clear_pending_with_sequence(self: Any, task_id: str) -> None:
            original_clear_pending(self, task_id)
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE task_state
                    SET pending_checkpoint_real_seq = ''
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )

        state_cls.clear_pending_delivery = clear_pending_with_sequence

    original_finish = automation_module._finish_task

    def finish_and_clear_sequence(window: Any, task_id: str) -> None:
        original_finish(window, task_id)
        _SEQUENCE_TO_COMMIT.pop(str(task_id), None)

    automation_module._finish_task = finish_and_clear_sequence
    state_cls._automation_sequence_cursor_state_installed = True


def _install_sequence_cursor_filter(automation_module: Any) -> None:
    if getattr(automation_module, "_automation_sequence_cursor_filter_installed", False):
        return

    original_handler = automation_module._handle_automation_payload

    def handle_with_sequence_cursor(
        window: Any,
        ui_module: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if (
            not isinstance(payload, dict)
            or not payload.get("automation_history")
            or payload.get("error")
        ):
            original_handler(window, ui_module, ai_module, payload)
            return

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
        if context is None or task is None:
            original_handler(window, ui_module, ai_module, payload)
            return

        fetched = [
            message
            for message in payload.get("messages", [])
            if isinstance(message, automation_module.ChatMessage)
        ]
        sequence_messages = [
            (sequence, message)
            for message in fetched
            for sequence in [_message_sequence(message)]
            if sequence is not None
        ]
        if not sequence_messages:
            original_handler(window, ui_module, ai_module, payload)
            return

        state = window.automation_state.state(task.task_id)
        checkpoint_sequence = _sequence_number(state.get("checkpoint_real_seq"))
        if checkpoint_sequence is None:
            checkpoint_message_id = str(state.get("checkpoint_message_id") or "")
            if checkpoint_message_id:
                checkpoint_sequence = next(
                    (
                        sequence
                        for sequence, message in sequence_messages
                        if str(getattr(message, "message_id", "") or "")
                        == checkpoint_message_id
                    ),
                    None,
                )

        synthetic = _synthetic_timestamp_batch(
            [message for _sequence, message in sequence_messages],
            context.cutoff,
        )
        selected: list[tuple[int, Any]] = []
        mode = ""

        if checkpoint_sequence is not None:
            selected = [
                (sequence, message)
                for sequence, message in sequence_messages
                if sequence > checkpoint_sequence
            ]
            mode = f"序号游标 {checkpoint_sequence}"
            # Sequence is authoritative once a cursor exists. Keep every record
            # at or below the cursor outside the time window, even if NapCat
            # stamped it with a plausible current time.
            selected_ids = {id(message) for _sequence, message in selected}
            for _sequence, message in sequence_messages:
                if id(message) not in selected_ids:
                    message.timestamp = datetime.min
        elif synthetic:
            # Legacy tasks created before the sequence cursor existed have no
            # reliable way to split a batch whose every timestamp was replaced
            # by NapCat's Date.now(). Process the returned latest batch once,
            # then persist its maximum real_seq for exact future increments.
            selected = sequence_messages
            mode = "首次序号迁移"
        else:
            selected = [
                (sequence, message)
                for sequence, message in sequence_messages
                if context.checkpoint <= message.timestamp <= context.cutoff
            ]
            mode = "时间窗口建立序号"

        if selected:
            _place_messages_inside_window(selected, context.checkpoint, context.cutoff)
            _SEQUENCE_TO_COMMIT[task.task_id] = str(max(sequence for sequence, _message in selected))
            window.append_log(
                f"定时任务“{task.name}”使用{mode}筛选历史："
                f"选中 {len(selected)} 条，提交序号 "
                f"{_SEQUENCE_TO_COMMIT[task.task_id]}"
            )
        elif checkpoint_sequence is not None:
            window.append_log(
                f"定时任务“{task.name}”序号游标为 {checkpoint_sequence}，"
                "本次历史中没有更大的消息序号"
            )

        adjusted_payload = dict(payload)
        adjusted_payload["messages"] = fetched
        original_handler(window, ui_module, ai_module, adjusted_payload)

    automation_module._handle_automation_payload = handle_with_sequence_cursor
    automation_module._automation_sequence_cursor_filter_installed = True


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
                before_count = sum(
                    1 for message in merged.values()
                    if message.timestamp < context.checkpoint
                )
                after_count = sum(
                    1 for message in merged.values()
                    if message.timestamp > context.cutoff
                )
                valid_times = [
                    message.timestamp
                    for message in fetched
                    if message.timestamp != datetime.min
                ]
                sequences = [
                    sequence
                    for message in fetched
                    for sequence in [_message_sequence(message)]
                    if sequence is not None
                ]
                details = (
                    f"定时任务“{task.name}”历史筛选：接口返回 {len(fetched)} 条，"
                    f"本地缓存 {len(current)} 条，合并后 {len(merged)} 条；"
                    f"时间范围内 {len(candidates)} 条，已处理 {len(processed)} 条，"
                    f"待处理 {pending_count} 条，范围前 {before_count} 条，"
                    f"截止后 {after_count} 条"
                )
                if valid_times:
                    details += (
                        f"；解析时间 {min(valid_times):%Y-%m-%d %H:%M:%S.%f}"
                        f" ～ {max(valid_times):%Y-%m-%d %H:%M:%S.%f}"
                    )
                if sequences:
                    details += f"；消息序号 {min(sequences)} ～ {max(sequences)}"
                if invalid_count:
                    details += f"；另有 {invalid_count} 条时间戳无法解析"
                window.append_log(details)

        original_handler(window, ui_module, ai_module, payload)

    automation_module._handle_automation_payload = handle_with_filter_diagnostics
    automation_module._automation_history_filter_diagnostics_installed = True


def _place_messages_inside_window(
    selected: list[tuple[int, Any]],
    checkpoint: datetime,
    cutoff: datetime,
) -> None:
    ordered = sorted(selected, key=lambda item: item[0])
    count = len(ordered)
    base = cutoff - timedelta(microseconds=max(count, 1))
    if base < checkpoint:
        base = checkpoint
    for index, (sequence, message) in enumerate(ordered, start=1):
        if isinstance(getattr(message, "raw_event", None), dict):
            message.raw_event["_qqmm_sequence_time_override"] = str(sequence)
            message.raw_event["_qqmm_original_timestamp"] = message.timestamp.isoformat()
        candidate = base + timedelta(microseconds=index)
        message.timestamp = min(candidate, cutoff)


def _synthetic_timestamp_batch(messages: list[Any], cutoff: datetime) -> bool:
    candidates = [
        message
        for message in messages
        if _is_synthetic_message(message, cutoff)
    ]
    return bool(candidates) and len(candidates) >= max(1, len(messages) // 2)


def _is_synthetic_message(message: Any, cutoff: datetime) -> bool:
    raw_event = getattr(message, "raw_event", None)
    if not isinstance(raw_event, dict):
        return False
    raw_time = raw_event.get("_qqmm_raw_history_time", raw_event.get("time"))
    try:
        numeric = abs(float(str(raw_time).strip()))
    except (TypeError, ValueError):
        return False
    if numeric < 1_000_000_000_000:
        return False
    timestamp = getattr(message, "timestamp", datetime.min)
    return timestamp != datetime.min and abs((timestamp - cutoff).total_seconds()) <= 15


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
        sequence = _sequence_number(raw_event.get(key))
        if sequence is not None:
            return sequence
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


def _history_raw_time(item: dict[str, Any]) -> Any:
    direct = _first_present(
        item.get("time"),
        item.get("msgTime"),
        item.get("timestamp"),
        item.get("sendTime"),
        item.get("send_time"),
        item.get("msg_time"),
    )
    if direct is not None:
        return direct
    for key in ("raw", "raw_event", "source", "message_info", "messageInfo"):
        nested = item.get(key)
        if not isinstance(nested, dict):
            continue
        value = _first_present(
            nested.get("msgTime"),
            nested.get("time"),
            nested.get("timestamp"),
            nested.get("sendTime"),
            nested.get("send_time"),
            nested.get("msg_time"),
        )
        if value is not None:
            return value
    return None


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
