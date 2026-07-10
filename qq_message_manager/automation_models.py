from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

AUTOMATION_TASKS_KEY = "automation/tasks"
SCHEDULE_INTERVAL = "interval"
SCHEDULE_DAILY = "daily"
OUTPUT_SILENT = "silent"
OUTPUT_SEND_TEXT = "send_text"
RECIPIENT_SELF = "self"
RECIPIENT_CONTACT = "contact"
RECIPIENT_MANUAL = "manual"
SUPPORTED_FILE_FORMATS = ("xlsx", "csv", "json", "md")
SCHEDULED_FILE_SKILL_ID = "scheduled_files"


@dataclass(slots=True)
class AutomationColumn:
    name: str
    value_type: str = "text"
    required: bool = False
    enum_values: list[str] = field(default_factory=list)
    default: str = ""
    ai_update: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AutomationColumn":
        return cls(
            name=str(raw.get("name") or "").strip(),
            value_type=str(raw.get("value_type") or "text").strip().lower(),
            required=bool(raw.get("required", False)),
            enum_values=[str(value).strip() for value in raw.get("enum_values", []) if str(value).strip()],
            default=str(raw.get("default") or ""),
            ai_update=bool(raw.get("ai_update", True)),
        )

    def normalized(self) -> "AutomationColumn":
        value_type = self.value_type if self.value_type in {"text", "number", "datetime", "boolean", "enum"} else "text"
        enum_values = [value for value in dict.fromkeys(self.enum_values) if value]
        if value_type == "enum" and not enum_values:
            value_type = "text"
        return AutomationColumn(
            name=" ".join(self.name.split())[:80],
            value_type=value_type,
            required=bool(self.required),
            enum_values=enum_values[:50],
            default=self.default[:500],
            ai_update=bool(self.ai_update),
        )


DEFAULT_COLUMNS = [
    AutomationColumn("时间", "datetime", required=True, ai_update=False),
    AutomationColumn("人员", "text", required=True, ai_update=False),
    AutomationColumn("内容", "text", required=True),
    AutomationColumn("状态", "enum", enum_values=["待处理", "处理中", "已完成", "忽略"], default="待处理"),
    AutomationColumn("处理结果", "text"),
]


