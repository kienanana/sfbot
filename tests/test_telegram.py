import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from sfbot.cache import DuplicateCache
from sfbot.games import parse_result
from sfbot.leaderboard import LeaderboardStore
from sfbot.telegram import (
    _UNKNOWN_SENDER,
    TelegramAPIError,
    _chad_line,
    _chad_variation,
    _chud_line,
    TelegramClient,
    handle_message,
    post_due_leaderboard,
    reset_due_season,
    send_due_reminders,
    run_polling,
)

SGT_OFFSET = 480
GROUP = -100
OTHER_GROUP = -200
DM = 555


class FakeTelegramClient:
    def __init__(self) -> None:
        self.replies: list[tuple[int, int, object]] = []
        self.reply_names: list[str | None] = []
        self.sent: list[tuple[int, str]] = []
        self.sent_entities: list[list[dict[str, object]] | None] = []
        self.reactions: list[tuple[int, int, str]] = []

    def send_sf_reply(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender: object,
        display_name: str | None = None,
    ) -> None:
        self.replies.append((chat_id, message_id, sender))
        self.reply_names.append(display_name)

    def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        entities: list[dict[str, object]] | None = None,
    ) -> None:
        self.sent.append((chat_id, text))
        self.sent_entities.append(entities)

    def set_message_reaction(
        self, *, chat_id: int, message_id: int, emoji: str
    ) -> None:
        self.reactions.append((chat_id, message_id, emoji))


class MissingReplyClient(FakeTelegramClient):
    def send_sf_reply(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender: object,
        display_name: str | None = None,
    ) -> None:
        raise TelegramAPIError(400, "Bad Request: message to be replied not found")


class AmbiguousCalloutClient(FakeTelegramClient):
    """Telegram delivered the first callout, but the response was lost."""

    def send_message(self, *, chat_id: int, text: str, entities=None) -> None:
        super().send_message(chat_id=chat_id, text=text, entities=entities)
        if len(self.sent) == 1:
            raise TimeoutError("response lost after delivery")


class AmbiguousReplyClient(FakeTelegramClient):
    """Telegram delivered the reply, but the response was lost."""

    def send_sf_reply(
        self,
        *,
        chat_id: int,
        message_id: int,
        sender: object,
        display_name: str | None = None,
    ) -> None:
        super().send_sf_reply(
            chat_id=chat_id,
            message_id=message_id,
            sender=sender,
            display_name=display_name,
        )
        if len(self.replies) == 1:
            raise TimeoutError("response lost after delivery")


class BlockedDirectMessageClient(FakeTelegramClient):
    """Refuses DMs to user 6, the way Telegram refuses someone who never started."""

    def send_message(self, *, chat_id: int, text: str) -> None:
        if chat_id == 6:
            raise TelegramAPIError(403, "Forbidden: bot can't initiate conversation")
        super().send_message(chat_id=chat_id, text=text)


def command_entity(text: str) -> list[dict[str, object]]:
    return [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]


class HandleMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "sfbot.db"
        self.cache = DuplicateCache(database_path)
        self.store = LeaderboardStore(database_path)
        self.client = FakeTelegramClient()
        # Alice and Bob have been seen in the group, so their DMs are trusted.
        for user_id in (5, 6):
            self.store.remember_member(
                chat_id=GROUP, user_id=user_id, seen_at=1_999_000_000
            )

    def tearDown(self) -> None:
        self.store.close()
        self.cache.close()
        self.temp_dir.cleanup()

    def handle(
        self,
        message: dict[str, object],
        *,
        client: FakeTelegramClient | None = None,
        board_chat_id: int | None = GROUP,
        action_word: str | None = None,
        nicknames: dict[int, str] | None = None,
    ) -> None:
        handle_message(
            message,  # type: ignore[arg-type]
            cache=self.cache,
            store=self.store,
            client=self.client if client is None else client,
            utc_offset_minutes=SGT_OFFSET,
            board_chat_id=board_chat_id,
            action_word=action_word,
            nicknames={} if nicknames is None else nicknames,
        )

    def dm(
        self,
        text: str,
        *,
        user_id: int = 5,
        name: str = "Alice",
        message_id: int = 7,
        nicknames: dict[int, str] | None = None,
    ) -> None:
        self.handle(
            {
                "chat": {"id": DM, "type": "private"},
                "message_id": message_id,
                "date": 2_000_000_000,
                "from": {"id": user_id, "first_name": name},
                "text": text,
            },
            nicknames=nicknames,
        )

    def test_duplicate_replies_to_original_message(self) -> None:
        first = {
            "chat": {"id": -100},
            "message_id": 41,
            "date": 2_000_000_000,
            "text": "https://twitter.com/alice/status/123?s=20",
        }
        second = {
            "chat": {"id": -100},
            "message_id": 99,
            "date": 2_000_000_010,
            "text": "https://x.com/bob/status/123",
        }
        self.handle(first)
        self.handle(second)
        self.assertEqual(self.client.replies, [(-100, 41, None)])

    def test_same_person_first_share_is_silent_and_second_gets_sf(self) -> None:
        first = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        second = {**first, "message_id": 42, "date": 2_000_000_010}

        self.handle(first, action_word="Nuke")
        self.assertEqual(self.client.replies, [])
        self.assertEqual(self.client.sent, [])

        self.handle(second, action_word="Nuke")
        self.handle(second, action_word="Nuke")
        self.assertEqual(
            self.client.replies, [(GROUP, 41, {"id": 5, "first_name": "Alice"})]
        )
        self.assertEqual(
            self.client.sent,
            [
                (GROUP, "Uh oh! Looks like Alice's being Sent to Nuke"),
                (GROUP, "Let's drop a /SendAliceToNuke"),
                (GROUP, "SF leaderboard\n1. Alice - 🏆🆕 1 SF"),
            ],
        )

    def test_an_action_word_calls_out_the_repeat_poster(self) -> None:
        first = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        second = {
            **first,
            "message_id": 99,
            "date": 2_000_000_010,
            "from": {"id": 6, "first_name": "Bob"},
        }
        self.handle(first, action_word="Nuke")
        self.handle(second, action_word="Nuke")

        self.assertEqual(
            self.client.replies,
            [(GROUP, 41, {"id": 6, "first_name": "Bob"})],
        )
        self.assertEqual(
            self.client.sent,
            [
                (GROUP, "Uh oh! Looks like Bob's being Sent to Nuke"),
                (GROUP, "Let's drop a /SendBobToNuke"),
                (GROUP, "SF leaderboard\n1. Bob - 🏆🆕 1 SF"),
            ],
        )

    def command(
        self,
        text: str,
        *,
        chat: dict[str, object] | None = None,
        reply_to: dict[str, object] | None = None,
        **settings: object,
    ) -> str:
        """Send a command from Alice and return the bot's last message."""

        message: dict[str, object] = {
            "chat": chat or {"id": GROUP, "type": "supergroup"},
            "message_id": 50,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": text,
            "entities": command_entity(text),
        }
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": 49, "from": reply_to}
        handle_message(
            message,  # type: ignore[arg-type]
            cache=self.cache,
            store=self.store,
            client=self.client,
            utc_offset_minutes=SGT_OFFSET,
            board_chat_id=GROUP,
            **settings,  # type: ignore[arg-type]
        )
        return self.client.sent[-1][1] if self.client.sent else ""

    def test_settings_shows_the_current_values(self) -> None:
        self.assertEqual(
            self.command(
                "/settings",
                action_word="Nuke",
                nicknames={6: "Juan"},
                post_minute=21 * 60,
                retention_days=5,
            ),
            "Action word: Nuke\nDaily post: 21:00\nDuplicate window: 5 days\n"
            "Monthly /overall reset: on\nNickname 6: Juan",
        )

    def test_monthly_reset_turns_on_and_off(self) -> None:
        self.assertEqual(self.command("/monthlyreset OFF"), "Monthly /overall reset: off")
        self.assertEqual(self.store.setting("monthly_reset"), "off")
        self.assertTrue(self.command("/monthlyreset maybe").startswith("Use "))
        self.assertEqual(
            self.command("/monthlyreset", monthly_reset=False), "Monthly /overall reset: off"
        )
        self.assertEqual(self.command("/monthlyreset on"), "Monthly /overall reset: on")
        self.assertEqual(self.store.setting("monthly_reset"), "on")

    def test_reset_overall_posts_the_final_standings_then_starts_today(self) -> None:
        self.assertEqual(
            self.command("/resetoverall"), "Overall totals reset. Counting from 2033-05-18."
        )
        self.assertEqual(len(self.client.sent), 1)

        self.store.mark_posted(chat_id=GROUP, local_date="2033-05-16", chad_winners=[(5, "Alice")])
        self.store.mark_posted(chat_id=GROUP, local_date="2033-05-18", chud_winners=[(6, "Bob")])
        self.client.sent.clear()
        # Today is already posted, so it stays in the closing season.
        self.command("/resetoverall")
        final = self.client.sent[0][1]
        self.assertTrue(final.startswith("Final overall leaderboard since 2033-05-18\n"))
        self.assertNotIn("Alice", final)
        self.assertIn("Bob - 👑 0  🚽 1", final)
        self.assertEqual(
            self.client.sent[1][1], "Overall totals reset. Counting from 2033-05-19."
        )
        self.assertEqual(self.store.setting("season_start"), "2033-05-19")
        self.assertEqual(
            self.command("/overall"), "No CHAD or CHUD titles have been recorded yet."
        )

    def test_setting_commands_store_their_values(self) -> None:
        self.assertEqual(self.command("/actionword Gulag"), "Action word: Gulag")
        self.assertEqual(self.command("/posttime 9:05"), "Daily post: 09:05")
        self.assertEqual(self.command("/retention 7"), "Duplicate window: 7 days")
        self.assertEqual(self.store.setting("action_word"), "Gulag")
        self.assertEqual(self.store.setting("post_at"), "09:05")
        self.assertEqual(self.store.setting("retention_days"), "7")

        self.assertEqual(self.command("/actionword off"), "Action word: off")
        self.assertEqual(self.command("/posttime OFF"), "Daily post: off")
        self.assertEqual(self.store.setting("action_word"), "off")
        self.assertEqual(self.store.setting("post_at"), "off")

    def test_a_command_without_an_argument_only_shows_the_value(self) -> None:
        self.assertEqual(
            self.command("/actionword", action_word="Nuke"), "Action word: Nuke"
        )
        self.assertEqual(self.command("/retention"), "Duplicate window: 5 days")
        self.assertIsNone(self.store.setting("action_word"))
        self.assertIsNone(self.store.setting("retention_days"))

    def test_invalid_setting_values_are_refused(self) -> None:
        for text in ("/posttime 25:00", "/posttime soon", "/retention 0", "/retention -1"):
            with self.subTest(text=text):
                self.assertTrue(self.command(text).startswith("Use "))
        self.assertIsNone(self.store.setting("post_at"))
        self.assertIsNone(self.store.setting("retention_days"))

    def test_nickname_names_the_sender_or_whoever_they_reply_to(self) -> None:
        self.assertEqual(self.command("/nickname Ali"), "Alice is now Ali.")
        bob = {"id": 6, "first_name": "Bob"}
        self.assertEqual(self.command("/nickname Juan", reply_to=bob), "Bob is now Juan.")
        self.assertEqual(self.store.nicknames(), {5: "Ali", 6: "Juan"})

        self.assertEqual(
            self.command("/nickname", reply_to=bob, nicknames={6: "Juan"}),
            "Bob goes by Juan.",
        )
        self.assertEqual(
            self.command("/nickname off", reply_to=bob), "Bob has no nickname now."
        )
        self.assertEqual(self.store.nicknames(), {5: "Ali", 6: ""})

    def test_settings_change_only_in_the_board_group(self) -> None:
        self.command("/actionword Gulag", chat={"id": OTHER_GROUP, "type": "supergroup"})
        self.assertEqual(self.client.sent, [])
        self.assertEqual(
            self.command("/actionword Gulag", chat={"id": DM, "type": "private"}),
            "Change settings in the group chat.",
        )
        self.assertIsNone(self.store.setting("action_word"))

    def test_line_commands_list_add_and_delete(self) -> None:
        self.assertEqual(
            self.command("/lines callout"),
            "1. Uh oh! Looks like {name}'s being *Sent to {word}*",
        )
        self.assertEqual(
            self.command("/addline callout {name} is off to {word}"),
            "Added to callout: {name} is off to {word}",
        )
        self.assertEqual(
            self.command("/delline callout 1"),
            "Deleted from callout: Uh oh! Looks like {name}'s being *Sent to {word}*",
        )
        self.assertEqual(self.store.message_lines("callout"), ["{name} is off to {word}"])
        self.assertTrue(self.command("/delline callout 1").startswith("That's the last"))
        self.assertTrue(self.command("/addline CHAD-group nice").startswith("Added"))
        self.assertEqual(self.store.message_lines("chad-group")[-1], "nice")

    def test_invalid_line_commands_are_refused(self) -> None:
        for text, reply in (
            ("/lines", "Name a set"),
            ("/addline crowd hi", "Name a set"),
            ("/addline chad", "Add the line"),
            ("/addline callout {nickname} goes", "A callout line can use only"),
            ("/delline chad 0", "Use a line number"),
            ("/delline chad 99", "Use a line number"),
        ):
            with self.subTest(text=text):
                self.assertTrue(self.command(text).startswith(reply))
        self.assertEqual(
            self.store.message_lines("callout"),
            ["Uh oh! Looks like {name}'s being *Sent to {word}*"],
        )

    def test_personal_lines_take_a_nickname_name_or_username(self) -> None:
        self.store.remember_member(
            chat_id=GROUP, user_id=6, seen_at=1, name="Aditya Kumar", username="adi"
        )
        self.assertEqual(
            self.command("/addline chad @Aditya Kumar this one isn't diddyflation!"),
            "Added to Aditya Kumar's chad lines: this one isn't diddyflation!",
        )
        self.assertEqual(
            self.command("/addline CHAD @ADI hi", chat={"id": DM, "type": "private"}),
            "Added to Aditya Kumar's chad lines: hi",
        )
        self.assertEqual(
            self.command("/lines chad @Diddy", nicknames={6: "Diddy"}),
            "1. this one isn't diddyflation!\n2. hi",
        )
        self.assertNotIn("hi", self.store.message_lines("chad"))
        self.assertEqual(
            self.command("/delline chad @adi 2"), "Deleted from Aditya Kumar's chad lines: hi"
        )
        # Someone's own lines may run out, unlike the group's.
        self.assertTrue(self.command("/delline chad @adi 1").startswith("Deleted"))
        self.assertEqual(self.command("/lines chad @adi"), "No Aditya Kumar's chad lines yet.")

    def test_the_longest_matching_name_wins(self) -> None:
        self.store.remember_member(chat_id=GROUP, user_id=6, seen_at=1, name="Ali")
        self.store.remember_member(chat_id=GROUP, user_id=7, seen_at=1, name="Ali Baba")
        self.command("/addline chud @Ali Baba open sesame")
        self.assertEqual(self.store.message_lines("chud:7"), ["open sesame"])
        self.command("/addline chud @ali Babar the elephant")
        self.assertEqual(self.store.message_lines("chud:6"), ["Babar the elephant"])

    def test_invalid_personal_line_commands_are_refused(self) -> None:
        self.store.remember_member(chat_id=GROUP, user_id=6, seen_at=1, name="Sam")
        self.store.remember_member(chat_id=GROUP, user_id=7, seen_at=1, username="sam")
        for text, reply in (
            ("/addline callout @Sam hi", "Only chad and chud lines"),
            ("/lines chad @nobody", "I don't know who that is"),
            ("/lines chad @sam", "That name fits more than one person"),
            ("/delline chud @Alice 1", "Use a line number from /lines chud @name"),
        ):
            with self.subTest(text=text):
                self.assertTrue(self.command(text).startswith(reply))

    def test_names_lists_every_name_a_member_answers_to(self) -> None:
        self.store.remember_member(
            chat_id=GROUP, user_id=6, seen_at=1, name="Aditya", username="adi"
        )
        self.assertEqual(
            self.command("/names", nicknames={6: "Diddy"}),
            "Names to use after @ in /lines, /addline and /delline:\n"
            "Alice\nDiddy (Aditya, adi)",
        )

    def test_line_commands_by_dm_need_the_group_roster(self) -> None:
        self.handle({
            "chat": {"id": DM, "type": "private"},
            "message_id": 50, "date": 2_000_000_000,
            "from": {"id": 9, "first_name": "Stranger"},
            "text": "/names", "entities": command_entity("/names"),
        })
        self.assertEqual(self.client.sent[-1][1], _UNKNOWN_SENDER)

    def test_a_winners_own_lines_join_the_pool(self) -> None:
        self.store.add_message_line("chad:5", "Nice job Claude!")
        self.store.add_message_line("chud:5", "this one isn't diddyflation!")
        for line, pool in (
            (_chad_line, self.store.message_lines("chad") + self.store.message_lines("chad-solo")),
            (_chud_line, self.store.message_lines("chud")),
        ):
            size = len(pool) + 1
            # The first day whose variation lands on the pool's last line, the
            # winner's own.
            day = next(
                (date(2033, 1, 1) + timedelta(days=offset)).isoformat()
                for offset in range(366)
                if _chad_variation(
                    GROUP, (date(2033, 1, 1) + timedelta(days=offset)).isoformat()
                ) % size == size - 1
            )
            self.assertIn(line(self.store, GROUP, day, [(5, "Alice")]), (
                "Nice job Claude!", "this one isn't diddyflation!"
            ))
            self.assertIn(line(self.store, GROUP, day, [(6, "Bob")]), pool)

    def test_the_callout_uses_a_line_from_the_set(self) -> None:
        self.store.add_message_line("callout", "{name}, off to {word} with you")
        self.store.delete_message_line("callout", 1)
        share = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        self.handle(share, action_word="Nuke")
        self.handle({**share, "message_id": 42}, action_word="Nuke")
        self.assertEqual(self.client.sent[0], (GROUP, "Alice, off to Nuke with you"))

    def test_asterisks_in_a_callout_line_become_bold(self) -> None:
        share = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Zoë 🐱"},
            "text": "https://x.com/someone/status/123",
        }
        self.handle(share, action_word="Nuke")
        for message_id, template, text, entities in (
            (
                42,
                "Uh oh! Looks like {name}'s being *Sent to {word}*",
                "Uh oh! Looks like Zoë 🐱's being Sent to Nuke",
                [{"type": "bold", "offset": 33, "length": 12}],
            ),
            (
                43,
                "**{name}** is *off*",
                "Zoë 🐱 is off",
                [
                    {"type": "bold", "offset": 0, "length": 6},
                    {"type": "bold", "offset": 10, "length": 3},
                ],
            ),
            (44, "2 * 3 {name}", "2 * 3 Zoë 🐱", []),
        ):
            with self.subTest(template=template):
                self.store.add_message_line("callout", template)
                self.store.delete_message_line("callout", 1)
                self.client.sent.clear()
                self.client.sent_entities.clear()
                self.handle({**share, "message_id": message_id}, action_word="Nuke")
                self.assertEqual(self.client.sent[0], (GROUP, text))
                self.assertEqual(self.client.sent_entities[0], entities)

    def test_a_nickname_replaces_the_name_in_the_callout(self) -> None:
        first = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        second = {
            **first,
            "message_id": 99,
            "date": 2_000_000_010,
            "from": {"id": 6, "first_name": "Bob"},
        }
        self.handle(first, action_word="Nuke", nicknames={6: "Juan"})
        self.handle(second, action_word="Nuke", nicknames={6: "Juan"})
        self.handle(second, action_word="Nuke", nicknames={6: "Juan"})

        self.assertEqual(self.client.reply_names, ["Juan"])
        self.assertEqual(
            self.client.sent,
            [
                (GROUP, "Uh oh! Looks like Juan's being Sent to Nuke"),
                (GROUP, "Let's drop a /SendJuanToNuke"),
                (GROUP, "SF leaderboard\n1. Juan - 🏆🆕 1 SF"),
            ],
        )

    def test_a_nickname_is_the_name_on_the_board(self) -> None:
        self.dm("Wordle 4,351 3/6", nicknames={5: "Juan"})

        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].display_name, "Alice")

        command = "/leaderboard"
        self.handle(
            {
                "chat": {"id": DM, "type": "private"},
                "message_id": 8,
                "date": 2_000_000_100,
                "from": {"id": 5, "first_name": "Alice"},
                "text": command,
                "entities": command_entity(command),
            },
            nicknames={5: "Nikki"},
        )
        self.assertIn("Nikki - 3/6", self.client.sent[-1][1])

    def test_replayed_shares_send_one_sf_each_even_after_restart(self) -> None:
        original = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        shares = [
            {
                **original,
                "message_id": message_id,
                "date": 2_000_000_000 + message_id,
                "from": {"id": 6, "first_name": "Bob"},
            }
            for message_id in (42, 43)
        ]
        self.handle(original, action_word="Nuke")
        for share in shares + shares:
            self.handle(share, action_word="Nuke")

        self.cache.close()
        self.cache = DuplicateCache(Path(self.temp_dir.name) / "sfbot.db")
        for share in shares:
            self.handle(share, action_word="Nuke")

        self.assertEqual(len(self.client.replies), 2)
        self.assertEqual([reply[1] for reply in self.client.replies], [41, 41])
        self.assertEqual(len(self.client.sent), 6)
        self.assertEqual(self.store.sf_ranking(chat_id=GROUP)[0].count, 2)

    def test_failed_callout_does_not_repeat_sf_on_update_replay(self) -> None:
        original = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        share = {
            **original,
            "message_id": 42,
            "date": 2_000_000_010,
            "from": {"id": 6, "first_name": "Bob"},
        }
        client = AmbiguousCalloutClient()
        self.handle(original, client=client, action_word="Nuke")
        with self.assertRaises(TimeoutError):
            self.handle(share, client=client, action_word="Nuke")

        self.handle(share, client=client, action_word="Nuke")
        self.assertEqual(len(client.replies), 1)
        self.assertEqual(
            client.sent, [(GROUP, "Uh oh! Looks like Bob's being Sent to Nuke")]
        )

    def test_ambiguous_sf_timeout_does_not_send_a_second_reply(self) -> None:
        original = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "text": "https://x.com/someone/status/123",
        }
        share = {**original, "message_id": 42, "date": 2_000_000_010}
        client = AmbiguousReplyClient()
        self.handle(original, client=client)
        with self.assertRaises(TimeoutError):
            self.handle(share, client=client)

        self.cache.close()
        self.cache = DuplicateCache(Path(self.temp_dir.name) / "sfbot.db")
        self.handle(share, client=client)
        self.assertEqual(client.replies, [(GROUP, 41, None)])

    def test_without_an_action_word_only_sf_is_sent(self) -> None:
        first = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        self.handle(first)
        self.handle({**first, "message_id": 99, "from": {"id": 6, "first_name": "Bob"}})

        self.assertEqual(
            self.client.replies,
            [(GROUP, 41, {"id": 6, "first_name": "Bob"})],
        )
        self.assertEqual(self.client.sent, [(GROUP, "SF leaderboard\n1. Bob - 🏆🆕 1 SF")])

    def test_sf_command_shows_totals_without_update_marks(self) -> None:
        first = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        self.handle(first)
        self.handle({**first, "message_id": 42, "from": {"id": 6, "first_name": "Bob"}})
        self.handle({**first, "message_id": 43, "from": {"id": 5, "first_name": "Alice"}})
        self.assertEqual(
            self.client.sent[-1],
            (GROUP, "SF leaderboard\n1. Alice - 🏆🆕 1 SF\n1. Bob   - 🏆   1 SF"),
        )

        command = "/sf@sfbot"
        self.handle({
            "chat": {"id": DM, "type": "private"},
            "message_id": 50,
            "date": 2_000_000_100,
            "from": {"id": 5, "first_name": "Alice"},
            "text": command,
            "entities": command_entity(command),
        }, nicknames={6: "Bobby"})
        self.assertEqual(
            self.client.sent[-1],
            (DM, "SF leaderboard\n1. Alice - 🏆   1 SF\n1. Bobby - 🏆   1 SF"),
        )
        self.assertEqual(self.client.sent_entities[-1][0]["type"], "pre")

    def test_sf_trophy_stays_with_leader_when_another_count_increases(self) -> None:
        original = {
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 41,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": "https://x.com/someone/status/123",
        }
        self.handle(original)
        self.handle({**original, "message_id": 42, "from": {"id": 6, "first_name": "Bob"}})
        self.handle({**original, "message_id": 43, "from": {"id": 6, "first_name": "Bob"}})
        self.handle({**original, "message_id": 44})
        self.assertEqual(
            self.client.sent[-1],
            (GROUP, "SF leaderboard\n1. Bob   - 🏆   2 SFs\n2. Alice - 🆕   1 SF"),
        )

    def test_sf_command_uses_the_chat_where_it_was_asked(self) -> None:
        command = "/sf"
        self.handle({
            "chat": {"id": OTHER_GROUP, "type": "supergroup"},
            "message_id": 1,
            "date": 2_000_000_000,
            "from": {"id": 5, "first_name": "Alice"},
            "text": command,
            "entities": command_entity(command),
        })
        self.assertEqual(
            self.client.sent[-1], (OTHER_GROUP, "No SFs have been recorded yet.")
        )

    def test_deleted_origin_promotes_current_message(self) -> None:
        first = {
            "chat": {"id": -100},
            "message_id": 41,
            "date": 2_000_000_000,
            "text": "https://x.com/alice/status/123",
        }
        second = {**first, "message_id": 42, "date": 2_000_000_010}
        third = {**first, "message_id": 43, "date": 2_000_000_020}

        self.handle(first)
        self.handle(second, client=MissingReplyClient())
        working_client = FakeTelegramClient()
        self.handle(third, client=working_client)

        self.assertEqual(working_client.replies, [(-100, 42, None)])

    def test_duplicate_reply_mentions_the_person_who_shared_it_again(self) -> None:
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 41,
                "date": 2_000_000_000,
                "from": {"id": 5, "first_name": "Alice"},
                "text": "https://x.com/alice/status/123",
            }
        )
        bob = {"id": 6, "first_name": "Bob", "username": "bob"}
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 42,
                "date": 2_000_000_010,
                "from": bob,
                "text": "https://x.com/bob/status/123",
            }
        )
        self.assertEqual(self.client.replies, [(GROUP, 41, bob)])

    def test_a_direct_message_scores_against_the_group_board(self) -> None:
        self.dm(
            "Wordle 4,351 3/6\n\n\U0001f7e9\U0001f7e9\U0001f7e9\U0001f7e9\U0001f7e9"
        )

        # Confirmed privately, and nothing at all reaches the group.
        self.assertEqual(self.client.sent, [(DM, "Recorded Wordle 4351 - 3/6")])
        self.assertEqual(self.client.reactions, [])
        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].display_name, "Alice")
        self.assertEqual(standings[0].entries[0].score, "3/6")
        self.assertEqual(self.store.standings(chat_id=DM, local_date="2033-05-18"), [])

    def test_a_repeat_direct_message_says_so(self) -> None:
        self.dm("Wordle 4,351 5/6")
        self.dm("Wordle 4,351 2/6", message_id=8)

        self.assertEqual(
            self.client.sent,
            [
                (DM, "Recorded Wordle 4351 - 5/6"),
                (DM, "You already submitted Wordle today."),
            ],
        )
        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].score, "5/6")

    def test_a_stale_puzzle_is_refused_and_not_recorded(self) -> None:
        self.dm("Wordle 4,350 3/6")

        self.assertEqual(
            self.client.sent, [(DM, "That's Wordle 4350, but today's is 4351.")]
        )
        self.assertEqual(self.store.standings(chat_id=GROUP, local_date="2033-05-18"), [])

    def test_a_stale_group_paste_is_ignored_silently(self) -> None:
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 7,
                "date": 2_000_000_000,
                "from": {"id": 5, "first_name": "Alice"},
                "text": "Wordle 4,350 3/6",
            }
        )

        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.reactions, [])
        self.assertEqual(self.store.standings(chat_id=GROUP, local_date="2033-05-18"), [])

    def test_unrecognized_direct_messages_are_left_alone(self) -> None:
        self.dm("hey what's the plan for tonight")

        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.reactions, [])

    def test_a_group_paste_still_counts_and_gets_a_reaction(self) -> None:
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 7,
                "date": 2_000_000_000,
                "from": {"id": 5, "first_name": "Alice"},
                "text": "Wordle 4,351 3/6",
            }
        )

        self.assertEqual(self.client.reactions, [(GROUP, 7, "\N{THUMBS UP SIGN}")])
        self.assertEqual(self.client.sent, [])
        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].score, "3/6")

    def test_leaderboard_command_answers_in_the_chat_that_asked(self) -> None:
        self.dm("Wordle 4,351 3/6")
        self.client.sent.clear()

        board = "Daily games - 2033-05-18\n\n🆆 Wordle 4351\n1. 👑 Alice - 3/6"
        for chat_id, chat_type, text in (
            (DM, "private", "/leaderboard"),
            (GROUP, "supergroup", "/leaderboard@sfbot"),
        ):
            self.handle(
                {
                    "chat": {"id": chat_id, "type": chat_type},
                    "message_id": 8,
                    "date": 2_000_000_100,
                    "from": {"id": 6, "first_name": "Bob"},
                    "text": text,
                    "entities": command_entity(text),
                }
            )

        # Same standings both times, delivered back to whoever asked.
        self.assertEqual(self.client.sent, [(DM, board), (GROUP, board)])

    def test_chad_and_chud_commands_repeat_the_posted_messages(self) -> None:
        self.dm("Wordle 4,351 2/6", user_id=5, name="Alice")
        self.dm("Wordle 4,351 5/6", user_id=6, name="Bob")
        self.client.sent.clear()

        def ask(command: str, *, chat_id: int = DM, user_id: int = 5) -> None:
            self.handle(
                {
                    "chat": {
                        "id": chat_id,
                        "type": "private" if chat_id == DM else "supergroup",
                    },
                    "message_id": 20,
                    "date": 2_000_035_800,
                    "from": {"id": user_id, "first_name": "Alice"},
                    "text": command,
                    "entities": command_entity(command),
                }
            )

        ask("/chad")
        ask("/chud")
        self.assertEqual(
            self.client.sent,
            [
                (DM, "The CHAD of the day hasn't been decided yet."),
                (DM, "The CHUD of the day hasn't been decided yet."),
            ],
        )
        self.client.sent.clear()
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60,
            now=2_000_035_800,
        )
        chad, chud = self.client.sent[1:3]
        self.assertIn("Alice is the CHAD of the day!", chad[1])
        self.assertEqual(chud[1], "🚽 Ding ding ding! Bob is the CHUD of the day!\nCan't add this one to the splitwise!")
        self.client.sent.clear()

        # A result that arrives after the post must not revise either winner.
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 21,
                "date": 2_000_035_900,
                "from": {"id": 7, "first_name": "Cara"},
                "text": "Wordle 4,351 1/6",
            }
        )
        # Nor may editing the lines change the closing lines already posted.
        for set_name in ("chad", "chad-solo", "chud"):
            for _ in self.store.message_lines(set_name):
                self.store.delete_message_line(set_name, 1)
            self.store.add_message_line(set_name, "edited")
        ask("/chad@sfbot", chat_id=GROUP)
        ask("/chud")
        self.assertEqual(self.client.sent, [(GROUP, chad[1]), (DM, chud[1])])

        self.client.sent.clear()
        ask("/chad", user_id=99)
        ask("/chud", chat_id=OTHER_GROUP, user_id=99)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("roster", self.client.sent[0][1])

    def test_daily_and_overall_commands_show_saved_titles_in_dm_and_group(self) -> None:
        self.dm("Wordle 4,351 2/6", user_id=5, name="Alice")
        self.dm("Wordle 4,351 5/6", user_id=6, name="Bob", message_id=8)
        self.client.sent.clear()

        def ask(command: str, chat_id: int = DM, user_id: int = 5) -> None:
            self.handle({
                "chat": {"id": chat_id, "type": "private" if chat_id == DM else "supergroup"},
                "message_id": 30, "date": 2_000_035_800,
                "from": {"id": user_id, "first_name": "Alice"},
                "text": command, "entities": command_entity(command),
            }, nicknames={5: "Juan"})

        ask("/daily")
        ask("/overall")
        self.assertEqual(self.client.sent[0], (DM, "Final rankings haven't been posted yet."))
        self.assertIn("Juan - 👑 0  🚽 0", self.client.sent[1][1])
        overall_text = self.client.sent[1][1]
        heading, _, body = overall_text.partition("\n")
        self.assertEqual(self.client.sent_entities[-1], [{
            "type": "pre",
            "offset": len((heading + "\n").encode("utf-16-le")) // 2,
            "length": len(body.encode("utf-16-le")) // 2,
        }])
        self.client.sent.clear()
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store, board_chat_id=GROUP, utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60, now=2_000_035_800,
        )
        saved_daily = self.client.sent[-1][1]
        self.assertIn("1. 👑 Alice — 1 point", saved_daily)
        self.assertIn("2. 🚽 Bob — 2 points", saved_daily)
        self.client.sent.clear()

        # A late better score cannot change the posted ranking or title totals.
        self.handle({
            "chat": {"id": GROUP, "type": "supergroup"},
            "message_id": 31, "date": 2_000_035_900,
            "from": {"id": 7, "first_name": "Cara"},
            "text": "Wordle 4,351 1/6",
        })
        ask("/daily", chat_id=GROUP)
        ask("/overall")
        self.assertIn("1. 👑 Juan — 1 point", self.client.sent[0][1])
        self.assertNotIn("Cara", self.client.sent[0][1])
        self.assertIn("Juan - 👑 1", self.client.sent[1][1])
        self.assertIn("Cara - 👑 0  🚽 0", self.client.sent[1][1])
        self.client.sent.clear()
        ask("/daily", user_id=99)
        ask("/overall", chat_id=OTHER_GROUP)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("roster", self.client.sent[0][1])

    def test_chad_command_repeats_the_streak_with_a_current_nickname(self) -> None:
        self.store.mark_posted(
            chat_id=GROUP, local_date="2033-05-17",
            chad_winners=[(5, "Alice")],
        )
        self.dm("Wordle 4,351 2/6", user_id=5, name="Alice")
        self.dm("Wordle 4,351 5/6", user_id=6, name="Bob", message_id=8)
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store, board_chat_id=GROUP, utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60, now=2_000_035_800,
        )
        self.client.sent.clear()
        command = "/chad"
        self.handle({
            "chat": {"id": DM, "type": "private"},
            "message_id": 30, "date": 2_000_035_900,
            "from": {"id": 5, "first_name": "Alice"},
            "text": command, "entities": command_entity(command),
        }, nicknames={5: "Juan"})
        self.assertIn("🔥 Juan: 2-day CHAD streak!", self.client.sent[0][1])

    def test_a_nickname_change_reaches_the_chad_and_chud_commands(self) -> None:
        self.dm("Wordle 4,351 2/6", user_id=5, name="Alice")
        self.dm("Wordle 4,351 5/6", user_id=6, name="Bob", message_id=8)
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60,
            now=2_000_035_800,
        )
        self.client.sent.clear()

        self.ask_both(2_000_035_900)

        chad, chud = self.client.sent
        self.assertTrue(
            chad[1].startswith("👑 Ding ding ding! Juan is the CHAD of the day!\n")
        )
        self.assertEqual(chud[1], "🚽 Ding ding ding! Diddy is the CHUD of the day!\nCan't add this one to the splitwise!")

    def test_an_announcement_from_an_older_build_still_follows_a_nickname(self) -> None:
        # A day posted before winners were recorded leaves only its message,
        # so the names in it are the only way back to the players.
        for user_id, name, text in (
            (5, "Alice", "Wordle 4,351 2/6"),
            (6, "Bob", "Wordle 4,351 5/6"),
        ):
            result = parse_result(text)
            assert result is not None
            self.store.record(
                chat_id=GROUP,
                local_date="2033-05-18",
                user_id=user_id,
                display_name=name,
                result=result,
                submitted_at=2_000_000_000 + user_id,
            )
        self.store.mark_posted(
            chat_id=GROUP,
            local_date="2033-05-18",
            chad_message="👑 Ding ding ding! Alice is the CHAD of the day!\nwahoo!",
            chud_message="🚽 Ding ding ding! Bob is the CHUD of the day!",
        )

        self.ask_both(2_000_035_900)

        chad, chud = self.client.sent
        self.assertEqual(
            chad[1], "👑 Ding ding ding! Juan is the CHAD of the day!\nwahoo!"
        )
        self.assertEqual(chud[1], "🚽 Ding ding ding! Diddy is the CHUD of the day!")

    def test_a_day_posted_with_no_saved_announcement_is_recomputed(self) -> None:
        self.dm("Wordle 4,351 2/6", user_id=5, name="Alice")
        self.dm("Wordle 4,351 5/6", user_id=6, name="Bob", message_id=8)
        # Posted before announcements were saved at all.
        self.store.mark_posted(chat_id=GROUP, local_date="2033-05-18")
        self.client.sent.clear()

        self.ask_both(2_000_035_900)

        chad, chud = self.client.sent
        self.assertTrue(
            chad[1].startswith("👑 Ding ding ding! Juan is the CHAD of the day!\n")
        )
        self.assertEqual(chud[1], "🚽 Ding ding ding! Diddy is the CHUD of the day!")

    def test_a_nickname_dropped_after_the_post_falls_back_to_the_name(self) -> None:
        self.dm("Wordle 4,351 2/6", user_id=5, name="Alice")
        self.dm("Wordle 4,351 5/6", user_id=6, name="Bob", message_id=8)
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60,
            now=2_000_035_800,
            nicknames={5: "Juan", 6: "Diddy"},
        )
        self.client.sent.clear()

        self.ask_both(2_000_035_900, nicknames={})

        chad, chud = self.client.sent
        self.assertTrue(
            chad[1].startswith("👑 Ding ding ding! Alice is the CHAD of the day!\n")
        )
        self.assertEqual(chud[1], "🚽 Ding ding ding! Bob is the CHUD of the day!\nCan't add this one to the splitwise!")

    def ask_both(
        self, date: int, nicknames: dict[int, str] | None = None
    ) -> None:
        """Ask the group for the day's CHAD and CHUD as it would today."""

        for command in ("/chad", "/chud"):
            self.handle(
                {
                    "chat": {"id": GROUP, "type": "supergroup"},
                    "message_id": 30,
                    "date": date,
                    "from": {"id": 5, "first_name": "Alice"},
                    "text": command,
                    "entities": command_entity(command),
                },
                nicknames={5: "Juan", 6: "Diddy"} if nicknames is None else nicknames,
            )

    def test_games_command_links_every_supported_game(self) -> None:
        for chat_id, chat_type, command in (
            (DM, "private", "/games"),
            (GROUP, "supergroup", "/games@sfbot"),
        ):
            self.handle(
                {
                    "chat": {"id": chat_id, "type": chat_type},
                    "message_id": 8,
                    "date": 2_000_000_100,
                    "from": {"id": 6, "first_name": "Bob"},
                    "text": command,
                    "entities": command_entity(command),
                },
                board_chat_id=None,
            )

        expected = "Games:\n🆆 Wordle\n🦐 Krillion\n🧮 Fermi\n🧩 Connections"
        self.assertEqual(self.client.sent, [(DM, expected), (GROUP, expected)])
        for entities in self.client.sent_entities:
            assert entities is not None
            encoded = expected.encode("utf-16-le")
            self.assertEqual(
                [
                    (
                        entity["url"],
                        encoded[
                            2 * int(entity["offset"]) : 2
                            * (int(entity["offset"]) + int(entity["length"]))
                        ].decode("utf-16-le"),
                    )
                    for entity in entities
                ],
                [
                    ("https://www.nytimes.com/games/wordle/index.html", "Wordle"),
                    ("https://krillion.io/", "Krillion"),
                    ("https://fermi.gg/", "Fermi"),
                    ("https://www.nytimes.com/games/connections", "Connections"),
                ],
            )

    def test_another_group_cannot_submit_or_read_the_board(self) -> None:
        self.handle(
            {
                "chat": {"id": OTHER_GROUP, "type": "supergroup"},
                "message_id": 9,
                "date": 2_000_000_000,
                "from": {"id": 99, "first_name": "Mallory"},
                "text": "Wordle 4,351 1/6",
            }
        )
        command = "/leaderboard"
        self.handle(
            {
                "chat": {"id": OTHER_GROUP, "type": "supergroup"},
                "message_id": 10,
                "date": 2_000_000_010,
                "from": {"id": 99, "first_name": "Mallory"},
                "text": command,
                "entities": command_entity(command),
            }
        )

        self.assertEqual(self.client.sent, [])
        self.assertEqual(
            self.store.standings(chat_id=GROUP, local_date="2033-05-18"), []
        )
        self.assertFalse(self.store.is_member(chat_id=GROUP, user_id=99))

        for message_id in (11, 12):
            self.handle(
                {
                    "chat": {"id": OTHER_GROUP, "type": "supergroup"},
                    "message_id": message_id,
                    "date": 2_000_000_020 + message_id,
                    "from": {"id": 99, "first_name": "Mallory"},
                    "text": "https://x.com/alice/status/123",
                }
            )
        self.assertEqual(
            self.client.replies,
            [(OTHER_GROUP, 11, {"id": 99, "first_name": "Mallory"})],
        )

    def test_a_stranger_cannot_submit_by_direct_message(self) -> None:
        self.dm("Wordle 4,351 1/6", user_id=99, name="Mallory")

        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("roster", self.client.sent[0][1])
        self.assertEqual(
            self.store.standings(chat_id=GROUP, local_date="2033-05-18"), []
        )

    def test_a_stranger_cannot_read_the_board_by_direct_message(self) -> None:
        text = "/leaderboard"
        self.handle(
            {
                "chat": {"id": DM, "type": "private"},
                "message_id": 7,
                "date": 2_000_000_000,
                "from": {"id": 99, "first_name": "Mallory"},
                "text": text,
                "entities": command_entity(text),
            }
        )

        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("roster", self.client.sent[0][1])
        self.assertNotIn("Daily games", self.client.sent[0][1])

    def test_ordinary_group_chatter_puts_you_on_the_roster(self) -> None:
        # Nothing game-related: just talking in the group is enough.
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 1,
                "date": 2_000_000_000,
                "from": {"id": 9, "first_name": "Dave"},
                "text": "morning all",
            }
        )
        self.assertTrue(self.store.is_member(chat_id=GROUP, user_id=9))

        self.dm("Wordle 4,351 4/6", user_id=9, name="Dave", message_id=2)

        self.assertEqual(self.client.sent, [(DM, "Recorded Wordle 4351 - 4/6")])
        board = self.store.standings(chat_id=GROUP, local_date="2033-05-18")[0]
        self.assertEqual(board.entries[0].display_name, "Dave")

    def test_a_direct_message_never_puts_you_on_the_roster(self) -> None:
        self.dm("hello", user_id=99, name="Mallory")
        self.assertFalse(self.store.is_member(chat_id=GROUP, user_id=99))

    def test_nothing_is_recorded_without_a_configured_group(self) -> None:
        self.handle(
            {
                "chat": {"id": DM, "type": "private"},
                "message_id": 7,
                "date": 2_000_000_000,
                "from": {"id": 5, "first_name": "Alice"},
                "text": "Wordle 4,351 3/6",
            },
            board_chat_id=None,
        )

        self.assertEqual(self.client.sent, [])
        self.assertEqual(
            self.store.standings(chat_id=GROUP, local_date="2033-05-18"), []
        )

    def test_a_link_bearing_message_is_not_treated_as_a_submission(self) -> None:
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 7,
                "date": 2_000_000_000,
                "from": {"id": 5, "first_name": "Alice"},
                "text": "Wordle 4,351 3/6 https://x.com/alice/status/123",
            }
        )

        self.assertEqual(self.client.reactions, [])
        self.assertEqual(
            self.store.standings(chat_id=GROUP, local_date="2033-05-18"), []
        )


class RunPollingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "sfbot.db"
        self.cache = DuplicateCache(path)
        self.store = LeaderboardStore(path)
        self.client = FakeTelegramClient()
        self.poll = Mock()
        self.client.get_updates = self.poll

    def tearDown(self) -> None:
        self.store.close()
        self.cache.close()
        self.temp_dir.cleanup()

    @staticmethod
    def update(update_id: int) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "chat": {"id": DM, "type": "private"},
                "message_id": update_id,
                "date": 2_000_000_000,
                "text": "/games",
                "entities": command_entity("/games"),
            },
        }

    def run_bot(self, batches, *, reminder_error=None, post_error=None):
        self.poll.side_effect = [*batches, KeyboardInterrupt()]
        with (
            patch("sfbot.telegram.time.sleep") as sleep,
            patch("sfbot.telegram.send_due_reminders", side_effect=reminder_error) as reminders,
            patch("sfbot.telegram.post_due_leaderboard", side_effect=post_error) as posts,
            self.assertRaises(KeyboardInterrupt),
        ):
            run_polling(
                self.client, self.cache, self.store,
                board_chat_id=GROUP, post_minute=21 * 60,
            )
        return sleep, reminders, posts

    def test_permanent_reply_error_does_not_block_later_updates(self) -> None:
        with patch.object(
            self.client, "send_message",
            side_effect=[TelegramAPIError(403, "blocked"), None],
        ) as send:
            sleep, reminders, posts = self.run_bot([[self.update(10), self.update(11)]])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.poll.call_args_list[1].kwargs["offset"], 12)
        sleep.assert_not_called()
        reminders.assert_called_once()
        posts.assert_called_once()

    def test_transient_reply_failure_is_retried(self) -> None:
        for error in (TimeoutError(), TelegramAPIError(500, "unavailable"),
                      TelegramAPIError(429, "rate limited")):
            with self.subTest(error=type(error).__name__), patch.object(
                self.client, "send_message", side_effect=[error, None],
            ) as send:
                sleep, reminders, posts = self.run_bot([[self.update(10)]])
                self.assertEqual(send.call_count, 2)
                self.assertEqual(self.poll.call_args.kwargs["offset"], 11)
                sleep.assert_called_once_with(1)
                self.assertGreaterEqual(posts.call_count, 1)
            self.poll.reset_mock()

    def test_persistent_transient_failure_has_bounded_retries(self) -> None:
        with patch.object(
            self.client, "send_message",
            side_effect=[TimeoutError(), TimeoutError(), TimeoutError(), None],
        ) as send:
            sleep, reminders, posts = self.run_bot([[self.update(10), self.update(11)]])
        self.assertEqual(send.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(self.poll.call_args.kwargs["offset"], 12)
        self.assertGreaterEqual(posts.call_count, 1)

    def test_malformed_message_does_not_stop_the_batch(self) -> None:
        broken = self.update(10)
        broken["message"]["date"] = "invalid"
        sleep, reminders, posts = self.run_bot([
            [None, {"update_id": "invalid"}, broken, self.update(11)],
        ])
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.poll.call_args.kwargs["offset"], 12)
        sleep.assert_not_called()
        posts.assert_called_once()

    def test_poll_failures_back_off_without_disabling_scheduled_tasks(self) -> None:
        sleep, reminders, posts = self.run_bot([
            TimeoutError(), TimeoutError(), [], TimeoutError(), [],
        ])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 1])
        self.assertEqual(reminders.call_count, 5)
        self.assertEqual(posts.call_count, 5)

    def test_reminder_failure_does_not_block_posts_or_updates(self) -> None:
        sleep, reminders, posts = self.run_bot(
            [[self.update(10)], [self.update(11)]], reminder_error=TimeoutError(),
        )
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.poll.call_args.kwargs["offset"], 12)
        self.assertEqual(posts.call_count, 2)

    def test_post_failure_does_not_block_next_poll(self) -> None:
        sleep, reminders, posts = self.run_bot(
            [[self.update(10)], [self.update(11)]],
            post_error=TelegramAPIError(403, "forbidden"),
        )
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.poll.call_args.kwargs["offset"], 12)
        self.assertEqual(reminders.call_count, 2)

    def test_stored_settings_override_the_environment_without_a_restart(self) -> None:
        def share(update_id: int, user_id: int, name: str) -> dict:
            return {
                "update_id": update_id,
                "message": {
                    "chat": {"id": GROUP, "type": "supergroup"},
                    "message_id": update_id,
                    "date": 2_000_000_000 + update_id,
                    "from": {"id": user_id, "first_name": name},
                    "text": "https://x.com/someone/status/123",
                },
            }

        command = {
            "update_id": 10,
            "message": {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 10,
                "date": 2_000_000_000,
                "from": {"id": 5, "first_name": "Alice"},
                "text": "/actionword Gulag",
                "entities": command_entity("/actionword Gulag"),
            },
        }
        self.store.set_nickname(user_id=6, nickname="Juan")
        self.store.set_setting("post_at", "off")
        self.store.set_setting("retention_days", "9")
        self.poll.side_effect = [
            [command, share(11, 5, "Alice"), share(12, 6, "Bob")], KeyboardInterrupt(),
        ]
        with (
            patch("sfbot.telegram.post_due_leaderboard") as posts,
            self.assertRaises(KeyboardInterrupt),
        ):
            run_polling(
                self.client, self.cache, self.store,
                board_chat_id=GROUP, post_minute=21 * 60, action_word="Nuke",
            )

        self.assertIn((GROUP, "Let's drop a /SendJuanToGulag"), self.client.sent)
        self.assertEqual(self.cache.retention_seconds, 9 * 24 * 60 * 60)
        posts.assert_not_called()


