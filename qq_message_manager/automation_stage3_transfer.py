from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import math
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .automation_models import TRANSFER_AUTO, TRANSFER_LOCAL, TRANSFER_STREAM

STREAM_PREFIX = "automation_stream|"
FRIEND_LIST_PREFIX = "automation_friends"
VERSION_PREFIX = "automation_version"
STREAM_CHUNK_SIZE = 64 * 1024
STREAM_RETENTION_MS = 5 * 60 * 1000


def install_automation_stage3_transfer(napcat_module: Any) -> None:
    """增加跨设备 Stream API 上传、好友列表和版本探测。"""
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_automation_stage3_transfer_installed", False):
        return

    original_handle = worker_cls._handle_action_response
    original_upload = worker_cls.upload_automation_file

    def upload_automation_file(
        self: Any,
        upload_id: str,
        user_id: str,
        file_path: str,
        file_name: str,
        transfer_mode: str = TRANSFER_AUTO,
    ) -> None:
        mode = _resolved_transfer_mode(self, transfer_mode)
        if mode == TRANSFER_LOCAL:
            original_upload(self, upload_id, user_id, file_path, file_name)
            return
        if self._loop is None or not self._loop.is_running():
            _emit_upload_failure(self, upload_id, "当前未连接 NapCatQQ", mode)
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._begin_stream_upload(upload_id, user_id, file_path, file_name, mode)
            )
        )

    async def _begin_stream_upload(
        self: Any,
        upload_id: str,
        user_id: str,
        file_path: str,
        file_name: str,
        mode: str,
    ) -> None:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            _emit_upload_failure(self, upload_id, "待发送文件不存在", mode)
            return
        try:
            size, digest = await asyncio.to_thread(_file_size_and_sha256, path)
        except OSError as exc:
            _emit_upload_failure(self, upload_id, f"无法读取待发送文件：{exc}", mode)
            return
        total_chunks = max(1, math.ceil(size / STREAM_CHUNK_SIZE))
        state = {
            "upload_id": upload_id,
            "user_id": str(user_id),
            "path": path,
            "file_name": file_name or path.name,
            "mode": mode,
            "stream_id": str(uuid.uuid4()),
            "size": size,
            "sha256": digest,
            "total_chunks": total_chunks,
            "next_index": 0,
            "awaiting": None,
        }
        uploads = getattr(self, "_automation_stream_uploads", None)
        if not isinstance(uploads, dict):
            uploads = {}
            self._automation_stream_uploads = uploads
        uploads[upload_id] = state
        self.log.emit(
            f"定时任务开始 Stream API 上传：{state['file_name']}，"
            f"{size} 字节，{total_chunks} 个分片"
        )
        await self._send_automation_stream_chunk(upload_id)

    async def _send_automation_stream_chunk(self: Any, upload_id: str) -> None:
        state = _stream_state(self, upload_id)
        if state is None:
            return
        index = int(state.get("next_index") or 0)
        total = int(state.get("total_chunks") or 1)
        if index >= total:
            await self._complete_automation_stream(upload_id)
            return
        try:
            chunk = await asyncio.to_thread(
                _read_chunk,
                Path(state["path"]),
                index,
                STREAM_CHUNK_SIZE,
            )
        except OSError as exc:
            _fail_stream(self, upload_id, f"读取文件分片失败：{exc}")
            return
        params = {
            "stream_id": state["stream_id"],
            "chunk_data": base64.b64encode(chunk).decode("ascii"),
            "chunk_index": index,
            "total_chunks": total,
            "file_size": state["size"],
            "expected_sha256": state["sha256"],
            "filename": state["file_name"],
            "file_retention": STREAM_RETENTION_MS,
        }
        state["awaiting"] = ("chunk", index)
        await self._send_action(
            "upload_file_stream",
            params,
            self._next_echo(f"{STREAM_PREFIX}{upload_id}|chunk|{index}"),
        )

    async def _complete_automation_stream(self: Any, upload_id: str) -> None:
        state = _stream_state(self, upload_id)
        if state is None:
            return
        state["awaiting"] = ("complete", -1)
        await self._send_action(
            "upload_file_stream",
            {"stream_id": state["stream_id"], "is_complete": True},
            self._next_echo(f"{STREAM_PREFIX}{upload_id}|complete|-1"),
        )

    def request_automation_friend_list(self: Any) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._send_action("get_friend_list", {}, self._next_echo(FRIEND_LIST_PREFIX))
            )
        )

    def request_automation_version_info(self: Any) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._send_action("get_version_info", {}, self._next_echo(VERSION_PREFIX))
            )
        )

    def handle_action_response(self: Any, event: dict[str, Any]) -> bool:
        echo_text = str(event.get("echo") or "")
        ok = event.get("status") == "ok" or event.get("retcode") == 0

        if echo_text.startswith(STREAM_PREFIX):
            stable = echo_text.rsplit(":", 1)[0]
            body = stable[len(STREAM_PREFIX) :]
            upload_id, separator, remainder = body.partition("|")
            phase, separator2, index_text = remainder.partition("|")
            if not separator or not separator2:
                return True
            state = _stream_state(self, upload_id)
            if state is None:
                return True
            expected = state.get("awaiting")
            try:
                index = int(index_text)
            except ValueError:
                index = -1
            if expected != (phase, index):
                return True
            if not ok:
                _fail_stream(self, upload_id, _action_error(napcat_module, event))
                return True
            if phase == "chunk":
                state["next_index"] = index + 1
                state["awaiting"] = None
                asyncio.create_task(self._send_automation_stream_chunk(upload_id))
                return True
            remote_path = _extract_remote_file_path(event.get("data"))
            if not remote_path:
                _fail_stream(self, upload_id, "Stream API 完成响应中没有返回远程文件路径")
                return True
            state["awaiting"] = ("private", -1)
            asyncio.create_task(
                self._send_action(
                    "upload_private_file",
                    {
                        "user_id": napcat_module._onebot_id(state["user_id"]),
                        "file": remote_path,
                        "name": state["file_name"],
                    },
                    self._next_echo(f"automation_upload|{upload_id}"),
                )
            )
            self.log.emit(f"Stream API 上传完成，正在发送私聊文件：{state['file_name']}")
            return True

        if echo_text.startswith(FRIEND_LIST_PREFIX):
            data = event.get("data") if ok else []
            friends = _normalize_friends(data)
            self.history_messages_received.emit(
                {
                    "automation_friend_list": True,
                    "ok": ok,
                    "friends": friends,
                    "error": "" if ok else _action_error(napcat_module, event),
                }
            )
            return True

        if echo_text.startswith(VERSION_PREFIX):
            data = event.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            version = str(
                data.get("app_version")
                or data.get("version")
                or data.get("app_full_name")
                or ""
            )
            self.history_messages_received.emit(
                {
                    "automation_version_info": True,
                    "ok": ok,
                    "version": version,
                    "data": data,
                    "error": "" if ok else _action_error(napcat_module, event),
                }
            )
            return True

        if echo_text.startswith("automation_upload|"):
            stable = echo_text.rsplit(":", 1)[0]
            upload_id = stable[len("automation_upload|") :]
            uploads = getattr(self, "_automation_stream_uploads", {})
            if isinstance(uploads, dict):
                uploads.pop(upload_id, None)

        return original_handle(self, event)

    def thread_upload_automation_file(
        self: Any,
        upload_id: str,
        user_id: str,
        file_path: str,
        file_name: str,
        transfer_mode: str = TRANSFER_AUTO,
    ) -> None:
        mode = transfer_mode
        if mode == TRANSFER_AUTO:
            task_id = Path(file_path).expanduser().parent.name
            mode = str(getattr(self, "automation_transfer_modes", {}).get(task_id) or TRANSFER_AUTO)
        self.worker.upload_automation_file(
            upload_id,
            user_id,
            file_path,
            file_name,
            mode,
        )

    def thread_request_automation_friend_list(self: Any) -> None:
        self.worker.request_automation_friend_list()

    def thread_request_automation_version_info(self: Any) -> None:
        self.worker.request_automation_version_info()

    worker_cls.upload_automation_file = upload_automation_file
    worker_cls._begin_stream_upload = _begin_stream_upload
    worker_cls._send_automation_stream_chunk = _send_automation_stream_chunk
    worker_cls._complete_automation_stream = _complete_automation_stream
    worker_cls.request_automation_friend_list = request_automation_friend_list
    worker_cls.request_automation_version_info = request_automation_version_info
    worker_cls._handle_action_response = handle_action_response
    thread_cls.upload_automation_file = thread_upload_automation_file
    thread_cls.request_automation_friend_list = thread_request_automation_friend_list
    thread_cls.request_automation_version_info = thread_request_automation_version_info
    worker_cls._automation_stage3_transfer_installed = True


