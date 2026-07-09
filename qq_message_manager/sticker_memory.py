from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STICKER_MEMORY_LIMIT = 50
STICKER_MARKER_RE = re.compile(r"<STICKER:([A-Za-z0-9_\-:.]+)>")
APP_DATA_DIR = Path.home() / ".qq_message_manager"
STICKER_MEMORY_PATH = APP_DATA_DIR / "sticker_memory.json"


@dataclass(slots=True)
class StickerRecord:
    id: str
    source_type: str = "image"
    summary: str = ""
    usage_hint: str = ""
    emoji_id: str = ""
    emoji_package_id: str = ""
    key: str = ""
    file: str = ""
    file_id: str = ""
    file_unique: str = ""
    url: str = ""
    path: str = ""
    use_count: int = 0
    created_at: str = ""
    last_used_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StickerRecord":
        fields = {field: data.get(field, "") for field in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        fields["use_count"] = int(data.get("use_count") or 0)
        return cls(**fields)

    def to_ai_option(self) -> dict[str, str]:
        hint = self.usage_hint or self.summary or "适合当前气氛时使用"
        return {
            "id": self.id,
            "summary": self.summary or self.id,
            "usage_hint": hint,
        }

    def to_cq_code(self) -> str:
        if self.source_type == "mface" and self.emoji_id and self.emoji_package_id and self.key:
            return _cq(
                "mface",
                {
                    "emoji_id": self.emoji_id,
                    "emoji_package_id": self.emoji_package_id,
                    "key": self.key,
                    "summary": self.summary,
                },
            )
        image_file = self.path or self.url or self.file_id or (self.file if self.file != "marketface" else "")
        if not image_file:
            return ""
        return _cq("image", {"file": image_file, "summary": self.summary})


class StickerMemory:
    def __init__(self, path: Path = STICKER_MEMORY_PATH, limit: int = STICKER_MEMORY_LIMIT) -> None:
        self.path = path
        self.limit = limit
        self.records: dict[str, StickerRecord] = {}
        self.load()

    def load(self) -> None:
        self.records.clear()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raw = []
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                record = StickerRecord.from_dict(item)
            except Exception:  # noqa: BLE001
                continue
            if record.id:
                self.records[record.id] = record
        self._prune()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self.records.values(), key=lambda record: record.created_at)
        self.path.write_text(json.dumps([asdict(record) for record in ordered], ensure_ascii=False, indent=2), encoding="utf-8")

    def remember_from_event(self, event: dict[str, Any]) -> int:
        records = extract_sticker_records_from_event(event)
        added = 0
        now = _now()
        for record in records:
            existing = self.records.get(record.id)
            if existing is None:
                record.created_at = record.created_at or now
                record.last_used_at = record.last_used_at or ""
                self.records[record.id] = record
                added += 1
                continue
            existing.summary = existing.summary or record.summary
            existing.usage_hint = existing.usage_hint or record.usage_hint
            existing.source_type = "mface" if record.source_type == "mface" else existing.source_type
            for field in ("emoji_id", "emoji_package_id", "key", "file", "file_id", "file_unique", "url", "path"):
                current = getattr(existing, field)
                incoming = getattr(record, field)
                if not current and incoming:
                    setattr(existing, field, incoming)
        if records:
            self._prune()
            self.save()
        return added

    def get(self, sticker_id: str) -> StickerRecord | None:
        return self.records.get(sticker_id)

    def mark_used(self, sticker_id: str) -> None:
        record = self.records.get(sticker_id)
        if record is None:
            return
        record.use_count += 1
        record.last_used_at = _now()
        self.save()

    def ai_options(self) -> list[dict[str, str]]:
        # 优先给 AI 最近/常用的表情包，但数量仍严格不超过 limit。
        ordered = sorted(
            self.records.values(),
            key=lambda record: (record.use_count, record.last_used_at or record.created_at),
            reverse=True,
        )
        return [record.to_ai_option() for record in ordered[: self.limit]]

    def _prune(self) -> None:
        while len(self.records) > self.limit:
            victim = min(
                self.records.values(),
                key=lambda record: (record.use_count, record.last_used_at or "", record.created_at or ""),
            )
            self.records.pop(victim.id, None)


