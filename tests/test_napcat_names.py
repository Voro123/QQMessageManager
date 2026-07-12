from __future__ import annotations

import unittest

from qq_message_manager.napcat_client import NapCatWorker, _extract_friend_names


class NapCatFriendNameTests(unittest.TestCase):
    def test_friend_list_prefers_remark_then_nickname(self) -> None:
        names = _extract_friend_names([
            {"user_id": 10001, "nickname": "昵称甲", "remark": "备注甲"},
            {"user_id": "10002", "nickname": "昵称乙", "remark": ""},
        ])
        self.assertEqual(names, {"10001": "备注甲", "10002": "昵称乙"})

    def test_friend_list_response_updates_private_session_names(self) -> None:
        worker = NapCatWorker("ws://127.0.0.1:3001")
        updates: list[tuple[str, str]] = []
        worker.session_name_updated.connect(
            lambda session_id, name: updates.append((session_id, name))
        )
        handled = worker._handle_action_response({
            "status": "ok",
            "retcode": 0,
            "echo": "private_friend_list:1",
            "data": [{"user_id": 10001, "nickname": "昵称", "remark": "好友备注"}],
        })
        self.assertTrue(handled)
        self.assertEqual(worker._private_names["10001"], "好友备注")
        self.assertEqual(updates, [("private:10001", "好友备注")])

    def test_cached_friend_name_is_applied_when_history_creates_session(self) -> None:
        worker = NapCatWorker("ws://127.0.0.1:3001")
        worker._private_names["10001"] = "好友昵称"
        batches: list[list] = []
        worker.history_messages_received.connect(lambda messages: batches.append(messages))
        worker._handle_action_response({
            "status": "ok",
            "retcode": 0,
            "echo": "history:private:10001:1",
            "data": {
                "messages": [{
                    "user_id": 10001,
                    "sender": {"user_id": 10001, "nickname": "好友昵称"},
                    "message": [{"type": "text", "data": {"text": "你好"}}],
                    "time": 1700000000,
                    "message_id": "m1",
                }],
            },
        })
        self.assertEqual(batches[0][0].session_name, "好友昵称")


if __name__ == "__main__":
    unittest.main()
