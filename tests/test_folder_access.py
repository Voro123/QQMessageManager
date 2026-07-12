from __future__ import annotations

import ast
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QSettings

from qq_message_manager import ai_client
from qq_message_manager.folder_access_agent import (
    FolderAccessAgent,
    FolderAgentProtocolError,
    parse_agent_response,
)
from qq_message_manager.folder_access_feature import FolderAccessController
from qq_message_manager.folder_access_models import FolderGrant
from qq_message_manager.folder_access_service import FolderAccessService
from qq_message_manager.folder_access_store import FolderGrantStore


class FolderAccessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "grant"
        self.root.mkdir()
        self.audit = Path(self.temp.name) / "audit.jsonl"
        self.backups = Path(self.temp.name) / "backups"
        self.grant = FolderGrant(
            grant_id="grant-1",
            alias="A项目",
            description="测试项目",
            root_path=str(self.root),
            allowed_sender_ids=["10001"],
            allowed_extensions=[".txt", ".md", ".py", ".json"],
        ).normalized()
        self.grants = [self.grant]
        self.service = FolderAccessService(
            lambda: self.grants,
            audit_path=self.audit,
            backup_root=self.backups,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute(self, tool: str, arguments: dict, **kwargs):
        return self.service.execute(
            tool,
            kwargs.pop("alias", "A项目"),
            arguments,
            session_id=kwargs.pop("session_id", "group-1"),
            sender_id=kwargs.pop("sender_id", "10001"),
            skill_enabled=kwargs.pop("skill_enabled", True),
        )


class StoreTests(FolderAccessTestCase):
    def test_qsettings_json_save_and_restore(self) -> None:
        settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        store = FolderGrantStore(settings)
        store.save([self.grant])
        loaded = FolderGrantStore(settings).load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].alias, "A项目")
        self.assertEqual(loaded[0].root_path, str(self.root))

    def test_alias_is_trimmed_and_case_insensitively_unique(self) -> None:
        settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        duplicate = FolderGrant(alias=" a项目 ", root_path=str(self.root))
        existing = FolderGrant(alias="A项目", root_path=str(self.root))
        with self.assertRaises(ValueError):
            FolderGrantStore(settings).save([existing, duplicate])


