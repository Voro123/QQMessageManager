from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_message_manager import image_cache
from qq_message_manager.models import ChatImage
from qq_message_manager.sticker_send_reliability import (
    _discard_invalid_record,
    _discard_unusable_persisted_records,
)


VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ImageCacheValidationTests(unittest.TestCase):
    def test_empty_base64_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(image_cache, "IMAGE_CACHE_DIR", Path(temp_dir)):
                result = image_cache.ensure_cached(ChatImage(file="base64://"))
        self.assertIsNone(result)

    def test_non_image_response_is_rejected(self) -> None:
        payload = base64.b64encode(b"<html>upstream error</html>").decode("ascii")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(image_cache, "IMAGE_CACHE_DIR", Path(temp_dir)):
                result = image_cache.ensure_cached(ChatImage(file=f"base64://{payload}"))
        self.assertIsNone(result)

    def test_decodable_image_is_cached(self) -> None:
        payload = base64.b64encode(VALID_PNG).decode("ascii")
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(image_cache, "IMAGE_CACHE_DIR", Path(temp_dir)):
                result = image_cache.ensure_cached(ChatImage(file=f"base64://{payload}"))
                self.assertIsNotNone(result)
                self.assertEqual(Path(result).read_bytes(), VALID_PNG)


class InvalidStickerRemovalTests(unittest.TestCase):
    def test_unlocked_invalid_record_is_removed(self) -> None:
        class Memory:
            def __init__(self) -> None:
                self.records = {"bad": object()}

            def is_locked(self, sticker_id: str) -> bool:
                return False

            def delete_record(self, sticker_id: str) -> bool:
                return self.records.pop(sticker_id, None) is not None

        memory = Memory()
        _discard_invalid_record(memory, "bad")
        self.assertNotIn("bad", memory.records)

    def test_locked_invalid_record_is_preserved(self) -> None:
        class Memory:
            def __init__(self) -> None:
                self.records = {"locked": object()}

            def is_locked(self, sticker_id: str) -> bool:
                return True

            def delete_record(self, sticker_id: str) -> bool:
                self.records.pop(sticker_id, None)
                return True

        memory = Memory()
        _discard_invalid_record(memory, "locked")
        self.assertIn("locked", memory.records)

    def test_persisted_records_without_local_images_are_removed_on_load(self) -> None:
        class Record:
            path = ""

        class Memory:
            def __init__(self) -> None:
                self.records = {"bad-a": Record(), "bad-b": Record()}
                self.saved = False

            def is_locked(self, sticker_id: str) -> bool:
                return False

            def save(self) -> None:
                self.saved = True

        memory = Memory()
        removed = _discard_unusable_persisted_records(memory)
        self.assertEqual(removed, 2)
        self.assertEqual(memory.records, {})
        self.assertTrue(memory.saved)

    def test_persisted_locked_record_without_local_image_is_preserved(self) -> None:
        class Record:
            path = ""

        class Memory:
            def __init__(self) -> None:
                self.records = {"locked": Record()}
                self.saved = False

            def is_locked(self, sticker_id: str) -> bool:
                return sticker_id == "locked"

            def save(self) -> None:
                self.saved = True

        memory = Memory()
        removed = _discard_unusable_persisted_records(memory)
        self.assertEqual(removed, 0)
        self.assertIn("locked", memory.records)
        self.assertFalse(memory.saved)

    def test_persisted_decodable_local_image_is_preserved(self) -> None:
        class Record:
            def __init__(self, path: str) -> None:
                self.path = path

        class Memory:
            def __init__(self, path: str) -> None:
                self.records = {"good": Record(path)}
                self.saved = False

            def is_locked(self, sticker_id: str) -> bool:
                return False

            def save(self) -> None:
                self.saved = True

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "valid.png"
            image_path.write_bytes(VALID_PNG)
            memory = Memory(str(image_path))
            removed = _discard_unusable_persisted_records(memory)
        self.assertEqual(removed, 0)
        self.assertIn("good", memory.records)
        self.assertFalse(memory.saved)


if __name__ == "__main__":
    unittest.main()
