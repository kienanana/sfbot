"""Minimal Telegram Bot API client and long-polling application."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .cache import DuplicateCache
from .games import GAMES, parse_result
from .leaderboard import (
    GameStandings,
    LeaderboardStore,
    format_chud,
    format_standings,
)
from .links import extract_tweet_ids
from .times import due_post_date, due_reminder_date, local_date

LOG = logging.getLogger(__name__)

_LEADERBOARD_COMMAND = "/leaderboard"
_GAMES_COMMAND = "/games"
_ACKNOWLEDGEMENT = "\N{THUMBS UP SIGN}"
_UNKNOWN_SENDER = (
    "I don't have you on the group roster yet. Say anything in the group chat,"
    " then send this again."
)
_NO_NICKNAMES: Mapping[int, str] = MappingProxyType({})


class TelegramAPIError(RuntimeError):
    def __init__(self, status: int, description: str):
        super().__init__(f"Telegram API error {status}: {description}")
        self.status = status
        self.description = description


class TelegramClient:
    def __init__(self, token: str, *, api_root: str = "https://api.telegram.org"):
        self._base_url = f"{api_root.rstrip('/')}/bot{token}"

    def _call(self, method: str, payload: Mapping[str, object], *, timeout: int) -> Any:
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                body = json.load(error)
                description = str(body.get("description", error.reason))
            except (ValueError, AttributeError):
                description = str(error.reason)
            raise TelegramAPIError(error.code, description) from error

        if not result.get("ok"):
            raise TelegramAPIError(
                int(result.get("error_code", 500)),
                str(result.get("description", "unknown error")),
            )
        return result.get("result")

    def get_updates(
        self, *, offset: int | None, poll_timeout: int
    ) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": poll_timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=poll_timeout + 10)
        return result if isinstance(result, list) else []

    def send_sf_reply(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender: Mapping[str, object] | None,
        display_name: str | None = None,
    ) -> None:
        text = "sf"
        entities: list[dict[str, object]] = []
        if sender is not None and isinstance(sender.get("id"), int):
            username = sender.get("username")
            if isinstance(username, str) and username:
                # A real @handle pings them, so it beats any name the bot has.
                text += f" @{username}"
            else:
                name = display_name or sender.get("first_name")
                if not isinstance(name, str) or not name:
                    name = str(sender["id"])
                mention = f"@{name}"
                text += f" {mention}"
                entities.append(
                    {
                        "type": "text_mention",
                        "offset": 3,
                        "length": _utf16_length(mention),
                        "user": {
                            "id": sender["id"],
                            "is_bot": bool(sender.get("is_bot", False)),
                            "first_name": name,
                        },
                    }
                )

        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "reply_parameters": {
                "message_id": message_id,
                "allow_sending_without_reply": False,
            },
        }
        if entities:
            payload["entities"] = entities
        self._call(
            "sendMessage",
            payload,
            timeout=20,
        )

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        entities: list[dict[str, object]] | None = None,
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if entities:
            payload["entities"] = entities
        self._call("sendMessage", payload, timeout=20)

    def set_message_reaction(
        self, *, chat_id: int, message_id: int, emoji: str
    ) -> None:
        self._call(
            "setMessageReaction",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
            timeout=20,
        )


def _callout(action_word: str, display_name: str) -> list[str]:
    """Return the lines that name the repeat poster after the `sf` reply.

    Sent without a parse_mode like everything else the bot says, so the
    asterisks show up literally and no display name needs escaping.
    """

    handle = "".join(display_name.split())
    return [
        f"Uh oh! Looks like {display_name}'s getting *{action_word}d*",
        f"Let's drop a /{action_word}{handle}",
    ]


def _message_body(message: Mapping[str, object]) -> str:
    for key in ("text", "caption"):
        body = message.get(key)
        if isinstance(body, str):
            return body
    return ""


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _games_message() -> tuple[str, list[dict[str, object]]]:
    """Build a game list with links on the names and Telegram-safe offsets."""

    text = "Games:"
    entities: list[dict[str, object]] = []
    for game in GAMES:
        prefix = f"\n{game.emoji} "
        entities.append(
            {
                "type": "text_link",
                "offset": _utf16_length(text + prefix),
                "length": _utf16_length(game.name),
                "url": game.url,
            }
        )
        text += prefix + game.name
    return text, entities


def _leading_command(message: Mapping[str, object]) -> str | None:
    """Return the message's leading bot command, or None if it has none."""

    entities = message.get("entities")
    if not isinstance(entities, list):
        return None

    for entity in entities:
        if not isinstance(entity, Mapping):
            continue
        if entity.get("type") != "bot_command" or entity.get("offset") != 0:
            continue
        length = entity.get("length")
        if not isinstance(length, int):
            return None
        # An entity length counts UTF-16 units, which matches Python slicing for
        # a leading ASCII command. The @botname suffix is dropped rather than
        # checked because the group runs a single bot.
        return _message_body(message)[:length].split("@", 1)[0].lower()
    return None


