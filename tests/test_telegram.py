import tempfile
import unittest
from pathlib import Path

from sfbot.cache import DuplicateCache
from sfbot.games import parse_result
from sfbot.leaderboard import LeaderboardStore
from sfbot.telegram import (
    TelegramAPIError,
    handle_message,
    post_due_leaderboard,
    send_due_reminders,
)

SGT_OFFSET = 480
GROUP = -100
DM = 555


class FakeTelegramClient:
    def __init__(self) -> None:
        self.replies: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str]] = []
        self.reactions: list[tuple[int, int, str]] = []

    def send_reply(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.replies.append((chat_id, message_id))
        self.sent.append((chat_id, text))

    def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def set_message_reaction(
        self, *, chat_id: int, message_id: int, emoji: str
    ) -> None:
        self.reactions.append((chat_id, message_id, emoji))


class MissingReplyClient(FakeTelegramClient):
    def send_reply(self, *, chat_id: int, message_id: int, text: str) -> None:
        raise TelegramAPIError(400, "Bad Request: message to be replied not found")


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
    ) -> None:
        handle_message(
            message,  # type: ignore[arg-type]
            cache=self.cache,
            store=self.store,
            client=self.client if client is None else client,
            utc_offset_minutes=SGT_OFFSET,
            board_chat_id=board_chat_id,
            action_word=action_word,
        )

    def dm(
        self, text: str, *, user_id: int = 5, name: str = "Alice", message_id: int = 7
    ) -> None:
        self.handle(
            {
                "chat": {"id": DM, "type": "private"},
                "message_id": message_id,
                "date": 2_000_000_000,
                "from": {"id": user_id, "first_name": name},
                "text": text,
            }
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
        self.assertEqual(self.client.replies, [(-100, 41)])

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
        self.handle(first, action_word="Kick")
        self.handle(second, action_word="Kick")

        self.assertEqual(self.client.replies, [(GROUP, 41)])
        self.assertEqual(
            self.client.sent,
            [
                (GROUP, "sf"),
                (GROUP, "Uh oh! Looks like Bob's getting *Kicked*"),
                (GROUP, "Let's drop a /KickBob"),
            ],
        )

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

        self.assertEqual(self.client.sent, [(GROUP, "sf")])

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

        self.assertEqual(working_client.replies, [(-100, 42)])

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

        board = "Daily games - 2033-05-18\n\nWordle 1234\n1. Alice - 3/6"
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
            ("Connections\nPuzzle #99\n\U0001f7e8\U0001f7e8\U0001f7e8\U0001f7e8", "Alice"),
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

    def post(self, now: int) -> None:
        post_due_leaderboard(
            self.client,  # type: ignore[arg-type]
            self.store,
            board_chat_id=GROUP,
            utc_offset_minutes=SGT_OFFSET,
            post_minute=21 * 60,
            now=now,
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

        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0][0], GROUP)
        self.assertIn("Wordle 1234", self.client.sent[0][1])

    def test_nothing_is_posted_when_no_results_exist(self) -> None:
        self.post(2_000_035_800)
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()
