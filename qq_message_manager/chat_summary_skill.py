from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from .models import ChatMessage
from .skill_library_feature import SUMMARY_SKILL_ID, is_skill_enabled

SUMMARY_SKILL_PATH = Path(__file__).resolve().parent / "skills" / SUMMARY_SKILL_ID / "SKILL.md"
SUMMARY_DEFAULT_COUNT = 200
SUMMARY_OUTPUT_CHUNK_SIZE = 1800
SUMMARY_REQUEST_RE = re.compile(
    r"^\s*(?:请|帮我|麻烦(?:你)?|能不能|可以)?\s*"
    r"(?:总结|概括|汇总)(?:一下|下)?(?P<body>.*)$",
    re.IGNORECASE,
)
COUNT_RE = re.compile(r"(?P<count>\d{1,4}|[零〇一二两三四五六七八九十百千万]+)\s*条")
RECENT_COUNT_RE = re.compile(
    r"(?:最近|前)\s*(?P<count>\d{1,4}|[零〇一二两三四五六七八九十百千万]+)(?:\s*条)?"
)
FILTER_MARKER_RE = re.compile(
    r"(?:只看|仅看|只总结|仅总结|只关注|仅关注|筛选|过滤)"
    r"(?:人员|用户|发言人|成员)?(?:为|是|：|:)?\s*(?P<people>.+)$",
    re.IGNORECASE,
)
SUMMARY_CONTEXT_WORDS = (
    "最近",
    "前",
    "聊天",
    "消息",
    "对话",
    "记录",
    "发言",
    "本群",
    "群聊",
    "私聊",
    "条",
    "全部",
    "所有人",
    "只看",
    "仅看",
    "只总结",
    "仅总结",
    "过滤",
    "筛选",
)
GENERIC_PERSON_WORDS = {
    "",
    "所有人",
    "全部",
    "大家",
    "全员",
    "本群",
    "群聊",
    "私聊",
    "聊天",
    "消息",
    "对话",
    "记录",
    "发言",
    "内容",
}


@dataclass(slots=True)
class ParsedSummaryRequest:
    count: int = SUMMARY_DEFAULT_COUNT
    people: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SummaryDelivery:
    source: str
    requester_id: str = ""
    people: list[str] = field(default_factory=list)
    exclude_message_id: str = ""
    exclude_sender_id: str = ""
    exclude_text: str = ""
    exclude_timestamp: int = 0


class SummarySkillBridge(QObject):
    ready = Signal(str, str, str, int, str, str)
    failed = Signal(str, str, str, str)


def install_chat_summary_skill(
    ui_module: Any,
    summary_module: Any,
    ai_summary_module: Any,
) -> None:
    """把聊天总结变成可加载 Skill，并支持 count、人员过滤和发送结果。"""
    _install_summary_prompt(ai_summary_module)
    _install_summary_runtime(ui_module, summary_module, ai_summary_module)


