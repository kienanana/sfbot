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
        )


if __name__ == "__main__":
    main()