def _sender(
    message: Mapping[str, object], nicknames: Mapping[int, str]
) -> tuple[int, str] | None:
    """Return the sender's ID and the name the bot calls them by.

    A configured nickname wins over the Telegram name everywhere the bot names
    someone, which is the leaderboard and the repeat-poster callout.
    """

    sender = message.get("from")
    if not isinstance(sender, Mapping) or not isinstance(sender.get("id"), int):
        return None

    user_id = int(sender["id"])
    nickname = nicknames.get(user_id)
    if nickname:
        return user_id, nickname
    for key in ("first_name", "username"):
        value = sender.get(key)
        if isinstance(value, str) and value:
            return user_id, value
    return user_id, str(user_id)


def _send_leaderboard(
    client: TelegramClient,
    store: LeaderboardStore,
    *,
    board_chat_id: int,
    to_chat_id: int,
    day: str,
    nicknames: Mapping[int, str],
) -> list[GameStandings]:
    """Send the board's standings to a chat, which need not be the board itself."""

    standings = store.standings(
        chat_id=board_chat_id, local_date=day, nicknames=nicknames
    )
    client.send_message(
        chat_id=to_chat_id, text=format_standings(standings, local_date=day)
    )
    return standings


def _record_submission(
    message: Mapping[str, object],
    *,
    store: LeaderboardStore,
    client: TelegramClient,
    board_chat_id: int,
    origin_chat_id: int,
    message_id: int,
    seen_at: int,
    utc_offset_minutes: int,
    is_private: bool,
) -> None:
    result = parse_result(_message_body(message))
    if result is None:
        return

    # Keep Telegram's name in storage so config changes also affect scores
    # submitted before the change. The nickname is applied when rendering.
    sender = _sender(message, _NO_NICKNAMES)
    if sender is None:
        return

    user_id, display_name = sender

    # Posting in the group is itself proof of membership, so only a direct
    # message has to be matched against the roster the bot has learned.
    if is_private and not store.is_member(chat_id=board_chat_id, user_id=user_id):
        LOG.info("Ignored a %s result from unrecognized user %s", result.game, user_id)
        client.send_message(chat_id=origin_chat_id, text=_UNKNOWN_SENDER)
        return

    recorded = store.record(
        chat_id=board_chat_id,
        local_date=local_date(seen_at, utc_offset_minutes=utc_offset_minutes),
        user_id=user_id,
        display_name=display_name,
        result=result,
        submitted_at=seen_at,
    )

    # A direct message costs the group nothing, so it gets a written
    # confirmation. In the group only a reaction is acceptable.
    if not recorded:
        LOG.info("Ignored a repeat %s result from user %s", result.game, user_id)
        if is_private:
            client.send_message(
                chat_id=origin_chat_id,
                text=f"You already submitted {result.game} today.",
            )
        return

    if is_private:
        client.send_message(
            chat_id=origin_chat_id,
            text=f"Recorded {result.game} {result.puzzle_id} - {result.score}",
        )
    else:
        client.set_message_reaction(
            chat_id=origin_chat_id, message_id=message_id, emoji=_ACKNOWLEDGEMENT
        )


