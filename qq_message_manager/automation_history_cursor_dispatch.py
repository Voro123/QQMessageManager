from __future__ import annotations

import asyncio
import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

CURSOR_HISTORY_PREFIX = "automation_cursor_history|"


def install_automation_history_cursor_dispatch(
    automation_module: Any,
    reliability_module: Any,
    napcat_module: Any,
    ui_module: Any,
) -> None:
    """Read scheduled history forward from the committed cursor.

    Older compatibility patches moved selected messages into the scheduled time
    window by changing ``ChatMessage.timestamp``.  That made diagnostics and AI
    context report the task cutoff instead of the message's real timestamp.
    This layer keeps timestamps untouched and carries selection separately.
    """

    _install_cursor_history_request(napcat_module)
    _install_window_cursor_resolver(automation_module, ui_module)
    _install_cursor_native_dispatch(automation_module, reliability_module, ui_module)


def _install_cursor_history_request(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_automation_cursor_history_request_installed", False):
        return

    original_worker_handle = worker_cls._handle_action_response
    original_thread_request = thread_cls.request_automation_history

    def request_automation_history_from_cursor(
        self: Any,
        request_id: str,
        session_id: str,
        count: int,
        anchor_message_id: str,
        cursor_real_seq: str,
    ) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            self.history_messages_received.emit(
                {
                    "automation_history": True,
                    "request_id": str(request_id),
                    "session_id": str(session_id),
                    "messages": [],
                    "error": "当前未连接 NapCatQQ",
                    "history_cursor_request": True,
                }
            )
            return
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._request_automation_history_from_cursor(
                    str(request_id),
                    str(session_id),
                    int(count),
                    str(anchor_message_id),
                    str(cursor_real_seq),
                )
            )
        )

    async def request_history_from_cursor(
        self: Any,
        request_id: str,
        session_id: str,
        count: int,
        anchor_message_id: str,
        cursor_real_seq: str,
    ) -> None:
        kind, target_id = napcat_module._split_session_id(session_id)
        if kind not in {"group", "private"} or not target_id or not anchor_message_id:
            # Let the existing latest-message anchor handle first-run migration
            # and malformed legacy state.
            self.request_automation_history(request_id, session_id, count)
            return

        state = {
            "request_id": request_id,
            "session_id": session_id,
            "count": max(20, min(int(count), 5000)),
            "anchor_message_id": anchor_message_id,
            "cursor_real_seq": cursor_real_seq,
        }
        requests = _cursor_requests(self)
        requests[request_id] = state
        common = {
            "message_seq": anchor_message_id,
            "count": state["count"],
            # Starting at the last committed message, ask NapCat for the newer
            # direction. The committed record itself may be returned and is
            # removed later with real_seq > cursor_real_seq.
            "reverse_order": True,
            "disable_get_url": True,
            "parse_mult_msg": False,
            "quick_reply": True,
        }
        if kind == "group":
            action = "get_group_msg_history"
            params = {"group_id": napcat_module._onebot_id(target_id), **common}
        else:
            action = "get_friend_msg_history"
            params = {"user_id": napcat_module._onebot_id(target_id), **common}
        await self._send_action(
            action,
            params,
            self._next_echo(f"{CURSOR_HISTORY_PREFIX}{request_id}|{session_id}"),
        )

    def handle_action_response(self: Any, event: dict[str, Any]) -> bool:
        echo_text = str(event.get("echo") or "")
        if not echo_text.startswith(CURSOR_HISTORY_PREFIX):
            return original_worker_handle(self, event)

        request_id, session_id = _parse_echo(echo_text, CURSOR_HISTORY_PREFIX)
        state = _cursor_requests(self).pop(request_id, None)
        if state is None:
            return True
        ok = event.get("status") == "ok" or event.get("retcode") == 0
        if not ok:
            self.log.emit(
                f"定时任务按序号游标读取失败，回退到最新消息锚点：{session_id}；"
                f"{napcat_module._action_error(event)}"
            )
            # Existing anchoring logic remains the fallback. It will emit the
            # normal automation_history payload and the dispatch layer below
            # still keeps original timestamps untouched.
            self.request_automation_history(
                request_id,
                session_id,
                int(state.get("count") or 20),
            )
            return True

        kind, _target_id = napcat_module._split_session_id(session_id)
        messages = napcat_module._extract_history_messages(event.get("data"), session_id, kind)
        self.history_messages_received.emit(
            {
                "automation_history": True,
                "request_id": request_id,
                "session_id": session_id,
                "messages": messages,
                "error": "",
                "history_cursor_request": True,
                "history_cursor_real_seq": str(state.get("cursor_real_seq") or ""),
                "history_cursor_anchor_message_id": str(
                    state.get("anchor_message_id") or ""
                ),
            }
        )
        self.log.emit(
            f"定时任务已从序号游标 {state.get('cursor_real_seq') or '?'} "
            f"向更新方向读取 {session_id} 的 {len(messages)} 条历史消息"
        )
        return True

    def thread_request_automation_history(
        self: Any,
        request_id: str,
        session_id: str,
        count: int,
    ) -> None:
        resolver = getattr(self, "_automation_history_cursor_resolver", None)
        resolved = resolver(str(request_id), str(session_id)) if callable(resolver) else None
        if isinstance(resolved, dict):
            anchor_message_id = str(resolved.get("anchor_message_id") or "").strip()
            cursor_real_seq = str(resolved.get("cursor_real_seq") or "").strip()
            if anchor_message_id and _sequence_number(cursor_real_seq) is not None:
                self.worker.request_automation_history_from_cursor(
                    str(request_id),
                    str(session_id),
                    int(count),
                    anchor_message_id,
                    cursor_real_seq,
                )
                return
        original_thread_request(self, request_id, session_id, count)

    worker_cls.request_automation_history_from_cursor = request_automation_history_from_cursor
    worker_cls._request_automation_history_from_cursor = request_history_from_cursor
    worker_cls._handle_action_response = handle_action_response
    thread_cls.request_automation_history = thread_request_automation_history
    worker_cls._automation_cursor_history_request_installed = True