def _resolved_transfer_mode(worker: Any, configured: str) -> str:
    if configured in {TRANSFER_LOCAL, TRANSFER_STREAM}:
        return configured
    host = (urlparse(str(getattr(worker, "websocket_url", ""))).hostname or "").strip().lower()
    if host in {"localhost", "localhost.localdomain"}:
        return TRANSFER_LOCAL
    try:
        if ipaddress.ip_address(host).is_loopback:
            return TRANSFER_LOCAL
    except ValueError:
        pass
    return TRANSFER_STREAM


def _file_size_and_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _read_chunk(path: Path, index: int, chunk_size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(index * chunk_size)
        return handle.read(chunk_size)


def _stream_state(worker: Any, upload_id: str) -> dict[str, Any] | None:
    uploads = getattr(worker, "_automation_stream_uploads", {})
    state = uploads.get(upload_id) if isinstance(uploads, dict) else None
    return state if isinstance(state, dict) else None


def _fail_stream(worker: Any, upload_id: str, error: str) -> None:
    uploads = getattr(worker, "_automation_stream_uploads", {})
    state = uploads.pop(upload_id, None) if isinstance(uploads, dict) else None
    mode = str(state.get("mode") or TRANSFER_STREAM) if isinstance(state, dict) else TRANSFER_STREAM
    _emit_upload_failure(worker, upload_id, f"Stream API 上传失败：{error}", mode)


def _emit_upload_failure(worker: Any, upload_id: str, error: str, mode: str) -> None:
    worker.history_messages_received.emit(
        {
            "automation_upload": True,
            "upload_id": upload_id,
            "ok": False,
            "error": error,
            "transfer_mode": mode,
        }
    )
    worker.log.emit(error)


def _extract_remote_file_path(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("file_path", "path", "file"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key in ("data", "result", "response"):
            candidate = _extract_remote_file_path(value.get(key))
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _extract_remote_file_path(item)
            if candidate:
                return candidate
    elif isinstance(value, str):
        text = value.strip()
        if text and ("/" in text or "\\" in text):
            return text
    return ""


def _normalize_friends(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        for key in ("friends", "data", "list", "items"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        qq = str(item.get("user_id") or item.get("uin") or item.get("qq") or "").strip()
        if not qq or qq in seen:
            continue
        seen.add(qq)
        name = str(item.get("remark") or item.get("nickname") or item.get("nick") or f"QQ {qq}").strip()
        result.append({"user_id": qq, "name": name})
    return result


def _action_error(napcat_module: Any, event: dict[str, Any]) -> str:
    try:
        return str(napcat_module._action_error(event))
    except Exception:  # noqa: BLE001
        return str(event.get("wording") or event.get("message") or "未知错误")
