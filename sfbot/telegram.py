"""Minimal Telegram Bot API client and long-polling application."""

from __future__ import annotations

from dataclasses import replace

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from types import MappingProxyType
from typing import Any

from .cache import DuplicateCache
from .games import GAMES, expected_puzzle, parse_result
from .leaderboard import (
    MESSAGE_SETS,
    PERSONAL_SETS,
    GameStandings,
    LeaderboardStore,
    PostedAnnouncement,
    SeasonArchive,
    Winner,
    apply_nicknames,
    chad_winners,
    chud_winners,
    daily_ranking,
    format_chad,
    format_chad_winners,
    format_chud,
    format_chud_winners,
    format_daily_ranking,
    format_overall_ranking,
    format_sf_ranking,
    format_standings,
)
from .links import extract_tweet_ids
from .times import due_post_date, due_reminder_date, local_date, parse_daily_time

LOG = logging.getLogger(__name__)

_LEADERBOARD_COMMAND = "/leaderboard"
_GAMES_COMMAND = "/games"
_SCORE_SYSTEM_COMMAND = "/scoresystem"
_CHAD_COMMAND = "/chad"
_CHUD_COMMAND = "/chud"
_DAILY_COMMAND = "/daily"
_OVERALL_COMMAND = "/overall"
_SEASONS_COMMAND = "/seasons"
_SF_COMMAND = "/sf"
_SETTINGS_COMMAND = "/settings"
_ACTION_WORD_COMMAND = "/actionword"
_NICKNAME_COMMAND = "/nickname"
_POST_TIME_COMMAND = "/posttime"
_RETENTION_COMMAND = "/retention"
_MONTHLY_RESET_COMMAND = "/monthlyreset"
_RESET_OVERALL_COMMAND = "/resetoverall"
_LINES_COMMAND = "/lines"
_ADD_LINE_COMMAND = "/addline"
_DELETE_LINE_COMMAND = "/delline"
_NAMES_COMMAND = "/names"
_CALLOUT_PLACEHOLDERS = {"name", "word"}
_OFF = "off"
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


def _callout(
    template: str, action_word: str, display_name: str
) -> list[tuple[str, list[dict[str, object]]]]:
    """Return the lines, with their entities, that name the repeat poster.

    Text between asterisks in the template is sent as a bold entity rather
    than through a parse_mode, so no display name needs escaping. A template
    with unpaired asterisks keeps them as written.
    """

    parts = re.split(r"\*+", template)
    if len(parts) % 2 == 0:
        parts = [template]
    text = ""
    entities: list[dict[str, object]] = []
    for index, part in enumerate(parts):
        part = part.replace("{name}", display_name).replace("{word}", action_word)
        if index % 2 and part:
            entities.append(
                {"type": "bold", "offset": _utf16_length(text), "length": _utf16_length(part)}
            )
        text += part
    handle = "".join(display_name.split())
    return [(text, entities), (f"Let's drop a /Send{handle}To{action_word}", [])]


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


_SCORE_SYSTEM = """How the day is scored

Each game's board ranks its players best first, and ties share a placing: two players tied for first both place 1, and the next places 3. Your day score is your average placing over the games you played, plus 0.5 for each game you skipped. Lower is better. The best day score is CHAD and the worst is CHUD; ties share the title.

The average keeps one bad game from sinking a good day, and the 0.5 a skip costs is half of what one place lower on that board would. So skipping still hurts, but finishing every game is no longer the only way to win.

Wordle: fewer guesses is better, and X/6 is last. Hard mode (a * after the score) makes every guess reuse the hints so far, so the same count is a better result there. Each result ranks by where it falls among a solver's results in its own mode: the share of games it beats, counting half of those it ties. The solver played every answer from eleven common openers, in each mode. That puts 3/6* ahead of 3/6, 4/6* ahead of 4/6, and so on, while 3/6 still beats 4/6*. Hard mode never ranks below the same count in normal mode, and 1/6, 2/6 and X/6 tie across modes.

Connections: fewer mistakes is better, and four mistakes is a loss. Within the same mistakes, each unsolved category costs 0.1, then a solve order that leaves the harder colours (purple, then blue) until later costs a little more. Neither adjustment adds up to one mistake.

Krillion: a deeper dive is better.

Fermi: the score is how far off your guesses were, so 1× is perfect and lower is better."""


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