def handle_message(
    message: Mapping[str, object],
    *,
    cache: DuplicateCache,
    store: LeaderboardStore,
    client: TelegramClient,
    utc_offset_minutes: int,
    board_chat_id: int | None,
    action_word: str | None = None,
    nicknames: Mapping[int, str] = _NO_NICKNAMES,
) -> None:
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or not isinstance(chat.get("id"), int):
        return
    if not isinstance(message.get("message_id"), int):
        return

    chat_id = int(chat["id"])
    message_id = int(message["message_id"])
    seen_at = int(message.get("date", time.time()))
    is_private = chat.get("type") == "private"
    is_board_group = board_chat_id is not None and chat_id == board_chat_id
    sender = _sender(message, nicknames)
    if sender is not None:
        # The only way to learn the user IDs that SFBOT_NICKNAMES is keyed by.
        LOG.debug("Message from user %s (%s)", sender[0], sender[1])

    # Anything said in the group proves membership, which is what later lets a
    # direct message be trusted. Group Privacy is off, so ordinary chatter counts
    # and nobody has to post a result in the group to get on the roster.
    if is_board_group and sender is not None:
        store.remember_member(chat_id=board_chat_id, user_id=sender[0], seen_at=seen_at)

    command = _leading_command(message)
    if command == _GAMES_COMMAND:
        text, entities = _games_message()
        client.send_message(chat_id=chat_id, text=text, entities=entities)
        return

    if command == _LEADERBOARD_COMMAND:
        if board_chat_id is None:
            LOG.warning(
                "Ignoring /leaderboard: set SFBOT_LEADERBOARD_CHAT_ID to the group's"
                " chat ID. This chat's ID is %s",
                chat_id,
            )
            return
        if not is_private and not is_board_group:
            return
        if is_private and not (
            sender is not None
            and store.is_member(chat_id=board_chat_id, user_id=sender[0])
        ):
            client.send_message(chat_id=chat_id, text=_UNKNOWN_SENDER)
            return
        # The standings always come from the group's board, but the answer goes
        # back to whoever asked, so checking from a DM stays private.
        _send_leaderboard(
            client,
            store,
            board_chat_id=board_chat_id,
            to_chat_id=chat_id,
            day=local_date(seen_at, utc_offset_minutes=utc_offset_minutes),
            nicknames=nicknames,
        )
        return

    tweet_ids = extract_tweet_ids(message)
    if not tweet_ids:
        if board_chat_id is not None and (is_private or is_board_group):
            _record_submission(
                message,
                store=store,
                client=client,
                board_chat_id=board_chat_id,
                origin_chat_id=chat_id,
                message_id=message_id,
                seen_at=seen_at,
                utc_offset_minutes=utc_offset_minutes,
                is_private=is_private,
            )
        return

    for tweet_id in tweet_ids:
        original = cache.find_or_record(
            chat_id=chat_id,
            tweet_id=tweet_id,
            message_id=message_id,
            seen_at=seen_at,
        )
        if original is None:
            continue
        if not cache.claim_reply(
            chat_id=chat_id,
            message_id=message_id,
            tweet_id=tweet_id,
            seen_at=seen_at,
        ):
            continue

        try:
            sender_data = message.get("from")
            client.send_sf_reply(
                chat_id=chat_id,
                message_id=original.message_id,
                sender=sender_data if isinstance(sender_data, Mapping) else None,
                display_name=None if sender is None else sender[1],
            )
        except TelegramAPIError as error:
            description = error.description.lower()
            if (
                error.status == 400
                and "repl" in description
                and "not found" in description
            ):
                cache.replace_origin(
                    chat_id=chat_id,
                    tweet_id=tweet_id,
                    old_message_id=original.message_id,
                    new_message_id=message_id,
                    seen_at=seen_at,
                )
                LOG.info(
                    "Stored origin message was deleted; promoted message %s", message_id
                )
                continue
            raise

        # The callout names whoever reposted, so it goes to the chat rather than
        # onto the original message it would otherwise seem to be about.
        if action_word and sender is not None:
            for line in _callout(action_word, sender[1]):
                client.send_message(chat_id=chat_id, text=line)


