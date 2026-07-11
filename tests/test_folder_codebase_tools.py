from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qq_message_manager import folder_access_agent as agent_module
from qq_message_manager import folder_access_feature as feature_module
from qq_message_manager import folder_access_service as service_module
from qq_message_manager.folder_access_codebase_tools import (
    MAX_AGENT_ANALYSIS_STEPS,
    install_folder_access_codebase_tools,
)
from qq_message_manager.folder_access_models import FolderGrant
from qq_message_manager.folder_access_unrestricted_types import (
    install_folder_access_unrestricted_types,
)

install_folder_access_unrestricted_types(
    agent_module,
    service_module,
    feature_module,
)
install_folder_access_codebase_tools(
    agent_module,
    service_module,
    feature_module,
)


class CodebaseToolsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.audit = Path(self.temp.name) / "audit.jsonl"
        self.backups = Path(self.temp.name) / "backups"
        self.grant = FolderGrant(
            grant_id="repo-1",
            alias="A项目",
            description="测试代码库",
            root_path=str(self.root),
            allowed_sender_ids=["10001"],
            read_enabled=True,
            write_enabled=False,
        ).normalized()
        self.service = service_module.FolderAccessService(
            lambda: [self.grant],
            audit_path=self.audit,
            backup_root=self.backups,
        )
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n',
            encoding="utf-8",
        )
        package = self.root / "demo"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "service.py").write_text(
            "class Worker:\n"
            "    def run(self, value):\n"
            "        return helper(value)\n\n"
            "def helper(value):\n"
            "    return value + 1\n",
            encoding="utf-8",
        )
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_service.py").write_text(
            "from demo.service import Worker\n\n"
            "def test_worker():\n"
            "    assert Worker().run(1) == 2\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute(self, tool: str, arguments: dict):
        return self.service.execute(
            tool,
            "A项目",
            arguments,
            session_id="group:1",
            sender_id="10001",
            skill_enabled=True,
        )


class RepositoryReadToolTests(CodebaseToolsTestCase):
    def test_project_overview_detects_language_metadata_and_tests(self) -> None:
        result = self.execute("project_overview", {})
        self.assertTrue(result.success)
        languages = {
            item["language"]: item["files"]
            for item in result.data["languages"]
        }
        self.assertGreaterEqual(languages.get("Python", 0), 3)
        self.assertIn("pyproject.toml", result.data["metadata_files"])
        self.assertIn("tests", result.data["test_paths"])
        self.assertNotIn(str(self.root), json.dumps(result.data, ensure_ascii=False))

    def test_find_files_search_code_and_regex(self) -> None:
        files = self.execute(
            "find_files",
            {"query": "service", "glob": "**/*.py"},
        )
        self.assertTrue(files.success)
        self.assertIn(
            "demo/service.py",
            [item["path"] for item in files.data["matches"]],
        )

        search = self.execute(
            "search_code",
            {
                "query": r"def\s+helper",
                "regex": True,
                "glob": "**/*.py",
                "context_lines": 1,
            },
        )
        self.assertTrue(search.success)
        self.assertEqual(search.data["matches"][0]["path"], "demo/service.py")
        self.assertEqual(search.data["matches"][0]["line"], 5)

    def test_symbol_reference_and_line_reads(self) -> None:
        symbol = self.execute(
            "find_symbol",
            {"symbol": "Worker", "glob": "**/*.py"},
        )
        self.assertTrue(symbol.success)
        self.assertEqual(symbol.data["definitions"][0]["path"], "demo/service.py")

        references = self.execute(
            "find_references",
            {"symbol": "Worker", "glob": "**/*.py"},
        )
        self.assertTrue(references.success)
        paths = {item["path"] for item in references.data["references"]}
        self.assertIn("tests/test_service.py", paths)

        around = self.execute(
            "read_around",
            {"path": "demo/service.py", "line": 5, "before": 1, "after": 2},
        )
        self.assertTrue(around.success)
        self.assertIn("5: def helper", around.data["text"])

        batch = self.execute(
            "read_files",
            {
                "files": [
                    {"path": "demo/service.py", "start_line": 1, "end_line": 3},
                    {"path": "tests/test_service.py", "start_line": 1, "end_line": 4},
                ]
            },
        )
        self.assertTrue(batch.success)
        self.assertEqual(len(batch.data["files"]), 2)
        self.assertTrue(all(item["success"] for item in batch.data["files"]))

    def test_project_metadata_and_git_log_are_read_only(self) -> None:
        metadata = self.execute("read_project_metadata", {})
        self.assertTrue(metadata.success)
        self.assertEqual(
            metadata.data["metadata_files"][0]["path"],
            "pyproject.toml",
        )

        git_dir = self.root / ".git"
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "logs").mkdir()
        commit = "a" * 40
        (git_dir / "HEAD").write_text(
            "ref: refs/heads/master\n",
            encoding="ascii",
        )
        (git_dir / "refs" / "heads" / "master").write_text(
            commit + "\n",
            encoding="ascii",
        )
        (git_dir / "logs" / "HEAD").write_text(
            f"{'0' * 40} {commit} Tester <t@example.com> 1700000000 +0000\tcommit: init\n",
            encoding="utf-8",
        )

        log = self.execute("git_log", {"limit": 5})
        self.assertTrue(log.success)
        self.assertEqual(log.data["branch"], "master")
        self.assertEqual(log.data["entries"][0]["commit"], commit)

        status = self.execute("git_status", {})
        self.assertTrue(status.success)
        self.assertFalse(status.data["index_available"])


