from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from .models import ChatMessage

LOGGER = logging.getLogger(__name__)
RECENT_SESSION_LIMIT = 20
HISTORY_MESSAGE_LIMIT = 20


class NapCatWorker(QObject):
    """WebSocket worker running inside a QThread."""

    connected = Signal()
    disconnected = Signal(str)
    message_received = Signal(object)
    history_messages_received = Signal(object)
    session_name_updated = Signal(str, str)
    log = Signal(str)

    def __init__(self, websocket_url: str, token: str = "", reconnect_interval: float = 3.0) -> None:
        super().__init__()
        self.websocket_url = websocket_url.strip()
        self.token = token.strip()
        self.reconnect_interval = reconnect_interval
        self._stop_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current_websocket: Any | None = None
        self._echo_index = 0
        self._pending_group_info: set[str] = set()
        self._group_names: dict[str, str] = {}
        self._history_requested: set[str] = set()

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

    def send_text(self, session_id: str, text: str) -> None:
        if not text.strip():
            return
        if self._loop is None or not self._loop.is_running():
            self.log.emit("当前未连接，无法发送消息")
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send_text(session_id, text)))

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
                self.log.emit(f"{self.reconnect_interval:g} 秒后尝试重新连接")
                await asyncio.sleep(self.reconnect_interval)

    async def _connect_once(self) -> None:
        import websockets

        headers = self._build_headers()
        self.log.emit(f"正在连接 {self.websocket_url}")
        websocket = await self._open_websocket(websockets, self.websocket_url, headers)
        self._current_websocket = websocket
        self._pending_group_info.clear()
        self._history_requested.clear()
        try:
            async with websocket:
                self.connected.emit()
                self.log.emit("已连接 NapCatQQ WebSocket")
                await self._request_recent_sessions()
                async for payload in websocket:
                    if self._stop_requested:
                        break
                    self._handle_payload(payload)
        finally:
            self._current_websocket = None
        if not self._stop_requested:
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

        if self._handle_action_response(event):
            return

        message = normalize_message_event(event)
        if message is not None:
            self._enrich_group_message(message)
            self.message_received.emit(message)

    def _handle_action_response(self, event: dict[str, Any]) -> bool:
        echo = event.get("echo")
        if not echo:
            return False

        echo_text = str(echo)
        ok = event.get("status") == "ok" or event.get("retcode") == 0

        if echo_text.startswith("recent_sessions:"):
            if ok:
                contacts = _extract_recent_contacts(event.get("data"))
                if not contacts:
                    self.log.emit("未能从最近会话接口解析到会话；等待新消息时仍会正常显示。")
                for contact in contacts[:RECENT_SESSION_LIMIT]:
                    self._request_history(contact["session_id"], contact["kind"], contact.get("name", ""))
                self.log.emit(f"已请求最近 {min(len(contacts), RECENT_SESSION_LIMIT)} 个会话的历史消息")
            else:
                self.log.emit(f"获取最近会话失败：{_action_error(event)}")
            return True

        if echo_text.startswith("history:"):
            if ok:
                session_id = _echo_session_id(echo_text, "history")
                kind, _ = _split_session_id(session_id)
                messages = _extract_history_messages(event.get("data"), session_id, kind)
                if messages:
                    self.history_messages_received.emit(messages[-HISTORY_MESSAGE_LIMIT:])
                    self.log.emit(f"已加载 {session_id} 的 {len(messages[-HISTORY_MESSAGE_LIMIT:])} 条历史消息")
                else:
                    self.log.emit(f"{session_id} 没有解析到历史消息")
            else:
                self.log.emit(f"获取历史消息失败：{_action_error(event)}")
            return True

        if echo_text.startswith("get_group_info:"):
            group_id = echo_text.split(":", 2)[1]
            self._pending_group_info.discard(group_id)
            if ok:
                data = event.get("data") or {}
                group_name = _first_text(data.get("group_name"), data.get("group_remark"), f"群聊 {group_id}")
                self._group_names[group_id] = group_name
                self.session_name_updated.emit(f"group:{group_id}", group_name)
            else:
                self.log.emit(f"获取群信息失败：{_action_error(event)}")
            return True

        if echo_text.startswith("send_msg:"):
            if ok:
                self.log.emit("消息已发送")
            else:
                self.log.emit(f"消息发送失败：{_action_error(event)}")
            return True

        return True

    async def _request_recent_sessions(self) -> None:
        await self._send_action(
            "get_recent_contact",
            {"count": RECENT_SESSION_LIMIT},
            self._next_echo("recent_sessions"),
        )

    def _request_history(self, session_id: str, kind: str, name: str = "") -> None:
        if session_id in self._history_requested:
            return
        self._history_requested.add(session_id)
        if name:
            self.session_name_updated.emit(session_id, name)

        _, target_id = _split_session_id(session_id)
        if not target_id:
            return

        if kind == "group":
            action = "get_group_msg_history"
            params = {"group_id": _onebot_id(target_id), "count": HISTORY_MESSAGE_LIMIT}
        elif kind == "private":
            action = "get_friend_msg_history"
            params = {"user_id": _onebot_id(target_id), "count": HISTORY_MESSAGE_LIMIT}
        else:
            return

        asyncio.create_task(self._send_action(action, params, self._next_echo(f"history:{session_id}")))

    def _enrich_group_message(self, message: ChatMessage) -> None:
        if message.session_kind != "group":
            return

        group_id = _session_target_id(message.session_id)
        if not group_id:
            return

        cached_name = self._group_names.get(group_id)
        if cached_name:
            message.session_name = cached_name
            return

        event_name = _first_text(message.raw_event.get("group_name"), message.raw_event.get("group_remark"))
        if event_name:
            self._group_names[group_id] = event_name
            message.session_name = event_name
            self.session_name_updated.emit(message.session_id, event_name)
            return

        self._request_group_info(group_id)

    def _request_group_info(self, group_id: str) -> None:
        if group_id in self._pending_group_info:
            return
        self._pending_group_info.add(group_id)
        echo = self._next_echo(f"get_group_info:{group_id}")
        asyncio.create_task(
            self._send_action(
                "get_group_info",
                {"group_id": _onebot_id(group_id), "no_cache": False},
                echo,
            )
        )

    async def _send_text(self, session_id: str, text: str) -> None:
        kind, target_id = _split_session_id(session_id)
        if kind == "group":
            await self._send_action(
                "send_group_msg",
                {"group_id": _onebot_id(target_id), "message": text},
                self._next_echo(f"send_msg:{session_id}"),
            )
        elif kind == "private":
            await self._send_action(
                "send_private_msg",
                {"user_id": _onebot_id(target_id), "message": text},
                self._next_echo(f"send_msg:{session_id}"),
            )
        else:
            self.log.emit("当前会话不支持发送消息")

    async def _send_action(self, action: str, params: dict[str, Any], echo: str) -> None:
        websocket = self._current_websocket
        if websocket is None:
            self.log.emit("当前未连接，无法发送请求")
            return

        payload = {"action": action, "params": params, "echo": echo}
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            self.log.emit(f"发送请求失败：{exc}")

    def _next_echo(self, prefix: str) -> str:
        self._echo_index += 1
        return f"{prefix}:{self._echo_index}"


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
    message_id = str(event.get("message_id") or event.get("messageId") or event.get("msg_id") or "")

    if message_type == "group":
        group_id = str(event.get("group_id") or "未知群")
        session_id = f"group:{group_id}"
        session_name = _first_text(event.get("group_name"), event.get("group_remark"), f"群聊 {group_id}")
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
        message_id=message_id,
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


