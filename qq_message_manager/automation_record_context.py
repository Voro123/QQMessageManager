from __future__ import annotations

import json
from typing import Any

MAX_RECORD_CONTEXT_ITEMS = 500
MAX_RECORD_CONTEXT_CHARS = 24000
MAX_VALUE_CHARS = 1200

CLOSED_STATUS_WORDS = (
    "已完成",
    "已解决",
    "已关闭",
    "已回答",
    "已处理",
    "忽略",
    "无效",
    "完成",
    "解决",
    "关闭",
    "done",
    "closed",
    "resolved",
    "answered",
)
OPEN_STATUS_WORDS = (
    "待",
    "未",
    "处理中",
    "进行中",
    "开放",
    "open",
    "pending",
    "progress",
)


def install_automation_record_context(automation_module: Any) -> None:
    """优先向模型提供仍可能被后续聊天更新的旧记录。"""
    if getattr(automation_module, "_stage2_record_context_installed", False):
        return

    def prioritized_records_for_ai(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        indexed = list(enumerate(records))
        open_records = [
            (index, record)
            for index, record in indexed
            if _record_is_open(record)
        ]
        other_records = [
            (index, record)
            for index, record in indexed
            if not _record_is_open(record)
        ]
        ordered = [
            *sorted(open_records, key=lambda item: item[0], reverse=True),
            *sorted(other_records, key=lambda item: item[0], reverse=True),
        ]

        selected: list[dict[str, Any]] = []
        size = 2
        seen: set[str] = set()
        for _index, record in ordered:
            compact = _compact_record(record)
            record_id = str(compact.get("record_id") or "")
            if not record_id or record_id in seen:
                continue
            encoded = json.dumps(compact, ensure_ascii=False, default=str)
            extra = len(encoded) + (1 if selected else 0)
            if selected and size + extra > MAX_RECORD_CONTEXT_CHARS:
                continue
            selected.append(compact)
            seen.add(record_id)
            size += extra
            if len(selected) >= MAX_RECORD_CONTEXT_ITEMS:
                break

        # Prompt 中按原始时间顺序展示，便于模型理解状态变化。
        order = {
            str(record.get("record_id") or ""): index
            for index, record in indexed
        }
        selected.sort(key=lambda record: order.get(str(record.get("record_id") or ""), 0))
        return selected

    automation_module.records_for_ai = prioritized_records_for_ai
    automation_module._stage2_record_context_installed = True


def _record_is_open(record: dict[str, Any]) -> bool:
    values = record.get("values")
    if not isinstance(values, dict):
        return False
    statuses: list[str] = []
    for key, value in values.items():
        key_text = str(key).strip().lower()
        if any(marker in key_text for marker in ("状态", "进度", "status", "state")):
            statuses.append(str(value or "").strip().lower())
    if not statuses:
        return False
    combined = " ".join(statuses)
    if any(word.lower() in combined for word in CLOSED_STATUS_WORDS):
        return False
    return any(word.lower() in combined for word in OPEN_STATUS_WORDS) or not combined.strip()


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values")
    compact_values: dict[str, Any] = {}
    if isinstance(values, dict):
        for key, value in values.items():
            if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
                compact_values[str(key)] = value[:MAX_VALUE_CHARS] + "…"
            else:
                compact_values[str(key)] = value
    return {
        "record_id": str(record.get("record_id") or ""),
        "values": compact_values,
        "source_message_ids": [
            str(value)
            for value in list(record.get("source_message_ids") or [])[-20:]
            if str(value)
        ],
    }
