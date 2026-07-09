from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from .models import ChatMessage

LOGGER = logging.getLogger(__name__)


class NapCatWorker(QObject):
    """WebSocket worker running inside a QThread."""

    connected = Signal()
    disconnected = Signal(str)
    message_received = Signal(object)
    log = Signal(str)

    def __init__(self, websocket_url: str, token: str = "", reconnect_interval: float = 3.0) -> None:
        super().__init__()
        self.websocket_url = websocket_url.strip()
        self.token = token.strip()
        self.reconnect_interval = reconnect_interval
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current_websocket: Any | None = None

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_forever())
        finally:
            self._loop.close()
            self._loop = None

    def stop(self) -> None:
        self._stop_requested = True
        if self._loop and self._loop.is_running():
            async def close_current_websocket() -> None:
                if self._current_websocket is not None:
                    await self._current_websocket.close()

            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(close_current_websocket()))

    async def _run_forever(self) -> None:
        while not self._stop_requested:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = f"连接失败：{exc}"
                LOGGER.exception(message)
                self.disconnected.emit(message)
                if not self._stop_requested:
                    await asyncio.sleep(self.reconnect_interval)

    async def _connect_once(self) -> None:
        import websockets

        headers = self._build_headers()
        self.log.emit(f"正在连接 {self.websocket_url}")
        websocket = await self._open_websocket(websockets, self.websocket_url, headers)
        self._current_websocket = websocket
        try:
            async with websocket:
                self.connected.emit()
                self.log.emit("已连接 NapCatQQ WebSocket")
                async for payload in websocket:
                    if self._stop_requested:
                        break
                    self._handle_payload(payload)
        finally:
            self._current_websocket = None
        self.disconnected.emit("连接已断开")

    def _build_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": "Bearer " + self.token}

    @staticmethod
    async def _open_websocket(websockets_module: Any, url: str, headers: dict[str, str]) -> Any:
        try:
            return await websockets_module.connect(url, additional_headers=headers or None)
        except TypeError:
            return await websockets_module.connect(url, extra_headers=headers or None)

    def _handle_payload(self, payload: str | bytes) -> None:
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            event = json.loads(payload)
        except Exception as exc:
            self.log.emit(f"收到无法解析的数据：{exc}")
            return

        message = normalize_message_event(event)
        if message is not None:
            self.message_received.emit(message)


def normalize_message_event(event: dict[str, Any]) -> ChatMessage | None:
    if event.get("post_type") != "message":
        return None

    message_type = event.get("message_type")
    if message_type not in {"group", "private"}:
        return None

    sender = event.get("sender") or {}
    sender_id = str(event.get("user_id") or sender.get("user_id") or "未知用户")
    sender_name = _first_text(sender.get("card"), sender.get("nickname"), sender_id)
    text = message_to_text(event.get("message"), event.get("raw_message"))
    timestamp = _event_time(event.get("time"))

    if message_type == "group":
        group_id = str(event.get("group_id") or "未知群")
        session_id = f"group:{group_id}"
        session_name = f"群聊 {group_id}"
        session_kind = "group"
    else:
        session_id = f"private:{sender_id}"
        session_name = sender_name or f"QQ {sender_id}"
        session_kind = "private"

    return ChatMessage(
        session_id=session_id,
        session_name=session_name,
        session_kind=session_kind,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        timestamp=timestamp,
        raw_event=event,
    )


def message_to_text(message: Any, raw_message: Any = None) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = [_segment_to_text(segment) for segment in message]
        rendered = "".join(part for part in parts if part)
        return rendered or str(raw_message or "")
    if raw_message is not None:
        return str(raw_message)
    if message is None:
        return ""
    return str(message)


def _segment_to_text(segment: Any) -> str:
    if isinstance(segment, str):
        return segment
    if not isinstance(segment, dict):
        return str(segment)

    segment_type = segment.get("type")
    data = segment.get("data") or {}

    if segment_type == "text":
        return str(data.get("text", ""))
    if segment_type == "at":
        return f"@{data.get('qq') or data.get('user_id') or ''}"
    labels = {
        "face": "表情",
        "image": "图片",
        "record": "语音",
        "video": "视频",
        "reply": "回复",
        "json": "JSON 消息",
        "xml": "XML 消息",
        "file": "文件",
    }
    label = labels.get(str(segment_type), str(segment_type or "未知消息"))
    return f"[{label}]"


def _event_time(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value))
    except Exception:
        return datetime.now()


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


class NapCatClientThread(QThread):
    connected = Signal()
    disconnected = Signal(str)
    message_received = Signal(object)
    log = Signal(str)

    def __init__(self, websocket_url: str, token: str = "") -> None:
        super().__init__()
        self.worker = NapCatWorker(websocket_url, token)
        self.worker.moveToThread(self)
        self.started.connect(self.worker.run)
        self.finished.connect(self.worker.deleteLater)
        self.worker.connected.connect(self.connected)
        self.worker.disconnected.connect(self.disconnected)
        self.worker.message_received.connect(self.message_received)
        self.worker.log.connect(self.log)

    def stop(self) -> None:
        self.worker.stop()
        self.quit()
        self.wait(2000)