def _send_sf_ranking(
    client: TelegramClient, store: LeaderboardStore, *, board_chat_id: int,
    to_chat_id: int, nicknames: Mapping[int, str],
    updated_user_id: int | None = None,
) -> None:
    text = format_sf_ranking(
        store.sf_ranking(chat_id=board_chat_id, nicknames=nicknames),
        updated_user_id=updated_user_id,
    )
    heading, separator, body = text.partition("\n")
    entities = None
    if separator:
        entities = [{
            "type": "pre",
            "offset": _utf16_length(heading + separator),
            "length": _utf16_length(body),
        }]
    client.send_message(chat_id=to_chat_id, text=text, entities=entities)


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

    day = local_date(seen_at, utc_offset_minutes=utc_offset_minutes)
    expected = expected_puzzle(result.game, day)
    if expected is not None and result.puzzle_id != expected:
        LOG.info(
            "Ignored %s %s from user %s: expected %s",
            result.game,
            result.puzzle_id,
            user_id,
            expected,
        )
        if is_private:
            client.send_message(
                chat_id=origin_chat_id,
                text=(
                    f"That's {result.game} {result.puzzle_id}, but today's is"
                    f" {expected}."
                ),
            )
        return

    recorded = store.record(
        chat_id=board_chat_id,
        local_date=day,
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


def _render_announcement(
    announcement: PostedAnnouncement,
    *,
    store: LeaderboardStore,
    board_chat_id: int,
    day: str,
    is_chad: bool,
    nicknames: Mapping[int, str],
) -> str | None:
    """Name the day's CHAD or CHUD the way the bot would name them today.

    A day this build posted records who won, so they are renamed by whatever
    nickname they go by now. A day an older build posted recorded only the
    message it sent, so the names in it are swapped for nicknames where they
    still match a player that day. Neither path recomputes the winner: a result
    that arrives after the post must not revise it.
    """

    if announcement.winners is not None:
        if is_chad:
            rendered = format_chad_winners(
                announcement.winners,
                variation=_chad_variation(board_chat_id, day),
                nicknames=nicknames,
                line=announcement.line,
            )
        else:
            rendered = format_chud_winners(
                announcement.winners, nicknames=nicknames, line=announcement.line
            )
        return _with_streaks(
            rendered,
            store=store,
            board_chat_id=board_chat_id,
            day=day,
            winners=announcement.winners,
            is_chad=is_chad,
            nicknames=nicknames,
        )

    if announcement.message is not None:
        return apply_nicknames(
            announcement.message,
            players=store.daily_names(chat_id=board_chat_id, local_date=day),
            nicknames=nicknames,
        )

    # A day posted before announcements were saved at all has only its day's
    # results to go on.
    standings = store.standings(
        chat_id=board_chat_id, local_date=day, nicknames=nicknames
    )
    if is_chad:
        return format_chad(standings, variation=_chad_variation(board_chat_id, day))
    return format_chud(standings)


def _with_streaks(
    message: str | None,
    *,
    store: LeaderboardStore,
    board_chat_id: int,
    day: str,
    winners: Sequence[Winner],
    is_chad: bool,
    nicknames: Mapping[int, str],
) -> str | None:
    if message is None:
        return None
    streaks = store.title_streaks(
        chat_id=board_chat_id,
        local_date=day,
        winners=winners,
        is_chad=is_chad,
        since=store.setting("season_start"),
    )
    emoji, title = ("🔥", "CHAD") if is_chad else ("💩", "CHUD")
    lines = [
        f"{emoji} {nicknames.get(user_id, name)}: {length}-day {title} streak!"
        for user_id, name in winners
        if (length := streaks[user_id]) > 1
    ]
    return message + ("\n" + "\n".join(lines) if lines else "")


def _clock(minute: int | None) -> str:
    return _OFF if minute is None else f"{minute // 60:02}:{minute % 60:02}"


def _on_off(value: bool) -> str:
    return "on" if value else _OFF


def _change_setting(
    message: Mapping[str, object],
    command: str,
    *,
    store: LeaderboardStore,
    action_word: str | None,
    nicknames: Mapping[int, str],
    post_minute: int | None,
    retention_days: int,
    monthly_reset: bool,
) -> str:
    """Apply a settings command and return the reply describing the result.

    Without an argument a command only shows its current value.
    """

    parts = _message_body(message).split(maxsplit=1)
    argument = parts[1].strip() if len(parts) > 1 else ""
    switch_off = argument.lower() == _OFF

    if command == _SETTINGS_COMMAND:
        lines = [
            f"Action word: {action_word or _OFF}",
            f"Daily post: {_clock(post_minute)}",
            f"Duplicate window: {retention_days} days",
            f"Monthly /overall reset: {_on_off(monthly_reset)}",
        ]
        lines += [f"Nickname {user_id}: {name}" for user_id, name in nicknames.items()]
        return "\n".join(lines)

    if command == _ACTION_WORD_COMMAND:
        if argument:
            store.set_setting("action_word", _OFF if switch_off else argument)
            action_word = None if switch_off else argument
        return f"Action word: {action_word or _OFF}"

    if command == _NICKNAME_COMMAND:
        # Replying to someone names them; otherwise the sender names themselves.
        reply = message.get("reply_to_message")
        target = _sender(reply if isinstance(reply, Mapping) else message, {})
        if target is None:
            return "I can't tell whose nickname that is."
        user_id, telegram_name = target
        if argument:
            store.set_nickname(user_id=user_id, nickname="" if switch_off else argument)
            return (
                f"{telegram_name} has no nickname now."
                if switch_off
                else f"{telegram_name} is now {argument}."
            )
        nickname = nicknames.get(user_id)
        return (
            f"{telegram_name} goes by {nickname}."
            if nickname
            else f"{telegram_name} has no nickname."
        )

    if command == _POST_TIME_COMMAND:
        if argument:
            try:
                post_minute = None if switch_off else parse_daily_time(argument)
            except ValueError:
                return "Use HH:MM, for example /posttime 21:00, or off."
            store.set_setting("post_at", _clock(post_minute))
        return f"Daily post: {_clock(post_minute)}"

    if command == _MONTHLY_RESET_COMMAND:
        if argument:
            if argument.lower() not in ("on", _OFF):
                return "Use /monthlyreset on or /monthlyreset off."
            monthly_reset = argument.lower() == "on"
            store.set_setting("monthly_reset", _on_off(monthly_reset))
        return f"Monthly /overall reset: {_on_off(monthly_reset)}"

    if argument:
        if not argument.isdigit() or int(argument) == 0:
            return "Use a whole number of days, for example /retention 5."
        retention_days = int(argument)
        store.set_setting("retention_days", str(retention_days))
    return f"Duplicate window: {retention_days} days"


def _names(
    store: LeaderboardStore, board_chat_id: int, nicknames: Mapping[int, str]
) -> dict[int, list[str]]:
    """Return every name each group member answers to: nickname, Telegram name,
    and username, in that order."""

    people: dict[int, list[str]] = {}
    for user_id, (name, username) in store.member_names(chat_id=board_chat_id).items():
        labels = [nicknames.get(user_id), name, username]
        people[user_id] = list(dict.fromkeys(label for label in labels if label))
    return people


def _names_text(
    store: LeaderboardStore, board_chat_id: int, nicknames: Mapping[int, str]
) -> str:
    rows = []
    for labels in _names(store, board_chat_id, nicknames).values():
        if labels:
            others = ", ".join(labels[1:])
            rows.append(f"{labels[0]} ({others})" if others else labels[0])
    if not rows:
        return "Nobody's on the roster yet."
    return "Names to use after @ in /lines, /addline and /delline:\n" + "\n".join(
        sorted(rows, key=str.lower)
    )


def _person(
    text: str,
    *,
    store: LeaderboardStore,
    board_chat_id: int,
    nicknames: Mapping[int, str],
) -> tuple[int, str, str] | str:
    """Find who an @name at the start of text means, by nickname, Telegram name or
    username. Returns their ID, the name to show, and the rest of the text, or a
    reply explaining why nobody matched. The longest matching name wins, so a
    name with spaces is read whole."""

    lowered = text.lower()
    matches: dict[int, int] = {}
    people = _names(store, board_chat_id, nicknames)
    for user_id, labels in people.items():
        for label in labels:
            label = label.lower()
            if lowered == label or lowered.startswith(label + " "):
                matches[user_id] = max(matches.get(user_id, 0), len(label))
    if not matches:
        return "I don't know who that is. /names lists the names I know."
    longest = max(matches.values())
    matched = [user_id for user_id, length in matches.items() if length == longest]
    if len(matched) > 1:
        return "That name fits more than one person. /names lists the others to use."
    user_id = matched[0]
    return user_id, people[user_id][0], text[longest:].strip()


def _edit_lines(
    message: Mapping[str, object],
    command: str,
    *,
    store: LeaderboardStore,
    board_chat_id: int,
    nicknames: Mapping[int, str],
) -> str:
    """List, add to, or delete from a message set, returning the reply.

    An @name after chad or chud works on that person's own lines instead.
    """

    parts = _message_body(message).split(maxsplit=2)
    set_name = parts[1].lower() if len(parts) > 1 else ""
    argument = parts[2].strip() if len(parts) > 2 else ""
    if set_name not in MESSAGE_SETS:
        return (
            f"Name a set: {', '.join(MESSAGE_SETS)}. For example /lines callout,"
            " or /lines chad @name for someone's own lines."
        )
    key = label = set_name
    personal = argument.startswith("@")
    if personal:
        if set_name not in PERSONAL_SETS:
            return f"Only {' and '.join(PERSONAL_SETS)} lines can belong to someone."
        person = _person(
            argument[1:], store=store, board_chat_id=board_chat_id, nicknames=nicknames
        )
        if isinstance(person, str):
            return person
        user_id, name, argument = person
        key, label = f"{set_name}:{user_id}", f"{name}'s {set_name} lines"
    lines = store.message_lines(key)

    if command == _LINES_COMMAND:
        if not lines:
            return f"No {label} yet."
        return "\n".join(f"{number}. {line}" for number, line in enumerate(lines, 1))

    if command == _ADD_LINE_COMMAND:
        if not argument:
            return f"Add the line after the set, for example /addline {set_name} wahoo!"
        unknown = set(re.findall(r"\{([^{}]*)\}", argument)) - _CALLOUT_PLACEHOLDERS
        if set_name == "callout" and unknown:
            return "A callout line can use only {name} and {word}."
        store.add_message_line(key, argument)
        return f"Added to {label}: {argument}"

    if not argument.isdigit() or not 1 <= int(argument) <= len(lines):
        return f"Use a line number from /lines {set_name}{' @name' if personal else ''}."
    # Someone's own lines can run out; the group's always need one to draw on.
    if len(lines) == 1 and not personal:
        return f"That's the last line in {set_name}. Add another before deleting it."
    store.delete_message_line(key, int(argument))
    return f"Deleted from {label}: {lines[int(argument) - 1]}"


def _chad_line(
    store: LeaderboardStore, chat_id: int, day: str, winners: Sequence[Winner]
) -> str:
    lines = store.message_lines("chad") + store.message_lines(
        "chad-solo" if len(winners) == 1 else "chad-group"
    )
    for user_id, _ in winners:
        lines += store.message_lines(f"chad:{user_id}")
    return lines[_chad_variation(chat_id, day) % len(lines)]


def _chud_line(
    store: LeaderboardStore, chat_id: int, day: str, winners: Sequence[Winner]
) -> str:
    lines = store.message_lines("chud")
    for user_id, _ in winners:
        lines += store.message_lines(f"chud:{user_id}")
    return lines[_chad_variation(chat_id, day) % len(lines)]


def _current_settings(
    store: LeaderboardStore,
    *,
    action_word: str | None,
    nicknames: Mapping[int, str],
    post_minute: int | None,
    retention_days: int,
) -> dict[str, Any]:
    """Overlay what the group set by command on the environment's defaults.

    Monthly /overall resets have no environment setting and default to on.
    """

    stored_word = store.setting("action_word")
    if stored_word is not None:
        action_word = None if stored_word == _OFF else stored_word
    stored_time = store.setting("post_at")
    if stored_time is not None:
        post_minute = None if stored_time == _OFF else parse_daily_time(stored_time)
    stored_retention = store.setting("retention_days")
    if stored_retention is not None:
        retention_days = int(stored_retention)
    return {
        "action_word": action_word,
        "nicknames": {
            user_id: name
            for user_id, name in {**nicknames, **store.nicknames()}.items()
            if name
        },
        "post_minute": post_minute,
        "retention_days": retention_days,
        "monthly_reset": store.setting("monthly_reset") != _OFF,
    }


def _overall_heading(since: str | None) -> str:
    return f"Overall leaderboard since {since}" if since else "Overall leaderboard"


def _seasons_text(seasons: Sequence[SeasonArchive], *, page: int = 1) -> str:
    if not seasons:
        return "No closed seasons have been archived yet."
    start = (page - 1) * 10
    if start >= len(seasons):
        return f"No seasons on page {page}. Use /seasons to start from the newest."
    lines = [f"📅 Closed seasons (page {page}, newest first):"]
    for season in seasons[start:start + 10]:
        through = (date.fromisoformat(season.end_date) - timedelta(days=1)).isoformat()
        period = (
            f"{season.start_date} to {through}"
            if season.start_date else f"before {season.end_date}"
        )
        if season.reason == "legacy":
            period += " (legacy, reconstructed)"
        ranking = season.ranking
        if ranking and (ranking[0].chads or ranking[0].chuds):
            best = (ranking[0].chads, ranking[0].chuds)
            names = [
                rank.display_name for rank in ranking
                if (rank.chads, rank.chuds) == best
            ]
            winners = ", ".join(names)
        else:
            winners = "no titles"
        lines.append(f"#{season.id} {period} - {winners}")
    if start + 10 < len(seasons):
        lines.append(f"Use /seasons {page + 1} for older seasons.")
    lines.append("Use /overall <number> for the saved final board; /overall last for the newest.")
    return "\n".join(lines)


def _send_overall(client: TelegramClient, chat_id: int, text: str) -> None:
    heading, separator, body = text.partition("\n")
    entities = None
    if separator:
        # Telegram needs monospace text for the padded columns to align.
        entities = [{
            "type": "pre",
            "offset": _utf16_length(heading + separator),
            "length": _utf16_length(body),
        }]
    client.send_message(chat_id=chat_id, text=text, entities=entities)


def deliver_pending_seasons(
    client: TelegramClient, store: LeaderboardStore, *, board_chat_id: int
) -> None:
    """Resume confirmed parts of archived season announcements after a failure."""

    for season in store.pending_seasons(chat_id=board_chat_id):
        if not season.final_sent:
            _send_overall(client, board_chat_id, season.final_text)
            store.mark_season_sent(
                chat_id=board_chat_id, season_id=season.id, part="final_sent"
            )
        if not season.notice_sent:
            client.send_message(
                chat_id=board_chat_id,
                text=f"Overall totals reset. Counting from {season.end_date}.",
            )
            store.mark_season_sent(
                chat_id=board_chat_id, season_id=season.id, part="notice_sent"
            )


def _reset_overall(
    client: TelegramClient,
    store: LeaderboardStore,
    *,
    board_chat_id: int,
    start: str,
    nicknames: Mapping[int, str],
    reason: str = "manual",
    month: str | None = None,
    request_key: str | None = None,
) -> bool:
    """Post the closing season's standings, then count titles from ``start``."""

    season = store.close_season(
        chat_id=board_chat_id, end_date=start, reason=reason,
        nicknames=nicknames, month=month, request_key=request_key,
    )
    deliver_pending_seasons(client, store, board_chat_id=board_chat_id)
    return season is not None


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
    post_minute: int | None = None,
    retention_days: int = 5,
    monthly_reset: bool = True,
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
        raw = message["from"]
        assert isinstance(raw, Mapping)
        name, username = raw.get("first_name"), raw.get("username")
        store.remember_member(
            chat_id=board_chat_id,
            user_id=sender[0],
            seen_at=seen_at,
            name=name if isinstance(name, str) and name else None,
            username=username if isinstance(username, str) and username else None,
        )

    command = _leading_command(message)
    if command == _GAMES_COMMAND:
        text, entities = _games_message()
        client.send_message(chat_id=chat_id, text=text, entities=entities)
        return

    if command == _SF_COMMAND:
        sf_chat_id = board_chat_id if is_private and board_chat_id is not None else chat_id
        if is_private and board_chat_id is not None and not (
            sender is not None
            and store.is_member(chat_id=board_chat_id, user_id=sender[0])
        ):
            client.send_message(chat_id=chat_id, text=_UNKNOWN_SENDER)
            return
        _send_sf_ranking(
            client, store, board_chat_id=sf_chat_id,
            to_chat_id=chat_id, nicknames=nicknames,
        )
        return

    if command == _SCORE_SYSTEM_COMMAND:
        client.send_message(chat_id=chat_id, text=_SCORE_SYSTEM)
        return

    if command in (_LINES_COMMAND, _ADD_LINE_COMMAND, _DELETE_LINE_COMMAND, _NAMES_COMMAND):
        # Lines and names work by DM too, for anyone on the group's roster.
        if board_chat_id is None or not (is_private or is_board_group):
            return
        if is_private and not (
            sender is not None
            and store.is_member(chat_id=board_chat_id, user_id=sender[0])
        ):
            client.send_message(chat_id=chat_id, text=_UNKNOWN_SENDER)
            return
        if command == _NAMES_COMMAND:
            text = _names_text(store, board_chat_id, nicknames)
        else:
            text = _edit_lines(
                message,
                command,
                store=store,
                board_chat_id=board_chat_id,
                nicknames=nicknames,
            )
        client.send_message(chat_id=chat_id, text=text)
        return

    if command in (
        _SETTINGS_COMMAND,
        _ACTION_WORD_COMMAND,
        _NICKNAME_COMMAND,
        _POST_TIME_COMMAND,
        _RETENTION_COMMAND,
        _MONTHLY_RESET_COMMAND,
        _RESET_OVERALL_COMMAND,
    ):
        # Changes happen in the group only, so everyone sees them.
        if is_private:
            client.send_message(chat_id=chat_id, text="Change settings in the group chat.")
        elif command == _RESET_OVERALL_COMMAND:
            if is_board_group:
                # A day already posted stays in the season it was posted in.
                day = local_date(seen_at, utc_offset_minutes=utc_offset_minutes)
                if store.posted_announcements(chat_id=chat_id, local_date=day):
                    day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
                changed = _reset_overall(
                    client, store, board_chat_id=chat_id, start=day, nicknames=nicknames,
                    request_key=f"manual:{message_id}",
                )
                if not changed:
                    client.send_message(
                        chat_id=chat_id,
                        text=f"Current season already starts on {store.setting('season_start')}.",
                    )
        elif is_board_group:
            text = _change_setting(
                message,
                command,
                store=store,
                action_word=action_word,
                nicknames=nicknames,
                post_minute=post_minute,
                retention_days=retention_days,
                monthly_reset=monthly_reset,
            )
            client.send_message(chat_id=chat_id, text=text)
        return

    if command in (
        _LEADERBOARD_COMMAND,
        _CHAD_COMMAND,
        _CHUD_COMMAND,
        _DAILY_COMMAND,
        _OVERALL_COMMAND,
        _SEASONS_COMMAND,
    ):
        if board_chat_id is None:
            LOG.warning(
                "Ignoring %s: set SFBOT_LEADERBOARD_CHAT_ID to the group's"
                " chat ID. This chat's ID is %s",
                command,
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
        day = local_date(seen_at, utc_offset_minutes=utc_offset_minutes)
        if command == _LEADERBOARD_COMMAND:
            # The standings always come from the group's board, but the answer
            # goes back to whoever asked, so a DM check stays private.
            _send_leaderboard(
                client,
                store,
                board_chat_id=board_chat_id,
                to_chat_id=chat_id,
                day=day,
                nicknames=nicknames,
            )
            return

        if command == _SEASONS_COMMAND:
            arguments = _message_body(message).split(maxsplit=1)
            page = 1
            if len(arguments) > 1:
                requested = arguments[1].strip()
                if not re.fullmatch(r"[0-9]{1,12}", requested) or int(requested) < 1:
                    client.send_message(
                        chat_id=chat_id, text="Use /seasons or /seasons <page number>."
                    )
                    return
                page = int(requested)
            client.send_message(
                chat_id=chat_id,
                text=_seasons_text(store.seasons(chat_id=board_chat_id), page=page),
            )
            return

        if command == _OVERALL_COMMAND:
            arguments = _message_body(message).split(maxsplit=1)
            if len(arguments) > 1:
                requested = arguments[1].strip().lower()
                if requested == "last":
                    seasons = store.seasons(chat_id=board_chat_id)
                    season = seasons[0] if seasons else None
                elif re.fullmatch(r"[0-9]{1,12}", requested):
                    season = store.season(
                        chat_id=board_chat_id, season_id=int(requested)
                    )
                else:
                    client.send_message(
                        chat_id=chat_id,
                        text="Use /overall, /overall last, or /overall <season number>.",
                    )
                    return
                if season is None:
                    client.send_message(
                        chat_id=chat_id,
                        text="I don't have that season. Use /seasons to list them.",
                    )
                    return
                _send_overall(client, chat_id, season.final_text)
                return
            since = store.setting("season_start")
            ranking = store.overall_ranking(
                chat_id=board_chat_id, nicknames=nicknames, since=since
            )
            _send_overall(
                client,
                chat_id,
                format_overall_ranking(ranking, heading=_overall_heading(since)),
            )
            return

        if command == _DAILY_COMMAND:
            ranking = store.posted_daily_ranking(chat_id=board_chat_id, local_date=day)
            posted = store.posted_announcements(chat_id=board_chat_id, local_date=day)
            if ranking is None or posted is None:
                text = (
                    "Final rankings haven't been posted yet."
                    if posted is None
                    else "Final rankings weren't saved for this day."
                )
            else:
                text = format_daily_ranking(
                    ranking,
                    local_date=day,
                    chads=posted[0].winners or (),
                    chuds=posted[1].winners or (),
                    nicknames=nicknames,
                )
            client.send_message(chat_id=chat_id, text=text)
            return

        posted = store.posted_announcements(chat_id=board_chat_id, local_date=day)
        title = "CHAD" if command == _CHAD_COMMAND else "CHUD"
        if posted is None:
            client.send_message(
                chat_id=chat_id, text=f"The {title} of the day hasn't been decided yet."
            )
            return
        announcement = _render_announcement(
            posted[0] if command == _CHAD_COMMAND else posted[1],
            store=store,
            board_chat_id=board_chat_id,
            day=day,
            is_chad=command == _CHAD_COMMAND,
            nicknames=nicknames,
        )
        if announcement is not None:
            client.send_message(chat_id=chat_id, text=announcement)
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

        updated_user_id = None
        if sender is not None:
            raw_name = _sender(message, _NO_NICKNAMES)
            if raw_name is not None and store.record_sf(
                chat_id=chat_id,
                message_id=message_id,
                tweet_id=tweet_id,
                user_id=sender[0],
                display_name=raw_name[1],
            ):
                updated_user_id = sender[0]

        # The callout names whoever reposted, so it goes to the chat rather than
        # onto the original message it would otherwise seem to be about.
        if action_word and sender is not None:
            template = random.choice(store.message_lines("callout"))
            for line, entities in _callout(template, action_word, sender[1]):
                client.send_message(chat_id=chat_id, text=line, entities=entities)

        if updated_user_id is not None:
            _send_sf_ranking(
                client, store, board_chat_id=chat_id, to_chat_id=chat_id,
                nicknames=nicknames, updated_user_id=updated_user_id,
            )


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
    # Use one snapshot for the board, announcements, and saved winners.
    # Original names are saved so removing a nickname restores the latest
    # submitted name.
    recorded = store.standings(chat_id=board_chat_id, local_date=day)
    shown = [
        GameStandings(
            game=board.game,
            puzzle_id=board.puzzle_id,
            entries=tuple(
                replace(entry, display_name=nicknames.get(entry.user_id, entry.display_name))
                for entry in board.entries
            ),
        )
        for board in recorded
    ]
    client.send_message(
        chat_id=board_chat_id, text=format_standings(shown, local_date=day)
    )
    chads = chad_winners(recorded)
    chuds = chud_winners(recorded)
    chad_line = _chad_line(store, board_chat_id, day, chads) if chads else None
    chud_line = _chud_line(store, board_chat_id, day, chuds) if chuds else None
    chad = format_chad_winners(chads, nicknames=nicknames, line=chad_line)
    chud = format_chud_winners(chuds, nicknames=nicknames, line=chud_line)
    chad = _with_streaks(
        chad,
        store=store,
        board_chat_id=board_chat_id,
        day=day,
        winners=chads,
        is_chad=True,
        nicknames=nicknames,
    )
    chud = _with_streaks(
        chud,
        store=store,
        board_chat_id=board_chat_id,
        day=day,
        winners=chuds,
        is_chad=False,
        nicknames=nicknames,
    )
    if chad is not None:
        client.send_message(chat_id=board_chat_id, text=chad)
    if chud is not None:
        client.send_message(chat_id=board_chat_id, text=chud)
    ranks = daily_ranking(recorded)
    client.send_message(
        chat_id=board_chat_id,
        text=format_daily_ranking(
            ranks, local_date=day, chads=chads, chuds=chuds, nicknames=nicknames
        ),
    )
    store.mark_posted(
        chat_id=board_chat_id,
        local_date=day,
        chad_message=chad,
        chud_message=chud,
        chad_winners=chads if chad is not None else None,
        chud_winners=chuds if chud is not None else None,
        daily_ranking=ranks,
        chad_line=chad_line,
        chud_line=chud_line,
    )
    LOG.info("Posted the %s leaderboard to chat %s", day, board_chat_id)


def _chad_variation(chat_id: int, day: str) -> int:
    """Choose one line per group and day, consistently across send retries."""

    return zlib.crc32(f"{chat_id}:{day}".encode("utf-8"))


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


def reset_due_season(
    client: TelegramClient,
    store: LeaderboardStore,
    *,
    board_chat_id: int,
    utc_offset_minutes: int,
    monthly_reset: bool,
    now: int | None = None,
    nicknames: Mapping[int, str] = _NO_NICKNAMES,
) -> None:
    """Reset /overall on the first poll of a new month, if monthly resets are on.

    The first month seen is only recorded, so turning this on mid-month, or a
    first run, keeps the season going until the next month starts.
    """

    month = local_date(
        int(time.time()) if now is None else now,
        utc_offset_minutes=utc_offset_minutes,
    )[:7]
    seen = store.setting("season_month")
    if seen is None:
        store.set_setting("season_month", month)
    elif seen != month and monthly_reset:
        boundary = date.fromisoformat(f"{seen}-01")
        current = date.fromisoformat(f"{month}-01")
        while boundary < current:
            boundary = (
                date(boundary.year + 1, 1, 1)
                if boundary.month == 12
                else date(boundary.year, boundary.month + 1, 1)
            )
            _reset_overall(
                client,
                store,
                board_chat_id=board_chat_id,
                start=boundary.isoformat(),
                nicknames=nicknames,
                reason="monthly",
                month=boundary.strftime("%Y-%m"),
            )
    elif seen != month:
        store.set_setting("season_month", month)


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
    retention_days: int = 5,
) -> None:
    offset: int | None = None
    poll_backoff = 1
    request_errors = (
        TelegramAPIError, urllib.error.URLError, TimeoutError, ConnectionError,
        json.JSONDecodeError,
    )

    def current_settings() -> dict[str, Any]:
        # Read every time, so a command takes effect without a restart.
        current = _current_settings(
            store,
            action_word=action_word,
            nicknames=nicknames,
            post_minute=post_minute,
            retention_days=retention_days,
        )
        cache.retention_seconds = current["retention_days"] * 24 * 60 * 60
        return current

    def run_scheduled_tasks() -> None:
        current = current_settings()
        if board_chat_id is None:
            return
        tasks: list[tuple[str, Any]] = []
        if current["post_minute"] is not None:
            tasks += [
                ("reminders", send_due_reminders),
                ("leaderboard", post_due_leaderboard),
            ]
        # After any late daily post, so the closing standings include it.
        tasks.append(("season", reset_due_season))
        tasks.append(("season_delivery", deliver_pending_seasons))
        # A failed reminder round must not prevent the daily post, and no task
        # may prevent incoming updates from being processed.
        for name, task in tasks:
            options: dict[str, Any] = {"board_chat_id": board_chat_id}
            if name == "season":
                options["utc_offset_minutes"] = utc_offset_minutes
                options["monthly_reset"] = current["monthly_reset"]
            elif name != "season_delivery":
                options["utc_offset_minutes"] = utc_offset_minutes
                options["post_minute"] = current["post_minute"]
            if name in ("leaderboard", "season"):
                options["nicknames"] = current["nicknames"]
            try:
                task(client, store, **options)
            except request_errors as error:
                LOG.warning("Scheduled task %s failed (%s)", name, error)

    if board_chat_id is not None:
        store.archive_legacy(
            chat_id=board_chat_id, nicknames=current_settings()["nicknames"]
        )

    LOG.info("sfbot is listening for messages")
    while True:
        try:
            updates = client.get_updates(offset=offset, poll_timeout=poll_timeout)
        except request_errors as error:
            LOG.warning(
                "Telegram polling failed (%s); retrying in %ss", error, poll_backoff
            )
            run_scheduled_tasks()
            time.sleep(poll_backoff)
            poll_backoff = min(poll_backoff * 2, 30)
            continue
        poll_backoff = 1

        for update in updates:
            if not isinstance(update, Mapping):
                LOG.warning("Ignoring malformed Telegram update")
                continue
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                LOG.warning("Ignoring Telegram update without an integer ID")
                continue
            message = update.get("message")
            if isinstance(message, Mapping):
                # Bound retries so an unreachable sender or a persistently
                # failing request cannot hold up every later update. Existing
                # result/reply claims still protect against replayed effects.
                for attempt in range(3):
                    try:
                        handle_message(
                            message,
                            cache=cache,
                            store=store,
                            client=client,
                            utc_offset_minutes=utc_offset_minutes,
                            board_chat_id=board_chat_id,
                            **current_settings(),
                        )
                    except request_errors as error:
                        transient = not isinstance(error, TelegramAPIError) or (
                            error.status == 429 or error.status >= 500
                        )
                        if not transient or attempt == 2:
                            LOG.warning(
                                "Skipping update %s after reply failure (%s)",
                                update_id, error,
                            )
                            break
                        delay = 2 ** attempt
                        LOG.warning(
                            "Update %s request failed (%s); retrying in %ss",
                            update_id, error, delay,
                        )
                        run_scheduled_tasks()
                        time.sleep(delay)
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        LOG.warning(
                            "Skipping malformed update %s (%s)",
                            update_id, type(error).__name__,
                        )
                        break
                    else:
                        break
            # Successful, malformed, and exhausted updates are all acknowledged;
            # otherwise one permanent failure would poison the polling queue.
            offset = update_id + 1

        run_scheduled_tasks()
