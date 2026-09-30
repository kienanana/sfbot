import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sfbot.cache import DuplicateCache
from sfbot.games import parse_result
from sfbot.leaderboard import LeaderboardStore
from sfbot.telegram import (
    TelegramAPIError,
    TelegramClient,
    handle_message,
    post_due_leaderboard,
    send_due_reminders,
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
                (GROUP, "Uh oh! Looks like Alice's getting *Nuked*"),
                (GROUP, "Let's drop a /NukeAlice"),
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
                (GROUP, "Uh oh! Looks like Bob's getting *Nuked*"),
                (GROUP, "Let's drop a /NukeBob"),
            ],
        )

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
                (GROUP, "Uh oh! Looks like Juan's getting *Nuked*"),
                (GROUP, "Let's drop a /NukeJuan"),
            ],
        )

    def test_a_nickname_is_the_name_on_the_board(self) -> None:
        self.dm("Wordle 1,234 3/6", nicknames={5: "Juan"})

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
        self.assertEqual(len(self.client.sent), 4)

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
            client.sent, [(GROUP, "Uh oh! Looks like Bob's getting *Nuked*")]
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
        self.assertEqual(self.client.sent, [])

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
            "Wordle 1,234 3/6\n\n\U0001f7e9\U0001f7e9\U0001f7e9\U0001f7e9\U0001f7e9"
        )

        # Confirmed privately, and nothing at all reaches the group.
        self.assertEqual(self.client.sent, [(DM, "Recorded Wordle 1234 - 3/6")])
        self.assertEqual(self.client.reactions, [])
        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].display_name, "Alice")
        self.assertEqual(standings[0].entries[0].score, "3/6")
        self.assertEqual(self.store.standings(chat_id=DM, local_date="2033-05-18"), [])

    def test_a_repeat_direct_message_says_so(self) -> None:
        self.dm("Wordle 1,234 5/6")
        self.dm("Wordle 1,234 2/6", message_id=8)

        self.assertEqual(
            self.client.sent,
            [
                (DM, "Recorded Wordle 1234 - 5/6"),
                (DM, "You already submitted Wordle today."),
            ],
        )
        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].score, "5/6")

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
                "text": "Wordle 1,234 3/6",
            }
        )

        self.assertEqual(self.client.reactions, [(GROUP, 7, "\N{THUMBS UP SIGN}")])
        self.assertEqual(self.client.sent, [])
        standings = self.store.standings(chat_id=GROUP, local_date="2033-05-18")
        self.assertEqual(standings[0].entries[0].score, "3/6")

    def test_leaderboard_command_answers_in_the_chat_that_asked(self) -> None:
        self.dm("Wordle 1,234 3/6")
        self.client.sent.clear()

        board = "Daily games - 2033-05-18\n\n🆆 Wordle 1234\n1. 👑 Alice - 3/6"
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
        self.dm("Wordle 1,234 2/6", user_id=5, name="Alice")
        self.dm("Wordle 1,234 5/6", user_id=6, name="Bob")
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
        chad, chud = self.client.sent[1:]
        self.assertIn("Alice is the CHAD of the day!", chad[1])
        self.assertEqual(chud[1], "🚽 Ding ding ding! Bob is the CHUD of the day!")
        self.client.sent.clear()

        # A result that arrives after the post must not revise either winner.
        self.handle(
            {
                "chat": {"id": GROUP, "type": "supergroup"},
                "message_id": 21,
                "date": 2_000_035_900,
                "from": {"id": 7, "first_name": "Cara"},
                "text": "Wordle 1,234 1/6",
            }
        )
        ask("/chad@sfbot", chat_id=GROUP)
        ask("/chud")
        self.assertEqual(self.client.sent, [(GROUP, chad[1]), (DM, chud[1])])

        self.client.sent.clear()
        ask("/chad", user_id=99)
        ask("/chud", chat_id=OTHER_GROUP, user_id=99)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("roster", self.client.sent[0][1])

    def test_a_nickname_change_reaches_the_chad_and_chud_commands(self) -> None:
        self.dm("Wordle 1,234 2/6", user_id=5, name="Alice")
        self.dm("Wordle 1,234 5/6", user_id=6, name="Bob", message_id=8)
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
        self.assertEqual(chud[1], "🚽 Ding ding ding! Diddy is the CHUD of the day!")

    def test_an_announcement_from_an_older_build_still_follows_a_nickname(self) -> None:
        # A day posted before winners were recorded leaves only its message,
        # so the names in it are the only way back to the players.
        for user_id, name, text in (
            (5, "Alice", "Wordle 1,234 2/6"),
            (6, "Bob", "Wordle 1,234 5/6"),
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
        self.dm("Wordle 1,234 2/6", user_id=5, name="Alice")
        self.dm("Wordle 1,234 5/6", user_id=6, name="Bob", message_id=8)
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
        self.dm("Wordle 1,234 2/6", user_id=5, name="Alice")
        self.dm("Wordle 1,234 5/6", user_id=6, name="Bob", message_id=8)
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
        self.assertEqual(chud[1], "🚽 Ding ding ding! Bob is the CHUD of the day!")

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
                "text": "Wordle 1,234 1/6",
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
        self.dm("Wordle 1,234 1/6", user_id=99, name="Mallory")

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

        self.dm("Wordle 1,234 4/6", user_id=9, name="Dave", message_id=2)

        self.assertEqual(self.client.sent, [(DM, "Recorded Wordle 1234 - 4/6")])
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
                "text": "Wordle 1,234 3/6",
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
                "text": "Wordle 1,234 3/6 https://x.com/alice/status/123",
            }
        )

        self.assertEqual(self.client.reactions, [])
        self.assertEqual(
            self.store.standings(chat_id=GROUP, local_date="2033-05-18"), []
        )


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
        self.record("Wordle 1,234 3/6", user_id=5, name="Alice")

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
            ("Wordle 1,234 3/6", "Alice"),
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
        result = parse_result("Wordle 1,234 3/6")
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

        # The board, CHAD, and CHUD are sent once in that order.
        self.assertEqual(len(self.client.sent), 3)
        self.assertEqual(self.client.sent[0][0], GROUP)
        self.assertIn("Wordle 1234", self.client.sent[0][1])

    def test_the_daily_post_crowns_a_chud(self) -> None:
        for user_id, name, text in (
            (5, "Alice", "Wordle 1,234 2/6"),
            (6, "Bob", "Wordle 1,234 5/6"),
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
            (GROUP, "🚽 Ding ding ding! Bob is the CHUD of the day!"),
        )
        self.assertIn("Alice is the CHAD of the day!", self.client.sent[1][1])

    def test_nothing_is_posted_when_no_results_exist(self) -> None:
        self.post(2_000_035_800)
        self.assertEqual(self.client.sent, [])

    def test_scheduled_post_uses_the_current_nickname(self) -> None:
        result = parse_result("Wordle 1,234 3/6")
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

    def test_the_post_records_who_won_by_user_id(self) -> None:
        for user_id, name, text in (
            (5, "Alice", "Wordle 1,234 2/6"),
            (6, "Bob", "Wordle 1,234 5/6"),
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
