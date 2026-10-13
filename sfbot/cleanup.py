"""Full-history message cleanup over MTProto.

The Bot API cannot read a chat's past messages. Logged in over MTProto with the
same bot token, the bot still cannot list or search history, but it can fetch
messages by ID, and a supergroup numbers its messages from 1. Walking every ID
up to the newest therefore reads the whole chat.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

MIN_NEEDLE_LENGTH = 3
# The most message IDs Telegram accepts in one fetch or one delete.
_BATCH_SIZE = 100
# A long walk is expected to hit flood waits; sleep through them rather than
# abandon a half-swept chat.
_FLOOD_SLEEP_SECONDS = 60 * 60


def needle_pattern(needles: list[str]) -> re.Pattern[str]:
    """Match any needle as a whole word, ignoring case; `*` matches word characters.

    Lookarounds stand in for word boundaries so a needle may start or end with
    punctuation, like a /command.
    """

    words = (re.escape(needle).replace(r"\*", r"\w*") for needle in needles)
    return re.compile(
        "|".join(rf"(?<!\w){word}(?!\w)" for word in words), re.IGNORECASE
    )


async def sweep(
    client: Any, *, chat_id: int, needles: list[str], last_message_id: int
) -> int:
    """Delete every message up to last_message_id that matches any needle.

    Returns how many messages Telegram deleted.
    """

    pattern = needle_pattern(needles)
    chat = await client.get_input_entity(chat_id)
    deleted = 0
    for start in range(1, last_message_id + 1, _BATCH_SIZE):
        ids = list(range(start, min(start + _BATCH_SIZE, last_message_id + 1)))
        messages = await client.get_messages(chat, ids=ids)
        # An ID that was deleted or never belonged to this chat comes back as
        # None, and a service message has no text.
        matching = [
            message.id
            for message in messages
            if message is not None and pattern.search(message.raw_text or "")
        ]
        if matching:
            for affected in await client.delete_messages(chat, matching):
                deleted += affected.pts_count
    return deleted


class Cleaner:
    """Runs one history sweep at a time, off the polling thread."""

    def __init__(self, *, api_id: int, api_hash: str, token: str, session_path: Path):
        # Imported here so the bot and its tests run without Telethon unless
        # /cleanup is configured.
        from telethon import TelegramClient

        self._telethon_client = TelegramClient
        self._api_id = api_id
        self._api_hash = api_hash
        self._token = token
        self._session_path = session_path
        self._busy = threading.Lock()

    def start(
        self,
        *,
        chat_id: int,
        needles: list[str],
        last_message_id: int,
        report: Callable[[str], None],
    ) -> bool:
        """Begin a sweep in the background, or return False if one is running.

        A large chat takes long enough that sweeping inline would hold up
        every other message and the daily post. The outcome goes to report.
        """

        if not self._busy.acquire(blocking=False):
            return False
        threading.Thread(
            target=self._run,
            args=(chat_id, needles, last_message_id, report),
            name="cleanup",
            daemon=True,
        ).start()
        return True

    def _run(
        self,
        chat_id: int,
        needles: list[str],
        last_message_id: int,
        report: Callable[[str], None],
    ) -> None:
        try:
            try:
                deleted = asyncio.run(self._sweep(chat_id, needles, last_message_id))
            except Exception:
                # Nothing above this thread can catch it. The needles are left
                # out of the log like every other message body.
                LOG.exception("Cleanup of chat %s failed", chat_id)
                report(
                    "Cleanup failed partway. Check that I'm an admin who can"
                    " delete messages."
                )
            else:
                LOG.info("Cleanup deleted %s messages from chat %s", deleted, chat_id)
                report(f"Cleaned up {deleted} message{'' if deleted == 1 else 's'}.")
        finally:
            self._busy.release()

    async def _sweep(self, chat_id: int, needles: list[str], last_message_id: int) -> int:
        # The session file spares a fresh bot login per sweep, which Telegram
        # rate limits. It is as sensitive as the token.
        client = self._telethon_client(
            str(self._session_path),
            self._api_id,
            self._api_hash,
            receive_updates=False,
            flood_sleep_threshold=_FLOOD_SLEEP_SECONDS,
        )
        try:
            await client.start(bot_token=self._token)
            return await sweep(
                client,
                chat_id=chat_id,
                needles=needles,
                last_message_id=last_message_id,
            )
        finally:
            await client.disconnect()
