"""Persistent, indexed five-day cache for tweet origins."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OriginalMessage:
    message_id: int
    seen_at: int


class DuplicateCache:
    def __init__(self, path: str | Path, retention_seconds: int = 5 * 24 * 60 * 60):
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tweet_origins (
                chat_id INTEGER NOT NULL,
                tweet_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                seen_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, tweet_id)
            ) WITHOUT ROWID
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS tweet_origins_seen_at ON tweet_origins (seen_at)"
        )
        self._connection.commit()

    def find_or_record(
        self,
        *,
        chat_id: int,
        tweet_id: str,
        message_id: int,
        seen_at: int,
        now: int | None = None,
    ) -> OriginalMessage | None:
        """Return the unexpired origin, or record this message as the origin."""

        current_time = int(time.time()) if now is None else now
        cutoff = current_time - self.retention_seconds

        with self._lock, self._connection:
            self._connection.execute("DELETE FROM tweet_origins WHERE seen_at < ?", (cutoff,))
            row = self._connection.execute(
                """
                SELECT message_id, seen_at
                FROM tweet_origins
                WHERE chat_id = ? AND tweet_id = ?
                """,
                (chat_id, tweet_id),
            ).fetchone()

            if row is not None:
                # getUpdates may replay an update after a restart. It must not
                # turn the original message into a duplicate of itself.
                if int(row[0]) == message_id:
                    return None
                return OriginalMessage(message_id=int(row[0]), seen_at=int(row[1]))

            # A delayed update already outside the window cannot become a new
            # origin, but it also should not be called a duplicate.
            if seen_at >= cutoff:
                self._connection.execute(
                    """
                    INSERT INTO tweet_origins (chat_id, tweet_id, message_id, seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chat_id, tweet_id, message_id, seen_at),
                )
            return None

    def replace_origin(
        self, *, chat_id: int, tweet_id: str, old_message_id: int, new_message_id: int, seen_at: int
    ) -> None:
        """Promote a duplicate when Telegram says the stored reply target is gone."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE tweet_origins
                SET message_id = ?, seen_at = ?
                WHERE chat_id = ? AND tweet_id = ? AND message_id = ?
                """,
                (new_message_id, seen_at, chat_id, tweet_id, old_message_id),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "DuplicateCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