class PermissionAndPathTests(FolderAccessTestCase):
    def test_disabled_skill_cannot_operate(self) -> None:
        result = self.execute("list_directory", {}, skill_enabled=False)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "skill_disabled")

    def test_read_disabled_is_rejected(self) -> None:
        self.grant.read_enabled = False
        result = self.execute("list_directory", {})
        self.assertEqual(result.error_code, "read_disabled")

    def test_write_disabled_is_rejected(self) -> None:
        result = self.execute("write_text", {"path": "a.txt", "content": "x"})
        self.assertEqual(result.error_code, "write_disabled")

    def test_unauthorized_sender_is_rejected(self) -> None:
        result = self.execute("list_directory", {}, sender_id="99999")
        self.assertEqual(result.error_code, "sender_not_allowed")
        self.assertNotIn(str(self.root), result.message)

    def test_authorized_sender_can_read_normal_subdirectory_file(self) -> None:
        child = self.root / "docs"
        child.mkdir()
        (child / "note.txt").write_text("hello\nworld", encoding="utf-8")
        result = self.execute("read_text", {"path": "docs/note.txt", "start_line": 2, "end_line": 2})
        self.assertTrue(result.success)
        self.assertEqual(result.data["text"], "world")
        self.assertNotIn(str(self.root), json.dumps(result.to_model_dict(), ensure_ascii=False))

    def test_parent_escape_is_rejected(self) -> None:
        result = self.execute("read_text", {"path": "../secret.txt"})
        self.assertEqual(result.error_code, "path_escape")

    def test_absolute_path_is_rejected(self) -> None:
        result = self.execute("read_text", {"path": str((self.root / "a.txt").resolve())})
        self.assertEqual(result.error_code, "absolute_path")

    def test_posix_absolute_path_is_rejected_on_every_platform(self) -> None:
        result = self.execute("read_text", {"path": "/etc/passwd"})
        self.assertEqual(result.error_code, "absolute_path")

    def test_windows_drive_path_is_rejected(self) -> None:
        result = self.execute("read_text", {"path": "C:\\Windows\\win.ini"})
        self.assertEqual(result.error_code, "absolute_path")

    def test_unc_path_is_rejected(self) -> None:
        result = self.execute("read_text", {"path": "\\\\server\\share\\a.txt"})
        self.assertEqual(result.error_code, "absolute_path")

    def test_symlink_outside_root_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        result = self.execute("read_text", {"path": "link/secret.txt"})
        self.assertEqual(result.error_code, "path_escape")

    def test_binary_file_is_rejected(self) -> None:
        (self.root / "binary.txt").write_bytes(b"abc\x00def")
        result = self.execute("read_text", {"path": "binary.txt"})
        self.assertEqual(result.error_code, "binary_file")

    def test_large_file_is_rejected(self) -> None:
        (self.root / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        result = self.execute("read_text", {"path": "large.txt"})
        self.assertEqual(result.error_code, "file_too_large")

    def test_directory_listing_and_search_are_bounded(self) -> None:
        for index in range(205):
            (self.root / f"item-{index:03}.txt").write_text("needle\n", encoding="utf-8")
        listing = self.execute("list_directory", {})
        self.assertTrue(listing.success)
        self.assertEqual(len(listing.data["entries"]), 200)
        self.assertTrue(listing.data["truncated"])
        search = self.execute("search_text", {"query": "needle"})
        self.assertTrue(search.success)
        self.assertEqual(len(search.data["matches"]), 50)
        self.assertTrue(search.data["truncated"])


class WriteTests(FolderAccessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.grant.write_enabled = True
        self.grant.write_confirmation_required = False

    def test_write_uses_atomic_replace(self) -> None:
        with patch("qq_message_manager.folder_access_service.os.replace", wraps=os.replace) as replace:
            result = self.execute("write_text", {"path": "note.txt", "content": "new"})
        self.assertTrue(result.success)
        replace.assert_called_once()
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "new")

    def test_expected_sha256_mismatch_rejects_overwrite(self) -> None:
        target = self.root / "note.txt"
        target.write_text("old", encoding="utf-8")
        result = self.execute(
            "write_text",
            {"path": "note.txt", "content": "new", "expected_sha256": "0" * 64},
        )
        self.assertEqual(result.error_code, "file_changed")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_overwrite_creates_backup(self) -> None:
        target = self.root / "note.txt"
        target.write_text("old", encoding="utf-8")
        expected = hashlib.sha256(b"old").hexdigest()
        result = self.execute(
            "write_text",
            {"path": "note.txt", "content": "new", "expected_sha256": expected},
        )
        self.assertTrue(result.success)
        backups = list((self.backups / self.grant.grant_id).iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "old")

    def test_write_size_limit_and_audit_redaction(self) -> None:
        result = self.execute(
            "write_text",
            {"path": "too-large.txt", "content": "x" * (512 * 1024 + 1)},
        )
        self.assertEqual(result.error_code, "write_too_large")
        audit_text = self.audit.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), audit_text)
        self.assertNotIn("x" * 100, audit_text)