@dataclass(slots=True)
class AutomationTask:
    task_id: str
    name: str
    enabled: bool
    schedule_type: str
    created_at: str
    next_run_at: str
    interval_seconds: int
    daily_time: str
    target_session_id: str
    target_session_name: str
    instruction: str
    output_mode: str = OUTPUT_SILENT
    file_enabled: bool = False
    file_format: str = "xlsx"
    file_name_template: str = "{date}_{task_name}.xlsx"
    sheet_name: str = "记录"
    columns: list[AutomationColumn] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    dedup_fields: list[str] = field(default_factory=list)
    daily_delivery_enabled: bool = False
    delivery_time: str = "00:00"
    next_delivery_at: str = ""
    recipient_mode: str = RECIPIENT_SELF
    recipient_qq: str = ""
    delete_after_send: bool = True
    history_limit: int = 1000
    enabled_skills: list[str] = field(default_factory=list)

    @classmethod
    def create_default(cls) -> "AutomationTask":
        now = datetime.now().replace(microsecond=0)
        task = cls(
            task_id=f"task_{uuid.uuid4().hex[:16]}",
            name="新定时任务",
            enabled=True,
            schedule_type=SCHEDULE_INTERVAL,
            created_at=now.isoformat(),
            next_run_at="",
            interval_seconds=1800,
            daily_time="09:00",
            target_session_id="",
            target_session_name="",
            instruction="检查上次执行以来的聊天内容，并按要求完成任务。",
        )
        task.recalculate_next_times(now, reset=True)
        return task

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AutomationTask":
        columns_raw = raw.get("columns")
        columns = (
            [AutomationColumn.from_dict(item).normalized() for item in columns_raw if isinstance(item, dict)]
            if isinstance(columns_raw, list)
            else list(DEFAULT_COLUMNS)
        )
        task = cls(
            task_id=str(raw.get("task_id") or f"task_{uuid.uuid4().hex[:16]}"),
            name=str(raw.get("name") or "未命名任务"),
            enabled=bool(raw.get("enabled", True)),
            schedule_type=str(raw.get("schedule_type") or SCHEDULE_INTERVAL),
            created_at=str(raw.get("created_at") or datetime.now().replace(microsecond=0).isoformat()),
            next_run_at=str(raw.get("next_run_at") or ""),
            interval_seconds=int(raw.get("interval_seconds") or 1800),
            daily_time=str(raw.get("daily_time") or "09:00"),
            target_session_id=str(raw.get("target_session_id") or ""),
            target_session_name=str(raw.get("target_session_name") or ""),
            instruction=str(raw.get("instruction") or ""),
            output_mode=str(raw.get("output_mode") or OUTPUT_SILENT),
            file_enabled=bool(raw.get("file_enabled", False)),
            file_format=str(raw.get("file_format") or "xlsx").lower(),
            file_name_template=str(raw.get("file_name_template") or "{date}_{task_name}.xlsx"),
            sheet_name=str(raw.get("sheet_name") or "记录"),
            columns=[column for column in columns if column.name] or list(DEFAULT_COLUMNS),
            dedup_fields=[str(value).strip() for value in raw.get("dedup_fields", []) if str(value).strip()],
            daily_delivery_enabled=bool(raw.get("daily_delivery_enabled", False)),
            delivery_time=str(raw.get("delivery_time") or "00:00"),
            next_delivery_at=str(raw.get("next_delivery_at") or ""),
            recipient_mode=str(raw.get("recipient_mode") or RECIPIENT_SELF),
            recipient_qq=str(raw.get("recipient_qq") or ""),
            delete_after_send=bool(raw.get("delete_after_send", True)),
            history_limit=int(raw.get("history_limit") or 1000),
            enabled_skills=[str(value).strip() for value in raw.get("enabled_skills", []) if str(value).strip()],
        )
        task.normalize()
        if not task.next_run_at or (task.daily_delivery_enabled and not task.next_delivery_at):
            task.recalculate_next_times(datetime.now(), reset=False)
        return task

    def normalize(self) -> None:
        self.name = " ".join(self.name.split())[:100] or "未命名任务"
        self.schedule_type = self.schedule_type if self.schedule_type in {SCHEDULE_INTERVAL, SCHEDULE_DAILY} else SCHEDULE_INTERVAL
        self.interval_seconds = max(60, min(int(self.interval_seconds), 31 * 24 * 3600))
        self.daily_time = normalize_hhmm(self.daily_time, "09:00")
        self.delivery_time = normalize_hhmm(self.delivery_time, "00:00")
        self.output_mode = self.output_mode if self.output_mode in {OUTPUT_SILENT, OUTPUT_SEND_TEXT} else OUTPUT_SILENT
        self.file_format = self.file_format if self.file_format in SUPPORTED_FILE_FORMATS else "xlsx"
        self.file_name_template = self.file_name_template.strip()[:160] or f"{{date}}_{{task_name}}.{self.file_format}"
        self.sheet_name = re.sub(r"[\[\]:*?/\\]", "_", self.sheet_name.strip())[:31] or "记录"
        self.columns = [column.normalized() for column in self.columns if column.name]
        if not self.columns:
            self.columns = list(DEFAULT_COLUMNS)
        valid_names = {column.name for column in self.columns}
        self.dedup_fields = [name for name in dict.fromkeys(self.dedup_fields) if name in valid_names]
        self.recipient_mode = self.recipient_mode if self.recipient_mode in {RECIPIENT_SELF, RECIPIENT_CONTACT, RECIPIENT_MANUAL} else RECIPIENT_SELF
        self.recipient_qq = re.sub(r"\D", "", self.recipient_qq)[:20]
        self.history_limit = max(20, min(int(self.history_limit), 5000))
        self.enabled_skills = [skill for skill in dict.fromkeys(self.enabled_skills) if skill]
        if self.file_enabled and SCHEDULED_FILE_SKILL_ID not in self.enabled_skills:
            self.enabled_skills.append(SCHEDULED_FILE_SKILL_ID)
        if not self.file_enabled:
            self.enabled_skills = [skill for skill in self.enabled_skills if skill != SCHEDULED_FILE_SKILL_ID]

    def to_dict(self) -> dict[str, Any]:
        self.normalize()
        return asdict(self)

    @property
    def created_datetime(self) -> datetime:
        return parse_datetime(self.created_at) or datetime.now().replace(microsecond=0)

    @property
    def next_run_datetime(self) -> datetime | None:
        return parse_datetime(self.next_run_at)

    @property
    def next_delivery_datetime(self) -> datetime | None:
        return parse_datetime(self.next_delivery_at)

    def recalculate_next_times(self, now: datetime, *, reset: bool = False) -> None:
        self.normalize()
        if reset or not self.next_run_at or self.next_run_datetime is None:
            self.next_run_at = next_schedule_time(self, now, include_now=False).isoformat()
        if self.daily_delivery_enabled:
            if reset or not self.next_delivery_at or self.next_delivery_datetime is None:
                self.next_delivery_at = next_daily_time(self.delivery_time, now, include_now=False).isoformat()
        else:
            self.next_delivery_at = ""

    def advance_run_after_start(self, now: datetime) -> None:
        self.next_run_at = next_schedule_time(self, now, include_now=False).isoformat()

    def advance_delivery_after_start(self, now: datetime) -> None:
        if self.daily_delivery_enabled:
            self.next_delivery_at = next_daily_time(self.delivery_time, now, include_now=False).isoformat()