def _install_summary_prompt(ai_summary_module: Any) -> None:
    if getattr(ai_summary_module, "_chat_summary_skill_prompt_installed", False):
        return
    original_builder = ai_summary_module._build_summary_messages

    def build_summary_messages_with_skill(
        session_name: str,
        session_kind: str,
        messages: list[ChatMessage],
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list[dict[str, str]]:
        prompt_messages = original_builder(
            session_name,
            session_kind,
            messages,
            start_time,
            end_time,
        )
        skill_text = _load_summary_skill_text()
        if skill_text and prompt_messages:
            prompt_messages[0]["content"] += (
                "\n\n【聊天总结 Skill】\n"
                + skill_text
                + "\n【聊天总结 Skill 结束】"
            )
        return prompt_messages

    ai_summary_module._build_summary_messages = build_summary_messages_with_skill
    ai_summary_module._chat_summary_skill_prompt_installed = True


def _install_summary_runtime(ui_module: Any, summary_module: Any, ai_summary_module: Any) -> None:
    main_window_cls = ui_module.MainWindow
    if getattr(main_window_cls, "_chat_summary_skill_installed", False):
        return

    original_init = main_window_cls.__init__
    current_mention_handler = main_window_cls._maybe_schedule_mention_reply
    current_schedule_handler = main_window_cls._schedule_after_non_self_message_ai_reply
    current_disconnect = main_window_cls.disconnect_from_server

    def init_with_summary_skill(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.chat_summary_skill_consumed_sessions: set[str] = set()
        self.chat_summary_deliveries: dict[str, SummaryDelivery] = {}
        self.chat_summary_skill_bridge = SummarySkillBridge(self)
        self.chat_summary_skill_bridge.ready.connect(
            lambda session_id, title, summary, count, people_label, source: _deliver_summary(
                self,
                session_id,
                title,
                summary,
                count,
                people_label,
                source,
            )
        )
        self.chat_summary_skill_bridge.failed.connect(
            lambda session_id, title, error, source: _deliver_summary_error(
                self,
                session_id,
                title,
                error,
                source,
            )
        )

    def mention_handler_with_summary_skill(self: Any, message: ChatMessage) -> None:
        parsed = parse_summary_request(message)
        if (
            parsed is None
            or not is_skill_enabled(self.settings, SUMMARY_SKILL_ID)
            or message.session_id not in self.ai_managed_sessions
        ):
            current_mention_handler(self, message)
            return

        self.chat_summary_skill_consumed_sessions.add(message.session_id)
        delivery = SummaryDelivery(
            source="chat",
            requester_id=message.sender_id,
            people=parsed.people,
            exclude_message_id=message.message_id,
            exclude_sender_id=message.sender_id,
            exclude_text=(message.text or "").strip(),
            exclude_timestamp=int(message.timestamp.timestamp()),
        )
        _start_summary_request(
            self,
            ui_module,
            summary_module,
            session_id=message.session_id,
            count=parsed.count,
            people=parsed.people,
            delivery=delivery,
        )

    def schedule_without_summary_duplicate(self: Any, session_id: str) -> None:
        consumed = getattr(self, "chat_summary_skill_consumed_sessions", set())
        if session_id in consumed:
            consumed.discard(session_id)
            self._stop_ai_timer(session_id)
            return
        current_schedule_handler(self, session_id)

    def disconnect_with_summary_skill_clear(self: Any) -> None:
        getattr(self, "chat_summary_deliveries", {}).clear()
        getattr(self, "chat_summary_skill_consumed_sessions", set()).clear()
        current_disconnect(self)

    def open_summary_dialog(window: Any, _ui_module: Any) -> None:
        if not is_skill_enabled(window.settings, SUMMARY_SKILL_ID):
            QMessageBox.information(
                window,
                "聊天总结 Skill 未加载",
                "请先在 AI 设置的 Skill 库中加载“聊天总结”。",
            )
            return
        session_id = getattr(window, "current_session_id", None)
        if not session_id or session_id not in window.sessions:
            QMessageBox.information(window, "请选择会话", "请先选择一个群聊或私聊。")
            return
        session = window.sessions[session_id]
        if session.kind not in {"group", "private"}:
            QMessageBox.information(window, "无法总结", "当前会话类型不支持总结。")
            return
        if window.client_thread is None:
            QMessageBox.warning(window, "未连接", "当前未连接 NapCatQQ，无法读取历史消息。")
            return
        config = ui_module.load_ai_config(window.settings).normalized()
        if not config.api_key:
            QMessageBox.warning(window, "缺少 API Key", "请先在 AI 设置中填写 API Key。")
            return

        dialog = SummarySkillSettingsDialog(
            window.settings,
            session.name,
            session.kind,
            summary_module.SUMMARY_MAX_MESSAGES_LIMIT,
            window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start_time, end_time, count, people = dialog.values()
        _start_summary_request(
            window,
            ui_module,
            summary_module,
            session_id=session_id,
            count=count,
            people=people,
            start_time=start_time,
            end_time=end_time,
            delivery=SummaryDelivery(source="button", people=people),
        )

    def handle_summary_history(window: Any, _ui_module: Any, payload: Any) -> None:
        if not isinstance(payload, dict) or not payload.get("summary_history"):
            return
        request_id = str(payload.get("request_id") or "")
        request = window.chat_summary_pending.pop(request_id, None)
        delivery = window.chat_summary_deliveries.pop(request_id, SummaryDelivery(source="button"))
        if request is None:
            return
        error = str(payload.get("error") or "")
        if error:
            window.chat_summary_skill_bridge.failed.emit(
                request.session_id,
                request.session_name,
                error,
                delivery.source,
            )
            return

        fetched_messages = payload.get("messages") or []
        messages = summary_module._merge_messages(
            fetched_messages,
            window.messages.get(request.session_id, []),
        )
        messages = _exclude_trigger_message(messages, delivery)
        messages = summary_module._filter_summary_messages(
            messages,
            request.start_time,
            request.end_time,
            summary_module.SUMMARY_MAX_MESSAGES_LIMIT,
        )
        if delivery.people:
            messages = _filter_messages_by_people(messages, delivery.people)
        if len(messages) > request.max_messages:
            messages = messages[-request.max_messages :]

        if not messages:
            scope = f"（过滤人员：{'、'.join(delivery.people)}）" if delivery.people else ""
            window.chat_summary_skill_bridge.failed.emit(
                request.session_id,
                request.session_name,
                f"指定范围内没有可总结的消息{scope}。",
                delivery.source,
            )
            return

        config = ui_module.load_ai_config(window.settings).normalized()
        people_label = "、".join(delivery.people)
        window.append_log(
            f"已读取 {len(messages)} 条消息，正在调用聊天总结 Skill"
            + (f"；仅总结 {people_label}" if people_label else "")
        )

        def worker() -> None:
            try:
                summary = ai_summary_module.generate_chat_summary(
                    config,
                    session_name=request.session_name,
                    session_kind=request.session_kind,
                    messages=messages,
                    start_time=request.start_time,
                    end_time=request.end_time,
                )
                window.chat_summary_skill_bridge.ready.emit(
                    request.session_id,
                    request.session_name,
                    summary,
                    len(messages),
                    people_label,
                    delivery.source,
                )
            except Exception as exc:  # noqa: BLE001
                window.chat_summary_skill_bridge.failed.emit(
                    request.session_id,
                    request.session_name,
                    f"AI 总结失败：{exc}",
                    delivery.source,
                )

        threading.Thread(target=worker, daemon=True).start()

    main_window_cls.__init__ = init_with_summary_skill
    main_window_cls._maybe_schedule_mention_reply = mention_handler_with_summary_skill
    main_window_cls._schedule_after_non_self_message_ai_reply = schedule_without_summary_duplicate
    main_window_cls.disconnect_from_server = disconnect_with_summary_skill_clear
    summary_module._open_summary_dialog = open_summary_dialog
    summary_module._handle_summary_history = handle_summary_history
    main_window_cls._chat_summary_skill_installed = True


def _start_summary_request(
    window: Any,
    ui_module: Any,
    summary_module: Any,
    *,
    session_id: str,
    count: int,
    people: list[str],
    delivery: SummaryDelivery,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> None:
    session = window.sessions.get(session_id)
    if session is None or session.kind not in {"group", "private"}:
        _deliver_summary_error(window, session_id, "当前会话", "当前会话类型不支持总结。", delivery.source)
        return
    if window.client_thread is None:
        _deliver_summary_error(window, session_id, session.name, "当前未连接，无法读取历史消息。", delivery.source)
        return
    config = ui_module.load_ai_config(window.settings).normalized()
    if not config.api_key:
        _deliver_summary_error(window, session_id, session.name, "未配置 API Key，无法执行聊天总结。", delivery.source)
        return

    count = max(1, min(int(count), summary_module.SUMMARY_MAX_MESSAGES_LIMIT))
    request_id = summary_module._new_request_id()
    request = summary_module.SummaryRequest(
        request_id=request_id,
        session_id=session_id,
        session_name=session.name,
        session_kind=session.kind,
        start_time=start_time,
        end_time=end_time,
        max_messages=count,
    )
    window.chat_summary_pending[request_id] = request
    window.chat_summary_deliveries[request_id] = delivery

    fetch_count = count
    if people:
        fetch_count = min(summary_module.SUMMARY_MAX_MESSAGES_LIMIT, max(count, count * 4))
    people_label = "、".join(people)
    window.append_log(
        f"聊天总结 Skill 正在读取 {session.name} 最近最多 {fetch_count} 条历史消息"
        + (f"，筛选 {people_label} 后最多总结 {count} 条" if people_label else f"，最多总结 {count} 条")
    )
    window.client_thread.request_summary_history(request_id, session_id, fetch_count)


def parse_summary_request(message: ChatMessage) -> ParsedSummaryRequest | None:
    text = _request_text(message)
    match = SUMMARY_REQUEST_RE.match(text)
    if match is None:
        return None
    body = (match.group("body") or "").strip(" ：:，,。.!！?？\t\r\n")
    at_people = _at_people(message)
    if body and not at_people and not any(word in body for word in SUMMARY_CONTEXT_WORDS):
        return None

    count = _extract_count(body)
    people = _extract_people(body)
    for person in at_people:
        if person not in people:
            people.append(person)
    return ParsedSummaryRequest(count=count, people=people[:20])


def _request_text(message: ChatMessage) -> str:
    event = message.raw_event or {}
    segments = event.get("message")
    if isinstance(segments, list):
        parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data") or {}
            parts.append(str(data.get("text") or ""))
        text = "".join(parts)
    else:
        text = str(event.get("raw_message") or event.get("rawMessage") or message.text or "")
        text = re.sub(r"\[CQ:at,[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"@(?:all|\d+|我)\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _at_people(message: ChatMessage) -> list[str]:
    event = message.raw_event or {}
    self_id = str(event.get("self_id") or event.get("selfId") or "")
    result: list[str] = []
    segments = event.get("message")
    if not isinstance(segments, list):
        return result
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        target = str(data.get("qq") or data.get("user_id") or data.get("userId") or "").strip()
        if not target or target == "all" or target == self_id:
            continue
        if target not in result:
            result.append(target)
    return result


def _extract_count(body: str) -> int:
    match = COUNT_RE.search(body) or RECENT_COUNT_RE.search(body)
    if match is None:
        return SUMMARY_DEFAULT_COUNT
    raw = match.group("count")
    value = int(raw) if raw.isdigit() else _chinese_number(raw)
    return max(1, min(value or SUMMARY_DEFAULT_COUNT, 1000))


def _extract_people(body: str) -> list[str]:
    candidates: list[str] = []
    marker = FILTER_MARKER_RE.search(body)
    if marker is not None:
        candidates.extend(_split_people(marker.group("people")))

    prefix = re.search(
        r"^(?P<people>.+?)(?:的)?(?:最近|前)\s*"
        r"(?:\d{1,4}|[零〇一二两三四五六七八九十百千万]+)?\s*条?",
        body,
        flags=re.IGNORECASE,
    )
    if prefix is not None:
        candidates.extend(_split_people(prefix.group("people")))

    suffix = re.search(
        r"(?:最近|前)\s*(?:\d{1,4}|[零〇一二两三四五六七八九十百千万]+)\s*条?\s*"
        r"(?P<people>.+?)(?:的)?(?:消息|发言|对话|记录|内容)(?:$|[，,。；;])",
        body,
        flags=re.IGNORECASE,
    )
    if suffix is not None:
        candidates.extend(_split_people(suffix.group("people")))

    result: list[str] = []
    for person in candidates:
        normalized = person.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:20]


def _split_people(value: str) -> list[str]:
    cleaned = value.strip()
    cleaned = re.sub(
        r"(?:，|,)?\s*(?:最近|前)\s*"
        r"(?:\d{1,4}|[零〇一二两三四五六七八九十百千万]+)?\s*条?.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(?:的)?(?:消息|发言|对话|记录|内容|聊天)\s*$", "", cleaned)
    parts = re.split(r"[、，,；;/]|\s+(?:和|与|及)\s+|(?:和|与|及)", cleaned)
    result: list[str] = []
    for part in parts:
        person = part.strip(" \t\r\n@'\"“”‘’()（）[]【】")
        person = re.sub(r"^(?:只看|仅看|只总结|仅总结|只关注|仅关注|筛选|过滤)\s*", "", person)
        person = person.strip()
        if person in GENERIC_PERSON_WORDS or not person:
            continue
        if len(person) > 40:
            continue
        result.append(person)
    return result


def _filter_messages_by_people(messages: list[ChatMessage], people: list[str]) -> list[ChatMessage]:
    normalized_people = [(_normalize_person(value), value) for value in people]
    result: list[ChatMessage] = []
    for message in messages:
        sender_id = _normalize_person(message.sender_id)
        sender_name = _normalize_person(message.sender_name)
        matched = False
        for token, _original in normalized_people:
            if not token:
                continue
            if token.isdigit():
                matched = token == sender_id
            else:
                matched = token == sender_name
                if not matched and len(token) >= 2 and len(sender_name) >= 2:
                    matched = token in sender_name or sender_name in token
            if matched:
                break
        if matched:
            result.append(message)
    return result


def _normalize_person(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.lstrip("@").strip()
    text = re.sub(r"[\s_\-·•]+", "", text)
    return text


def _exclude_trigger_message(messages: list[ChatMessage], delivery: SummaryDelivery) -> list[ChatMessage]:
    result: list[ChatMessage] = []
    removed = False
    for message in messages:
        if not removed and delivery.exclude_message_id and message.message_id == delivery.exclude_message_id:
            removed = True
            continue
        if (
            not removed
            and delivery.exclude_text
            and message.sender_id == delivery.exclude_sender_id
            and (message.text or "").strip() == delivery.exclude_text
            and abs(int(message.timestamp.timestamp()) - delivery.exclude_timestamp) <= 3
        ):
            removed = True
            continue
        result.append(message)
    return result


def _deliver_summary(
    window: Any,
    session_id: str,
    title: str,
    summary: str,
    count: int,
    people_label: str,
    source: str,
) -> None:
    session = window.sessions.get(session_id)
    if session is None or window.client_thread is None:
        return
    scope = f"｜仅：{people_label}" if people_label else ""
    header = f"【聊天总结｜{count} 条{scope}】"
    payload = f"{header}\n{summary.strip()}".strip()
    chunks = _split_output(payload, SUMMARY_OUTPUT_CHUNK_SIZE)
    for chunk in chunks:
        window.client_thread.send_text(session_id, chunk)
        window.add_message(
            ChatMessage(
                session_id=session.session_id,
                session_name=session.name,
                session_kind=session.kind,
                sender_id="self",
                sender_name="AI代管",
                text=chunk,
                outgoing=True,
            )
        )
    window.append_log(
        f"已完成 {title} 的聊天总结并发送（{count} 条消息"
        + (f"，仅 {people_label}" if people_label else "")
        + f"，{len(chunks)} 段）"
    )


def _deliver_summary_error(
    window: Any,
    session_id: str,
    title: str,
    error: str,
    source: str,
) -> None:
    window.append_log(f"聊天总结 Skill 失败：{error}")
    if source == "button":
        QMessageBox.warning(window, f"总结失败 · {title}", error)
        return
    session = window.sessions.get(session_id)
    if session is None or window.client_thread is None:
        return
    text = f"聊天总结失败：{error}"
    window.client_thread.send_text(session_id, text)
    window.add_message(
        ChatMessage(
            session_id=session.session_id,
            session_name=session.name,
            session_kind=session.kind,
            sender_id="self",
            sender_name="AI代管",
            text=text,
            outgoing=True,
        )
    )


def _split_output(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in text.splitlines():
        candidate = f"{current}\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def _load_summary_skill_text() -> str:
    try:
        return SUMMARY_SKILL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _chinese_number(value: str) -> int:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    if all(char in digits for char in value):
        try:
            return int("".join(str(digits[char]) for char in value))
        except ValueError:
            return 0
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return 0
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number


class SummarySkillSettingsDialog(QDialog):
    def __init__(
        self,
        settings: Any,
        session_name: str,
        session_kind: str,
        maximum: int,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("聊天总结 Skill")
        self.setMinimumWidth(560)
        kind_label = "群聊" if session_kind == "group" else "私聊"

        self.start_enabled = QCheckBox("限制开始时间")
        self.end_enabled = QCheckBox("限制结束时间")
        now = QDateTime.currentDateTime()
        self.start_input = QDateTimeEdit(now.addDays(-1))
        self.end_input = QDateTimeEdit(now)
        for widget in (self.start_input, self.end_input):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            widget.setEnabled(False)
        self.start_enabled.toggled.connect(self.start_input.setEnabled)
        self.end_enabled.toggled.connect(self.end_input.setEnabled)

        self.max_messages = QSpinBox()
        self.max_messages.setRange(1, maximum)
        self.max_messages.setValue(_setting_int(settings, "summary/max_messages", SUMMARY_DEFAULT_COUNT))
        self.people_input = QLineEdit()
        self.people_input.setPlaceholderText("留空表示所有人；可填昵称或 QQ 号，用逗号分隔")

        form = QFormLayout()
        form.addRow("会话", QLabel(f"{kind_label} · {session_name}"))
        form.addRow("最近消息数", self.max_messages)
        form.addRow("仅总结这些人", self.people_input)
        form.addRow("开始", self.start_enabled)
        form.addRow("开始时间", self.start_input)
        form.addRow("结束", self.end_enabled)
        form.addRow("结束时间", self.end_input)

        tip = QLabel(
            "默认总结最近 200 条。人员过滤支持群昵称和 QQ 号；指定人员后，程序会多读取一些历史消息，"
            "再筛选出这些人的发言，最多总结上面设置的条数。总结完成后会直接发送到当前会话。"
        )
        tip.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(tip)
        layout.addWidget(buttons)

    def values(self) -> tuple[datetime | None, datetime | None, int, list[str]]:
        count = self.max_messages.value()
        self.settings.setValue("summary/max_messages", count)
        self.settings.sync()
        start_time = _qdatetime_to_datetime(self.start_input.dateTime()) if self.start_enabled.isChecked() else None
        end_time = _qdatetime_to_datetime(self.end_input.dateTime()) if self.end_enabled.isChecked() else None
        people = _split_people(self.people_input.text())
        return start_time, end_time, count, people


def _qdatetime_to_datetime(value: QDateTime) -> datetime:
    return datetime.fromtimestamp(value.toSecsSinceEpoch())


def _setting_int(settings: Any, key: str, default: int) -> int:
    value = settings.value(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
