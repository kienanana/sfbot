from __future__ import annotations

import logging
import os
from pathlib import Path

from .cache import DuplicateCache
from .telegram import TelegramClient, run_polling


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer") from error
    if value <= 0:
        raise SystemExit(f"{name} must be positive")
    return value


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
    database_path = Path(os.environ.get("SFBOT_DB_PATH", "data/sfbot.db"))

    with DuplicateCache(database_path, retention_seconds=retention_days * 24 * 60 * 60) as cache:
        run_polling(TelegramClient(token), cache, poll_timeout=poll_timeout)


if __name__ == "__main__":
    main()

