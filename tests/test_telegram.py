import tempfile
import threading
import unittest
from pathlib import Path

from sfbot.cache import DuplicateCache
from sfbot.telegram import (
    TelegramAPIError,
    TelegramClient,
    handle_message,
    run_polling,
    verify_no_active_webhook,
)


class FakeTelegramClient:
    def __init__(
        self, batches: list[list[dict]] | None = None, *, stop_event: threading.Event | None = None
    ) -> None:
        self.replies: list[tuple[int, int]] = []
        self._batches = batches or []
        self._batch_idx = 0
        self.stop_event = stop_event
        self.requested_offsets: list[int | None] = []
        self.webhook_info: dict = {"url": ""}

    def send_sf_reply(self, *, chat_id: int, message_id: int) -> None:
        self.replies.append((chat_id, message_id))

    def get_webhook_info(self) -> dict:
        return self.webhook_info

    def get_updates(self, *, offset: int | None, poll_timeout: int) -> list[dict]:
        self.requested_offsets.append(offset)
        if self._batch_idx < len(self._batches):
            batch = self._batches[self._batch_idx]
            self._batch_idx += 1
            if self._batch_idx >= len(self._batches) and self.stop_event:
                self.stop_event.set()
            return batch
        if self.stop_event:
            self.stop_event.set()
        return []



class MissingReplyClient(FakeTelegramClient):
    def send_sf_reply(self, *, chat_id: int, message_id: int) -> None:
        raise TelegramAPIError(400, "Bad Request: message to be replied not found")


class ErrorReplyClient(FakeTelegramClient):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def send_sf_reply(self, *, chat_id: int, message_id: int) -> None:
        raise self.error


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

    def test_replayed_duplicate_does_not_send_second_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with DuplicateCache(Path(directory) / "cache.db") as cache:
                client = FakeTelegramClient()
                first = {
                    "chat": {"id": -100},
                    "message_id": 41,
                    "date": 2_000_000_000,
                    "text": "https://twitter.com/alice/status/123",
                }
                second = {
                    "chat": {"id": -100},
                    "message_id": 99,
                    "date": 2_000_000_010,
                    "text": "https://x.com/bob/status/123",
                }
                handle_message(first, cache=cache, client=client)  # type: ignore[arg-type]
                handle_message(second, cache=cache, client=client)  # type: ignore[arg-type]
                self.assertEqual(len(client.replies), 1)

                # Replay second message (same update replayed after restart or network blip)
                handle_message(second, cache=cache, client=client)  # type: ignore[arg-type]
                self.assertEqual(len(client.replies), 1)

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

    def test_transient_error_is_reraised_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with DuplicateCache(Path(directory) / "cache.db") as cache:
                first = {
                    "chat": {"id": -100},
                    "message_id": 41,
                    "date": 2_000_000_000,
                    "text": "https://x.com/alice/status/123",
                }
                second = {**first, "message_id": 42, "date": 2_000_000_010}
                handle_message(first, cache=cache, client=FakeTelegramClient())  # type: ignore[arg-type]

                client = ErrorReplyClient(TelegramAPIError(502, "Bad Gateway"))
                with self.assertRaises(TelegramAPIError):
                    handle_message(second, cache=cache, client=client)  # type: ignore[arg-type]

    def test_non_retryable_client_error_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with DuplicateCache(Path(directory) / "cache.db") as cache:
                first = {
                    "chat": {"id": -100},
                    "message_id": 41,
                    "date": 2_000_000_000,
                    "text": "https://x.com/alice/status/123",
                }
                second = {**first, "message_id": 42, "date": 2_000_000_010}
                handle_message(first, cache=cache, client=FakeTelegramClient())  # type: ignore[arg-type]

                client = ErrorReplyClient(TelegramAPIError(403, "Forbidden: bot was kicked"))
                # Must not raise so update processing can proceed
                handle_message(second, cache=cache, client=client)  # type: ignore[arg-type]


