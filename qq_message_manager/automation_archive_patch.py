from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from .automation_models import task_work_date

GENERATED_SUFFIXES = {".xlsx", ".csv", ".json", ".md"}


def install_automation_archive_patch(automation_module: Any) -> None:
    """每日归档时合并所有未发送记录，并在发送成功后统一清理旧归档。"""
    if getattr(automation_module, "_archive_merge_installed", False):
        return

    delivery_paths: set[str] = set()
    original_start = automation_module._start_task
    original_load_records = automation_module.load_records
    original_write_artifact = automation_module.write_artifact
    original_delete_bundle = automation_module.delete_artifact_bundle

    def start_with_archive_merge(
        window: Any,
        ui_module: Any,
        ai_module: Any,
        task: Any,
        *,
        delivery: bool,
        manual: bool,
        attempt: int,
        advance_schedule: bool,
    ) -> None:
        if delivery and task.file_enabled:
            boundary = _latest_boundary(task.delivery_time, datetime.now().replace(microsecond=0))
            expected = automation_module.artifact_path(
                task,
                task_work_date(boundary, True),
            )
            delivery_paths.add(str(expected.resolve()))
        original_start(
            window,
            ui_module,
            ai_module,
            task,
            delivery=delivery,
            manual=manual,
            attempt=attempt,
            advance_schedule=advance_schedule,
        )

    def load_records_with_archive_merge(path: Path) -> list[dict[str, Any]]:
        resolved = str(Path(path).resolve())
        if resolved not in delivery_paths:
            return original_load_records(path)

        by_id: dict[str, dict[str, Any]] = {}
        parent = Path(path).resolve().parent
        for sidecar in sorted(parent.glob("*.records.json")):
            artifact = sidecar.with_name(sidecar.name[: -len(".records.json")])
            for record in original_load_records(artifact):
                record_id = str(record.get("record_id") or "")
                if not record_id:
                    continue
                previous = by_id.get(record_id)
                if previous is None or str(record.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                    by_id[record_id] = record
        return sorted(
            by_id.values(),
            key=lambda record: (
                str(record.get("created_at") or ""),
                str(record.get("record_id") or ""),
            ),
        )

    def write_artifact_and_clear_marker(task: Any, work_date: Any, records: list[dict[str, Any]]) -> Path:
        path = original_write_artifact(task, work_date, records)
        delivery_paths.discard(str(path.resolve()))
        return path

    def delete_all_generated_archives(path: Path) -> None:
        parent = Path(path).resolve().parent
        errors: list[str] = []
        for target in list(parent.iterdir()):
            is_sidecar = target.name.endswith(".records.json")
            is_artifact = target.suffix.lower() in GENERATED_SUFFIXES
            if not is_sidecar and not is_artifact:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{target.name}: {exc}")
        if errors:
            raise RuntimeError("；".join(errors[:3]))

    automation_module._start_task = start_with_archive_merge
    automation_module.load_records = load_records_with_archive_merge
    automation_module.write_artifact = write_artifact_and_clear_marker
    automation_module.delete_artifact_bundle = delete_all_generated_archives
    automation_module._archive_merge_installed = True


def _latest_boundary(hhmm: str, now: datetime) -> datetime:
    try:
        hour_text, minute_text = str(hhmm).split(":", 1)
        boundary = datetime.combine(now.date(), time(int(hour_text), int(minute_text)))
    except (TypeError, ValueError):
        boundary = datetime.combine(now.date(), time(0, 0))
    return boundary if boundary <= now else boundary - timedelta(days=1)
