from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QSettings

from qq_message_manager.speaking_style_feature import (
    CAT_STYLE_ID,
    SELECTED_STYLE_KEY,
    STYLE_DIMENSIONS,
    SpeakingStyle,
    SpeakingStyleStore,
    _learning_conversation_context,
    _learning_messages,
    _owned_stickers_from_message,
    parse_learning_update,
)
from qq_message_manager.ai_rules_cleanup import _build_clean_chat_messages
from qq_message_manager.sticker_memory import extract_sticker_records_from_event


class SpeakingStyleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            str(Path(self.temp.name) / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        self.store = SpeakingStyleStore(self.settings)

    def tearDown(self) -> None:
        self.settings.clear()
        self.settings.sync()
        self.temp.cleanup()

    def test_cat_preset_has_humanized_dimensions_and_owner(self) -> None:
        self.store.migrate_legacy()
        cat = self.store.find(CAT_STYLE_ID)
        self.assertIsNotNone(cat)
        assert cat is not None
        self.assertIn("黑猫", cat.identity)
        self.assertIn("水门", cat.identity)
        for key, _label in STYLE_DIMENSIONS:
            self.assertTrue(str(getattr(cat, key)).strip(), key)

    def test_legacy_custom_prompt_becomes_editable_style(self) -> None:
        self.settings.setValue("ai/prompt", "说话简短，偶尔使用括号动作")
        self.store.migrate_legacy()
        selected = self.store.selected()
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.name, "旧自定义风格")
        self.assertIn("括号动作", selected.custom_instructions)
        self.assertEqual(str(self.settings.value("ai/prompt", "")), "")

    def test_legacy_shuimen_selection_migrates_to_cat_and_is_removed(self) -> None:
        self.settings.setValue("ai/selected_skill", "shuimen")
        self.settings.setValue("ai/enabled_skills", '["shuimen", "chat_summary"]')
        self.store.migrate_legacy()
        self.assertEqual(self.settings.value(SELECTED_STYLE_KEY), CAT_STYLE_ID)
        enabled = json.loads(str(self.settings.value("ai/enabled_skills")))
        self.assertNotIn("shuimen", enabled)
        self.assertIn("chat_summary", enabled)

    def test_up_to_three_styles_can_learn_and_fourth_is_rejected(self) -> None:
        saved = [
            self.store.save_style(
                SpeakingStyle(
                    name=f"风格 {index}",
                    learning_enabled=True,
                    learning_qq=str(10000 + index),
                )
            )
            for index in range(3)
        ]
        self.assertEqual(len(self.store.active_learners()), 3)
        self.assertTrue(all(self.store.find(style.style_id).learning_enabled for style in saved))
        with self.assertRaisesRegex(ValueError, "最多只能同时学习 3 个"):
            self.store.save_style(
                SpeakingStyle(name="第四个", learning_enabled=True, learning_qq="20000")
            )

    def test_same_message_can_feed_multiple_active_styles_for_same_qq(self) -> None:
        for name in ("甲", "乙"):
            self.store.save_style(
                SpeakingStyle(
                    name=name,
                    learning_enabled=True,
                    learning_qq="10001",
                    learning_interval=5,
                )
            )
        message = SimpleNamespace(sender_id="10001", session_id="group:1", text="样本")
        for _index in range(4):
            self.assertEqual(self.store.append_learning_samples(message), [])
        ready = self.store.append_learning_samples(message)
        self.assertEqual({style.name for style, _samples in ready}, {"甲", "乙"})

    def test_custom_style_can_be_deleted_with_selection_and_learning_state(self) -> None:
        style = self.store.save_style(
            SpeakingStyle(name="旧对话风格", learning_enabled=True, learning_qq="10001")
        )
        self.store.set_selected_id(style.style_id)
        self.assertTrue(self.store.delete(style.style_id))
        self.assertIsNone(self.store.find(style.style_id))
        self.assertEqual(self.store.selected_id(), "")
        self.assertEqual(self.store.active_learners(), [])
        self.assertFalse(self.store.delete(CAT_STYLE_ID))

    def test_learning_runs_after_n_matching_messages_and_updates_dimensions(self) -> None:
        style = self.store.save_style(
            SpeakingStyle(
                name="学习风格",
                learning_enabled=True,
                learning_qq="10001",
                learning_interval=5,
            )
        )
        for index in range(4):
            ready = self.store.append_learning_sample(
                SimpleNamespace(sender_id="10001", session_id="private:10001", text=f"样本 {index}")
            )
            self.assertIsNone(ready)
        ready = self.store.append_learning_sample(
            SimpleNamespace(sender_id="10001", session_id="private:10001", text="样本 4")
        )
        self.assertIsNotNone(ready)
        assert ready is not None
        snapshot, samples = ready
        updates = {key: f"更新后的{label}" for key, label in STYLE_DIMENSIONS}
        updated = self.store.apply_learning_update(
            snapshot.style_id,
            snapshot.revision,
            samples,
            updates,
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.iteration_count, 1)
        self.assertEqual(updated.pending_samples, [])
        self.assertEqual(updated.wording, "更新后的用词与语气")

    def test_other_qq_does_not_enter_learning_samples(self) -> None:
        self.store.save_style(
            SpeakingStyle(name="学习风格", learning_enabled=True, learning_qq="10001")
        )
        ready = self.store.append_learning_sample(
            SimpleNamespace(sender_id="99999", session_id="private:99999", text="不应学习")
        )
        self.assertIsNone(ready)
        self.assertEqual(self.store.active_learner().pending_samples, [])

    def test_configured_qq_learns_in_private_and_groups(self) -> None:
        self.store.save_style(
            SpeakingStyle(
                name="跨会话学习",
                learning_enabled=True,
                learning_qq="10001",
                learning_session_id="private:10001",
                learning_interval=5,
            )
        )
        sessions = ["private:10001", "group:20001", "group:20002", "private:10001"]
        for index, session_id in enumerate(sessions):
            self.assertIsNone(
                self.store.append_learning_sample(
                    SimpleNamespace(sender_id="10001", session_id=session_id, text=f"消息 {index}")
                )
            )
        ready = self.store.append_learning_sample(
            SimpleNamespace(sender_id="10001", session_id="group:20003", text="第五句")
        )
        self.assertIsNotNone(ready)