class TelegramClientTests(unittest.TestCase):
    def test_sf_reply_mentions_a_username(self) -> None:
        client = TelegramClient("test")
        with patch.object(client, "_call") as call:
            client.send_sf_reply(
                chat_id=GROUP,
                message_id=41,
                sender={"id": 6, "first_name": "Bob", "username": "bob"},
            )

        payload = call.call_args.args[1]
        self.assertEqual(payload["text"], "sf @bob")
        self.assertEqual(payload["reply_parameters"]["message_id"], 41)
        self.assertNotIn("entities", payload)

    def test_sf_reply_mentions_a_nickname_instead_of_the_telegram_name(self) -> None:
        client = TelegramClient("test")
        with patch.object(client, "_call") as call:
            client.send_sf_reply(
                chat_id=GROUP,
                message_id=41,
                sender={"id": 6, "first_name": "Bob"},
                display_name="Juan",
            )

        payload = call.call_args.args[1]
        self.assertEqual(payload["text"], "sf @Juan")
        self.assertEqual(payload["entities"][0]["user"]["first_name"], "Juan")

    def test_sf_reply_prefers_a_real_username_to_a_nickname(self) -> None:
        client = TelegramClient("test")
        with patch.object(client, "_call") as call:
            client.send_sf_reply(
                chat_id=GROUP,
                message_id=41,
                sender={"id": 6, "first_name": "Bob", "username": "bob"},
                display_name="Juan",
            )

        self.assertEqual(call.call_args.args[1]["text"], "sf @bob")

    def test_sf_reply_mentions_a_user_without_a_username(self) -> None:
        client = TelegramClient("test")
        with patch.object(client, "_call") as call:
            client.send_sf_reply(
                chat_id=GROUP,
                message_id=41,
                sender={"id": 6, "first_name": "B🦐b"},
            )

        payload = call.call_args.args[1]
        self.assertEqual(payload["text"], "sf @B🦐b")
        self.assertEqual(
            payload["entities"],
            [
                {
                    "type": "text_mention",
                    "offset": 3,
                    "length": 5,
                    "user": {"id": 6, "is_bot": False, "first_name": "B🦐b"},
                }
            ],
        )


class SendDueRemindersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LeaderboardStore(Path(self.temp_dir.name) / "sfbot.db")
        self.client = FakeTelegramClient()
        for user_id in (5, 6):
            self.store.remember_member(
                chat_id=GROUP, user_id=user_id, seen_at=1_999_000_000
            )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def remind(self, now: int, *, client: FakeTelegramClient | None = None) -> None:
        send_due_reminders(
            self.client if client is None else client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60,
            now=now,
        )

    def record(self, text: str, *, user_id: int, name: str) -> None:
        result = parse_result(text)
        assert result is not None
        self.store.record(
            chat_id=GROUP,
            local_date="2033-05-18",
            user_id=user_id,
            display_name=name,
            result=result,
            submitted_at=2_000_000_000,
        )

    def test_each_member_is_dmd_only_the_games_they_still_owe(self) -> None:
        self.record("Wordle 4,351 3/6", user_id=5, name="Alice")

        # 2033-05-18 20:00 SGT, an hour before the 21:00 post.
        self.remind(2_000_030_400)

        self.assertEqual(
            self.client.sent,
            [
                (
                    5,
                    "An hour until the 2033-05-18 leaderboard. Still to play:"
                    " Krillion, Fermi, Connections",
                ),
                (
                    6,
                    "An hour until the 2033-05-18 leaderboard. Still to play:"
                    " Wordle, Krillion, Fermi, Connections",
                ),
            ],
        )

    def test_nothing_is_sent_before_the_window_opens(self) -> None:
        # 2033-05-18 19:30 SGT.
        self.remind(2_000_028_600)
        self.assertEqual(self.client.sent, [])

    def test_nothing_is_sent_once_the_board_has_gone_up(self) -> None:
        # 2033-05-18 21:30 SGT: too late to warn about a board already posted.
        self.remind(2_000_035_800)
        self.assertEqual(self.client.sent, [])

    def test_reminders_are_sent_once_a_day(self) -> None:
        self.remind(2_000_030_400)
        self.client.sent.clear()
        self.remind(2_000_031_000)

        self.assertEqual(self.client.sent, [])

    def test_someone_with_nothing_left_is_not_reminded(self) -> None:
        for text, name in (
            ("Wordle 4,351 3/6", "Alice"),
            ("Krillion #7\n1,200", "Alice"),
            ("Fermi 42\n1.0\u00d7 score", "Alice"),
            (
                "Connections\nPuzzle #99\n\U0001f7e8\U0001f7e8\U0001f7e8\U0001f7e8",
                "Alice",
            ),
        ):
            self.record(text, user_id=5, name=name)

        self.remind(2_000_030_400)

        self.assertEqual([chat_id for chat_id, _ in self.client.sent], [6])

    def test_one_unreachable_member_does_not_stop_the_rest(self) -> None:
        client = BlockedDirectMessageClient()
        self.remind(2_000_030_400, client=client)

        self.assertEqual([chat_id for chat_id, _ in client.sent], [5])
        self.assertFalse(
            self.store.is_awaiting_reminder(chat_id=GROUP, local_date="2033-05-18")
        )


class PostDueLeaderboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LeaderboardStore(Path(self.temp_dir.name) / "sfbot.db")
        self.client = FakeTelegramClient()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def post(self, now: int, *, nicknames: dict[int, str] | None = None) -> None:
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60,
            now=now,
            nicknames={} if nicknames is None else nicknames,
        )

    def test_the_group_is_posted_to_once_per_day(self) -> None:
        result = parse_result("Wordle 4,351 3/6")
        assert result is not None
        self.store.record(
            chat_id=GROUP,
            local_date="2033-05-18",
            user_id=5,
            display_name="Alice",
            result=result,
            submitted_at=2_000_000_000,
        )

        # 2033-05-18 21:30 SGT, then ten minutes later.
        self.post(2_000_035_800)
        self.post(2_000_036_400)

        # The game boards, titles, and final ranking are sent once in that order.
        self.assertEqual(len(self.client.sent), 4)
        self.assertEqual(self.client.sent[0][0], GROUP)
        self.assertIn("Wordle 4351", self.client.sent[0][1])
        self.assertIn("Final rankings", self.client.sent[3][1])
        self.assertIn("👑 🚽 Alice", self.client.sent[3][1])

    def test_the_daily_post_crowns_a_chud(self) -> None:
        for user_id, name, text in (
            (5, "Alice", "Wordle 4,351 2/6"),
            (6, "Bob", "Wordle 4,351 5/6"),
        ):
            result = parse_result(text)
            assert result is not None
            self.store.record(
                chat_id=GROUP,
                local_date="2033-05-18",
                user_id=user_id,
                display_name=name,
                result=result,
                submitted_at=2_000_000_000 + user_id,
            )

        self.post(2_000_035_800)

        self.assertEqual(
            self.client.sent[2],
            (GROUP, "🚽 Ding ding ding! Bob is the CHUD of the day!\nCan't add this one to the splitwise!"),
        )
        self.assertIn("Alice is the CHAD of the day!", self.client.sent[1][1])
        self.assertNotIn("🔥", self.client.sent[1][1])
        self.assertNotIn("💩", self.client.sent[2][1])

    def test_chad_and_chud_posts_acknowledge_consecutive_titles(self) -> None:
        self.store.mark_posted(
            chat_id=GROUP, local_date="2033-05-17",
            chad_winners=[(5, "Alice")], chud_winners=[(6, "Bob")],
        )
        for user_id, name, score in ((5, "Alice", "2/6"), (6, "Bob", "5/6")):
            result = parse_result(f"Wordle 4,351 {score}")
            assert result is not None
            self.store.record(
                chat_id=GROUP, local_date="2033-05-18", user_id=user_id,
                display_name=name, result=result, submitted_at=2_000_000_000 + user_id,
            )
        self.post(2_000_035_800, nicknames={5: "Juan"})
        self.assertIn("🔥 Juan: 2-day CHAD streak!", self.client.sent[1][1])
        self.assertIn("💩 Bob: 2-day CHUD streak!", self.client.sent[2][1])
        self.assertEqual(
            self.store.overall_ranking(chat_id=GROUP)[0].chad_streak, 2
        )

    def test_nothing_is_posted_when_no_results_exist(self) -> None:
        self.post(2_000_035_800)
        self.assertEqual(self.client.sent, [])

    def test_scheduled_post_uses_the_current_nickname(self) -> None:
        result = parse_result("Wordle 4,351 3/6")
        assert result is not None
        self.store.record(
            chat_id=GROUP,
            local_date="2033-05-18",
            user_id=5,
            display_name="Alice",
            result=result,
            submitted_at=2_000_000_000,
        )

        self.post(2_000_035_800, nicknames={5: "Juan"})

        self.assertIn("Juan - 3/6", self.client.sent[0][1])

    def test_shared_nicknames_keep_posted_and_saved_winners_consistent(self) -> None:
        for user_id, name, score in ((5, "Alice", "2/6"), (6, "Bob", "5/6")):
            result = parse_result(f"Wordle 4,351 {score}")
            assert result is not None
            self.store.record(
                chat_id=GROUP, local_date="2033-05-18", user_id=user_id,
                display_name=name, result=result, submitted_at=2_000_000_000 + user_id,
            )
        self.post(2_000_035_800, nicknames={5: "Alex", 6: "Alex"})
        announcements = self.store.posted_announcements(
            chat_id=GROUP, local_date="2033-05-18"
        )
        assert announcements is not None
        self.assertEqual(announcements[0].winners, ((5, "Alice"),))
        self.assertEqual(announcements[1].winners, ((6, "Bob"),))
        self.assertIn("Alex is the CHAD", self.client.sent[1][1])
        self.assertIn("Alex is the CHUD", self.client.sent[2][1])
        self.assertEqual(announcements[0].message, self.client.sent[1][1])
        self.assertEqual(announcements[1].message, self.client.sent[2][1])

    def test_the_post_records_who_won_by_user_id(self) -> None:
        for user_id, name, text in (
            (5, "Alice", "Wordle 4,351 2/6"),
            (6, "Bob", "Wordle 4,351 5/6"),
        ):
            result = parse_result(text)
            assert result is not None
            self.store.record(
                chat_id=GROUP,
                local_date="2033-05-18",
                user_id=user_id,
                display_name=name,
                result=result,
                submitted_at=2_000_000_000 + user_id,
            )

        self.post(2_000_035_800, nicknames={5: "Juan"})

        announcements = self.store.posted_announcements(
            chat_id=GROUP, local_date="2033-05-18"
        )
        assert announcements is not None
        # The nickname reaches the message, but the record keeps the name that
        # came with the score, so dropping the nickname falls back to it.
        self.assertEqual(announcements[0].winners, ((5, "Alice"),))
        self.assertEqual(announcements[1].winners, ((6, "Bob"),))
        self.assertIn("Juan is the CHAD", announcements[0].message or "")


