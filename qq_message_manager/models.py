from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SessionKind = Literal["group", "private", "system"]


@dataclass(slots=True)
class ChatImage:
    """一条图片附件的元数据。base64 不长期保存，仅运行时按需读取。"""

    url: str = ""
    path: str = ""
    file: str = ""
    file_id: str = ""
    file_unique: str = ""
    file_size: str = ""
    mime_type: str = ""
    local_path: str = ""
    load_failed: bool = False


@dataclass(slots=True)
class ChatMessage:
    """Normalized message displayed by the UI."""

    session_id: str
    session_name: str
    session_kind: SessionKind
    sender_id: str
    sender_name: str
    text: str
    timestamp: datetime = field(default_factory=datetime.now)
    raw_event: dict[str, Any] = field(default_factory=dict)
    outgoing: bool = False
    historical: bool = False
    message_id: str = ""
    images: list[ChatImage] = field(default_factory=list)


@dataclass(slots=True)
class ChatSession:
    """A conversation in the left-side QQ-like session list."""

    session_id: str
    name: str
    kind: SessionKind
    last_message: str = ""
    last_time: datetime = field(default_factory=datetime.now)
    unread_count: int = 0
    pinned: bool = False
