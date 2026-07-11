from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable


CODEBASE_READ_TOOLS = {
    "project_tree",
    "find_files",
    "search_code",
    "read_lines",
    "find_symbol",
}

MAX_CODE_SCAN_FILES = 2000
MAX_CODE_RESULTS = 100
MAX_TREE_ENTRIES = 1000
MAX_TREE_DEPTH = 6
MAX_READ_LINE_COUNT = 600
MAX_CONTEXT_LINES = 5
MAX_PATTERN_LENGTH = 300
MAX_SNIPPET_CHARS = 500

IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    ".next",
    "coverage",
}

_CODEBASE_PROMPT = """
你还可以使用以下代码库浏览工具：
- project_tree：查看项目目录树。参数：path、max_depth、max_entries。
- find_files：按文件名、相对路径或通配符定位文件。参数：path、pattern、max_results。
- search_code：在代码库中按关键词全文检索并返回命中上下文。参数：path、query、case_sensitive、file_pattern、context_lines、max_results。
- read_lines：按行号分段读取文件。参数：path、start_line、line_count。
- find_symbol：定位函数、类、方法、类型或变量的定义。参数：path、name、max_results。

当用户要求排查代码库问题时，不要仅凭文件名猜测。应先查看目录树或定位相关文件，再搜索报错文本、函数名、类名、配置键或调用处，随后分段读取命中附近代码。必要时继续搜索引用关系。最终回复应明确区分：已确认的代码事实、推断出的根因、建议修改的位置。不要声称运行过代码，除非工具结果明确表明已运行；当前没有执行命令或 Shell 的能力。
""".strip()


def install_folder_access_codebase_tools(
    agent_module: Any,
    service_module: Any,
    feature_module: Any,
) -> None:
    """Extend controlled-folder access with read-only codebase investigation tools."""

    if getattr(service_module, "_folder_access_codebase_tools_installed", False):
        return

    service_module.READ_TOOLS.update(CODEBASE_READ_TOOLS)
    service_module.ALL_TOOLS.update(CODEBASE_READ_TOOLS)
    # folder_access_agent imported ALL_TOOLS by object reference, but update it as
    # well in case another module replaced the set after import.
    agent_module.ALL_TOOLS.update(CODEBASE_READ_TOOLS)
    agent_module.MAX_TOOL_STEPS = max(int(agent_module.MAX_TOOL_STEPS), 12)

    agent_module.TOOL_ARGUMENT_FIELDS.update(
        {
            "project_tree": (
                {"path", "max_depth", "max_entries"},
                set(),
            ),
            "find_files": (
                {"path", "pattern", "max_results"},
                {"pattern"},
            ),
            "search_code": (
                {
                    "path",
                    "query",
                    "case_sensitive",
                    "file_pattern",
                    "context_lines",
                    "max_results",
                },
                {"query"},
            ),
            "read_lines": (
                {"path", "start_line", "line_count"},
                {"path"},
            ),
            "find_symbol": (
                {"path", "name", "max_results"},
                {"name"},
            ),
        }
    )

    _install_tool_dispatch(service_module)
    _install_protocol_validation(agent_module)
    _install_prompt_extension(agent_module)
    _install_code_intent_routing(feature_module)

    service_module._folder_access_codebase_tools_installed = True


def _install_tool_dispatch(service_module: Any) -> None:
    service_cls = service_module.FolderAccessService
    original_execute_authorized = service_cls._execute_authorized

    def execute_authorized_with_codebase_tools(
        self: Any,
        grant: Any,
        tool: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], str, int]:
        if tool == "project_tree":
            return _project_tree(self, grant, arguments, service_module)
        if tool == "find_files":
            return _find_files(self, grant, arguments, service_module)
        if tool == "search_code":
            return _search_code(self, grant, arguments, service_module)
        if tool == "read_lines":
            return _read_lines(self, grant, arguments, service_module)
        if tool == "find_symbol":
            return _find_symbol(self, grant, arguments, service_module)
        return original_execute_authorized(self, grant, tool, arguments)

    service_cls._execute_authorized = execute_authorized_with_codebase_tools


