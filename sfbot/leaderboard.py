"""Per-chat storage and rendering for the daily games leaderboard."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from .games import GAME_BY_NAME, ParsedResult


@dataclass(frozen=True, slots=True)
class Entry:
    display_name: str
    score: str
    rank_key: float


@dataclass(frozen=True, slots=True)
class GameStandings:
    game: str
    puzzle_id: str
    entries: tuple[Entry, ...]


class LeaderboardStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            self.path, timeout=10, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS game_results (
                chat_id INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                game TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                puzzle_id TEXT NOT NULL,
                score TEXT NOT NULL,
                rank_key REAL NOT NULL,
                submitted_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, local_date, game, user_id)
            ) WITHOUT ROWID
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS game_results_local_date ON game_results (local_date)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_days (
                chat_id INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                PRIMARY KEY (chat_id, local_date)
            ) WITHOUT ROWID
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reminded_days (
                chat_id INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                PRIMARY KEY (chat_id, local_date)
            ) WITHOUT ROWID
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                seen_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            ) WITHOUT ROWID
            """
        )
        self._connection.commit()

    def remember_member(self, *, chat_id: int, user_id: int, seen_at: int) -> None:
        """Record that a user was seen in the group, which lets them submit by DM."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO group_members (chat_id, user_id, seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET seen_at = excluded.seen_at
                """,
                (chat_id, user_id, seen_at),
            )

    def is_member(self, *, chat_id: int, user_id: int) -> bool:
        """Return whether a user has ever been seen in the group."""

        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM group_members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return row is not None

    def members(self, *, chat_id: int) -> list[int]:
        """Return every user the bot has seen in the group."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT user_id FROM group_members WHERE chat_id = ? ORDER BY user_id",
                (chat_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def submitted_games(self, *, chat_id: int, local_date: str) -> dict[int, set[str]]:
        """Return the games each person has already submitted on a day."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT user_id, game FROM game_results"
                " WHERE chat_id = ? AND local_date = ?",
                (chat_id, local_date),
            ).fetchall()

        submitted: dict[int, set[str]] = {}
        for user_id, game in rows:
            submitted.setdefault(int(user_id), set()).add(str(game))
        return submitted

    def record(
        self,
        *,
        chat_id: int,
        local_date: str,
        user_id: int,
        display_name: str,
        result: ParsedResult,
        submitted_at: int,
    ) -> bool:
        """Store a result, returning False when that person already submitted.

        The primary key makes the first submission of a game on a day final, so
        nobody can quietly replace a bad score with a better one.
        """

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO game_results (
                    chat_id, local_date, game, user_id, display_name,
                    puzzle_id, score, rank_key, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    local_date,
                    result.game,
                    user_id,
                    display_name,
                    result.puzzle_id,
                    result.score,
                    result.rank_key,
                    submitted_at,
                ),
            )
        return cursor.rowcount == 1

    def standings(self, *, chat_id: int, local_date: str) -> list[GameStandings]:
        """Return one board per game and puzzle, ordered best result first."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT game, puzzle_id, display_name, score, rank_key
                FROM game_results
                WHERE chat_id = ? AND local_date = ?
                ORDER BY game, puzzle_id, rank_key, submitted_at
                """,
                (chat_id, local_date),
            ).fetchall()

        boards: list[GameStandings] = []
        for (game, puzzle_id), game_rows in groupby(
            rows, key=lambda row: (row[0], row[1])
        ):
            grouped = list(game_rows)
            boards.append(
                GameStandings(
                    game=str(game),
                    puzzle_id=str(puzzle_id),
                    entries=tuple(
                        Entry(
                            display_name=str(row[2]),
                            score=str(row[3]),
                            rank_key=float(row[4]),
                        )
                        for row in grouped
                    ),
                )
            )
        return boards

    def is_awaiting_post(self, *, chat_id: int, local_date: str) -> bool:
        """Return whether a day has results for a board that is not yet posted."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM game_results
                    WHERE chat_id = ? AND local_date = ?
                ) AND NOT EXISTS (
                    SELECT 1 FROM posted_days
                    WHERE chat_id = ? AND local_date = ?
                )
                """,
                (chat_id, local_date, chat_id, local_date),
            ).fetchone()
        return bool(row[0])

    def mark_posted(self, *, chat_id: int, local_date: str) -> bool:
        """Record that a chat's daily post is done, returning False if it already was."""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO posted_days (chat_id, local_date) VALUES (?, ?)",
                (chat_id, local_date),
            )
        return cursor.rowcount == 1

    def is_awaiting_reminder(self, *, chat_id: int, local_date: str) -> bool:
        """Return whether a day's reminder round has not been sent yet."""

        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM reminded_days WHERE chat_id = ? AND local_date = ?",
                (chat_id, local_date),
            ).fetchone()
        return row is None

    def mark_reminded(self, *, chat_id: int, local_date: str) -> bool:
        """Record that a day's reminders are done, returning False if they already were."""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO reminded_days (chat_id, local_date) VALUES (?, ?)",
                (chat_id, local_date),
            )
        return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> LeaderboardStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def format_standings(standings: list[GameStandings], *, local_date: str) -> str:
    """Render the per-game boards as plain text.

    Deliberately unformatted: without a parse_mode nothing in a display name
    needs escaping.
    """

    if not standings:
        return f"No daily game results for {local_date} yet."

    lines = [f"Daily games - {local_date}"]
    for board in standings:
        lines.append("")
        game = GAME_BY_NAME.get(board.game)
        heading = f"{game.emoji} {board.game}" if game is not None else board.game
        lines.append(f"{heading} {board.puzzle_id}")
        place = 0
        previous_rank: float | None = None
        for index, entry in enumerate(board.entries, start=1):
            # Equal results share a placement.
            if entry.rank_key != previous_rank:
                place = index
                previous_rank = entry.rank_key
            crown = " 👑" if place == 1 else ""
            lines.append(f"{place}.{crown} {entry.display_name} - {entry.score}")
    return "\n".join(lines)
