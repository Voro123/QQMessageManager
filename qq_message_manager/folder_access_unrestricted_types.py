from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def install_folder_access_unrestricted_types(
    agent_module: Any,
    service_module: Any,
    feature_module: Any,
) -> None:
    """Remove suffix allowlists and tolerate harmless wrappers around strict JSON.

    Path isolation, sender authorization, read/write permission switches, binary-file
    detection, size limits, write confirmation and the strict tool schema remain in
    force.  Only the filename-extension allowlist is removed.
    """

    if getattr(service_module, "_folder_access_unrestricted_types_installed", False):
        return

    service_cls = service_module.FolderAccessService

    def allow_any_extension(self: Any, grant: Any, path: Path) -> None:
        del self, grant, path

    def search_text_without_suffix_filter(
        self: Any,
        grant: Any,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], str, int]:
        target, relative = self._resolve_target(
            grant,
            arguments.get("path", "."),
            allow_root=True,
        )
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            raise service_module.FolderAccessError(
                "invalid_arguments",
                "搜索文本不能为空。",
            )
        if not target.is_dir():
            raise service_module.FolderAccessError(
                "not_directory",
                "搜索范围必须是文件夹。",
            )

        case_sensitive = bool(arguments.get("case_sensitive", False))
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []
        scanned = 0
        skipped_large = 0
        bytes_read = 0
        root = Path(grant.root_path).expanduser().resolve(strict=True)

        for file_path in self._iter_safe_files(root, target):
            if (
                scanned >= service_module.MAX_SEARCH_FILES
                or len(matches) >= service_module.MAX_SEARCH_MATCHES
            ):
                break
            scanned += 1
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if size > service_module.MAX_READ_BYTES:
                skipped_large += 1
                continue
            try:
                data = file_path.read_bytes()
                text = service_module._decode_text(data)
            except (OSError, service_module.FolderAccessError):
                # No suffix is rejected, but non-UTF-8/binary content is still not
                # suitable for the text search tool and is skipped safely.
                continue

            bytes_read += len(data)
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                matches.append(
                    {
                        "path": file_path.relative_to(root).as_posix(),
                        "line": line_number,
                        "snippet": line.strip()[:240],
                    }
                )
                if len(matches) >= service_module.MAX_SEARCH_MATCHES:
                    break

        return {
            "matches": matches,
            "files_scanned": scanned,
            "skipped_large_files": skipped_large,
            "truncated": (
                scanned >= service_module.MAX_SEARCH_FILES
                or len(matches) >= service_module.MAX_SEARCH_MATCHES
            ),
        }, relative, bytes_read

    service_cls._check_extension = allow_any_extension
    service_cls._search_text = search_text_without_suffix_filter

    original_parse = agent_module.parse_agent_response

    def parse_wrapped_agent_response(raw: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for candidate in _json_candidates(raw):
            try:
                # Keep the original strict schema validator.  We only remove harmless
                # presentation wrappers around the JSON object.
                return original_parse(candidate)
            except agent_module.FolderAgentProtocolError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise agent_module.FolderAgentProtocolError("no JSON object found")

    agent_module.parse_agent_response = parse_wrapped_agent_response
    _hide_obsolete_extension_editor(feature_module)
    service_module._folder_access_unrestricted_types_installed = True


def _json_candidates(raw: str) -> list[str]:
    text = str(raw or "").lstrip("\ufeff").strip()
    if not text:
        return []

    candidates: list[str] = []

    def add(value: str) -> None:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    add(text)
    without_thinking = _THINK_BLOCK_RE.sub("", text).strip()
    add(without_thinking)

    for match in _JSON_FENCE_RE.finditer(without_thinking):
        add(match.group(1))

    # Some providers return a JSON string whose value is the actual JSON object.
    for candidate in list(candidates):
        try:
            decoded = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, str):
            add(decoded)

    # Models often prepend a sentence such as “工具请求如下：”.  Extract complete
    # balanced top-level JSON objects without accepting a relaxed object schema.
    for candidate in list(candidates):
        for value in _balanced_json_objects(candidate):
            add(value)

    return candidates


def _balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char != "}" or depth == 0:
            continue

        depth -= 1
        if depth == 0 and start >= 0:
            objects.append(text[start : index + 1])
            start = -1

    return objects


def _hide_obsolete_extension_editor(feature_module: Any) -> None:
    dialog_cls = feature_module.FolderGrantEditDialog
    if getattr(dialog_cls, "_unrestricted_file_types_ui_installed", False):
        return

    original_init = dialog_cls.__init__

    def init_without_extension_editor(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        editor = getattr(self, "extensions", None)
        if editor is not None:
            editor.hide()
            outer = self.layout()
            if outer is not None:
                for index in range(outer.count()):
                    form = outer.itemAt(index).layout()
                    if isinstance(form, feature_module.QFormLayout):
                        label = form.labelForField(editor)
                        if label is not None:
                            label.hide()
                        break

        for label in self.findChildren(feature_module.QLabel):
            if "真实路径不会发送给 AI" in label.text():
                label.setText(
                    "默认只读、禁止写入、写入需确认。文件扩展名不受限制；"
                    "文本读取仍会拒绝二进制或非 UTF-8 内容。真实路径不会发送给 AI。"
                )
                label.setWordWrap(True)
                break

    dialog_cls.__init__ = init_without_extension_editor
    dialog_cls._unrestricted_file_types_ui_installed = True
