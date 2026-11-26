"""Per-chat storage and rendering for the daily games leaderboard."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from itertools import groupby
from pathlib import Path

from .games import GAME_BY_NAME, ParsedResult

# A player named in an announcement: their user ID and the name recorded when
# the announcement was made. The ID is what survives a name change.
Winner = tuple[int, str]


@dataclass(frozen=True, slots=True)
class Entry:
    user_id: int
    display_name: str
    score: str
    rank_key: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class GameStandings:
    game: str
    puzzle_id: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class PostedAnnouncement:
    """What a posted day recorded for one of the two announcements."""

    message: str | None
    winners: tuple[Winner, ...] | None
    line: str | None = None


@dataclass(frozen=True, slots=True)
class DailyRank:
    user_id: int
    display_name: str
    points: float


@dataclass(frozen=True, slots=True)
class OverallRank:
    user_id: int
    display_name: str
    chads: int
    chuds: int
    chad_streak: int
    chud_streak: int


@dataclass(frozen=True, slots=True)
class SeasonArchive:
    id: int
    start_date: str | None
    end_date: str  # exclusive
    reason: str
    ranking: tuple[OverallRank, ...]
    final_text: str
    final_sent: bool
    notice_sent: bool


@dataclass(frozen=True, slots=True)
class SFRank:
    user_id: int
    display_name: str
    count: int


class LeaderboardStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Closing a season reads other store methods inside one transaction.
        self._lock = threading.RLock()
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
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (chat_id, local_date, game, user_id)
            ) WITHOUT ROWID
            """
        )
        columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(game_results)")
        }
        if "detail" not in columns:
            self._connection.execute(
                "ALTER TABLE game_results ADD COLUMN detail TEXT NOT NULL DEFAULT ''"
            )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS game_results_local_date ON game_results (local_date)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS posted_days (
                chat_id INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                chad_message TEXT,
                chud_message TEXT,
                chad_winners TEXT,
                chud_winners TEXT,
                daily_ranking TEXT,
                chad_line TEXT,
                chud_line TEXT,
                PRIMARY KEY (chat_id, local_date)
            ) WITHOUT ROWID
            """
        )
        posted_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(posted_days)")
        }
        for column in (
            "chad_message",
            "chud_message",
            "chad_winners",
            "chud_winners",
            "daily_ranking",
            "chad_line",
            "chud_line",
        ):
            if column not in posted_columns:
                self._connection.execute(
                    f"ALTER TABLE posted_days ADD COLUMN {column} TEXT"
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
                name TEXT,
                username TEXT,
                PRIMARY KEY (chat_id, user_id)
            ) WITHOUT ROWID
            """
        )
        member_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(group_members)")
        }
        for column in ("name", "username"):
            if column not in member_columns:
                self._connection.execute(
                    f"ALTER TABLE group_members ADD COLUMN {column} TEXT"
                )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sf_events (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                tweet_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, tweet_id)
            ) WITHOUT ROWID
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS sf_events_ranking ON sf_events (chat_id, user_id)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS announced_versions (
                chat_id INTEGER PRIMARY KEY,
                commit_hash TEXT NOT NULL
            )
            """
        )
        # Settings the group changes by command. A missing key falls back to the
        # environment, so .env stays the default rather than being overwritten.
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                start_date TEXT,
                end_date TEXT NOT NULL,
                reason TEXT NOT NULL,
                request_key TEXT NOT NULL,
                ranking TEXT NOT NULL,
                final_text TEXT NOT NULL,
                final_sent INTEGER NOT NULL DEFAULT 0,
                notice_sent INTEGER NOT NULL DEFAULT 0,
                UNIQUE (chat_id, request_key)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS seasons_chat_id ON seasons (chat_id, id DESC)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS nicknames (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT NOT NULL
            )
            """
        )
        # Message sets start from the lines in code, once, so a line the group
        # deletes stays deleted.
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_lines (
                id INTEGER PRIMARY KEY,
                set_name TEXT NOT NULL,
                text TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS seeded_sets (set_name TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        for set_name, lines in MESSAGE_SETS.items():
            if self._connection.execute(
                "INSERT OR IGNORE INTO seeded_sets (set_name) VALUES (?)", (set_name,)
            ).rowcount:
                self._connection.executemany(
                    "INSERT INTO message_lines (set_name, text) VALUES (?, ?)",
                    [(set_name, line) for line in lines],
                )
        self._connection.commit()

    def record_sf(
        self, *, chat_id: int, message_id: int, tweet_id: str,
        user_id: int, display_name: str,
    ) -> bool:
        """Record one successful SF, even if an update is processed again."""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO sf_events
                   (chat_id, message_id, tweet_id, user_id, display_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, message_id, tweet_id, user_id, display_name),
            )
            return cursor.rowcount == 1

    def sf_ranking(
        self, *, chat_id: int, nicknames: Mapping[int, str] | None = None
    ) -> list[SFRank]:
        """Return lifetime SF totals in this chat, highest first."""

        with self._lock:
            rows = self._connection.execute(
                """SELECT user_id, COUNT(*) FROM sf_events
                   WHERE chat_id = ? GROUP BY user_id""",
                (chat_id,),
            ).fetchall()
            names = self._connection.execute(
                """SELECT user_id, display_name FROM sf_events
                   WHERE chat_id = ? ORDER BY message_id""",
                (chat_id,),
            ).fetchall()
        latest = {int(user_id): str(name) for user_id, name in names}
        overrides = nicknames or {}
        ranking = [
            SFRank(int(user_id), overrides.get(int(user_id), latest[int(user_id)]), int(count))
            for user_id, count in rows
        ]
        return sorted(ranking, key=lambda rank: (-rank.count, rank.display_name.casefold(), rank.user_id))

    def announced_commit(self, *, chat_id: int) -> str | None:
        """Return the last version whose changelog was posted, if any."""

        with self._lock:
            row = self._connection.execute(
                "SELECT commit_hash FROM announced_versions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return None if row is None else row[0]

    def mark_announced(self, *, chat_id: int, commit: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO announced_versions (chat_id, commit_hash)"
                " VALUES (?, ?)",
                (chat_id, commit),
            )

    def setting(self, key: str) -> str | None:
        """Return a value set by command, or None to use the environment's."""

        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )

    def close_season(
        self,
        *,
        chat_id: int,
        end_date: str,
        reason: str,
        request_key: str | None = None,
        nicknames: Mapping[int, str] | None = None,
        month: str | None = None,
    ) -> SeasonArchive | None:
        """Freeze the closing board and advance the season in one transaction.

        ``end_date`` is the first day of the next season. ``request_key`` makes
        a replay of the same reset command return the original archive.
        """

        date.fromisoformat(end_date)
        request_key = request_key or f"{reason}:{end_date}"
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT id FROM seasons WHERE chat_id = ? AND request_key = ?",
                (chat_id, request_key),
            ).fetchone()
            if existing:
                return self.season(chat_id=chat_id, season_id=int(existing[0]))
            start_date = self.setting("season_start")
            if start_date is not None and start_date >= end_date:
                if month is not None:
                    self._connection.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES ('season_month', ?)",
                        (month,),
                    )
                return None
            ranking = self.overall_ranking(
                chat_id=chat_id, nicknames=nicknames, since=start_date, before=end_date
            )
            heading = "Final overall leaderboard"
            if start_date is not None:
                heading += f" since {start_date}"
            has_titles = any(rank.chads or rank.chuds for rank in ranking)
            final_text = (
                format_overall_ranking(ranking, heading=heading)
                if has_titles else f"{heading}\nNo CHAD or CHUD titles were recorded."
            )
            cursor = self._connection.execute(
                """INSERT INTO seasons
                   (chat_id, start_date, end_date, reason, request_key, ranking,
                    final_text, final_sent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, start_date, end_date, reason, request_key,
                    _encode_overall_ranking(ranking), final_text, int(not has_titles),
                ),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('season_start', ?)",
                (end_date,),
            )
            if month is not None:
                self._connection.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('season_month', ?)",
                    (month,),
                )
            return self.season(chat_id=chat_id, season_id=cursor.lastrowid)

    def archive_legacy(self, *, chat_id: int, nicknames: Mapping[int, str]) -> None:
        """Keep pre-archive titles as one labeled period when an old reset exists."""

        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self.setting("season_archive_initialized") is not None:
                return
            start_date = self.setting("season_start")
            if start_date is not None:
                ranking = self.overall_ranking(
                    chat_id=chat_id, nicknames=nicknames, before=start_date
                )
                if any(rank.chads or rank.chuds for rank in ranking):
                    self._connection.execute(
                        """INSERT OR IGNORE INTO seasons
                           (chat_id, start_date, end_date, reason, request_key,
                            ranking, final_text, final_sent, notice_sent)
                           VALUES (?, NULL, ?, 'legacy', ?, ?, ?, 1, 1)""",
                        (
                            chat_id, start_date, f"legacy:{start_date}",
                            _encode_overall_ranking(ranking),
                            format_overall_ranking(
                                ranking,
                                heading=f"Legacy titles before {start_date} (reconstructed)",
                            ),
                        ),
                    )
            self._connection.execute(
                """INSERT OR REPLACE INTO settings (key, value)
                   VALUES ('season_archive_initialized', '1')"""
            )

    def season(self, *, chat_id: int, season_id: int) -> SeasonArchive | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT id, start_date, end_date, reason, ranking, final_text,
                          final_sent, notice_sent FROM seasons
                   WHERE chat_id = ? AND id = ?""",
                (chat_id, season_id),
            ).fetchone()
        return _decode_season(row) if row else None

    def seasons(self, *, chat_id: int) -> list[SeasonArchive]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT id, start_date, end_date, reason, ranking, final_text,
                          final_sent, notice_sent FROM seasons
                   WHERE chat_id = ? ORDER BY id DESC""",
                (chat_id,),
            ).fetchall()
        return [_decode_season(row) for row in rows]

    def pending_seasons(self, *, chat_id: int) -> list[SeasonArchive]:
        return [
            season for season in reversed(self.seasons(chat_id=chat_id))
            if not season.final_sent or not season.notice_sent
        ]

    def mark_season_sent(self, *, chat_id: int, season_id: int, part: str) -> None:
        if part not in ("final_sent", "notice_sent"):
            raise ValueError("unknown season message")
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE seasons SET {part} = 1 WHERE chat_id = ? AND id = ?",
                (chat_id, season_id),
            )

    def nicknames(self) -> dict[int, str]:
        """Return nicknames set by command; an empty one clears an .env nickname."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT user_id, nickname FROM nicknames"
            ).fetchall()
        return {int(user_id): str(nickname) for user_id, nickname in rows}

    def set_nickname(self, *, user_id: int, nickname: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO nicknames (user_id, nickname) VALUES (?, ?)",
                (user_id, nickname),
            )

    def message_lines(self, set_name: str) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT text FROM message_lines WHERE set_name = ? ORDER BY id",
                (set_name,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def add_message_line(self, set_name: str, text: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO message_lines (set_name, text) VALUES (?, ?)",
                (set_name, text),
            )

    def delete_message_line(self, set_name: str, number: int) -> None:
        """Delete the set's line at a 1-based position in message_lines order."""

        with self._lock, self._connection:
            self._connection.execute(
                """DELETE FROM message_lines WHERE id = (
                       SELECT id FROM message_lines WHERE set_name = ?
                       ORDER BY id LIMIT 1 OFFSET ?
                   )""",
                (set_name, number - 1),
            )

    def remember_member(
        self,
        *,
        chat_id: int,
        user_id: int,
        seen_at: int,
        name: str | None = None,
        username: str | None = None,
    ) -> None:
        """Record that a user was seen in the group, which lets them submit by DM.

        Their latest Telegram name and username are kept so commands can name them.
        """

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO group_members (chat_id, user_id, seen_at, name, username)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    seen_at = excluded.seen_at,
                    name = excluded.name,
                    username = excluded.username
                """,
                (chat_id, user_id, seen_at, name, username),
            )

    def member_names(self, *, chat_id: int) -> dict[int, tuple[str | None, str | None]]:
        """Return each member's latest Telegram name and username."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT user_id, name, username FROM group_members WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
        return {int(user_id): (name, username) for user_id, name, username in rows}

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
                    puzzle_id, score, rank_key, submitted_at, detail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    result.detail,
                ),
            )
        return cursor.rowcount == 1

    def standings(
        self,
        *,
        chat_id: int,
        local_date: str,
        nicknames: Mapping[int, str] | None = None,
    ) -> list[GameStandings]:
        """Return one board per game and puzzle, ordered best result first."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT game, puzzle_id, user_id, display_name, score, rank_key, detail,
                       submitted_at
                FROM game_results
                WHERE chat_id = ? AND local_date = ?
                ORDER BY game, puzzle_id, rank_key, submitted_at
                """,
                (chat_id, local_date),
            ).fetchall()

        current_nicknames = nicknames if nicknames is not None else {}
        # A player can change their Telegram name between submissions. Render
        # all their entries with the latest submitted name, but keep identity
        # separate from that label when calculating daily winners.
        names = {
            int(row[2]): str(row[3])
            for row in sorted(rows, key=lambda row: row[7])
        }
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
                            user_id=int(row[2]),
                            display_name=current_nicknames.get(
                                int(row[2]), names[int(row[2])]
                            ),
                            score=str(row[4]),
                            rank_key=float(row[5]),
                            detail=str(row[6]),
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

    def mark_posted(
        self,
        *,
        chat_id: int,
        local_date: str,
        chad_message: str | None = None,
        chud_message: str | None = None,
        chad_winners: Sequence[Winner] | None = None,
        chud_winners: Sequence[Winner] | None = None,
        daily_ranking: Sequence[DailyRank] | None = None,
        chad_line: str | None = None,
        chud_line: str | None = None,
    ) -> bool:
        """Record that a chat's daily post is done, returning False if it already was."""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO posted_days
                   (chat_id, local_date, chad_message, chud_message,
                    chad_winners, chud_winners, daily_ranking, chad_line,
                    chud_line)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    local_date,
                    chad_message,
                    chud_message,
                    _encode_winners(chad_winners),
                    _encode_winners(chud_winners),
                    _encode_daily_ranking(daily_ranking),
                    chad_line,
                    chud_line,
                ),
            )
        return cursor.rowcount == 1

    def posted_announcements(
        self, *, chat_id: int, local_date: str
    ) -> tuple[PostedAnnouncement, PostedAnnouncement] | None:
        """Return what a posted day recorded, or None if it was never posted.

        A day posted by an older build has no saved winners, and the oldest
        posted days have no saved announcement texts either.
        """

        with self._lock:
            row = self._connection.execute(
                """SELECT chad_message, chud_message, chad_winners, chud_winners,
                          chad_line, chud_line
                   FROM posted_days
                   WHERE chat_id = ? AND local_date = ?""",
                (chat_id, local_date),
            ).fetchone()
        if row is None:
            return None
        return (
            PostedAnnouncement(
                message=row[0], winners=_decode_winners(row[2]), line=row[4]
            ),
            PostedAnnouncement(
                message=row[1], winners=_decode_winners(row[3]), line=row[5]
            ),
        )

    def posted_daily_ranking(
        self, *, chat_id: int, local_date: str
    ) -> tuple[DailyRank, ...] | None:
        """Return the ranking frozen when the day was posted, if available."""

        with self._lock:
            row = self._connection.execute(
                "SELECT daily_ranking FROM posted_days WHERE chat_id = ? AND local_date = ?",
                (chat_id, local_date),
            ).fetchone()
        return _decode_daily_ranking(row[0]) if row else None

    def title_streaks(
        self,
        *,
        chat_id: int,
        local_date: str,
        winners: Sequence[Winner],
        is_chad: bool,
        since: str | None = None,
    ) -> dict[int, int]:
        """Count consecutive calendar days ending with the given winners today.

        A streak counts no posted day before ``since``, the start of the season.
        """

        column = "chad_winners" if is_chad else "chud_winners"
        streaks = {user_id: 1 for user_id, _ in winners}
        active = set(streaks)
        expected = date.fromisoformat(local_date) - timedelta(days=1)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT local_date, {column} FROM posted_days "
                "WHERE chat_id = ? AND local_date < ? AND local_date >= ? "
                "ORDER BY local_date DESC",
                (chat_id, local_date, since or ""),
            )
            for posted_date, raw_winners in rows:
                if not active or posted_date != expected.isoformat():
                    break
                past_ids = {
                    user_id for user_id, _ in _decode_winners(raw_winners) or ()
                }
                active &= past_ids
                for user_id in active:
                    streaks[user_id] += 1
                expected -= timedelta(days=1)
        return streaks

    def overall_ranking(
        self,
        *,
        chat_id: int,
        nicknames: Mapping[int, str] | None = None,
        since: str | None = None,
        before: str | None = None,
    ) -> list[OverallRank]:
        """Count saved titles and active streaks through the latest posted day.

        Days are counted from ``since`` up to but excluding ``before``.
        """

        with self._lock:
            rows = self._connection.execute(
                "SELECT local_date, chad_winners, chud_winners FROM posted_days "
                "WHERE chat_id = ? AND local_date >= ? "
                "AND (? IS NULL OR local_date < ?) "
                "ORDER BY local_date",
                (chat_id, since or "", before, before),
            ).fetchall()
            names_rows = self._connection.execute(
                "SELECT user_id, display_name FROM game_results "
                "WHERE chat_id = ? AND local_date >= ? "
                "AND (? IS NULL OR local_date < ?) "
                "ORDER BY submitted_at, local_date",
                (chat_id, since or "", before, before),
            ).fetchall()
        names = {int(user_id): str(name) for user_id, name in names_rows}
        counts: dict[int, list[int]] = {user_id: [0, 0] for user_id in names}
        streaks: dict[int, list[int]] = {}
        previous: date | None = None
        for day, raw_chads, raw_chuds in rows:
            current = date.fromisoformat(day)
            consecutive = (
                previous is not None and current - previous == timedelta(days=1)
            )
            today_streaks: dict[int, list[int]] = {}
            for index, raw in enumerate((raw_chads, raw_chuds)):
                winners = _decode_winners(raw) or ()
                for user_id, name in winners:
                    names.setdefault(user_id, name)
                    counts.setdefault(user_id, [0, 0])[index] += 1
                    yesterday = streaks.get(user_id, [0, 0])[index] if consecutive else 0
                    today_streaks.setdefault(user_id, [0, 0])[index] = yesterday + 1
            streaks = today_streaks
            previous = current
        current_names = nicknames or {}
        ranking = [
            OverallRank(
                user_id, current_names.get(user_id, names.get(user_id, str(user_id))),
                titles[0], titles[1], *streaks.get(user_id, [0, 0]),
            )
            for user_id, titles in counts.items()
        ]
        return sorted(
            ranking,
            key=lambda rank: (-rank.chads, rank.chuds, rank.display_name, rank.user_id),
        )

    def posted_messages(
        self, *, chat_id: int, local_date: str
    ) -> tuple[str | None, str | None] | None:
        """Return the announcements for a posted day, or None if undecided.

        Older posted days have no saved announcement texts.
        """

        announcements = self.posted_announcements(
            chat_id=chat_id, local_date=local_date
        )
        if announcements is None:
            return None
        return (announcements[0].message, announcements[1].message)

    def daily_names(self, *, chat_id: int, local_date: str) -> list[Winner]:
        """Return each player's recorded name that day, for naming them later."""

        with self._lock:
            rows = self._connection.execute(
                """SELECT DISTINCT user_id, display_name FROM game_results
                   WHERE chat_id = ? AND local_date = ?""",
                (chat_id, local_date),
            ).fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]

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