def _install_protocol_validation(agent_module: Any) -> None:
    original_parse = agent_module.parse_agent_response

    def parse_with_codebase_validation(raw: str) -> dict[str, Any]:
        value = original_parse(raw)
        if value.get("kind") != "tool" or value.get("tool") not in CODEBASE_READ_TOOLS:
            return value
        tool = str(value["tool"])
        arguments = value["arguments"]
        _validate_codebase_arguments(tool, arguments, agent_module)
        return value

    agent_module.parse_agent_response = parse_with_codebase_validation


def _install_prompt_extension(agent_module: Any) -> None:
    agent_cls = agent_module.FolderAccessAgent
    original_init = agent_cls.__init__

    def init_with_codebase_prompt(
        self: Any,
        service: Any,
        completion: Callable[..., str],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        original_init(
            self,
            service,
            _completion_with_codebase_prompt(completion),
            *args,
            **kwargs,
        )

    agent_cls.__init__ = init_with_codebase_prompt


def _completion_with_codebase_prompt(completion: Callable[..., str]) -> Callable[..., str]:
    def wrapped(
        config: Any,
        messages: list[dict[str, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        copied = [dict(message) for message in messages]
        if copied and copied[0].get("role") == "system":
            copied[0]["content"] = str(copied[0].get("content") or "") + "\n\n" + _CODEBASE_PROMPT
        return completion(config, copied, *args, **kwargs)

    return wrapped


def _install_code_intent_routing(feature_module: Any) -> None:
    existing = feature_module.FILE_INTENT_RE
    feature_module.FILE_INTENT_RE = re.compile(
        rf"(?:{existing.pattern})|(?:代码库|源码|报错|错误|异常|bug|排查|定位|调用链|引用|定义|函数|方法|类|模块|依赖)",
        re.IGNORECASE,
    )


def _validate_codebase_arguments(tool: str, arguments: dict[str, Any], agent_module: Any) -> None:
    string_fields = {
        "project_tree": {"path"},
        "find_files": {"path", "pattern"},
        "search_code": {"path", "query", "file_pattern"},
        "read_lines": {"path"},
        "find_symbol": {"path", "name"},
    }[tool]
    integer_fields = {
        "project_tree": {"max_depth", "max_entries"},
        "find_files": {"max_results"},
        "search_code": {"context_lines", "max_results"},
        "read_lines": {"start_line", "line_count"},
        "find_symbol": {"max_results"},
    }[tool]
    for name in string_fields:
        if name in arguments and not isinstance(arguments[name], str):
            raise agent_module.FolderAgentProtocolError(f"{name} must be a string")
    for name in integer_fields:
        if name in arguments and (
            not isinstance(arguments[name], int) or isinstance(arguments[name], bool)
        ):
            raise agent_module.FolderAgentProtocolError(f"{name} must be an integer")
    if "case_sensitive" in arguments and not isinstance(arguments["case_sensitive"], bool):
        raise agent_module.FolderAgentProtocolError("case_sensitive must be boolean")


def _project_tree(
    service: Any,
    grant: Any,
    arguments: dict[str, Any],
    service_module: Any,
) -> tuple[dict[str, Any], str, int]:
    target, relative = service._resolve_target(
        grant,
        arguments.get("path", "."),
        allow_root=True,
    )
    if not target.is_dir():
        raise service_module.FolderAccessError("not_directory", "目录树起点必须是文件夹。")
    max_depth = _bounded_int(
        arguments.get("max_depth", 3),
        0,
        MAX_TREE_DEPTH,
        "max_depth",
        service_module,
    )
    max_entries = _bounded_int(
        arguments.get("max_entries", 400),
        1,
        MAX_TREE_ENTRIES,
        "max_entries",
        service_module,
    )
    root = Path(grant.root_path).expanduser().resolve(strict=True)
    queue: list[tuple[Path, int]] = [(target, 0)]
    entries: list[dict[str, Any]] = []
    truncated = False

    while queue and len(entries) < max_entries:
        current, depth = queue.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        except OSError as exc:
            raise service_module.FolderAccessError("read_failed", "无法读取目录树。") from exc
        for child in children:
            try:
                resolved = child.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue
                is_dir = resolved.is_dir()
                is_file = resolved.is_file()
                if not is_dir and not is_file:
                    continue
                item_relative = resolved.relative_to(root).as_posix()
                ignored = is_dir and child.name in IGNORED_DIRECTORY_NAMES
                entries.append(
                    {
                        "path": item_relative,
                        "type": "directory" if is_dir else "file",
                        "size": resolved.stat().st_size if is_file else 0,
                        "ignored": ignored,
                    }
                )
                if len(entries) >= max_entries:
                    truncated = True
                    break
                if is_dir and not ignored and depth < max_depth:
                    queue.append((resolved, depth + 1))
            except (OSError, RuntimeError, ValueError):
                continue
    if queue:
        truncated = True
    return {
        "entries": entries,
        "truncated": truncated,
        "max_depth": max_depth,
        "ignored_directories": sorted(IGNORED_DIRECTORY_NAMES),
    }, relative, 0


def _find_files(
    service: Any,
    grant: Any,
    arguments: dict[str, Any],
    service_module: Any,
) -> tuple[dict[str, Any], str, int]:
    target, relative = service._resolve_target(
        grant,
        arguments.get("path", "."),
        allow_root=True,
    )
    if not target.is_dir():
        raise service_module.FolderAccessError("not_directory", "文件定位范围必须是文件夹。")
    pattern = _required_text(arguments.get("pattern"), "pattern", service_module)
    max_results = _bounded_int(
        arguments.get("max_results", 100),
        1,
        MAX_CODE_RESULTS,
        "max_results",
        service_module,
    )
    root = Path(grant.root_path).expanduser().resolve(strict=True)
    matches: list[dict[str, Any]] = []
    scanned = 0
    use_glob = any(char in pattern for char in "*?[]")
    folded_pattern = pattern.casefold()

    for file_path in _iter_project_files(root, target):
        scanned += 1
        rel = file_path.relative_to(root).as_posix()
        if use_glob:
            matched = fnmatch.fnmatchcase(rel.casefold(), folded_pattern) or fnmatch.fnmatchcase(
                file_path.name.casefold(), folded_pattern
            )
        else:
            matched = folded_pattern in rel.casefold() or folded_pattern in file_path.name.casefold()
        if not matched:
            if scanned >= MAX_CODE_SCAN_FILES:
                break
            continue
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        matches.append({"path": rel, "size": size})
        if len(matches) >= max_results or scanned >= MAX_CODE_SCAN_FILES:
            break
    return {
        "matches": matches,
        "files_scanned": scanned,
        "truncated": len(matches) >= max_results or scanned >= MAX_CODE_SCAN_FILES,
    }, relative, 0


def _search_code(
    service: Any,
    grant: Any,
    arguments: dict[str, Any],
    service_module: Any,
) -> tuple[dict[str, Any], str, int]:
    target, relative = service._resolve_target(
        grant,
        arguments.get("path", "."),
        allow_root=True,
    )
    if not target.is_dir():
        raise service_module.FolderAccessError("not_directory", "代码检索范围必须是文件夹。")
    query = _required_text(arguments.get("query"), "query", service_module)
    case_sensitive = bool(arguments.get("case_sensitive", False))
    file_pattern = str(arguments.get("file_pattern") or "").strip()
    if len(file_pattern) > MAX_PATTERN_LENGTH:
        raise service_module.FolderAccessError("invalid_arguments", "file_pattern 过长。")
    context_lines = _bounded_int(
        arguments.get("context_lines", 2),
        0,
        MAX_CONTEXT_LINES,
        "context_lines",
        service_module,
    )
    max_results = _bounded_int(
        arguments.get("max_results", 50),
        1,
        MAX_CODE_RESULTS,
        "max_results",
        service_module,
    )
    root = Path(grant.root_path).expanduser().resolve(strict=True)
    needle = query if case_sensitive else query.casefold()
    matches: list[dict[str, Any]] = []
    scanned = 0
    skipped_large = 0
    skipped_binary = 0
    bytes_read = 0

    for file_path in _iter_project_files(root, target):
        if scanned >= MAX_CODE_SCAN_FILES or len(matches) >= max_results:
            break
        rel = file_path.relative_to(root).as_posix()
        if file_pattern and not (
            fnmatch.fnmatchcase(rel.casefold(), file_pattern.casefold())
            or fnmatch.fnmatchcase(file_path.name.casefold(), file_pattern.casefold())
        ):
            continue
        scanned += 1
        try:
            size = file_path.stat().st_size
        except OSError:
            continue
        if size > service_module.MAX_READ_BYTES:
            skipped_large += 1
            continue
        try:
            raw = file_path.read_bytes()
            text, encoding = _decode_source_text(raw, service_module)
        except (OSError, service_module.FolderAccessError):
            skipped_binary += 1
            continue
        bytes_read += len(raw)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            haystack = line if case_sensitive else line.casefold()
            if needle not in haystack:
                continue
            first = max(0, index - context_lines)
            last = min(len(lines), index + context_lines + 1)
            matches.append(
                {
                    "path": rel,
                    "line": index + 1,
                    "encoding": encoding,
                    "match": line.strip()[:MAX_SNIPPET_CHARS],
                    "context_start_line": first + 1,
                    "context": [
                        {"line": line_index + 1, "text": lines[line_index][:MAX_SNIPPET_CHARS]}
                        for line_index in range(first, last)
                    ],
                }
            )
            if len(matches) >= max_results:
                break
    return {
        "matches": matches,
        "files_scanned": scanned,
        "bytes_read": bytes_read,
        "skipped_large_files": skipped_large,
        "skipped_binary_files": skipped_binary,
        "truncated": scanned >= MAX_CODE_SCAN_FILES or len(matches) >= max_results,
    }, relative, bytes_read


def _read_lines(
    service: Any,
    grant: Any,
    arguments: dict[str, Any],
    service_module: Any,
) -> tuple[dict[str, Any], str, int]:
    target, relative = service._resolve_target(
        grant,
        arguments.get("path"),
        allow_root=False,
    )
    if not target.is_file():
        raise service_module.FolderAccessError("not_file", "目标不是普通文件。")
    start_line = _bounded_int(
        arguments.get("start_line", 1),
        1,
        10_000_000,
        "start_line",
        service_module,
    )
    line_count = _bounded_int(
        arguments.get("line_count", 200),
        1,
        MAX_READ_LINE_COUNT,
        "line_count",
        service_module,
    )
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise service_module.FolderAccessError("read_failed", "无法读取文件信息。") from exc
    if size > service_module.MAX_READ_BYTES:
        raise service_module.FolderAccessError("file_too_large", "单个文件不能超过 2 MiB。")
    try:
        raw = target.read_bytes()
        text, encoding = _decode_source_text(raw, service_module)
    except OSError as exc:
        raise service_module.FolderAccessError("read_failed", "读取文件失败。") from exc
    lines = text.splitlines()
    if start_line > max(1, len(lines)):
        raise service_module.FolderAccessError("line_range", "起始行超出文件范围。")
    selected = lines[start_line - 1 : start_line - 1 + line_count]
    end_line = start_line + len(selected) - 1 if selected else 0
    numbered = "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(selected, start_line)
    )
    return {
        "path": relative,
        "encoding": encoding,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": len(lines),
        "text": numbered,
        "has_more": end_line < len(lines),
    }, relative, len(raw)


def _find_symbol(
    service: Any,
    grant: Any,
    arguments: dict[str, Any],
    service_module: Any,
) -> tuple[dict[str, Any], str, int]:
    target, relative = service._resolve_target(
        grant,
        arguments.get("path", "."),
        allow_root=True,
    )
    if not target.is_dir():
        raise service_module.FolderAccessError("not_directory", "符号检索范围必须是文件夹。")
    name = _required_text(arguments.get("name"), "name", service_module)
    if len(name) > 200:
        raise service_module.FolderAccessError("invalid_arguments", "符号名过长。")
    max_results = _bounded_int(
        arguments.get("max_results", 50),
        1,
        MAX_CODE_RESULTS,
        "max_results",
        service_module,
    )
    root = Path(grant.root_path).expanduser().resolve(strict=True)
    patterns = _symbol_patterns(name)
    definitions: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    scanned = 0
    bytes_read = 0

    for file_path in _iter_project_files(root, target):
        if scanned >= MAX_CODE_SCAN_FILES or len(definitions) >= max_results:
            break
        scanned += 1
        try:
            size = file_path.stat().st_size
        except OSError:
            continue
        if size > service_module.MAX_READ_BYTES:
            continue
        try:
            raw = file_path.read_bytes()
            text, encoding = _decode_source_text(raw, service_module)
        except (OSError, service_module.FolderAccessError):
            continue
        bytes_read += len(raw)
        rel = file_path.relative_to(root).as_posix()
        for line_number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if any(pattern.search(line) for pattern in patterns):
                definitions.append(
                    {
                        "path": rel,
                        "line": line_number,
                        "encoding": encoding,
                        "text": stripped[:MAX_SNIPPET_CHARS],
                    }
                )
                if len(definitions) >= max_results:
                    break
            elif len(references) < min(max_results, 30) and re.search(
                rf"(?<![\w$]){re.escape(name)}(?![\w$])",
                line,
            ):
                references.append(
                    {
                        "path": rel,
                        "line": line_number,
                        "text": stripped[:MAX_SNIPPET_CHARS],
                    }
                )
    return {
        "definitions": definitions,
        "reference_samples": references,
        "files_scanned": scanned,
        "bytes_read": bytes_read,
        "truncated": scanned >= MAX_CODE_SCAN_FILES or len(definitions) >= max_results,
    }, relative, bytes_read


def _iter_project_files(root: Path, start: Path) -> Iterable[Path]:
    yielded = 0
    for current, directories, files in os.walk(start, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directories, key=str.casefold):
            if name in IGNORED_DIRECTORY_NAMES:
                continue
            candidate = current_path / name
            try:
                resolved = candidate.resolve(strict=True)
                if resolved.is_dir() and resolved.is_relative_to(root):
                    safe_directories.append(name)
            except (OSError, RuntimeError):
                continue
        directories[:] = safe_directories
        for name in sorted(files, key=str.casefold):
            candidate = current_path / name
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file() or not resolved.is_relative_to(root):
                    continue
            except (OSError, RuntimeError):
                continue
            yield resolved
            yielded += 1
            if yielded >= MAX_CODE_SCAN_FILES:
                return


def _decode_source_text(data: bytes, service_module: Any) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise service_module.FolderAccessError("binary_file", "文件不是可识别的文本。") from exc
        return text, "utf-16"
    if b"\x00" in data:
        raise service_module.FolderAccessError("binary_file", "拒绝读取二进制文件。")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(text):
            return text, encoding
    raise service_module.FolderAccessError("binary_file", "文件不是可识别的文本编码。")


def _looks_like_text(text: str) -> bool:
    if not text:
        return True
    sample = text[:10000]
    printable = sum(char.isprintable() or char in "\r\n\t" for char in sample)
    return printable / max(1, len(sample)) >= 0.90


def _symbol_patterns(name: str) -> list[re.Pattern[str]]:
    escaped = re.escape(name)
    return [
        re.compile(rf"^\s*(?:async\s+def|def|class)\s+{escaped}\b"),
        re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b"),
        re.compile(rf"^\s*(?:export\s+)?class\s+{escaped}\b"),
        re.compile(rf"^\s*(?:const|let|var)\s+{escaped}\s*="),
        re.compile(rf"^\s*func\s+(?:\([^)]*\)\s*)?{escaped}\b"),
        re.compile(rf"^\s*type\s+{escaped}\b"),
        re.compile(rf"\b(?:class|interface|struct|enum|trait)\s+{escaped}\b"),
        re.compile(rf"^\s*(?:public|private|protected|static|final|virtual|override|async|\s)+[\w<>,\[\]?]+\s+{escaped}\s*\("),
    ]


def _required_text(value: Any, name: str, service_module: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise service_module.FolderAccessError("invalid_arguments", f"{name} 不能为空。")
    text = value.strip()
    if len(text) > MAX_PATTERN_LENGTH:
        raise service_module.FolderAccessError("invalid_arguments", f"{name} 过长。")
    return text


def _bounded_int(
    value: Any,
    minimum: int,
    maximum: int,
    name: str,
    service_module: Any,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise service_module.FolderAccessError("invalid_arguments", f"{name} 必须是整数。")
    if value < minimum or value > maximum:
        raise service_module.FolderAccessError(
            "invalid_arguments",
            f"{name} 必须在 {minimum} 到 {maximum} 之间。",
        )
    return value
