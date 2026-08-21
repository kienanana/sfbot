import tempfile
import unittest
from pathlib import Path

from sfbot.cache import DuplicateCache


class DuplicateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = DuplicateCache(Path(self.temp_dir.name) / "cache.db", retention_seconds=100)

    def tearDown(self) -> None:
        self.cache.close()
        self.temp_dir.cleanup()

    def test_first_message_is_recorded_and_second_returns_origin(self) -> None:
        first = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=50, seen_at=1_000, now=1_000
        )
        duplicate = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=51, seen_at=1_010, now=1_010
        )
        self.assertIsNone(first)
        self.assertEqual(duplicate.message_id, 50)

    def test_expired_origin_is_replaced(self) -> None:
        self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=50, seen_at=1_000, now=1_000
        )
        result = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=60, seen_at=1_101, now=1_101
        )
        duplicate = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=61, seen_at=1_102, now=1_102
        )
        self.assertIsNone(result)
        self.assertEqual(duplicate.message_id, 60)

    def test_cache_is_scoped_to_each_chat(self) -> None:
        self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=50, seen_at=1_000, now=1_000
        )
        self.assertIsNone(
            self.cache.find_or_record(
                chat_id=-2, tweet_id="10", message_id=70, seen_at=1_010, now=1_010
            )
        )

    def test_replayed_original_is_not_a_duplicate(self) -> None:
        self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=50, seen_at=1_000, now=1_000
        )
        replay = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=50, seen_at=1_000, now=1_001
        )
        self.assertIsNone(replay)


if __name__ == "__main__":
    unittest.main()

