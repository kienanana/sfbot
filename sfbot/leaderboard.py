"""Per-chat storage and rendering for the daily games leaderboard."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
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


@dataclass(frozen=True, slots=True)
class DailyRank:
    user_id: int
    display_name: str
    points: int


@dataclass(frozen=True, slots=True)
class OverallRank:
    user_id: int
    display_name: str
    chads: int
    chuds: int
    chad_streak: int
    chud_streak: int


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
                PRIMARY KEY (chat_id, user_id)
            ) WITHOUT ROWID
            """
        )
        member_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(group_members)")
        }
        if "display_name" not in member_columns:
            self._connection.execute(
                "ALTER TABLE group_members ADD COLUMN display_name TEXT"
            )
        self._connection.commit()

    def remember_member(
        self,
        *,
        chat_id: int,
        user_id: int,
        seen_at: int,
        display_name: str | None = None,
    ) -> None:
        """Record that a user was seen in the group, which lets them submit by DM."""

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO group_members (chat_id, user_id, seen_at, display_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    seen_at = excluded.seen_at,
                    display_name = COALESCE(excluded.display_name, display_name)
                """,
                (chat_id, user_id, seen_at, display_name),
            )

    def member_names(self, *, chat_id: int) -> dict[int, str]:
        """Return each member's latest known name, from their messages or results."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT m.user_id, COALESCE(m.display_name, (
                    SELECT r.display_name FROM game_results r
                    WHERE r.chat_id = m.chat_id AND r.user_id = m.user_id
                    ORDER BY r.submitted_at DESC LIMIT 1
                ))
                FROM group_members m WHERE m.chat_id = ?
                """,
                (chat_id,),
            ).fetchall()
        return {int(user_id): str(name) for user_id, name in rows if name}

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
    ) -> bool:
        """Record that a chat's daily post is done, returning False if it already was."""

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO posted_days
                   (chat_id, local_date, chad_message, chud_message,
                    chad_winners, chud_winners, daily_ranking)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    local_date,
                    chad_message,
                    chud_message,
                    _encode_winners(chad_winners),
                    _encode_winners(chud_winners),
                    _encode_daily_ranking(daily_ranking),
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
                """SELECT chad_message, chud_message, chad_winners, chud_winners
                   FROM posted_days
                   WHERE chat_id = ? AND local_date = ?""",
                (chat_id, local_date),
            ).fetchone()
        if row is None:
            return None
        return (
            PostedAnnouncement(message=row[0], winners=_decode_winners(row[2])),
            PostedAnnouncement(message=row[1], winners=_decode_winners(row[3])),
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
        self, *, chat_id: int, local_date: str, winners: Sequence[Winner], is_chad: bool
    ) -> dict[int, int]:
        """Count consecutive calendar days ending with the given winners today."""

        column = "chad_winners" if is_chad else "chud_winners"
        streaks = {user_id: 1 for user_id, _ in winners}
        active = set(streaks)
        expected = date.fromisoformat(local_date) - timedelta(days=1)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT local_date, {column} FROM posted_days "
                "WHERE chat_id = ? AND local_date < ? ORDER BY local_date DESC",
                (chat_id, local_date),
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
        self, *, chat_id: int, nicknames: Mapping[int, str] | None = None
    ) -> list[OverallRank]:
        """Count saved titles and active streaks through the latest posted day."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT local_date, chad_winners, chud_winners FROM posted_days "
                "WHERE chat_id = ? ORDER BY local_date",
                (chat_id,),
            ).fetchall()
            names_rows = self._connection.execute(
                "SELECT user_id, display_name FROM game_results "
                "WHERE chat_id = ? ORDER BY submitted_at, local_date",
                (chat_id,),
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


def _total_placings(standings: Sequence[GameStandings]) -> dict[int, int]:
    """Sum placements across boards; skipping costs one worse than last place."""
    totals: dict[int, int] = {}
    for board in standings:
        for entry in board.entries:
            totals.setdefault(entry.user_id, 0)
    for board in standings:
        places = _placings(board.entries)
        skipped = max(places) + 1
        played = {
            entry.user_id: place for entry, place in zip(board.entries, places)
        }
        for user_id in totals:
            totals[user_id] += played.get(user_id, skipped)
    return totals


def _winner_entries(standings: Sequence[GameStandings], *, best: bool) -> list[Entry]:
    """Return one entry per winning player, named in sorted order.

    Players are identified by user ID independently of their display names.
    """

    totals = _total_placings(standings)
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


def format_chad_winners(
    winners: Sequence[Winner],
    *,
    variation: int = 0,
    nicknames: Mapping[int, str] | None = None,
) -> str | None:
    """Name the best total placement, sharing ties, with a fitting closing line."""

    names = _named(winners, nicknames)
    if not names:
        return None

    lines = _CHAD_LINES_BOTH + (
        _CHAD_LINES_SINGULAR if len(names) == 1 else _CHAD_LINES_PLURAL
    )
    verb = "is the CHAD" if len(names) == 1 else "are the CHADs"
    return (
        f"👑 Ding ding ding! {_named_winners(names)} {verb} of the day!\n"
        f"{lines[variation % len(lines)]}"
    )


def format_chud_winners(
    winners: Sequence[Winner],
    *,
    nicknames: Mapping[int, str] | None = None,
) -> str | None:
    """Name whoever did worst across the day's games, sharing ties."""

    names = _named(winners, nicknames)
    if not names:
        return None
    if len(names) == 1:
        return f"🚽 Ding ding ding! {names[0]} is the CHUD of the day!"
    return f"🚽 Ding ding ding! {_named_winners(names)} are the CHUDs of the day!"


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
            DailyRank(int(user_id), str(name), int(points))
            for user_id, name, points in data
        )
    except (ValueError, TypeError):
        return None


def daily_ranking(standings: Sequence[GameStandings]) -> list[DailyRank]:
    """Rank all participants by the placement total used for CHAD and CHUD."""

    totals = _total_placings(standings)
    names = {
        entry.user_id: entry.display_name
        for board in standings
        for entry in board.entries
    }
    return sorted(
        (
            DailyRank(user_id, names[user_id], points)
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
        "Lower is better: sum of game placements; a missed game scores one past last place.",
    ]
    chad_ids = {user_id for user_id, _ in chads}
    chud_ids = {user_id for user_id, _ in chuds}
    names = nicknames or {}
    place = 0
    previous: int | None = None
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
            f"— {rank.points} {unit}"
        )
    return "\n".join(lines)


def format_overall_ranking(ranking: Sequence[OverallRank]) -> str:
    if not ranking:
        return "No CHAD or CHUD titles have been recorded yet."
    lines = [
        "Overall leaderboard",
        "👑 CHADs  🚽 CHUDs  🔥 CHAD streak  💩 CHUD streak",
    ]
    place = 0
    previous: tuple[int, int] | None = None
    for index, rank in enumerate(ranking, start=1):
        score = (rank.chads, rank.chuds)
        if score != previous:
            place = index
            previous = score
        parts = [
            f"{place}. {rank.display_name} — 👑 {rank.chads}",
            f"🚽 {rank.chuds}",
        ]
        if rank.chad_streak > 0:
            parts.append(f"🔥 {rank.chad_streak}")
        if rank.chud_streak > 0:
            parts.append(f"💩 {rank.chud_streak}")
        lines.append("  ".join(parts))
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