def parse_sticker_marker(reply: str) -> tuple[str, str]:
    sticker_id = ""

    def replace(match: re.Match[str]) -> str:
        nonlocal sticker_id
        if not sticker_id:
            sticker_id = match.group(1)
        return ""

    text = STICKER_MARKER_RE.sub(replace, reply or "").strip()
    return text, sticker_id


def extract_sticker_records_from_event(event: dict[str, Any]) -> list[StickerRecord]:
    message = event.get("message")
    if isinstance(message, list):
        return _extract_from_segments(message)
    if isinstance(message, str):
        return _extract_from_cq(message)
    raw_message = event.get("raw_message") or event.get("rawMessage")
    if isinstance(raw_message, str):
        return _extract_from_cq(raw_message)
    return []


def _extract_from_segments(segments: list[Any]) -> list[StickerRecord]:
    result: list[StickerRecord] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "")
        if segment_type not in {"mface", "image"}:
            continue
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            continue
        record = _record_from_data(data, segment_type)
        if record is not None:
            result.append(record)
    return result


def _extract_from_cq(text: str) -> list[StickerRecord]:
    result: list[StickerRecord] = []
    for match in re.finditer(r"\[CQ:(mface|image),([^\]]*)\]", text):
        segment_type = match.group(1)
        data = _parse_cq_params(match.group(2))
        record = _record_from_data(data, segment_type)
        if record is not None:
            result.append(record)
    return result


def _record_from_data(data: dict[str, Any], segment_type: str) -> StickerRecord | None:
    emoji_id = _text(data.get("emoji_id") or data.get("emojiId"))
    emoji_package_id = _text(data.get("emoji_package_id") or data.get("emojiPackageId"))
    key = _text(data.get("key"))
    file = _text(data.get("file"))
    summary = _text(data.get("summary") or data.get("name") or data.get("text"))
    file_unique = _text(data.get("file_unique") or data.get("file_unique_id") or data.get("fileUnique"))
    file_id = _text(data.get("file_id") or data.get("fileId"))
    url = _text(data.get("url"))
    path = _text(data.get("path"))

    looks_like_mface = segment_type == "mface" or file == "marketface" or bool(emoji_id or emoji_package_id or key or summary)
    if not looks_like_mface:
        return None

    sticker_id = _stable_id(emoji_id, emoji_package_id, key, file_unique, file_id, url, path, file)
    if not sticker_id:
        return None
    source_type = "mface" if emoji_id and emoji_package_id and key else "image"
    return StickerRecord(
        id=sticker_id,
        source_type=source_type,
        summary=summary or "表情包",
        usage_hint=_usage_hint(summary),
        emoji_id=emoji_id,
        emoji_package_id=emoji_package_id,
        key=key,
        file=file,
        file_id=file_id,
        file_unique=file_unique,
        url=url,
        path=path,
        created_at=_now(),
    )


def _stable_id(*parts: str) -> str:
    raw = "|".join(value for value in parts if value)
    if not raw:
        return ""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"mf_{digest}"


def _usage_hint(summary: str) -> str:
    summary = summary.strip()
    if not summary:
        return "适合当前气氛时使用"
    return f"含义/作用：{summary}。适合表达类似情绪或语气时使用。"


def _parse_cq_params(body: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in body.split(","):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        params[key.strip()] = value.strip()
    return params


def _cq(segment_type: str, data: dict[str, str]) -> str:
    body = ",".join(f"{key}={_escape_cq(value)}" for key, value in data.items() if value)
    return f"[CQ:{segment_type},{body}]" if body else ""


def _escape_cq(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