def _extract_recent_contacts(data: Any) -> list[dict[str, str]]:
    items = _extract_list(data, ("items", "list", "contacts", "recent", "rows", "data"))
    contacts: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        kind = _contact_kind(item)
        target_id = _contact_target_id(item, kind)
        if not kind or not target_id:
            continue
        session_id = f"{kind}:{target_id}"
        if session_id in seen:
            continue
        seen.add(session_id)
        contacts.append(
            {
                "kind": kind,
                "target_id": target_id,
                "session_id": session_id,
                "name": _contact_name(item, kind, target_id),
            }
        )
    return contacts


def _contact_kind(item: dict[str, Any]) -> str:
    if _first_text(item.get("group_id"), item.get("groupId"), item.get("group_uin"), item.get("groupUin")):
        return "group"

    raw_kind = _first_text(
        item.get("type"),
        item.get("chat_type"),
        item.get("chatType"),
        item.get("peerType"),
        item.get("msgType"),
        item.get("contactType"),
    ).lower()

    if raw_kind in {"group", "group_chat", "troop", "2", "群", "群聊"} or "group" in raw_kind:
        return "group"
    if raw_kind in {"private", "friend", "c2c", "user", "0", "1", "好友", "私聊"} or "friend" in raw_kind:
        return "private"
    return "private" if _first_text(item.get("user_id"), item.get("userId"), item.get("uin"), item.get("peerUin")) else ""