class AgentTests(FolderAccessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.grant.write_enabled = True
        self.responses: list[str] = []
        self.captured_messages: list[list[dict]] = []

        def completion(_config, messages, **_kwargs):
            self.captured_messages.append(messages)
            return self.responses.pop(0)

        self.agent = FolderAccessAgent(self.service, completion, skill_enabled=lambda: True)

    def test_write_confirmation_requires_same_session_and_sender(self) -> None:
        self.grant.write_confirmation_required = True
        self.responses = [json.dumps({
            "kind": "tool", "tool": "write_text", "alias": "A项目",
            "arguments": {"path": "plan.txt", "content": "hello", "create_only": True},
        }, ensure_ascii=False)]
        reply = self.agent.run(None, user_text="写入A项目", session_id="s1", sender_id="10001", required_alias="A项目")
        action_id = next(iter(self.agent.pending_actions))
        self.assertIn(action_id, reply)
        denied = self.agent.confirm(action_id, session_id="s2", sender_id="10001")
        self.assertIn("不能确认", denied)
        self.assertIn(action_id, self.agent.pending_actions)
        success = self.agent.confirm(action_id, session_id="s1", sender_id="10001")
        self.assertIn("已完成", success)
        self.assertEqual((self.root / "plan.txt").read_text(encoding="utf-8"), "hello")

    def test_confirmation_expiry_is_rejected(self) -> None:
        self.grant.write_confirmation_required = True
        clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        self.agent.now_provider = lambda: clock[0]
        self.responses = [json.dumps({
            "kind": "tool", "tool": "write_text", "alias": "A项目",
            "arguments": {"path": "plan.txt", "content": "hello"},
        }, ensure_ascii=False)]
        self.agent.run(None, user_text="写入A项目", session_id="s1", sender_id="10001", required_alias="A项目")
        action_id = next(iter(self.agent.pending_actions))
        clock[0] += timedelta(minutes=6)
        reply = self.agent.confirm(action_id, session_id="s1", sender_id="10001")
        self.assertIn("过期", reply)
        self.assertFalse((self.root / "plan.txt").exists())

    def test_malformed_unknown_and_extra_fields_are_rejected(self) -> None:
        for raw in (
            "not json",
            '{"kind":"tool","tool":"delete_file","alias":"A项目","arguments":{}}',
            '{"kind":"final","text":"ok","extra":1}',
        ):
            with self.assertRaises(FolderAgentProtocolError):
                parse_agent_response(raw)

    def test_write_confirmation_is_disabled_by_default(self) -> None:
        self.assertFalse(FolderGrant().write_confirmation_required)
        restored = FolderGrant.from_dict({"alias": "A项目", "root_path": str(self.root)})
        self.assertFalse(restored.write_confirmation_required)

    def test_more_than_four_tool_steps_can_reach_final_reply(self) -> None:
        request = json.dumps({
            "kind": "tool", "tool": "list_directory", "alias": "A项目", "arguments": {},
        }, ensure_ascii=False)
        self.responses = [request] * 6 + ['{"kind":"final","text":"排查完成"}']
        reply = self.agent.run(None, user_text="列出A项目", session_id="s1", sender_id="10001", required_alias="A项目")
        self.assertEqual(reply, "排查完成")
        self.assertEqual(len(self.captured_messages), 7)

    def test_agent_can_select_an_authorized_alias_not_written_in_message(self) -> None:
        second = FolderGrant(
            grant_id="grant-2",
            alias="跑团代码",
            description="AI 房间回复和跑团流程实现",
            root_path=str(self.root),
            allowed_sender_ids=["10001"],
            allowed_extensions=[".txt", ".py"],
        ).normalized()
        self.grants.append(second)
        self.responses = [
            json.dumps({
                "kind": "tool", "tool": "list_directory", "alias": "跑团代码", "arguments": {},
            }, ensure_ascii=False),
            '{"kind":"final","text":"已经定位回复流程"}',
        ]
        reply = self.agent.run(
            None,
            user_text="每次 AI 房间内回复会经过什么操作？",
            session_id="s1",
            sender_id="10001",
            allowed_aliases=["A项目", "跑团代码"],
        )
        self.assertEqual(reply, "已经定位回复流程")
        first_prompt = json.dumps(self.captured_messages[0], ensure_ascii=False)
        self.assertIn("跑团代码", first_prompt)
        self.assertIn("AI 房间回复和跑团流程实现", first_prompt)

    def test_model_context_never_contains_root_path(self) -> None:
        self.responses = ['{"kind":"final","text":"完成"}']
        self.agent.run(
            None,
            user_text=f"查看A项目，原路径是 {self.root}",
            session_id="s1",
            sender_id="10001",
            required_alias="A项目",
        )
        serialized = json.dumps(self.captured_messages[0], ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertIn("A项目", serialized)

    def test_prompt_defines_project_main_directory_as_grant_root(self) -> None:
        self.responses = ['{"kind":"final","text":"准备创建"}']
        self.agent.run(
            None,
            user_text="请在项目主目录创建 1.txt，内容为 123",
            session_id="s1",
            sender_id="10001",
            required_alias="A项目",
        )
        system_prompt = str(self.captured_messages[0][0]["content"])
        self.assertIn("每个 alias 本身就代表对应授权文件夹的根目录", system_prompt)
        self.assertIn("write_text 的 path 使用文件名本身，例如 1.txt", system_prompt)
        self.assertIn("不要因此再次询问目录 alias 或精确目录名", system_prompt)

    def test_tool_file_content_is_redacted_before_returning_to_model(self) -> None:
        (self.root / "paths.txt").write_text(f"configured={self.root}", encoding="utf-8")
        self.responses = [
            json.dumps({
                "kind": "tool", "tool": "read_text", "alias": "A项目",
                "arguments": {"path": "paths.txt"},
            }, ensure_ascii=False),
            '{"kind":"final","text":"完成"}',
        ]
        self.agent.run(None, user_text="查看A项目文件", session_id="s1", sender_id="10001", required_alias="A项目")
        serialized = json.dumps(self.captured_messages[-1], ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertIn("[已隐藏的授权路径]", serialized)


class RoutingAndCompatibilityTests(FolderAccessTestCase):
    def _controller(self, enabled: bool) -> FolderAccessController:
        controller = FolderAccessController.__new__(FolderAccessController)
        controller._load_grants = lambda: self.grants
        controller.skill_enabled = lambda: enabled
        return controller

    def test_latest_authorized_sender_routes_to_agent(self) -> None:
        message = SimpleNamespace(
            historical=False, outgoing=False, text="请读取A项目的README文件", sender_id="10001",
        )
        route = self._controller(True).route(message)
        self.assertIsNotNone(route)
        self.assertEqual(route.alias, "A项目")

    def test_authorized_message_without_exact_alias_lets_agent_choose(self) -> None:
        message = SimpleNamespace(
            historical=False, outgoing=False,
            text="帮我检查 AI 房间项目的回复流程", sender_id="10001",
        )
        route = self._controller(True).route(message)
        self.assertIsNotNone(route)
        self.assertFalse(route.immediate_reply)
        self.assertEqual(route.aliases, ("A项目",))

    def test_history_and_outgoing_messages_never_route(self) -> None:
        controller = self._controller(True)
        for historical, outgoing in ((True, False), (False, True)):
            message = SimpleNamespace(
                historical=historical, outgoing=outgoing,
                text="请读取A项目文件", sender_id="10001",
            )
            self.assertIsNone(controller.route(message))

    def test_skill_off_does_not_route_or_change_ordinary_reply(self) -> None:
        message = SimpleNamespace(
            historical=False, outgoing=False, text="请读取A项目的README文件", sender_id="10001",
        )
        self.assertIsNone(self._controller(False).route(message))
        config = ai_client.AiReplyConfig(provider=ai_client.AI_PROVIDER_OPENAI, api_key="test")
        with patch.object(ai_client, "generate_raw_completion", return_value="正常回复"):
            reply = ai_client.generate_ai_reply(
                config, session_name="测试", session_kind="group",
                context_messages=[{"sender_name": "用户", "text": "你好", "outgoing": "0"}],
            )
        self.assertEqual(reply, "正常回复")

    def test_untrusted_sender_without_alias_does_not_learn_aliases(self) -> None:
        message = SimpleNamespace(
            historical=False, outgoing=False, text="请帮我读取项目文件", sender_id="99999",
        )
        route = self._controller(True).route(message)
        self.assertIsNotNone(route)
        self.assertIn("没有操作受控文件夹的权限", route.immediate_reply)
        self.assertNotIn("A项目", route.immediate_reply)


class ForbiddenApiTests(unittest.TestCase):
    def test_folder_modules_do_not_use_execution_apis(self) -> None:
        root = Path(__file__).resolve().parents[1] / "qq_message_manager"
        names = [
            "folder_access_models.py", "folder_access_store.py", "folder_access_service.py",
            "folder_access_agent.py", "folder_access_feature.py",
        ]
        forbidden_imports = {"subprocess"}
        forbidden_calls = {"eval", "exec", "system", "popen", "Popen", "check_call", "check_output"}
        for name in names:
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    self.assertTrue(forbidden_imports.isdisjoint(modules), name)
                if isinstance(node, ast.Call):
                    called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                    if isinstance(node.func, ast.Name) or called != "exec":
                        self.assertNotIn(called, forbidden_calls, name)


if __name__ == "__main__":
    unittest.main()