def post_due_leaderboard(
    client: TelegramClient,
    store: LeaderboardStore,
    *,
    board_chat_id: int,
    utc_offset_minutes: int,
    post_minute: int,
    now: int | None = None,
    nicknames: Mapping[int, str] = _NO_NICKNAMES,
) -> None:
    """Post the day's leaderboard to the group if it is owed one."""

    day = due_post_date(
        int(time.time()) if now is None else now,
        utc_offset_minutes=utc_offset_minutes,
        post_minute=post_minute,
    )
    if not store.is_awaiting_post(chat_id=board_chat_id, local_date=day):
        return

    # Sending before marking means a failed send is retried on the next poll
    # rather than silently swallowed.
    standings = _send_leaderboard(
        client,
        store,
        board_chat_id=board_chat_id,
        to_chat_id=board_chat_id,
        day=day,
        nicknames=nicknames,
    )
    # Only the daily post crowns a CHUD; /leaderboard stays a plain readout.
    chud = format_chud(standings)
    if chud is not None:
        client.send_message(chat_id=board_chat_id, text=chud)
    store.mark_posted(chat_id=board_chat_id, local_date=day)
    LOG.info("Posted the %s leaderboard to chat %s", day, board_chat_id)


def send_due_reminders(
    client: TelegramClient,
    store: LeaderboardStore,
    *,
    board_chat_id: int,
    utc_offset_minutes: int,
    post_minute: int,
    now: int | None = None,
) -> None:
    """DM everyone the games they still owe, an hour before the day's post."""

    moment = int(time.time()) if now is None else now
    day = due_reminder_date(
        moment, utc_offset_minutes=utc_offset_minutes, post_minute=post_minute
    )
    # Unlike a missed post, a missed reminder is worse than useless once the
    # board it warns about has gone up. The two dates agree exactly outside the
    # hour between a day's reminder and its post.
    if day == due_post_date(
        moment, utc_offset_minutes=utc_offset_minutes, post_minute=post_minute
    ):
        return
    if not store.is_awaiting_reminder(chat_id=board_chat_id, local_date=day):
        return

    submitted = store.submitted_games(chat_id=board_chat_id, local_date=day)
    for user_id in store.members(chat_id=board_chat_id):
        played = submitted.get(user_id, frozenset())
        missing = [game.name for game in GAMES if game.name not in played]
        if not missing:
            continue
        try:
            client.send_message(
                chat_id=user_id,
                text=(
                    f"An hour until the {day} leaderboard. Still to play:"
                    f" {', '.join(missing)}"
                ),
            )
        except TelegramAPIError as error:
            # Usually someone who never pressed Start, or who blocked the bot.
            # Their reminder is not worth holding up everyone else's.
            LOG.info("Could not remind user %s (%s)", user_id, error)

    # Marked once the round is over, so a member added later today is not
    # reminded twice.
    store.mark_reminded(chat_id=board_chat_id, local_date=day)
    LOG.info("Sent the %s reminders for chat %s", day, board_chat_id)


def run_polling(
    client: TelegramClient,
    cache: DuplicateCache,
    store: LeaderboardStore,
    *,
    poll_timeout: int = 30,
    utc_offset_minutes: int = 0,
    board_chat_id: int | None = None,
    post_minute: int | None = None,
    action_word: str | None = None,
    nicknames: Mapping[int, str] = _NO_NICKNAMES,
) -> None:
    offset: int | None = None
    backoff = 1
    LOG.info("sfbot is listening for messages")

    while True:
        try:
            updates = client.get_updates(offset=offset, poll_timeout=poll_timeout)
            backoff = 1
            for update in updates:
                update_id = update.get("update_id")
                message = update.get("message")
                if isinstance(message, Mapping):
                    handle_message(
                        message,
                        cache=cache,
                        store=store,
                        client=client,
                        utc_offset_minutes=utc_offset_minutes,
                        board_chat_id=board_chat_id,
                        action_word=action_word,
                        nicknames=nicknames,
                    )
                # Only acknowledge an update after all of its side effects have
                # succeeded. A transient sendMessage failure is then retried.
                if isinstance(update_id, int):
                    offset = update_id + 1

            if board_chat_id is not None and post_minute is not None:
                send_due_reminders(
                    client,
                    store,
                    board_chat_id=board_chat_id,
                    utc_offset_minutes=utc_offset_minutes,
                    post_minute=post_minute,
                )
                post_due_leaderboard(
                    client,
                    store,
                    board_chat_id=board_chat_id,
                    utc_offset_minutes=utc_offset_minutes,
                    post_minute=post_minute,
                    nicknames=nicknames,
                )
        except (TelegramAPIError, urllib.error.URLError, TimeoutError) as error:
            LOG.warning("Telegram request failed (%s); retrying in %ss", error, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