def _placings(entries: Sequence[Entry]) -> list[int]:
    """Return each entry's placement on its board, equal results sharing one."""

    places: list[int] = []
    place = 0
    previous_rank: float | None = None
    for index, entry in enumerate(entries, start=1):
        if entry.rank_key != previous_rank:
            place = index
            previous_rank = entry.rank_key
        places.append(place)
    return places


# A skipped game costs half a place on top of the average, so playing every
# game still counts without one missed game deciding the day outright.
SKIP_PENALTY = Fraction(1, 2)


def _day_scores(standings: Sequence[GameStandings]) -> dict[int, Fraction]:
    """Average each player's placings over the games they played, plus a fixed
    penalty for each game they skipped. Fractions keep equal scores equal."""

    played: dict[int, list[int]] = {}
    for board in standings:
        for entry, place in zip(board.entries, _placings(board.entries)):
            played.setdefault(entry.user_id, []).append(place)
    return {
        user_id: Fraction(sum(places), len(places))
        + SKIP_PENALTY * (len(standings) - len(places))
        for user_id, places in played.items()
    }


def _winner_entries(standings: Sequence[GameStandings], *, best: bool) -> list[Entry]:
    """Return one entry per winning player, named in sorted order.

    Players are identified by user ID independently of their display names.
    """

    totals = _day_scores(standings)
    if not totals:
        return []

    target = min(totals.values()) if best else max(totals.values())
    winning = {user_id for user_id, total in totals.items() if total == target}
    carriers: dict[int, Entry] = {}
    for board in standings:
        for entry in board.entries:
            if entry.user_id in winning:
                carriers.setdefault(entry.user_id, entry)
    return sorted(carriers.values(), key=lambda entry: (entry.display_name, entry.user_id))


