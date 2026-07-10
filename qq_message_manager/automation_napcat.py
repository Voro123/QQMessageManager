from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

AUTOMATION_HISTORY_PREFIX = "automation_history|"
AUTOMATION_UPLOAD_PREFIX = "automation_upload|"
AUTOMATION_LOGIN_PREFIX = "automation_login"


def install_automation_napcat(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_automation_actions_installed", False):
        return

    original_handle_action_response = worker_cls._handle_action_response

    def request_automation_history(self: Any, request_id: str, session_id: str, count: int) -> None:
        count = max(20, min(int(count), 5000))
        if self._loop is None or not self._loop.is_running():
            self.history_messages_received.emit(
                {
                    "automation_history": True,
                    "request_id": request_id,
                    "session_id": session_id,
                    "messages": [],
                    "error": "当前未连接 NapCatQQ",
                }
            )
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._request_automation_history(request_id, session_id, count))
        )

    async def _request_automation_history(self: Any, request_id: str, session_id: str, count: int) -> None:
        kind, target_id = napcat_module._split_session_id(session_id)
        if not target_id:
            self.history_messages_received.emit(
                {
                    "automation_history": True,
                    "request_id": request_id,
                    "session_id": session_id,
                    "messages": [],
                    "error": "无效会话",
                }
            )
            return
        common_params = {
            "count": count,
            # Scheduled analysis only needs message content and metadata. Avoid
            # resolving old attachment URLs and nested forwards, which can make
            # NapCat perform additional lookups against expired message records.
            "disable_get_url": True,
            "parse_mult_msg": False,
            "quick_reply": True,
            "reverse_order": False,
        }
        if kind == "group":
            action = "get_group_msg_history"
            params = {
                "group_id": napcat_module._onebot_id(target_id),
                **common_params,
            }
        elif kind == "private":
            action = "get_friend_msg_history"
            params = {
                "user_id": napcat_module._onebot_id(target_id),
                **common_params,
            }
        else:
            self.history_messages_received.emit(
                {
                    "automation_history": True,
                    "request_id": request_id,
                    "session_id": session_id,
                    "messages": [],
                    "error": "定时任务仅支持群聊或私聊",
                }
            )
            return
        prefix = f"{AUTOMATION_HISTORY_PREFIX}{request_id}|{session_id}"
        await self._send_action(action, params, self._next_echo(prefix))

    def request_automation_login_info(self: Any) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._send_action("get_login_info", {}, self._next_echo(AUTOMATION_LOGIN_PREFIX))
            )
        )

    def upload_automation_file(
        self: Any,
        upload_id: str,
        user_id: str,
        file_path: str,
        file_name: str,
    ) -> None:
        if self._loop is None or not self._loop.is_running():
            self.history_messages_received.emit(
                {
                    "automation_upload": True,
                    "upload_id": upload_id,
                    "ok": False,
                    "error": "当前未连接 NapCatQQ",
                }
            )
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._upload_automation_file(upload_id, user_id, file_path, file_name)
            )
        )

    async def _upload_automation_file(
        self: Any,
        upload_id: str,
        user_id: str,
        file_path: str,
        file_name: str,
    ) -> None:
        path = Path(file_path).resolve()
        if not path.is_file():
            self.history_messages_received.emit(
                {
                    "automation_upload": True,
                    "upload_id": upload_id,
                    "ok": False,
                    "error": "待发送文件不存在",
                }
            )
            return
        await self._send_action(
            "upload_private_file",
            {
                "user_id": napcat_module._onebot_id(str(user_id)),
                "file": str(path),
                "name": file_name or path.name,
            },
            self._next_echo(f"{AUTOMATION_UPLOAD_PREFIX}{upload_id}"),
        )

    def handle_action_response(self: Any, event: dict[str, Any]) -> bool:
        echo = event.get("echo")
        echo_text = str(echo) if echo else ""
        ok = event.get("status") == "ok" or event.get("retcode") == 0

        if echo_text.startswith(AUTOMATION_HISTORY_PREFIX):
            stable = echo_text.rsplit(":", 1)[0]
            body = stable[len(AUTOMATION_HISTORY_PREFIX) :]
            request_id, separator, session_id = body.partition("|")
            if not separator:
                request_id, session_id = "", ""
            if ok:
                kind, _target_id = napcat_module._split_session_id(session_id)
                messages = napcat_module._extract_history_messages(event.get("data"), session_id, kind)
                self.history_messages_received.emit(
                    {
                        "automation_history": True,
                        "request_id": request_id,
                        "session_id": session_id,
                        "messages": messages,
                        "error": "",
                    }
                )
                self.log.emit(f"定时任务已读取 {session_id} 的 {len(messages)} 条历史消息")
            else:
                error = f"定时任务读取历史失败：{napcat_module._action_error(event)}"
                self.history_messages_received.emit(
                    {
                        "automation_history": True,
                        "request_id": request_id,
                        "session_id": session_id,
                        "messages": [],
                        "error": error,
                    }
                )
            return True

        if echo_text.startswith(AUTOMATION_UPLOAD_PREFIX):
            stable = echo_text.rsplit(":", 1)[0]
            upload_id = stable[len(AUTOMATION_UPLOAD_PREFIX) :]
            self.history_messages_received.emit(
                {
                    "automation_upload": True,
                    "upload_id": upload_id,
                    "ok": ok,
                    "error": "" if ok else napcat_module._action_error(event),
                    "data": event.get("data"),
                }
            )
            self.log.emit("定时任务文件已发送" if ok else f"定时任务文件发送失败：{napcat_module._action_error(event)}")
            return True

        if echo_text.startswith(AUTOMATION_LOGIN_PREFIX):
            data = event.get("data") or {}
            self.history_messages_received.emit(
                {
                    "automation_login_info": True,
                    "ok": ok,
                    "user_id": str(data.get("user_id") or data.get("uin") or "") if isinstance(data, dict) else "",
                    "nickname": str(data.get("nickname") or "") if isinstance(data, dict) else "",
                    "error": "" if ok else napcat_module._action_error(event),
                }
            )
            return True

        return original_handle_action_response(self, event)

    def thread_request_automation_history(self: Any, request_id: str, session_id: str, count: int) -> None:
        self.worker.request_automation_history(request_id, session_id, count)

    def thread_request_automation_login_info(self: Any) -> None:
        self.worker.request_automation_login_info()

    def thread_upload_automation_file(
        self: Any,
        upload_id: str,
        user_id: str,
        file_path: str,
        file_name: str,
    ) -> None:
        self.worker.upload_automation_file(upload_id, user_id, file_path, file_name)

    worker_cls.request_automation_history = request_automation_history
    worker_cls._request_automation_history = _request_automation_history
    worker_cls.request_automation_login_info = request_automation_login_info
    worker_cls.upload_automation_file = upload_automation_file
    worker_cls._upload_automation_file = _upload_automation_file
    worker_cls._handle_action_response = handle_action_response
    thread_cls.request_automation_history = thread_request_automation_history
    thread_cls.request_automation_login_info = thread_request_automation_login_info
    thread_cls.upload_automation_file = thread_upload_automation_file
    worker_cls._automation_actions_installed = True