if __name__ == "__main__":
    unittest.main()


class ResetDueSeasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LeaderboardStore(Path(self.temp_dir.name) / "sfbot.db")
        self.client = FakeTelegramClient()
        self.store.mark_posted(chat_id=GROUP, local_date="2033-05-16", chad_winners=[(5, "Alice")])

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def reset(self, now: int, *, monthly_reset: bool = True) -> None:
        reset_due_season(
            self.client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            monthly_reset=monthly_reset,
            now=now,
            nicknames={5: "Juan"},
        )

    def test_a_new_month_posts_the_final_standings_and_resets(self) -> None:
        # The first month seen is only recorded, so a first run resets nothing.
        self.reset(2_000_000_000)
        self.reset(2_000_000_000)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.store.setting("season_month"), "2033-05")

        # 2033-06-01 11:33 SGT.
        self.reset(2_001_209_600)
        self.reset(2_001_209_600)
        self.assertEqual(len(self.client.sent), 2)
        self.assertTrue(self.client.sent[0][1].startswith("Final overall leaderboard\n"))
        self.assertIn("Juan - 👑 1", self.client.sent[0][1])
        self.assertEqual(
            self.client.sent[1][1], "Overall totals reset. Counting from 2033-06-01."
        )
        self.assertEqual(self.store.setting("season_start"), "2033-06-01")

    def test_nothing_resets_when_monthly_resets_are_off(self) -> None:
        self.reset(2_000_000_000)
        self.reset(2_001_209_600, monthly_reset=False)
        self.assertEqual(self.client.sent, [])
        self.assertIsNone(self.store.setting("season_start"))
        self.assertEqual(self.store.setting("season_month"), "2033-06")