class WebhookAndClientTests(unittest.TestCase):
    def test_verify_no_active_webhook_raises_when_configured(self) -> None:
        client = FakeTelegramClient()
        client.webhook_info = {"url": "https://example.com/webhook"}
        with self.assertRaises(RuntimeError) as ctx:
            verify_no_active_webhook(client)  # type: ignore[arg-type]
        self.assertIn("Telegram webhook is currently configured", str(ctx.exception))

    def test_verify_no_active_webhook_passes_when_empty(self) -> None:
        client = FakeTelegramClient()
        client.webhook_info = {"url": ""}
        verify_no_active_webhook(client)  # type: ignore[arg-type]

    def test_token_redaction(self) -> None:
        client = TelegramClient("SECRET123TOKEN")
        redacted = client._redact("https://api.telegram.org/botSECRET123TOKEN/getUpdates")
        self.assertNotIn("SECRET123TOKEN", redacted)
        self.assertIn("[REDACTED]", redacted)


class RunPollingTests(unittest.TestCase):
    def test_run_polling_persists_offset_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.db"
            batches = [
                [
                    {
                        "update_id": 100,
                        "message": {
                            "chat": {"id": -10},
                            "message_id": 1,
                            "date": 1000,
                            "text": "https://x.com/u/status/1",
                        },
                    },
                    {
                        "update_id": 101,
                        "message": {
                            "chat": {"id": -10},
                            "message_id": 2,
                            "date": 1005,
                            "text": "https://x.com/u/status/2",
                        },
                    },
                ]
            ]
            stop_event = threading.Event()
            client = FakeTelegramClient(batches=batches, stop_event=stop_event)

            # Polling run 1
            with DuplicateCache(db_path) as cache:
                run_polling(client, cache, poll_timeout=1, stop_event=stop_event)  # type: ignore[arg-type]
                self.assertEqual(cache.get_last_update_id(), 101)

            # Polling run 2 (simulate bot restart)
            with DuplicateCache(db_path) as cache:
                stop_event2 = threading.Event()
                client2 = FakeTelegramClient(batches=[], stop_event=stop_event2)
                run_polling(client2, cache, poll_timeout=1, stop_event=stop_event2)  # type: ignore[arg-type]
                # Next starting offset must be last_update_id + 1 (102)
                self.assertEqual(client2.requested_offsets[0], 102)

    def test_run_polling_isolates_malformed_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.db"
            batches = [
                [
                    {
                        "update_id": 500,
                        # Corrupted message with unhandled type
                        "message": None,
                    },
                    {
                        "update_id": 501,
                        "message": {
                            "chat": {"id": -10},
                            "message_id": 10,
                            "date": 1000,
                            "text": "https://x.com/u/status/99",
                        },
                    },
                ]
            ]
            stop_event = threading.Event()
            client = FakeTelegramClient(batches=batches, stop_event=stop_event)
            with DuplicateCache(db_path) as cache:
                run_polling(client, cache, poll_timeout=1, stop_event=stop_event)  # type: ignore[arg-type]
                self.assertEqual(cache.get_last_update_id(), 501)

    def test_run_polling_stops_on_keyboard_interrupt(self) -> None:
        class InterruptedClient(FakeTelegramClient):
            def get_updates(self, *, offset: int | None, poll_timeout: int) -> list[dict]:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.db"
            client = InterruptedClient()
            with DuplicateCache(db_path) as cache:
                # Must exit cleanly without re-raising KeyboardInterrupt
                run_polling(client, cache, poll_timeout=1)  # type: ignore[arg-type]

    def test_database_closed_on_context_exit(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.db"
            with DuplicateCache(db_path) as cache:
                cache.set_last_update_id(999)
            with self.assertRaises(sqlite3.ProgrammingError):
                cache._connection.execute("SELECT 1")




if __name__ == "__main__":
    unittest.main()