def chad_winners(standings: Sequence[GameStandings]) -> list[Winner]:
    """Return whoever earned the best total placement, sharing ties."""

    return [
        (entry.user_id, entry.display_name)
        for entry in _winner_entries(standings, best=True)
    ]


def chud_winners(standings: Sequence[GameStandings]) -> list[Winner]:
    """Return whoever earned the worst total placement, sharing ties."""

    return [
        (entry.user_id, entry.display_name)
        for entry in _winner_entries(standings, best=False)
    ]


def _named_winners(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _named(winners: Sequence[Winner], nicknames: Mapping[int, str] | None) -> list[str]:
    """Return the names to show, current nicknames winning over recorded ones."""

    current = nicknames or {}
    return sorted(current.get(user_id, name) for user_id, name in winners)


_CHAD_LINES_BOTH = (
    "i hope your parents are proud of you! 🥹",
    "the big leagues are calling! 📞",
    "awwwww",
    "chill out! it's just a game!",
    "touch grass! 🌱",
    "cool.",
    "congratulations! 🎉",
    "wahoo!",
    "yippee!",
    "that's super hot! 🔥",
    "everybody clap. 👏",
    "it's like everyone else didn't even try!",
    "*feels the aura* 😈",
)
_CHAD_LINES_SINGULAR = (
    "we gotta audit this guy. 🔎",
    "you're the alpha of the pack! 🐺",
    "i'm from tel aviv and this is my favourite quizzer!",
)
_CHAD_LINES_PLURAL = ("are you guys poly? 👀",)

# CHAD and CHUD lines can also belong to one person, joining the pool on days
# they win. Their sets are stored as "<set>:<user ID>" and start empty.
PERSONAL_SETS = ("chad", "chud")

# The sets the group edits by command, with the lines each starts from. A
# callout line names the repeat poster with {name} and the action word with
# {word}; a CHAD line closes the announcement, drawing on chad and on chad-solo
# or chad-group by how many won; a chud line closes the CHUD announcement.
MESSAGE_SETS: dict[str, tuple[str, ...]] = {
    "callout": ("Uh oh! Looks like {name}'s being *Sent to {word}*",),
    "chad": _CHAD_LINES_BOTH,
    "chad-solo": _CHAD_LINES_SINGULAR,
    "chad-group": _CHAD_LINES_PLURAL,
    "chud": ("Can't add this one to the splitwise!",),
}


def format_chad_winners(
    winners: Sequence[Winner],
    *,
    variation: int = 0,
    nicknames: Mapping[int, str] | None = None,
    line: str | None = None,
) -> str | None:
    """Name the best total placement, sharing ties, with a fitting closing line.

    Without a line, one of the lines in code is chosen by variation.
    """

    names = _named(winners, nicknames)
    if not names:
        return None

    if line is None:
        lines = _CHAD_LINES_BOTH + (
            _CHAD_LINES_SINGULAR if len(names) == 1 else _CHAD_LINES_PLURAL
        )
        line = lines[variation % len(lines)]
    verb = "is the CHAD" if len(names) == 1 else "are the CHADs"
    return f"👑 Ding ding ding! {_named_winners(names)} {verb} of the day!\n{line}"


def format_chud_winners(
    winners: Sequence[Winner],
    *,
    nicknames: Mapping[int, str] | None = None,
    line: str | None = None,
) -> str | None:
    """Name whoever did worst across the day's games, sharing ties.

    A day posted before CHUD lines existed has no line, and gets none.
    """

    names = _named(winners, nicknames)
    if not names:
        return None
    if len(names) == 1:
        message = f"🚽 Ding ding ding! {names[0]} is the CHUD of the day!"
    else:
        message = f"🚽 Ding ding ding! {_named_winners(names)} are the CHUDs of the day!"
    return message if line is None else f"{message}\n{line}"


def format_chad(
    standings: Sequence[GameStandings],
    *,
    variation: int = 0,
    nicknames: Mapping[int, str] | None = None,
) -> str | None:
    """Name the best total placement from a day's standings."""

    return format_chad_winners(
        chad_winners(standings), variation=variation, nicknames=nicknames
    )


def format_chud(
    standings: Sequence[GameStandings],
    *,
    nicknames: Mapping[int, str] | None = None,
) -> str | None:
    """Name whoever did worst across a day's standings, sharing ties."""

    return format_chud_winners(chud_winners(standings), nicknames=nicknames)


def apply_nicknames(
    text: str,
    *,
    players: Sequence[Winner],
    nicknames: Mapping[int, str],
) -> str:
    """Swap the recorded names in a frozen announcement for nicknames.

    Used for announcements a day recorded as text alone, before the winners
    were saved: those are named by whoever the players were at the time. Only
    the line that names them is rewritten, since a CHAD announcement's closing
    line is a joke that may repeat a player's name. A name is replaced only
    where it whole-word matches and belongs to a single nickname, so a name two
    players share is left alone rather than guessed at.
    """

    candidates: dict[str, set[str]] = {}
    for user_id, name in players:
        nickname = nicknames.get(user_id)
        if name and nickname:
            candidates.setdefault(name, set()).add(nickname)
    replacements = {
        name: next(iter(found))
        for name, found in candidates.items()
        if len(found) == 1
    }

    # Longest first, so replacing a shorter name cannot eat part of a longer
    # one that is about to be replaced itself.
    named_line, newline, closing = text.partition("\n")
    for name in sorted(replacements, key=len, reverse=True):
        replacement = replacements[name]
        named_line = re.sub(
            rf"(?<!\w){re.escape(name)}(?!\w)",
            lambda _match, shown=replacement: shown,
            named_line,
        )
    return named_line + newline + closing


def _encode_winners(winners: Sequence[Winner] | None) -> str | None:
    """Render winners as JSON for storage, or None when there are none."""

    if winners is None:
        return None
    pairs = [[user_id, name] for user_id, name in winners]
    return json.dumps(pairs, ensure_ascii=False)


def _decode_winners(raw: object) -> tuple[Winner, ...] | None:
    """Read stored winners, treating anything unreadable as unsaved.

    A day posted before winners were stored, or with a row this build cannot
    parse, falls back to the announcement text it recorded.
    """

    if not isinstance(raw, str):
        return None
    try:
        stored = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(stored, list):
        return None

    winners: list[Winner] = []
    for item in stored:
        if not isinstance(item, list) or len(item) != 2:
            return None
        user_id, name = item
        if not isinstance(name, str):
            return None
        try:
            winners.append((int(user_id), name))
        except (TypeError, ValueError):
            return None
    return tuple(winners) or None


def _encode_daily_ranking(ranking: Sequence[DailyRank] | None) -> str | None:
    if ranking is None:
        return None
    return json.dumps(
        [[rank.user_id, rank.display_name, rank.points] for rank in ranking],
        ensure_ascii=False,
    )


def _decode_daily_ranking(raw: object) -> tuple[DailyRank, ...] | None:
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        return tuple(
            DailyRank(int(user_id), str(name), float(points))
            for user_id, name, points in data
        )
    except (ValueError, TypeError):
        return None


def _encode_overall_ranking(ranking: Sequence[OverallRank]) -> str:
    return json.dumps(
        [
            [rank.user_id, rank.display_name, rank.chads, rank.chuds,
             rank.chad_streak, rank.chud_streak]
            for rank in ranking
        ],
        ensure_ascii=False,
    )


def _decode_season(row: tuple[object, ...]) -> SeasonArchive:
    ranks = json.loads(str(row[4]))
    return SeasonArchive(
        id=int(row[0]),
        start_date=str(row[1]) if row[1] is not None else None,
        end_date=str(row[2]),
        reason=str(row[3]),
        ranking=tuple(OverallRank(*values) for values in ranks),
        final_text=str(row[5]),
        final_sent=bool(row[6]),
        notice_sent=bool(row[7]),
    )


def daily_ranking(standings: Sequence[GameStandings]) -> list[DailyRank]:
    """Rank all participants by the placement total used for CHAD and CHUD."""

    totals = _day_scores(standings)
    names = {
        entry.user_id: entry.display_name
        for board in standings
        for entry in board.entries
    }
    return sorted(
        (
            DailyRank(user_id, names[user_id], float(points))
            for user_id, points in totals.items()
        ),
        key=lambda rank: (rank.points, rank.display_name, rank.user_id),
    )


def format_daily_ranking(
    ranking: Sequence[DailyRank],
    *,
    local_date: str,
    chads: Sequence[Winner],
    chuds: Sequence[Winner],
    nicknames: Mapping[int, str] | None = None,
) -> str:
    """Show the final placing score, marking everyone tied for either title."""

    lines = [
        f"Final rankings - {local_date}",
        "Lower is better: average placing over the games played, plus 0.5 per game"
        " skipped. /scoresystem explains the scoring.",
    ]
    chad_ids = {user_id for user_id, _ in chads}
    chud_ids = {user_id for user_id, _ in chuds}
    names = nicknames or {}
    place = 0
    previous: float | None = None
    for index, rank in enumerate(ranking, start=1):
        if rank.points != previous:
            place = index
            previous = rank.points
        badges = ("👑 " if rank.user_id in chad_ids else "") + (
            "🚽 " if rank.user_id in chud_ids else ""
        )
        unit = "point" if rank.points == 1 else "points"
        lines.append(
            f"{place}. {badges}{names.get(rank.user_id, rank.display_name)} "
            f"— {round(rank.points, 2):g} {unit}"
        )
    return "\n".join(lines)


def format_overall_ranking(
    ranking: Sequence[OverallRank], *, heading: str = "Overall leaderboard"
) -> str:
    if not ranking:
        return "No CHAD or CHUD titles have been recorded yet."
    place_width = len(str(len(ranking)))
    name_width = max(len(rank.display_name) for rank in ranking)
    chad_width = max(len(str(rank.chads)) for rank in ranking)
    chud_width = max(len(str(rank.chuds)) for rank in ranking)
    streak_width = max(
        len(str(streak))
        for rank in ranking
        for streak in (rank.chad_streak, rank.chud_streak)
    )
    lines = [
        heading,
        "👑 CHADs  🚽 CHUDs  🔥/💩 streak",
    ]
    place = 0
    previous: tuple[int, int] | None = None
    for index, rank in enumerate(ranking, start=1):
        score = (rank.chads, rank.chuds)
        if score != previous:
            place = index
            previous = score
        parts = [
            f"{place:>{place_width}}. {rank.display_name:<{name_width}} - 👑 {rank.chads:>{chad_width}}",
            f"🚽 {rank.chuds:>{chud_width}}",
        ]
        if rank.chad_streak > 0:
            parts.append(f"🔥 {rank.chad_streak:>{streak_width}}")
        elif rank.chud_streak > 0:
            parts.append(f"💩 {rank.chud_streak:>{streak_width}}")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def format_sf_ranking(
    ranking: Sequence[SFRank], *, updated_user_id: int | None = None
) -> str:
    """Render SF totals, marking leaders and the latest updated count."""

    if not ranking:
        return "No SFs have been recorded yet."
    lines = ["SF leaderboard"]
    place_width = len(str(len(ranking)))
    name_width = max(len(rank.display_name) for rank in ranking)
    count_width = max(len(str(rank.count)) for rank in ranking)
    place = 0
    previous_count: int | None = None
    for index, rank in enumerate(ranking, start=1):
        if rank.count != previous_count:
            place = index
            previous_count = rank.count
        markers = ("🏆" if place == 1 else "") + (
            "🆕" if rank.user_id == updated_user_id else ""
        )
        marker_column = markers + " " * (4 - 2 * len(markers))
        lines.append(
            f"{place:>{place_width}}. {rank.display_name:<{name_width}}"
            f" - {marker_column} {rank.count:>{count_width}}"
            f" {'SF' if rank.count == 1 else 'SFs'}"
        )
    return "\n".join(lines)


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
        for entry, place in zip(board.entries, _placings(board.entries)):
            crown = " 👑" if place == 1 else ""
            detail = f" {entry.detail}" if entry.detail else ""
            lines.append(
                f"{place}.{crown} {entry.display_name}{detail} - {entry.score}"
            )
    return "\n".join(lines)
