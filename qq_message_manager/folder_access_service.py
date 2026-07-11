from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable
from uuid import uuid4

from .folder_access_models import FolderGrant, FolderToolResult

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 512 * 1024
MAX_READ_LINES = 1000
MAX_LIST_ITEMS = 200
MAX_SEARCH_FILES = 500
MAX_SEARCH_MATCHES = 50
MAX_RECURSIVE_DEPTH = 3
MAX_BACKUPS_PER_GRANT = 20
READ_TOOLS = {"list_directory", "read_text", "search_text", "file_info"}
WRITE_TOOLS = {"create_directory", "write_text"}
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS


def folder_root_is_directory(path: str) -> bool:
    return bool(canonical_folder_root(path))


def canonical_folder_root(path: str) -> str:
    try:
        resolved = Path(path).expanduser().resolve(strict=True) if path else None
        return str(resolved) if resolved is not None and resolved.is_dir() else ""
    except (OSError, RuntimeError):
        return ""


class FolderAccessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FolderAccessService:
    """The only component allowed to translate aliases into real filesystem paths."""

    def __init__(
        self,
        grants_provider: Callable[[], list[FolderGrant]],
        *,
        audit_path: Path | None = None,
        backup_root: Path | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        app_data = Path.home() / ".qq_message_manager"
        self._grants_provider = grants_provider
        self._audit_path = audit_path or app_data / "folder_access_audit.jsonl"
        self._backup_root = backup_root or app_data / "folder_access_backups"
        self._log_callback = log_callback

    def public_grants(self, sender_id: str = "") -> list[dict[str, Any]]:
        return [
            {
                "alias": grant.alias,
                "description": self.redact_configured_roots(grant.description),
                "read_enabled": grant.read_enabled,
                "write_enabled": grant.write_enabled,
            }
            for grant in self._grants_provider()
            if grant.enabled and (not sender_id or str(sender_id) in grant.allowed_sender_ids)
        ]

    def redact_configured_roots(self, text: str) -> str:
        redacted = str(text or "")
        for grant in self._grants_provider():
            raw = str(grant.root_path or "").strip()
            values = {raw, raw.replace("\\", "/")}
            try:
                resolved = str(Path(raw).expanduser().resolve(strict=True)) if raw else ""
            except (OSError, RuntimeError):
                resolved = ""
            values.update({resolved, resolved.replace("\\", "/")})
            for value in sorted((item for item in values if item), key=len, reverse=True):
                redacted = re_sub_case_insensitive(redacted, value, "[已隐藏的授权路径]")
        return redacted

    def redact_model_data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_configured_roots(value)
        if isinstance(value, list):
            return [self.redact_model_data(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.redact_model_data(item) for key, item in value.items()}
        return value

    @staticmethod
    def test_grant_access(grant: FolderGrant) -> tuple[bool, str]:
        try:
            root = Path(grant.root_path).expanduser().resolve(strict=True)
            if not root.is_dir():
                return False, "授权路径不是文件夹。"
            if grant.read_enabled:
                next(root.iterdir(), None)
            if grant.write_enabled and not os.access(root, os.W_OK):
                return False, "当前进程没有该文件夹的写入权限。"
        except OSError:
            return False, "无法访问授权文件夹。"
        return True, "读取/写入基础权限检查通过。" if grant.write_enabled else "读取基础权限检查通过。"

    def find_grant(self, alias: str) -> FolderGrant | None:
        folded = str(alias or "").strip().casefold()
        return next(
            (grant for grant in self._grants_provider() if grant.alias.casefold() == folded),
            None,
        )

    def audit_denied_request(
        self,
        *,
        session_id: str,
        sender_id: str,
        alias: str,
        error_code: str,
    ) -> None:
        self._audit(
            session_id, sender_id, self._safe_alias(alias), "request", ".",
            "denied", False, error_code, 0,
        )

    def execute(
        self,
        tool: str,
        alias: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        sender_id: str,
        skill_enabled: bool,
    ) -> FolderToolResult:
        relative_path = _argument_path(arguments)
        bytes_count = 0
        try:
            grant = self._authorize(tool, alias, sender_id, skill_enabled)
            data, relative_path, bytes_count = self._execute_authorized(grant, tool, arguments)
            result = FolderToolResult(True, data=data)
            self._audit(session_id, sender_id, grant.alias, tool, relative_path, "allowed", True, "", bytes_count)
            self._log(grant.alias, tool, relative_path, True, bytes_count)
            return result
        except FolderAccessError as exc:
            safe_alias = self._safe_alias(alias)
            self._audit(session_id, sender_id, safe_alias, tool, relative_path, "denied", False, exc.code, 0)
            self._log(safe_alias, tool, relative_path, False, 0)
            return FolderToolResult(False, error_code=exc.code, message=exc.message)
        except Exception:  # noqa: BLE001 - never expose filesystem/provider details
            safe_alias = self._safe_alias(alias)
            self._audit(session_id, sender_id, safe_alias, tool, relative_path, "error", False, "internal_error", 0)
            self._log(safe_alias, tool, relative_path, False, 0)
            return FolderToolResult(False, error_code="internal_error", message="文件操作失败，请检查授权和文件状态。")

    def validate_write_request(
        self,
        tool: str,
        alias: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        sender_id: str,
        skill_enabled: bool,
    ) -> FolderToolResult:
        relative_path = _argument_path(arguments)
        try:
            grant = self._authorize(tool, alias, sender_id, skill_enabled)
            target, normalized = self._resolve_target(grant, relative_path, allow_root=False)
            byte_count = 0
            if tool == "write_text":
                self._check_extension(grant, target)
                content = arguments.get("content")
                if not isinstance(content, str):
                    raise FolderAccessError("invalid_arguments", "写入内容必须是文本。")
                encoded = content.encode("utf-8")
                if len(encoded) > MAX_WRITE_BYTES:
                    raise FolderAccessError("write_too_large", "单次写入不能超过 512 KiB。")
                self._check_write_preconditions(target, arguments)
                byte_count = len(encoded)
            elif tool == "create_directory":
                if target.exists() and not bool(arguments.get("exist_ok", False)):
                    raise FolderAccessError("already_exists", "目标已经存在。")
            else:
                raise FolderAccessError("unknown_tool", "不支持的写入工具。")
            operation = f"{tool}_prepare"
            self._audit(session_id, sender_id, grant.alias, operation, normalized, "allowed", True, "", 0)
            return FolderToolResult(True, data={"relative_path": normalized, "bytes": byte_count})
        except FolderAccessError as exc:
            self._audit(session_id, sender_id, self._safe_alias(alias), f"{tool}_prepare", relative_path, "denied", False, exc.code, 0)
            return FolderToolResult(False, error_code=exc.code, message=exc.message)

    def _authorize(self, tool: str, alias: str, sender_id: str, skill_enabled: bool) -> FolderGrant:
        if not skill_enabled:
            raise FolderAccessError("skill_disabled", "受控文件夹访问 Skill 未开启。")
        if tool not in ALL_TOOLS:
            raise FolderAccessError("unknown_tool", "不支持的文件工具。")
        grant = self.find_grant(alias)
        if grant is None:
            raise FolderAccessError("unknown_alias", "没有找到这个文件夹关联名。")
        if not grant.enabled:
            raise FolderAccessError("grant_disabled", f"{grant.alias} 文件夹授权已停用。")
        if str(sender_id) not in grant.allowed_sender_ids:
            raise FolderAccessError("sender_not_allowed", f"你没有操作 {grant.alias} 文件夹的权限。")
        if tool in READ_TOOLS and not grant.read_enabled:
            raise FolderAccessError("read_disabled", f"{grant.alias} 未开放读取权限。")
        if tool in WRITE_TOOLS and not grant.write_enabled:
            raise FolderAccessError("write_disabled", f"{grant.alias} 未开放写入权限。")
        return grant

    def _execute_authorized(
        self,
        grant: FolderGrant,
        tool: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], str, int]:
        if tool == "list_directory":
            return self._list_directory(grant, arguments)
        if tool == "read_text":
            return self._read_text(grant, arguments)
        if tool == "search_text":
            return self._search_text(grant, arguments)
        if tool == "file_info":
            return self._file_info(grant, arguments)
        if tool == "create_directory":
            return self._create_directory(grant, arguments)
        if tool == "write_text":
            return self._write_text(grant, arguments)
        raise FolderAccessError("unknown_tool", "不支持的文件工具。")

    def _resolve_target(
        self,
        grant: FolderGrant,
        relative_path: Any,
        *,
        allow_root: bool,
    ) -> tuple[Path, str]:
        raw = str(relative_path if relative_path is not None else "").strip()
        if "\x00" in raw:
            raise FolderAccessError("invalid_path", "路径包含非法字符。")
        windows_path = PureWindowsPath(raw)
        if raw.startswith(("/", "\\")) or windows_path.is_absolute() or windows_path.drive:
            raise FolderAccessError("absolute_path", "只能使用授权文件夹内的相对路径。")
        if ":" in raw:
            raise FolderAccessError("invalid_path", "路径中不允许使用冒号。")
        normalized_input = raw.replace("\\", "/")
        parts = [part for part in normalized_input.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise FolderAccessError("path_escape", "路径不能包含上级目录跳转。")
        if not parts and not allow_root:
            raise FolderAccessError("invalid_path", "必须提供文件夹内的相对路径。")
        try:
            root = Path(grant.root_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise FolderAccessError("invalid_root", "授权文件夹当前不可用。") from exc
        if not root.is_dir():
            raise FolderAccessError("invalid_root", "授权文件夹当前不可用。")
        try:
            target = root.joinpath(*parts).resolve(strict=False)
            inside = target == root or target.is_relative_to(root)
        except (OSError, RuntimeError) as exc:
            raise FolderAccessError("invalid_path", "无法验证目标路径。") from exc
        if not inside:
            raise FolderAccessError("path_escape", "目标路径超出了授权文件夹。")
        normalized = "/".join(parts) or "."
        return target, normalized

    def _check_extension(self, grant: FolderGrant, path: Path) -> None:
        if path.suffix.lower() not in set(grant.allowed_extensions):
            raise FolderAccessError("extension_denied", "该文件扩展名不在授权范围内。")

    def _list_directory(self, grant: FolderGrant, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(grant, arguments.get("path", "."), allow_root=True)
        if not target.is_dir():
            raise FolderAccessError("not_directory", "目标不是文件夹。")
        recursive = bool(arguments.get("recursive", False))
        depth = int(arguments.get("max_depth", 1 if recursive else 0))
        if depth < 0 or depth > MAX_RECURSIVE_DEPTH:
            raise FolderAccessError("invalid_arguments", "递归深度必须在 0 到 3 之间。")
        if not recursive:
            depth = 0
        root = Path(grant.root_path).expanduser().resolve(strict=True)
        entries: list[dict[str, Any]] = []
        truncated = False
        queue: list[tuple[Path, int]] = [(target, 0)]
        while queue and len(entries) < MAX_LIST_ITEMS:
            current, current_depth = queue.pop(0)
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise FolderAccessError("read_failed", "无法读取文件夹内容。") from exc
            for child in children:
                try:
                    resolved = child.resolve(strict=True)
                    if not (resolved == root or resolved.is_relative_to(root)):
                        continue
                    item_relative = resolved.relative_to(root).as_posix()
                    is_dir = resolved.is_dir()
                    size = resolved.stat().st_size if resolved.is_file() else 0
                except (OSError, RuntimeError, ValueError):
                    continue
                child_depth = current_depth + 1
                if recursive and child_depth > depth:
                    continue
                entries.append({"path": item_relative, "type": "directory" if is_dir else "file", "size": size})
                if len(entries) >= MAX_LIST_ITEMS:
                    truncated = True
                    break
                if recursive and is_dir and child_depth < depth:
                    queue.append((resolved, child_depth))
        if queue:
            truncated = True
        return {"entries": entries, "truncated": truncated}, relative, 0

    def _read_text(self, grant: FolderGrant, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(grant, arguments.get("path"), allow_root=False)
        self._check_extension(grant, target)
        data = self._read_file_bytes(target)
        text = _decode_text(data)
        lines = text.splitlines()
        start_line = _positive_int(arguments.get("start_line", 1), "start_line")
        if not lines:
            if start_line != 1:
                raise FolderAccessError("line_range", "起始行超出文件范围。")
            stat = target.stat()
            return {
                "path": relative, "text": "", "start_line": 1, "end_line": 0,
                "total_lines": 0, "sha256": hashlib.sha256(data).hexdigest(),
                "size": 0, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }, relative, 0
        if start_line > len(lines):
            raise FolderAccessError("line_range", "起始行超出文件范围。")
        end_line = int(arguments.get("end_line", min(len(lines), start_line + MAX_READ_LINES - 1)))
        if end_line < start_line or end_line - start_line + 1 > MAX_READ_LINES:
            raise FolderAccessError("line_range", "单次最多读取 1000 行。")
        selected = lines[start_line - 1:end_line]
        stat = target.stat()
        return {
            "path": relative,
            "text": "\n".join(selected),
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "total_lines": len(lines),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }, relative, len(data)

    def _search_text(self, grant: FolderGrant, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(grant, arguments.get("path", "."), allow_root=True)
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            raise FolderAccessError("invalid_arguments", "搜索文本不能为空。")
        if not target.is_dir():
            raise FolderAccessError("not_directory", "搜索范围必须是文件夹。")
        case_sensitive = bool(arguments.get("case_sensitive", False))
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        scanned = 0
        skipped_large = 0
        bytes_read = 0
        root = Path(grant.root_path).expanduser().resolve(strict=True)
        for file_path in self._iter_safe_files(root, target):
            if scanned >= MAX_SEARCH_FILES or len(matches) >= MAX_SEARCH_MATCHES:
                break
            if file_path.suffix.lower() not in set(grant.allowed_extensions):
                continue
            scanned += 1
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if size > MAX_READ_BYTES:
                skipped_large += 1
                continue
            try:
                data = file_path.read_bytes()
                text = _decode_text(data)
            except (OSError, FolderAccessError):
                continue
            bytes_read += len(data)
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append({
                        "path": file_path.relative_to(root).as_posix(),
                        "line": line_number,
                        "snippet": line.strip()[:240],
                    })
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        break
        return {
            "matches": matches,
            "files_scanned": scanned,
            "skipped_large_files": skipped_large,
            "truncated": scanned >= MAX_SEARCH_FILES or len(matches) >= MAX_SEARCH_MATCHES,
        }, relative, bytes_read

    def _file_info(self, grant: FolderGrant, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(grant, arguments.get("path"), allow_root=True)
        if not target.exists():
            raise FolderAccessError("not_found", "目标不存在。")
        stat = target.stat()
        data: dict[str, Any] = {
            "path": relative,
            "type": "directory" if target.is_dir() else "file",
            "size": stat.st_size if target.is_file() else 0,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }
        if target.is_file():
            self._check_extension(grant, target)
            if stat.st_size <= MAX_READ_BYTES:
                file_data = self._read_file_bytes(target)
                data["sha256"] = hashlib.sha256(file_data).hexdigest()
        return data, relative, 0

    def _create_directory(self, grant: FolderGrant, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(grant, arguments.get("path"), allow_root=False)
        exist_ok = bool(arguments.get("exist_ok", False))
        try:
            target.mkdir(parents=True, exist_ok=exist_ok)
        except FileExistsError as exc:
            raise FolderAccessError("already_exists", "目标已经存在。") from exc
        except OSError as exc:
            raise FolderAccessError("write_failed", "无法创建文件夹。") from exc
        return {"path": relative, "created": True}, relative, 0

    def _write_text(self, grant: FolderGrant, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(grant, arguments.get("path"), allow_root=False)
        self._check_extension(grant, target)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise FolderAccessError("invalid_arguments", "写入内容必须是文本。")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise FolderAccessError("write_too_large", "单次写入不能超过 512 KiB。")
        self._check_write_preconditions(target, arguments)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._create_backup(grant, target, relative)
            fd, temp_name = tempfile.mkstemp(prefix=".qqmm-write-", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            except Exception:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise FolderAccessError("write_failed", "写入文件失败。") from exc
        return {
            "path": relative,
            "bytes_written": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }, relative, len(data)

    def _check_write_preconditions(self, target: Path, arguments: dict[str, Any]) -> None:
        create_only = bool(arguments.get("create_only", False))
        expected = arguments.get("expected_sha256")
        if expected is not None and not isinstance(expected, str):
            raise FolderAccessError("invalid_arguments", "expected_sha256 格式无效。")
        if target.exists() and not target.is_file():
            raise FolderAccessError("not_file", "目标不是普通文件。")
        if create_only and target.exists():
            raise FolderAccessError("already_exists", "目标文件已经存在。")
        if expected:
            if not target.exists():
                raise FolderAccessError("file_changed", "文件已变化：目标文件不存在。")
            current = hashlib.sha256(self._read_file_bytes(target)).hexdigest()
            if current.casefold() != expected.strip().casefold():
                raise FolderAccessError("file_changed", "文件已变化，未执行覆盖。")

    def _read_file_bytes(self, target: Path) -> bytes:
        if not target.is_file():
            raise FolderAccessError("not_file", "目标不是普通文件。")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise FolderAccessError("read_failed", "无法读取文件信息。") from exc
        if size > MAX_READ_BYTES:
            raise FolderAccessError("file_too_large", "单个文件不能超过 2 MiB。")
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise FolderAccessError("read_failed", "读取文件失败。") from exc
        if len(data) > MAX_READ_BYTES:
            raise FolderAccessError("file_too_large", "单个文件不能超过 2 MiB。")
        return data

    def _iter_safe_files(self, root: Path, start: Path) -> Iterable[Path]:
        for current, directories, files in os.walk(start, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in directories:
                candidate = current_path / name
                try:
                    resolved = candidate.resolve(strict=True)
                    if resolved.is_relative_to(root):
                        safe_directories.append(name)
                except (OSError, RuntimeError):
                    continue
            directories[:] = safe_directories
            for name in sorted(files, key=str.casefold):
                candidate = current_path / name
                try:
                    resolved = candidate.resolve(strict=True)
                    if resolved.is_file() and resolved.is_relative_to(root):
                        yield resolved
                except (OSError, RuntimeError):
                    continue

    def _create_backup(self, grant: FolderGrant, target: Path, relative: str) -> None:
        backup_dir = self._backup_root / grant.grant_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_name = relative.replace("/", "__").replace("\\", "__")[-120:]
        destination = backup_dir / f"{timestamp}-{uuid4().hex[:8]}-{safe_name}"
        shutil.copy2(target, destination)
        backups = sorted(
            (path for path in backup_dir.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for old in backups[MAX_BACKUPS_PER_GRANT:]:
            old.unlink(missing_ok=True)

    def _safe_alias(self, alias: str) -> str:
        grant = self.find_grant(alias)
        return grant.alias if grant is not None else str(alias or "").strip()[:80]

    def _audit(
        self,
        session_id: str,
        sender_id: str,
        alias: str,
        operation: str,
        relative_path: str,
        permission_result: str,
        success: bool,
        error_code: str,
        bytes_count: int,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": str(session_id),
            "sender_id": str(sender_id),
            "alias": str(alias),
            "operation": str(operation),
            "relative_path": str(relative_path or "."),
            "permission_result": permission_result,
            "success": bool(success),
            "error_code": str(error_code),
        }
        if bytes_count:
            record["bytes_written" if operation == "write_text" else "bytes_read"] = int(bytes_count)
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _log(self, alias: str, operation: str, relative_path: str, success: bool, byte_count: int) -> None:
        if self._log_callback is None:
            return
        state = "成功" if success else "拒绝或失败"
        suffix = f"，处理 {byte_count} 字节" if byte_count else ""
        self._log_callback(f"文件夹访问：{alias}/{operation} {relative_path or '.'} {state}{suffix}")


def _argument_path(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return "."
    raw = str(arguments.get("path", ".") or ".")[:500]
    windows_path = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    if (
        "\x00" in raw
        or ":" in raw
        or raw.startswith(("/", "\\"))
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in normalized.split("/")
    ):
        return "[invalid path]"
    return normalized


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FolderAccessError("invalid_arguments", f"{name} 必须是正整数。") from exc
    if parsed < 1:
        raise FolderAccessError("invalid_arguments", f"{name} 必须是正整数。")
    return parsed


def _decode_text(data: bytes) -> str:
    if b"\x00" in data:
        raise FolderAccessError("binary_file", "拒绝读取二进制文件。")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FolderAccessError("binary_file", "文件不是可识别的 UTF-8 文本。") from exc


def re_sub_case_insensitive(text: str, value: str, replacement: str) -> str:
    if not value:
        return text
    return re.sub(re.escape(value), replacement, text, flags=re.IGNORECASE)