class RepositoryAgentTests(CodebaseToolsTestCase):
    def test_code_analysis_can_use_more_than_four_steps(self) -> None:
        request = json.dumps(
            {
                "kind": "tool",
                "tool": "project_overview",
                "alias": "A项目",
                "arguments": {},
            },
            ensure_ascii=False,
        )
        responses = [request] * 5 + [
            '{"kind":"final","text":"已完成五步代码库分析"}'
        ]
        calls: list[list[dict]] = []

        def completion(_config, messages, **_kwargs):
            calls.append(messages)
            return responses.pop(0)

        agent = agent_module.FolderAccessAgent(
            self.service,
            completion,
            skill_enabled=lambda: True,
        )
        reply = agent.run(
            SimpleNamespace(provider="test", model="test"),
            user_text="请排查 A项目 的代码报错",
            session_id="group:1",
            sender_id="10001",
            required_alias="A项目",
        )
        self.assertEqual(reply, "已完成五步代码库分析")
        self.assertEqual(len(calls), 6)
        self.assertGreater(MAX_AGENT_ANALYSIS_STEPS, 4)

    def test_protocol_error_is_sent_back_for_two_repair_attempts(self) -> None:
        responses = [
            "不是 JSON",
            json.dumps(
                {
                    "kind": "tool",
                    "tool": "search_code",
                    "alias": "A项目",
                    "arguments": {"query": "helper"},
                },
                ensure_ascii=False,
            ),
            '{"kind":"final","text":"已定位 helper"}',
        ]
        captured: list[list[dict]] = []

        def completion(_config, messages, **_kwargs):
            captured.append(messages)
            return responses.pop(0)

        agent = agent_module.FolderAccessAgent(
            self.service,
            completion,
            skill_enabled=lambda: True,
        )
        reply = agent.run(
            SimpleNamespace(provider="test", model="test"),
            user_text="排查 A项目 helper 的调用问题",
            session_id="group:1",
            sender_id="10001",
            required_alias="A项目",
        )
        self.assertEqual(reply, "已定位 helper")
        serialized = json.dumps(captured[1], ensure_ascii=False)
        self.assertIn("上一条输出不符合工具协议", serialized)

    def test_simple_file_request_still_uses_four_step_limit(self) -> None:
        request = json.dumps(
            {
                "kind": "tool",
                "tool": "list_directory",
                "alias": "A项目",
                "arguments": {},
            },
            ensure_ascii=False,
        )
        responses = [request] * 4
        calls = 0

        def completion(_config, _messages, **_kwargs):
            nonlocal calls
            calls += 1
            return responses.pop(0)

        agent = agent_module.FolderAccessAgent(
            self.service,
            completion,
            skill_enabled=lambda: True,
        )
        reply = agent.run(
            SimpleNamespace(provider="test", model="test"),
            user_text="列出 A项目 文件",
            session_id="group:1",
            sender_id="10001",
            required_alias="A项目",
        )
        self.assertIn("最多 4 个工具步骤", reply)
        self.assertEqual(calls, 4)


if __name__ == "__main__":
    unittest.main()
