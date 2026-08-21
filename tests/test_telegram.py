import tempfile
import unittest
from pathlib import Path

from sfbot.cache import DuplicateCache
from sfbot.telegram import TelegramAPIError, handle_message


class FakeTelegramClient:
    def __init__(self) -> None:
        self.replies: list[tuple[int, int]] = []

    def send_sf_reply(self, *, chat_id: int, message_id: int) -> None:
        self.replies.append((chat_id, message_id))


class MissingReplyClient(FakeTelegramClient):
    def send_sf_reply(self, *, chat_id: int, message_id: int) -> None:
        raise TelegramAPIError(400, "Bad Request: message to be replied not found")


class HandleMessageTests(unittest.TestCase):
    def test_duplicate_replies_to_original_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with DuplicateCache(Path(directory) / "cache.db") as cache:
                client = FakeTelegramClient()
                first = {
                    "chat": {"id": -100},
                    "message_id": 41,
                    "date": 2_000_000_000,
                    "text": "https://twitter.com/alice/status/123?s=20",
                }
                second = {
                    "chat": {"id": -100},
                    "message_id": 99,
                    "date": 2_000_000_010,
                    "text": "https://x.com/bob/status/123",
                }
                handle_message(first, cache=cache, client=client)  # type: ignore[arg-type]
                handle_message(second, cache=cache, client=client)  # type: ignore[arg-type]
                self.assertEqual(client.replies, [(-100, 41)])

    def test_deleted_origin_promotes_current_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with DuplicateCache(Path(directory) / "cache.db") as cache:
                first = {
                    "chat": {"id": -100},
                    "message_id": 41,
                    "date": 2_000_000_000,
                    "text": "https://x.com/alice/status/123",
                }
                second = {**first, "message_id": 42, "date": 2_000_000_010}
                third = {**first, "message_id": 43, "date": 2_000_000_020}

                handle_message(first, cache=cache, client=FakeTelegramClient())  # type: ignore[arg-type]
                handle_message(second, cache=cache, client=MissingReplyClient())  # type: ignore[arg-type]
                working_client = FakeTelegramClient()
                handle_message(third, cache=cache, client=working_client)  # type: ignore[arg-type]

                self.assertEqual(working_client.replies, [(-100, 42)])


if __name__ == "__main__":
    unittest.main()