def _contact_target_id(item: dict[str, Any], kind: str) -> str:
    if kind == "group":
        return _first_text(
            item.get("group_id"),
            item.get("groupId"),
            item.get("group_uin"),
            item.get("groupUin"),
            item.get("peerUin"),
            item.get("peer_uin"),
            item.get("uin"),
        )
    return _first_text(
        item.get("user_id"),
        item.get("userId"),
        item.get("user_uin"),
        item.get("userUin"),
        item.get("peerUin"),
        item.get("peer_uin"),
        item.get("uin"),
    )


def _contact_name(item: dict[str, Any], kind: str, target_id: str) -> str:
    fallback = f"群聊 {target_id}" if kind == "group" else f"QQ {target_id}"
    return _first_text(
        item.get("group_name"),
        item.get("groupName"),
        item.get("peerName"),
        item.get("remark"),
        item.get("nick"),
        item.get("nickname"),
        item.get("name"),
        fallback,
    )


def _extract_history_messages(data: Any, session_id: str, kind: str) -> list[ChatMessage]:
    items = _extract_list(data, ("messages", "msg_list", "msgList", "list", "items", "data", "rows"))
    messages = [
        message
        for item in items
        if isinstance(item, dict)
        for message in [_history_item_to_message(item, session_id, kind)]
        if message is not None
    ]
    messages.sort(key=lambda message: message.timestamp)
    return messages


def _history_item_to_message(item: dict[str, Any], session_id: str, kind: str) -> ChatMessage | None:
    session_kind = "group" if kind == "group" else "private"
    target_id = _session_target_id(session_id)
    sender = item.get("sender") or item.get("sender_info") or item.get("senderInfo") or {}
    if not isinstance(sender, dict):
        sender = {}
    sender_id = _first_text(
        item.get("user_id"),
        item.get("userId"),
        item.get("sender_uin"),
        item.get("senderUin"),
        sender.get("user_id"),
        sender.get("userId"),
        sender.get("uin"),
        "未知用户",
    )
    sender_name = _first_text(
        sender.get("card"),
        sender.get("nickname"),
        item.get("sendNickName"),
        item.get("senderName"),
        sender_id,
    )
    text = message_to_text(
        item.get("message") or item.get("elements") or item.get("messageList"),
        item.get("raw_message") or item.get("rawMessage") or item.get("msg") or item.get("msgText"),
    )
    if not text:
        text = "[历史消息]"
    session_name = _first_text(
        item.get("group_name"),
        item.get("groupName"),
        item.get("peerName"),
        f"群聊 {target_id}" if session_kind == "group" else f"QQ {target_id}",
    )
    return ChatMessage(
        session_id=session_id,
        session_name=session_name,
        session_kind=session_kind,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        timestamp=_event_time(item.get("time") or item.get("msgTime") or item.get("timestamp")),
        raw_event=item,
        historical=True,
        message_id=str(item.get("message_id") or item.get("messageId") or item.get("msg_id") or item.get("msgId") or item.get("msgSeq") or item.get("seq") or ""),
    )


def _extract_list(data: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _extract_list(value, keys)
            if nested:
                return nested
    return []


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


def _action_error(event: dict[str, Any]) -> str:
    return str(event.get("message") or event.get("wording") or event.get("retcode") or "未知错误")


def _split_session_id(session_id: str) -> tuple[str, str]:
    if ":" not in session_id:
        return session_id, ""
    kind, target_id = session_id.split(":", 1)
    return kind, target_id


def _session_target_id(session_id: str) -> str:
    return _split_session_id(session_id)[1]


def _echo_session_id(echo_text: str, prefix: str) -> str:
    body = echo_text[len(prefix) + 1 :]
    return ":".join(body.split(":")[:-1]) if ":" in body else body


def _onebot_id(value: str) -> int | str:
    return int(value) if value.isdigit() else value


class NapCatClientThread(QThread):
    connected = Signal()
    disconnected = Signal(str)
    message_received = Signal(object)
    history_messages_received = Signal(object)
    session_name_updated = Signal(str, str)
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
        self.worker.history_messages_received.connect(self.history_messages_received)
        self.worker.session_name_updated.connect(self.session_name_updated)
        self.worker.log.connect(self.log)

    def send_text(self, session_id: str, text: str) -> None:
        self.worker.send_text(session_id, text)

    def stop(self) -> None:
        self.worker.stop()
        self.quit()
        self.wait(4000)