def load_automation_tasks(settings: Any) -> list[AutomationTask]:
    raw = settings.value(AUTOMATION_TASKS_KEY, "[]")
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        parsed = []
    tasks = [AutomationTask.from_dict(item) for item in parsed if isinstance(item, dict)]
    return sorted(tasks, key=lambda task: (not task.enabled, task.name.lower(), task.task_id))


def save_automation_tasks(settings: Any, tasks: list[AutomationTask]) -> None:
    settings.setValue(AUTOMATION_TASKS_KEY, json.dumps([task.to_dict() for task in tasks], ensure_ascii=False))
    settings.sync()


def task_by_id(tasks: list[AutomationTask], task_id: str) -> AutomationTask | None:
    return next((task for task in tasks if task.task_id == task_id), None)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def normalize_hhmm(value: str, default: str) -> str:
    match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", str(value or "").strip())
    if not match:
        return default
    hour = max(0, min(int(match.group(1)), 23))
    minute = max(0, min(int(match.group(2)), 59))
    return f"{hour:02d}:{minute:02d}"


def next_daily_time(hhmm: str, now: datetime, *, include_now: bool) -> datetime:
    hour, minute = (int(part) for part in normalize_hhmm(hhmm, "00:00").split(":"))
    candidate = datetime.combine(now.date(), time(hour, minute))
    if candidate < now or (candidate == now and not include_now):
        candidate += timedelta(days=1)
    return candidate


def next_schedule_time(task: AutomationTask, now: datetime, *, include_now: bool) -> datetime:
    if task.schedule_type == SCHEDULE_DAILY:
        return next_daily_time(task.daily_time, now, include_now=include_now)
    anchor = task.created_datetime
    interval = max(60, task.interval_seconds)
    if now < anchor:
        return anchor
    elapsed = max(0.0, (now - anchor).total_seconds())
    steps = math.floor(elapsed / interval)
    candidate = anchor + timedelta(seconds=steps * interval)
    if candidate < now or (candidate == now and not include_now):
        candidate += timedelta(seconds=interval)
    return candidate


def task_work_date(cutoff: datetime, delivery: bool) -> date:
    return (cutoff - timedelta(microseconds=1)).date() if delivery else cutoff.date()
