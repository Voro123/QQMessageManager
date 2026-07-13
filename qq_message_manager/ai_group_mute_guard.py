from __future__ import annotations

import json
import weakref
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, Signal


class GroupMuteBridge(QObject):
    changed = Signal(str, bool, int)


_BRIDGE_REFS: list[weakref.ReferenceType[GroupMuteBridge]] = []


def parse_bot_group_mute_notice(
    event: Any,
    known_self_id: str = "",
) -> tuple[str, bool, int] | None:
    """Return ``(group_id, muted, duration_seconds)`` for bot mute notices."""
    if not isinstance(event, dict):
        return None
    post_type = str(event.get("post_type") or event.get("postType") or "").strip().lower()
    if post_type != "notice":
        return None

    notice_type = str(event.get("notice_type") or event.get("noticeType") or "").strip().lower()
    if notice_type not in {
        "group_ban",
        "group_ban_notice",
        "group_whole_ban",
        "group_whole_ban_notice",
    }:
        return None

    group_id = str(event.get("group_id") or event.get("groupId") or "").strip()
    if not group_id:
        return None

    self_id = str(event.get("self_id") or event.get("selfId") or known_self_id or "").strip()
    target_id = str(
        event.get("user_id")
        or event.get("userId")
        or event.get("target_id")
        or event.get("targetId")
        or ""
    ).strip()
    whole_group = notice_type.startswith("group_whole_ban") or target_id.lower() in {"0", "all"}
    if not whole_group and (not self_id or target_id != self_id):
        return None

    subtype = str(event.get("sub_type") or event.get("subType") or "").strip().lower()
    try:
        duration = max(0, int(event.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0

    if subtype in {"lift_ban", "unban", "disable", "off"}:
        muted = False
    elif subtype in {"ban", "enable", "on"}:
        muted = True
    elif "enable" in event:
        muted = bool(event.get("enable"))
    else:
        muted = duration > 0
    return group_id, muted, duration


def install_ai_group_mute_guard(
    ui_module: Any,
    napcat_module: Any,
    speaking_style_module: Any | None = None,
) -> None:
    """Prevent quota-consuming conversational AI work while the bot is muted."""
    _install_notice_observer(napcat_module)
    _install_window_guard(ui_module)
    if speaking_style_module is not None:
        _install_style_learning_guard(speaking_style_module)


def _install_notice_observer(napcat_module: Any) -> None:
    worker_cls = napcat_module.NapCatWorker
    if getattr(worker_cls, "_qqmm_group_mute_notice_installed", False):
        return
    original_handle_payload = worker_cls._handle_payload

    def handle_payload_with_group_mute(self: Any, payload: str | bytes) -> None:
        event: dict[str, Any] | None = None
        try:
            decoded = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                event = parsed
                observed_self_id = str(parsed.get("self_id") or parsed.get("selfId") or "").strip()
                if observed_self_id:
                    self._qqmm_known_self_id = observed_self_id
        except Exception:
            event = None

        if event is not None:
            notice = parse_bot_group_mute_notice(
                event,
                str(getattr(self, "_qqmm_known_self_id", "") or ""),
            )
            if notice is not None:
                _emit_group_mute(*notice)
        original_handle_payload(self, payload)

    worker_cls._handle_payload = handle_payload_with_group_mute
    worker_cls._qqmm_group_mute_notice_installed = True


def _emit_group_mute(group_id: str, muted: bool, duration: int) -> None:
    live_refs: list[weakref.ReferenceType[GroupMuteBridge]] = []
    for bridge_ref in list(_BRIDGE_REFS):
        bridge = bridge_ref()
        if bridge is None:
            continue
        live_refs.append(bridge_ref)
        bridge.changed.emit(str(group_id), bool(muted), int(duration))
    _BRIDGE_REFS[:] = live_refs


def _install_window_guard(ui_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_qqmm_group_mute_guard_installed", False):
        return

    original_init = main_window_cls.__init__
    original_schedule = main_window_cls._schedule_after_non_self_message_ai_reply
    original_mention = main_window_cls._maybe_schedule_mention_reply
    original_request = main_window_cls._request_ai_reply
    original_ready = main_window_cls._handle_ai_reply_ready

    def init_with_group_mute_guard(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self._qqmm_muted_group_until: dict[str, datetime | None] = {}
        self._qqmm_group_mute_bridge = GroupMuteBridge(self)
        self._qqmm_group_mute_bridge.changed.connect(self._qqmm_apply_group_mute)
        _BRIDGE_REFS.append(weakref.ref(self._qqmm_group_mute_bridge))

    def apply_group_mute(self: Any, group_id: str, muted: bool, duration: int) -> None:
        session_id = f"group:{group_id}"
        if muted:
            until = datetime.now() + timedelta(seconds=duration) if duration > 0 else None
            self._qqmm_muted_group_until[session_id] = until
            self._stop_ai_timer(session_id)
            suffix = f"，预计 {duration} 秒后解除" if duration > 0 else ""
            self.append_log(f"AI 代管暂停：机器人在 {session_id} 被禁言{suffix}，禁言期间不会调用模型")
            if session_id in getattr(self, "ai_inflight_sessions", set()):
                self.append_log("该会话已有模型请求正在进行，已产生的额度无法追回；返回结果将被丢弃")
        else:
            was_muted = session_id in self._qqmm_muted_group_until
            self._qqmm_muted_group_until.pop(session_id, None)
            if was_muted:
                self.append_log(f"AI 代管恢复：机器人在 {session_id} 的禁言已解除")

    def is_group_muted(self: Any, session_id: str) -> bool:
        if not str(session_id).startswith("group:"):
            return False
        states = getattr(self, "_qqmm_muted_group_until", {})
        if session_id not in states:
            return False
        until = states.get(session_id)
        if until is not None and datetime.now() >= until:
            states.pop(session_id, None)
            self.append_log(f"AI 代管恢复：{session_id} 的本地禁言计时已结束")
            return False
        return True

    def schedule_with_mute_guard(self: Any, session_id: str) -> None:
        if self._qqmm_is_group_muted(session_id):
            self._stop_ai_timer(session_id)
            return
        original_schedule(self, session_id)

    def mention_with_mute_guard(self: Any, message: Any) -> None:
        session_id = str(getattr(message, "session_id", "") or "")
        if self._qqmm_is_group_muted(session_id):
            self._stop_ai_timer(session_id)
            return
        original_mention(self, message)

    def request_with_mute_guard(self: Any, session_id: str, reason: str) -> None:
        if self._qqmm_is_group_muted(session_id):
            self._stop_ai_timer(session_id)
            self.append_log(f"AI 代管跳过：{reason}，机器人当前在该群被禁言，未调用模型")
            return
        original_request(self, session_id, reason)

    def ready_with_mute_guard(self: Any, session_id: str, reply: str) -> None:
        if self._qqmm_is_group_muted(session_id):
            getattr(self, "ai_inflight_sessions", set()).discard(session_id)
            self.append_log("AI 代管丢弃已完成回复：机器人当前在该群被禁言")
            return
        original_ready(self, session_id, reply)

    main_window_cls._qqmm_apply_group_mute = apply_group_mute
    main_window_cls._qqmm_is_group_muted = is_group_muted
    main_window_cls.__init__ = init_with_group_mute_guard
    main_window_cls._schedule_after_non_self_message_ai_reply = schedule_with_mute_guard
    main_window_cls._maybe_schedule_mention_reply = mention_with_mute_guard
    main_window_cls._request_ai_reply = request_with_mute_guard
    main_window_cls._handle_ai_reply_ready = ready_with_mute_guard
    main_window_cls._qqmm_group_mute_guard_installed = True


def _install_style_learning_guard(speaking_style_module: Any) -> None:
    learner_cls = speaking_style_module.SpeakingStyleLearner
    if getattr(learner_cls, "_qqmm_group_mute_guard_installed", False):
        return
    original_observe = learner_cls.observe

    def observe_with_group_mute_guard(self: Any, message: Any) -> None:
        session_id = str(getattr(message, "session_id", "") or "")
        checker = getattr(self.window, "_qqmm_is_group_muted", None)
        if callable(checker) and checker(session_id):
            return
        original_observe(self, message)

    learner_cls.observe = observe_with_group_mute_guard
    learner_cls._qqmm_group_mute_guard_installed = True
