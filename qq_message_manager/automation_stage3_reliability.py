from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .automation_models import TRANSFER_AUTO, TRANSFER_LOCAL, TRANSFER_STREAM, task_by_id

UPLOAD_CONFIRM_TIMEOUT_SECONDS = 300


def install_automation_stage3_reliability(
    automation_module: Any,
    transfer_module: Any,
    feature_module: Any,
    napcat_module: Any,
) -> None:
    """处理 Stream 中间响应、上传确认超时和发送记录文件大小。"""
    _ignore_stream_progress_events(transfer_module, napcat_module)
    _install_upload_cancel(napcat_module)
    _install_delivery_size_repair(automation_module)
    _install_upload_confirmation_watchdog(automation_module, feature_module)
    feature_module._effective_transfer_mode = _effective_transfer_mode


def _ignore_stream_progress_events(transfer_module: Any, napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    if getattr(worker_cls, "_automation_stream_progress_guard_installed", False):
        return
    original_handle = worker_cls._handle_action_response

    def handle_with_progress_guard(self: Any, event: dict[str, Any]) -> bool:
        echo_text = str(event.get("echo") or "")
        if echo_text.startswith(transfer_module.STREAM_PREFIX):
            ok = event.get("status") == "ok" or event.get("retcode") == 0
            if ok and _is_stream_progress(event.get("data")):
                # Stream API 可能使用同一个 echo 先上报分片进度，再上报最终 response。
                return True
        return original_handle(self, event)

    worker_cls._handle_action_response = handle_with_progress_guard
    worker_cls._automation_stream_progress_guard_installed = True


def _install_upload_cancel(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    thread_cls = napcat_module.NapCatClientThread
    if getattr(worker_cls, "_automation_upload_cancel_installed", False):
        return

    def cancel_automation_upload(self: Any, upload_id: str) -> None:
        def cancel() -> None:
            uploads = getattr(self, "_automation_stream_uploads", {})
            if isinstance(uploads, dict):
                uploads.pop(str(upload_id), None)

        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(cancel)
        else:
            cancel()

    def thread_cancel_automation_upload(self: Any, upload_id: str) -> None:
        self.worker.cancel_automation_upload(upload_id)

    worker_cls.cancel_automation_upload = cancel_automation_upload
    thread_cls.cancel_automation_upload = thread_cancel_automation_upload
    worker_cls._automation_upload_cancel_installed = True


def _install_delivery_size_repair(automation_module: Any) -> None:
    state_cls = automation_module.AutomationStateStore
    if getattr(state_cls, "_automation_delivery_size_repair_installed", False):
        return

    def repair_latest_delivery_size(
        self: Any,
        task_id: str,
        file_name: str,
        file_size: int,
    ) -> None:
        if not task_id or not file_name or file_size <= 0:
            return
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM delivery_history
                WHERE task_id = ? AND file_name = ?
                ORDER BY id DESC LIMIT 1
                """,
                (task_id, file_name),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE delivery_history SET file_size = ? WHERE id = ?",
                    (int(file_size), int(row[0])),
                )

    state_cls.repair_latest_delivery_size = repair_latest_delivery_size
    state_cls._automation_delivery_size_repair_installed = True


def _install_upload_confirmation_watchdog(automation_module: Any, feature_module: Any) -> None:
    if getattr(automation_module, "_automation_upload_watchdog_installed", False):
        return
    original_ready = automation_module._handle_execution_ready
    original_payload = automation_module._handle_automation_payload
    original_tick = automation_module._automation_tick
    original_test = feature_module._test_selected_delivery

    def ready_with_upload_start(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        before = set(getattr(window, "automation_uploads", {}))
        original_ready(window, ui_module, ai_module, payload)
        after = set(getattr(window, "automation_uploads", {}))
        starts = _upload_starts(window)
        now = datetime.now()
        for upload_id in after - before:
            starts[upload_id] = now

    def test_with_upload_start(*args: Any, **kwargs: Any) -> None:
        dialog = args[0] if args else None
        window = getattr(dialog, "window", None)
        before = set(getattr(window, "automation_test_uploads", {})) if window is not None else set()
        original_test(*args, **kwargs)
        if window is None:
            return
        after = set(getattr(window, "automation_test_uploads", {}))
        starts = _upload_starts(window)
        now = datetime.now()
        for upload_id in after - before:
            starts[upload_id] = now

    def payload_with_upload_cleanup(window: Any, ui_module: Any, ai_module: Any, payload: Any) -> None:
        captured: dict[str, Any] | None = None
        if isinstance(payload, dict) and payload.get("automation_upload"):
            upload_id = str(payload.get("upload_id") or "")
            _upload_starts(window).pop(upload_id, None)
            upload = getattr(window, "automation_uploads", {}).get(upload_id)
            if upload is not None:
                task = task_by_id(getattr(window, "automation_tasks", []), upload.run.task_id)
                path = Path(str(upload.path))
                try:
                    size = path.stat().st_size if path.is_file() else 0
                except OSError:
                    size = 0
                captured = {
                    "task_id": task.task_id if task is not None else "",
                    "file_name": path.name,
                    "file_size": size,
                }
        original_payload(window, ui_module, ai_module, payload)
        if captured is not None:
            try:
                window.automation_state.repair_latest_delivery_size(
                    captured["task_id"],
                    captured["file_name"],
                    captured["file_size"],
                )
            except Exception as exc:  # noqa: BLE001
                window.append_log(f"修复文件发送记录大小失败：{exc}")

    def tick_with_upload_timeout(window: Any, ui_module: Any, ai_module: Any) -> None:
        now = datetime.now()
        starts = _upload_starts(window)
        active = set(getattr(window, "automation_uploads", {})) | set(
            getattr(window, "automation_test_uploads", {})
        )
        # 上传重试会直接从重试队列创建上下文，不经过 ready 回调；在这里统一接管。
        for upload_id in active:
            starts.setdefault(upload_id, now)
        for upload_id in list(starts):
            if upload_id not in active:
                starts.pop(upload_id, None)
                continue
            started = starts.get(upload_id)
            if not isinstance(started, datetime):
                starts[upload_id] = now
                continue
            if now - started < timedelta(seconds=UPLOAD_CONFIRM_TIMEOUT_SECONDS):
                continue
            starts.pop(upload_id, None)
            client = getattr(window, "client_thread", None)
            if client is not None and hasattr(client, "cancel_automation_upload"):
                client.cancel_automation_upload(upload_id)
            automation_module._handle_automation_payload(
                window,
                ui_module,
                ai_module,
                {
                    "automation_upload": True,
                    "upload_id": upload_id,
                    "ok": False,
                    "error": f"等待 NapCat 文件上传确认超过 {UPLOAD_CONFIRM_TIMEOUT_SECONDS} 秒",
                },
            )
        original_tick(window, ui_module, ai_module)

    automation_module._handle_execution_ready = ready_with_upload_start
    automation_module._handle_automation_payload = payload_with_upload_cleanup
    automation_module._automation_tick = tick_with_upload_timeout
    feature_module._test_selected_delivery = test_with_upload_start
    automation_module._automation_upload_watchdog_installed = True


def _upload_starts(window: Any) -> dict[str, datetime]:
    starts = getattr(window, "automation_upload_started_at", None)
    if not isinstance(starts, dict):
        starts = {}
        window.automation_upload_started_at = starts
    return starts


def _is_stream_progress(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    response_type = str(value.get("type") or "").strip().lower()
    data_type = str(value.get("data_type") or "").strip().lower()
    if response_type == "stream" or data_type in {"data_chunk", "stream"}:
        return True
    nested = value.get("data")
    return _is_stream_progress(nested) if isinstance(nested, dict) else False


def _effective_transfer_mode(window: Any, task: Any) -> str:
    configured = str(getattr(task, "file_transfer_mode", TRANSFER_AUTO) or TRANSFER_AUTO)
    if configured in {TRANSFER_LOCAL, TRANSFER_STREAM}:
        return configured
    client = getattr(window, "client_thread", None)
    worker = getattr(client, "worker", None)
    websocket_url = str(getattr(worker, "websocket_url", ""))
    host = (urlparse(websocket_url).hostname or "").strip().lower()
    if host in {"localhost", "localhost.localdomain"}:
        return TRANSFER_LOCAL
    try:
        return TRANSFER_LOCAL if ipaddress.ip_address(host).is_loopback else TRANSFER_STREAM
    except ValueError:
        return TRANSFER_STREAM