def _install_window_cursor_resolver(automation_module: Any, ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_automation_cursor_resolver_installed", False):
        return
    original_start = main_window_cls.start

    def start_with_cursor_resolver(self: Any, *args: Any, **kwargs: Any) -> None:
        original_start(self, *args, **kwargs)
        client = getattr(self, "client_thread", None)
        if client is None:
            return

        def resolve(request_id: str, _session_id: str) -> dict[str, str] | None:
            context = getattr(self, "automation_pending", {}).get(str(request_id))
            if context is None:
                return None
            state_store = getattr(self, "automation_state", None)
            if state_store is None:
                return None
            state = state_store.state(str(getattr(context, "task_id", "") or ""))
            cursor = str(state.get("checkpoint_real_seq") or "").strip()
            anchor = str(state.get("checkpoint_message_id") or "").strip()
            if _sequence_number(cursor) is None or not anchor:
                return None
            return {
                "cursor_real_seq": cursor,
                "anchor_message_id": anchor,
            }

        client._automation_history_cursor_resolver = resolve

    main_window_cls.start = start_with_cursor_resolver
    main_window_cls._automation_cursor_resolver_installed = True


def _install_cursor_native_dispatch(
    automation_module: Any,
    reliability_module: Any,
    ui_module: Any,
) -> None:
    if getattr(automation_module, "_automation_cursor_native_dispatch_installed", False):
        return
    original_handler = automation_module._handle_automation_payload

    def handle_cursor_native(
        window: Any,
        ui_module_arg: Any,
        ai_module: Any,
        payload: Any,
    ) -> None:
        if (
            not isinstance(payload, dict)
            or not payload.get("automation_history")
            or payload.get("error")
        ):
            original_handler(window, ui_module_arg, ai_module, payload)
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
            original_handler(window, ui_module_arg, ai_module, payload)
            return

        # Consume the request here, before any older compatibility wrapper can
        # move timestamps into the scheduling window.
        window.automation_pending.pop(request_id, None)
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
        state = window.automation_state.state(task.task_id)
        stored_cursor = _sequence_number(state.get("checkpoint_real_seq"))
        payload_cursor = _sequence_number(payload.get("history_cursor_real_seq"))
        cursor = payload_cursor if payload_cursor is not None else stored_cursor

        sequence_pairs = [
            (sequence, message)
            for message in fetched
            for sequence in [_message_sequence(message)]
            if sequence is not None
        ]
        selected_pairs: list[tuple[int, Any]] = []
        selection_mode = ""
        if cursor is not None:
            selected_pairs = [
                (sequence, message)
                for sequence, message in sequence_pairs
                if sequence > cursor
            ]
            selection_mode = f"序号游标 {cursor}"
        elif sequence_pairs:
            # First migration: trust real timestamps only for choosing rows. If
            # the returned page is stale, establish a baseline without pretending
            # those messages happened at the current task cutoff.
            selected_pairs = [
                (sequence, message)
                for sequence, message in sequence_pairs
                if context.checkpoint <= message.timestamp <= context.cutoff
            ]
            selection_mode = "首次序号基线"

        selected_by_key = {
            automation_module.message_key(message): message
            for _sequence, message in sorted(selected_pairs, key=lambda item: item[0])
        }
        # Live WebSocket messages without usable sequence metadata still use the
        # real scheduling time window as a fallback.
        for message in sorted(current, key=lambda item: item.timestamp):
            if context.checkpoint <= message.timestamp <= context.cutoff:
                selected_by_key.setdefault(automation_module.message_key(message), message)

        ordered_messages = list(selected_by_key.values())
        keys = [automation_module.message_key(message) for message in ordered_messages]
        processed = window.automation_state.processed_keys(task.task_id, keys)
        messages = [
            message
            for message in ordered_messages
            if automation_module.message_key(message) not in processed
        ]

        commit_sequence: int | None = None
        if selected_pairs:
            commit_sequence = max(sequence for sequence, _message in selected_pairs)
        elif cursor is None and sequence_pairs:
            # A stale first page is still useful as a starting cursor. The next
            # run reads forward from this baseline and catches newer records.
            commit_sequence = max(sequence for sequence, _message in sequence_pairs)
        elif cursor is not None:
            commit_sequence = cursor
        if commit_sequence is not None:
            reliability_module._SEQUENCE_TO_COMMIT[task.task_id] = str(commit_sequence)

        raw_times = [
            message.timestamp
            for message in fetched
            if isinstance(getattr(message, "timestamp", None), datetime)
            and message.timestamp != datetime.min
        ]
        sequence_values = [sequence for sequence, _message in sequence_pairs]
        detail = (
            f"定时任务“{task.name}”历史增量筛选：方式 {selection_mode or '真实时间窗口'}，"
            f"接口返回 {len(fetched)} 条，按序号选中 {len(selected_pairs)} 条，"
            f"实时缓存补充 {max(0, len(selected_by_key) - len(selected_pairs))} 条，"
            f"已处理 {len(processed)} 条，待处理 {len(messages)} 条"
        )
        if raw_times:
            detail += (
                f"；NapCat 原始解析时间 {min(raw_times):%Y-%m-%d %H:%M:%S.%f}"
                f" ～ {max(raw_times):%Y-%m-%d %H:%M:%S.%f}"
            )
        if sequence_values:
            detail += f"；消息序号 {min(sequence_values)} ～ {max(sequence_values)}"
        if commit_sequence is not None:
            detail += f"；成功后提交序号 {commit_sequence}"
        window.append_log(detail)

        if messages:
            window.append_log(
                f"定时任务“{task.name}”已取得 {len(messages)} 条未处理消息，正在调用 AI"
            )
        elif not context.delivery:
            window.append_log(
                f"定时任务“{task.name}”本轮没有新消息，仍按计划调用 AI 执行指令"
            )
        else:
            window.append_log(
                f"定时任务“{task.name}”没有新增消息，正在准备每日文件归档"
            )

        config = ui_module.load_ai_config(window.settings).normalized()

        def worker() -> None:
            try:
                work_date = automation_module.task_work_date(context.cutoff, context.delivery)
                path = (
                    automation_module.artifact_path(task, work_date)
                    if task.file_enabled
                    else None
                )
                existing = automation_module.load_records(path) if path is not None else []
                result = None
                if messages or not context.delivery:
                    result = automation_module.generate_scheduled_result(
                        ai_module,
                        config,
                        task,
                        messages,
                        automation_module.records_for_ai(existing),
                        checkpoint_time=context.checkpoint,
                        cutoff_time=context.cutoff,
                    )
                stats = {"inserted": 0, "updated": 0, "ignored": 0}
                if task.file_enabled:
                    if result is not None:
                        existing, stats = automation_module.apply_operations(
                            task,
                            existing,
                            result.operations,
                        )
                    path = automation_module.write_artifact(task, work_date, existing)

                checkpoint_message_id = ""
                if selected_pairs:
                    checkpoint_message_id = str(
                        max(selected_pairs, key=lambda item: item[0])[1].message_id or ""
                    )
                window.automation_bridge.ready.emit(
                    {
                        "context": context,
                        "task_id": task.task_id,
                        "messages": messages,
                        "message_keys": [
                            automation_module.message_key(message) for message in messages
                        ],
                        "checkpoint_message_id": checkpoint_message_id,
                        "text": result.text if result is not None else "",
                        "stats": stats,
                        "path": str(Path(path)) if path is not None else "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                window.automation_bridge.failed.emit(
                    {
                        "context": context,
                        "task_id": task.task_id,
                        "error": str(exc),
                    }
                )

        threading.Thread(target=worker, daemon=True).start()

    automation_module._handle_automation_payload = handle_cursor_native
    automation_module._automation_cursor_native_dispatch_installed = True


def _cursor_requests(worker: Any) -> dict[str, dict[str, Any]]:
    requests = getattr(worker, "_qqmm_automation_cursor_history_requests", None)
    if not isinstance(requests, dict):
        requests = {}
        worker._qqmm_automation_cursor_history_requests = requests
    return requests


def _parse_echo(echo_text: str, prefix: str) -> tuple[str, str]:
    stable = echo_text.rsplit(":", 1)[0]
    body = stable[len(prefix) :]
    request_id, separator, session_id = body.partition("|")
    return (request_id, session_id) if separator else ("", "")


def _message_sequence(message: Any) -> int | None:
    raw_event = getattr(message, "raw_event", None)
    if not isinstance(raw_event, dict):
        return None
    for key in (
        "real_seq",
        "realSeq",
        "msgSeq",
        "msg_seq",
        "message_seq",
        "messageSeq",
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
