from __future__ import annotations

import unittest
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject

from qq_message_manager.ai_group_mute_guard import (
    install_ai_group_mute_guard,
    parse_bot_group_mute_notice,
)


class MuteNoticeTests(unittest.TestCase):
    def test_bot_member_ban_and_unban_are_detected(self) -> None:
        banned = parse_bot_group_mute_notice(
            {
                "post_type": "notice",
                "notice_type": "group_ban",
                "sub_type": "ban",
                "group_id": 123,
                "self_id": 456,
                "user_id": 456,
                "duration": 600,
            }
        )
        self.assertEqual(banned, ("123", True, 600))

        unbanned = parse_bot_group_mute_notice(
            {
                "post_type": "notice",
                "notice_type": "group_ban",
                "sub_type": "lift_ban",
                "group_id": "123",
                "self_id": "456",
                "user_id": "456",
                "duration": 0,
            }
        )
        self.assertEqual(unbanned, ("123", False, 0))

    def test_other_member_ban_is_ignored(self) -> None:
        self.assertIsNone(
            parse_bot_group_mute_notice(
                {
                    "post_type": "notice",
                    "notice_type": "group_ban",
                    "sub_type": "ban",
                    "group_id": 123,
                    "self_id": 456,
                    "user_id": 789,
                    "duration": 60,
                }
            )
        )

    def test_whole_group_ban_is_detected(self) -> None:
        self.assertEqual(
            parse_bot_group_mute_notice(
                {
                    "post_type": "notice",
                    "notice_type": "group_whole_ban",
                    "sub_type": "ban",
                    "group_id": 123,
                    "self_id": 456,
                    "user_id": 0,
                }
            ),
            ("123", True, 0),
        )


class GuardRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_muted_group_never_reaches_model_request_or_send_handler(self) -> None:
        class FakeWorker:
            def _handle_payload(self, _payload):
                return None

        class FakeWindow(QObject):
            def __init__(self):
                super().__init__()
                self.ai_inflight_sessions = set()
                self.calls = []
                self.logs = []

            def _schedule_after_non_self_message_ai_reply(self, session_id):
                self.calls.append(("schedule", session_id))

            def _maybe_schedule_mention_reply(self, message):
                self.calls.append(("mention", message.session_id))

            def _request_ai_reply(self, session_id, reason):
                self.calls.append(("model", session_id, reason))

            def _handle_ai_reply_ready(self, session_id, reply):
                self.calls.append(("send", session_id, reply))

            def _stop_ai_timer(self, session_id):
                self.calls.append(("stop", session_id))

            def append_log(self, text):
                self.logs.append(text)

        ui_module = SimpleNamespace(MainWindow=FakeWindow)
        napcat_module = SimpleNamespace(NapCatWorker=FakeWorker)
        install_ai_group_mute_guard(ui_module, napcat_module)

        window = FakeWindow()
        window._qqmm_apply_group_mute("123", True, 600)
        window.calls.clear()
        window._request_ai_reply("group:123", "测试")
        window._schedule_after_non_self_message_ai_reply("group:123")
        window._maybe_schedule_mention_reply(SimpleNamespace(session_id="group:123"))
        window.ai_inflight_sessions.add("group:123")
        window._handle_ai_reply_ready("group:123", "不应发送")

        self.assertFalse(any(call[0] == "model" for call in window.calls))
        self.assertFalse(any(call[0] == "send" for call in window.calls))
        self.assertNotIn("group:123", window.ai_inflight_sessions)
        self.assertTrue(any("未调用模型" in text for text in window.logs))

        window._qqmm_apply_group_mute("123", False, 0)
        window._request_ai_reply("group:123", "解除后")
        self.assertTrue(any(call[0] == "model" for call in window.calls))


if __name__ == "__main__":
    unittest.main()
