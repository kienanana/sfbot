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

    def test_exact_expiration_boundary(self) -> None:
        # Retention is 100s. Message seen at 1000.
        self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=50, seen_at=1_000, now=1_000
        )
        # Exactly at cutoff (1000 + 100 = 1100), cutoff is 1100 - 100 = 1000.
        # seen_at (1000) is NOT < cutoff (1000), so it is still active.
        boundary_dup = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=51, seen_at=1_100, now=1_100
        )
        self.assertIsNotNone(boundary_dup)
        self.assertEqual(boundary_dup.message_id, 50)

        # 1 second past cutoff (1101 - 100 = 1001).
        # seen_at (1000) is < cutoff (1001), so origin has expired and is pruned.
        expired = self.cache.find_or_record(
            chat_id=-1, tweet_id="10", message_id=52, seen_at=1_101, now=1_101
        )
        self.assertIsNone(expired)

    def test_update_offset_persistence(self) -> None:
        self.assertIsNone(self.cache.get_last_update_id())
        self.cache.set_last_update_id(12345)
        self.assertEqual(self.cache.get_last_update_id(), 12345)
        self.cache.set_last_update_id(12346)
        self.assertEqual(self.cache.get_last_update_id(), 12346)

    def test_replayed_duplicate_reply_tracking(self) -> None:
        self.assertFalse(self.cache.has_replied(chat_id=-1, message_id=51, tweet_id="10"))
        self.cache.record_reply(chat_id=-1, message_id=51, tweet_id="10", seen_at=1_000)
        self.assertTrue(self.cache.has_replied(chat_id=-1, message_id=51, tweet_id="10"))

        # Prune check: now at 1105 (cutoff = 1005 > 1000)
        self.cache.find_or_record(chat_id=-1, tweet_id="99", message_id=99, seen_at=1_105, now=1_105)
        self.assertFalse(self.cache.has_replied(chat_id=-1, message_id=51, tweet_id="10"))



if __name__ == "__main__":
    unittest.main()