class SpeakingStyleProtocolTests(unittest.TestCase):
    def test_learning_response_requires_all_dimensions(self) -> None:
        payload = {key: label for key, label in STYLE_DIMENSIONS}
        parsed = parse_learning_update(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(set(parsed), {key for key, _label in STYLE_DIMENSIONS})
        with self.assertRaises(ValueError):
            parse_learning_update('{"wording":"简短"}')

    def test_learning_keeps_only_locally_available_sticker_ids(self) -> None:
        payload = {key: label for key, label in STYLE_DIMENSIONS}
        payload["stickers"] = (
            "<STICKER:mf_owned>：开心时使用\n"
            "<STICKER:mf_missing>：不知道什么时候使用"
        )
        parsed = parse_learning_update(
            json.dumps(payload, ensure_ascii=False),
            allowed_sticker_ids={"mf_owned"},
        )
        self.assertIn("mf_owned", parsed["stickers"])
        self.assertNotIn("mf_missing", parsed["stickers"])

    def test_received_sticker_is_learned_only_when_local_memory_can_send_it(self) -> None:
        event = {
            "message": [{
                "type": "mface",
                "data": {
                    "emoji_id": "11",
                    "emoji_package_id": "22",
                    "key": "secret-key",
                    "summary": "开心",
                },
            }]
        }
        record = extract_sticker_records_from_event(event)[0]

        class Memory:
            def __init__(self, owned: bool) -> None:
                self.owned = owned

            def get(self, sticker_id: str):
                return record if self.owned and sticker_id == record.id else None

        message = SimpleNamespace(raw_event=event)
        learned = _owned_stickers_from_message(Memory(True), message)
        self.assertEqual(learned[0]["id"], record.id)
        self.assertEqual(learned[0]["summary"], "开心")
        self.assertEqual(_owned_stickers_from_message(Memory(False), message), [])

    def test_prompt_combines_dimensions_without_exposing_learning_process(self) -> None:
        style = SpeakingStyle(name="测试", personality="温和", rhythm="短句")
        prompt = style.prompt_block()
        self.assertIn("性格与价值倾向：温和", prompt)
        self.assertIn("句式与聊天节奏：短句", prompt)
        self.assertIn("不要在回复中声称自己正在模仿或学习某人", prompt)

    def test_common_prompt_avoids_forced_participation_when_context_is_unknown(self) -> None:
        fake_ai = SimpleNamespace(
            NO_REPLY_TOKEN="__NO_REPLY__",
            _build_skill_prompt_block=lambda _selected: "",
            _build_sticker_prompt_block=lambda _enabled, _options: "",
        )
        builder = _build_clean_chat_messages(fake_ai)
        messages = builder(
            "测试群",
            "group",
            "",
            "",
            True,
            [{"sender_name": "甲", "text": "那个还是按之前的来", "outgoing": "0"}],
        )
        system_prompt = str(messages[0]["content"])
        self.assertIn("不要假装理解、硬接话或编造背景", system_prompt)
        self.assertIn("无法充分理解话题背景与当前发言含义时，只输出 __NO_REPLY__", system_prompt)

    def test_learning_prompt_rewrites_compact_fields_and_uses_context_only_for_meaning(self) -> None:
        style = SpeakingStyle(name="测试", wording="旧画像")
        messages = _learning_messages(style, [], [])
        system_prompt = messages[0]["content"]
        self.assertIn("重写并压缩现有九维画像", system_prompt)
        self.assertIn("不要在原文末尾不断追加", system_prompt)
        self.assertIn("每个字段最多 200 个中文字符", system_prompt)
        self.assertIn("不得把其他人的表达习惯学到目标画像", system_prompt)

    def test_learning_sample_includes_preceding_other_people_context(self) -> None:
        other = SimpleNamespace(
            sender_id="20001",
            sender_name="其他人",
            text="今晚还是老地方",
            outgoing=False,
        )
        target = SimpleNamespace(
            session_id="group:1",
            sender_id="10001",
            sender_name="目标",
            text="行，照旧",
            outgoing=False,
        )
        window = SimpleNamespace(messages={"group:1": [other, target]})
        context = _learning_conversation_context(window, target)
        self.assertEqual(context[0]["sender_id"], "20001")
        self.assertEqual(context[0]["text"], "今晚还是老地方")


if __name__ == "__main__":
    unittest.main()
