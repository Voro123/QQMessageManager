from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


DEFAULT_TEXT_EXTENSIONS = [
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".css", ".js", ".ts",
    ".jsx", ".tsx", ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".php", ".rb", ".sh", ".sql", ".log",
]


def _normalized_extensions(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = DEFAULT_TEXT_EXTENSIONS
    result: set[str] = set()
    for value in values:
        extension = str(value or "").strip().lower()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        if extension.count(".") != 1 or any(char in extension for char in "/\\:\x00"):
            continue
        result.add(extension)
    return sorted(result or set(DEFAULT_TEXT_EXTENSIONS))


@dataclass(slots=True)
class FolderGrant:
    grant_id: str = field(default_factory=lambda: uuid4().hex)
    alias: str = ""
    description: str = ""
    root_path: str = ""
    enabled: bool = True
    read_enabled: bool = True
    write_enabled: bool = False
    write_confirmation_required: bool = True
    allowed_sender_ids: list[str] = field(default_factory=list)
    allowed_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_TEXT_EXTENSIONS))

    def normalized(self) -> "FolderGrant":
        return FolderGrant(
            grant_id=str(self.grant_id or uuid4().hex).strip(),
            alias=str(self.alias or "").strip(),
            description=str(self.description or "").strip(),
            root_path=str(self.root_path or "").strip(),
            enabled=bool(self.enabled),
            read_enabled=bool(self.read_enabled),
            write_enabled=bool(self.write_enabled),
            write_confirmation_required=bool(self.write_confirmation_required),
            allowed_sender_ids=sorted({str(value).strip() for value in self.allowed_sender_ids if str(value).strip()}),
            allowed_extensions=_normalized_extensions(self.allowed_extensions),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalized())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FolderGrant":
        return cls(
            grant_id=str(value.get("grant_id") or uuid4().hex),
            alias=str(value.get("alias") or ""),
            description=str(value.get("description") or ""),
            root_path=str(value.get("root_path") or ""),
            enabled=bool(value.get("enabled", True)),
            read_enabled=bool(value.get("read_enabled", True)),
            write_enabled=bool(value.get("write_enabled", False)),
            write_confirmation_required=bool(value.get("write_confirmation_required", True)),
            allowed_sender_ids=list(value.get("allowed_sender_ids") or []),
            allowed_extensions=list(value.get("allowed_extensions") or DEFAULT_TEXT_EXTENSIONS),
        ).normalized()


@dataclass(slots=True)
class PendingFolderAction:
    action_id: str
    grant_id: str
    alias: str
    session_id: str
    sender_id: str
    tool: str
    arguments: dict[str, Any]
    created_at: datetime
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current >= self.expires_at


@dataclass(slots=True)
class FolderToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""

    def to_model_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"success": self.success}
        if self.data:
            value["data"] = self.data
        if self.error_code:
            value["error_code"] = self.error_code
        if self.message:
            value["message"] = self.message
        return value
