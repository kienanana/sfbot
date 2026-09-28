from __future__ import annotations

import logging
import os
from pathlib import Path

from .cache import DuplicateCache
from .leaderboard import LeaderboardStore
from .telegram import TelegramClient, run_polling
from .times import parse_daily_time


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer") from error
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


def _offset_minutes(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer") from error
    if not -1440 < value < 1440:
        raise SystemExit(f"{name} must be within one day of UTC")
    return value


def _board_chat_id() -> int | None:
    """Return the group the leaderboard belongs to, or None when unconfigured."""

    raw = os.environ.get("SFBOT_LEADERBOARD_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as error:
        raise SystemExit("SFBOT_LEADERBOARD_CHAT_ID must be an integer") from error


def _post_minute() -> int | None:
    """Return the daily post time in local minutes, or None when disabled."""

    raw = os.environ.get("SFBOT_LEADERBOARD_AT", "21:00").strip()
    if not raw or raw.lower() == "off":
        return None
    try:
        return parse_daily_time(raw)
    except ValueError as error:
        raise SystemExit(f"SFBOT_LEADERBOARD_AT must be HH:MM ({error})") from error


def _action_word() -> str | None:
    """Return the callout verb for a repeated link, or None when unconfigured."""

    raw = os.environ.get("SFBOT_ACTION_WORD", "").strip()
    return raw or None


def _nicknames() -> dict[int, str]:
    """Return the nickname to call each user by, empty when unconfigured.

    Keyed by user ID rather than name because Telegram first names get changed
    and collide. Run with SFBOT_LOG_LEVEL=DEBUG to have every group message log
    the ID behind its sender's name.
    """

    raw = os.environ.get("SFBOT_NICKNAMES", "").strip()
    if not raw:
        return {}

    nicknames: dict[int, str] = {}
    for entry in raw.split(","):
        user_id, separator, nickname = entry.partition(":")
        if not separator or not nickname.strip():
            raise SystemExit("SFBOT_NICKNAMES entries must be user_id:nickname")
        try:
            nicknames[int(user_id)] = nickname.strip()
        except ValueError as error:
            raise SystemExit(
                f"SFBOT_NICKNAMES user IDs must be integers, not {user_id.strip()!r}"
            ) from error
    return nicknames


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")

    logging.basicConfig(
        level=os.environ.get("SFBOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    retention_days = _positive_int("SFBOT_RETENTION_DAYS", 5)
    poll_timeout = _positive_int("SFBOT_POLL_TIMEOUT", 30)
    utc_offset_minutes = _offset_minutes("SFBOT_UTC_OFFSET_MINUTES", 480)
    board_chat_id = _board_chat_id()
    post_minute = _post_minute()
    action_word = _action_word()
    nicknames = _nicknames()
    database_path = Path(os.environ.get("SFBOT_DB_PATH", "data/sfbot.db"))

    if board_chat_id is None:
        logging.getLogger(__name__).warning(
            "SFBOT_LEADERBOARD_CHAT_ID is unset; the leaderboard is disabled. Send"
            " /leaderboard in the group to have its chat ID logged."
        )

    with (
        DuplicateCache(
            database_path, retention_seconds=retention_days * 24 * 60 * 60
        ) as cache,
        LeaderboardStore(database_path) as store,
    ):
        run_polling(
            TelegramClient(token),
            cache,
            store,
            poll_timeout=poll_timeout,
            utc_offset_minutes=utc_offset_minutes,
            board_chat_id=board_chat_id,
            post_minute=post_minute,
            action_word=action_word,
            nicknames=nicknames,
        )


if __name__ == "__main__":
    main()
