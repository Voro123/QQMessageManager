from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SessionKind = Literal["group", "private", "system"]


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
