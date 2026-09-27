import tempfile
import unittest
from pathlib import Path

from sfbot.games import ParsedResult
from sfbot.leaderboard import LeaderboardStore, format_standings

DAY = "2033-05-18"


def wordle(score: str, rank_key: int) -> ParsedResult:
    return ParsedResult(game="Wordle", puzzle_id="1234", score=score, rank_key=rank_key)


class LeaderboardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = LeaderboardStore(Path(self.temp_dir.name) / "sfbot.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def record(
        self,
        name: str,
        result: ParsedResult,
        *,
        user_id: int,
        chat_id: int = -100,
        submitted_at: int = 2_000_000_000,
    ) -> bool:
        return self.store.record(
            chat_id=chat_id,
            local_date=DAY,
            user_id=user_id,
            display_name=name,
            result=result,
            submitted_at=submitted_at,
        )

    def test_the_first_submission_of_a_game_wins(self) -> None:
        self.assertTrue(self.record("Alice", wordle("5/6", 5), user_id=5))
        self.assertFalse(self.record("Alice", wordle("2/6", 2), user_id=5))

        board = self.store.standings(chat_id=-100, local_date=DAY)[0]
        self.assertEqual([entry.score for entry in board.entries], ["5/6"])

    def test_a_different_game_from_the_same_person_is_accepted(self) -> None:
        self.assertTrue(self.record("Alice", wordle("3/6", 3), user_id=5))
        connections = ParsedResult(
            game="Connections", puzzle_id="500", score="perfect", rank_key=0
        )
        self.assertTrue(self.record("Alice", connections, user_id=5))

        boards = self.store.standings(chat_id=-100, local_date=DAY)
        self.assertEqual([board.game for board in boards], ["Connections", "Wordle"])

    def test_failures_sort_last_and_ties_share_a_placement(self) -> None:
        self.record("Alice", wordle("X/6", 7), user_id=5, submitted_at=2_000_000_000)
        self.record("Bob", wordle("3/6", 3), user_id=6, submitted_at=2_000_000_010)
        self.record("Cara", wordle("3/6", 3), user_id=7, submitted_at=2_000_000_020)

        text = format_standings(
            self.store.standings(chat_id=-100, local_date=DAY), local_date=DAY
        )
        self.assertEqual(
            text,
            "Daily games - 2033-05-18\n\nWordle 1234\n"
            "1. Bob - 3/6\n1. Cara - 3/6\n3. Alice - X/6",
        )

    def test_chats_have_independent_boards(self) -> None:
        self.record("Alice", wordle("3/6", 3), user_id=5, chat_id=-100)
        self.record("Bob", wordle("4/6", 4), user_id=6, chat_id=-200)

        for chat_id, name in ((-100, "Alice"), (-200, "Bob")):
            board = self.store.standings(chat_id=chat_id, local_date=DAY)[0]
            self.assertEqual([entry.display_name for entry in board.entries], [name])

    def test_a_chat_is_only_awaiting_its_post_once(self) -> None:
        self.record("Alice", wordle("3/6", 3), user_id=5)

        self.assertTrue(self.store.is_awaiting_post(chat_id=-100, local_date=DAY))
        self.assertTrue(self.store.mark_posted(chat_id=-100, local_date=DAY))
        self.assertFalse(self.store.is_awaiting_post(chat_id=-100, local_date=DAY))
        self.assertFalse(self.store.mark_posted(chat_id=-100, local_date=DAY))

    def test_a_day_without_results_is_never_awaiting_a_post(self) -> None:
        self.assertFalse(self.store.is_awaiting_post(chat_id=-100, local_date=DAY))

    def test_the_roster_remembers_who_was_seen_in_the_group(self) -> None:
        self.assertFalse(self.store.is_member(chat_id=-100, user_id=5))

        self.store.remember_member(chat_id=-100, user_id=5, seen_at=2_000_000_000)
        self.assertTrue(self.store.is_member(chat_id=-100, user_id=5))

        # Seeing them again is not an error and refreshes nothing else.
        self.store.remember_member(chat_id=-100, user_id=5, seen_at=2_000_000_100)
        self.assertTrue(self.store.is_member(chat_id=-100, user_id=5))

    def test_the_roster_is_scoped_per_group(self) -> None:
        self.store.remember_member(chat_id=-100, user_id=5, seen_at=2_000_000_000)
        self.assertFalse(self.store.is_member(chat_id=-200, user_id=5))


class FormatStandingsTests(unittest.TestCase):
    def test_an_empty_day_says_so(self) -> None:
        self.assertEqual(
            format_standings([], local_date=DAY),
            "No daily game results for 2033-05-18 yet.",
        )


if __name__ == "__main__":
    unittest.main()
