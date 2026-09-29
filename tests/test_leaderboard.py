import sqlite3
import tempfile
import unittest
from pathlib import Path

from sfbot.games import ParsedResult
from sfbot.leaderboard import (
    Entry,
    GameStandings,
    LeaderboardStore,
    format_chad,
    format_chud,
    format_standings,
)

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

    def test_a_detail_survives_the_round_trip(self) -> None:
        connections = ParsedResult(
            game="Connections",
            puzzle_id="500",
            score="perfect",
            rank_key=0.04,
            detail="🟪🟦🟩🟨",
        )
        self.record("Alice", connections, user_id=5)

        board = self.store.standings(chat_id=-100, local_date=DAY)[0]
        self.assertEqual(board.entries[0].detail, "🟪🟦🟩🟨")

    def test_a_database_predating_the_detail_column_is_upgraded(self) -> None:
        path = Path(self.temp_dir.name) / "old.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE game_results (
                    chat_id INTEGER NOT NULL,
                    local_date TEXT NOT NULL,
                    game TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    puzzle_id TEXT NOT NULL,
                    score TEXT NOT NULL,
                    rank_key REAL NOT NULL,
                    submitted_at INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, local_date, game, user_id)
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                "INSERT INTO game_results"
                " VALUES (-100, ?, 'Wordle', 5, 'Alice', '1234', '3/6', 3, 0)",
                (DAY,),
            )

        with LeaderboardStore(path) as store:
            board = store.standings(chat_id=-100, local_date=DAY)[0]
            self.assertEqual(board.entries[0].detail, "")

    def test_a_different_game_from_the_same_person_is_accepted(self) -> None:
        self.assertTrue(self.record("Alice", wordle("3/6", 3), user_id=5))
        connections = ParsedResult(
            game="Connections", puzzle_id="500", score="perfect", rank_key=0
        )
        self.assertTrue(self.record("Alice", connections, user_id=5))

        boards = self.store.standings(chat_id=-100, local_date=DAY)
        self.assertEqual([board.game for board in boards], ["Connections", "Wordle"])

    def test_different_puzzles_on_the_same_day_have_separate_boards(self) -> None:
        self.record("Alice", wordle("4/6", 4), user_id=5)
        next_puzzle = ParsedResult(
            game="Wordle", puzzle_id="1235", score="2/6", rank_key=2
        )
        self.record("Bob", next_puzzle, user_id=6)

        boards = self.store.standings(chat_id=-100, local_date=DAY)
        self.assertEqual([board.puzzle_id for board in boards], ["1234", "1235"])
        self.assertEqual(
            [board.entries[0].display_name for board in boards], ["Alice", "Bob"]
        )

    def test_failures_sort_last_and_ties_share_a_placement(self) -> None:
        self.record("Alice", wordle("X/6", 7), user_id=5, submitted_at=2_000_000_000)
        self.record("Bob", wordle("3/6", 3), user_id=6, submitted_at=2_000_000_010)
        self.record("Cara", wordle("3/6", 3), user_id=7, submitted_at=2_000_000_020)

        text = format_standings(
            self.store.standings(chat_id=-100, local_date=DAY), local_date=DAY
        )
        self.assertEqual(
            text,
            "Daily games - 2033-05-18\n\n🆆 Wordle 1234\n"
            "1. 👑 Bob - 3/6\n1. 👑 Cara - 3/6\n3. Alice - X/6",
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

    def test_a_posted_day_keeps_its_announcements(self) -> None:
        self.assertIsNone(self.store.posted_messages(chat_id=-100, local_date=DAY))
        self.assertTrue(
            self.store.mark_posted(
                chat_id=-100,
                local_date=DAY,
                chad_message="Alice is the CHAD",
                chud_message="Bob is the CHUD",
            )
        )
        self.assertEqual(
            self.store.posted_messages(chat_id=-100, local_date=DAY),
            ("Alice is the CHAD", "Bob is the CHUD"),
        )
        self.assertFalse(
            self.store.mark_posted(
                chat_id=-100, local_date=DAY, chad_message="Changed"
            )
        )
        self.assertEqual(
            self.store.posted_messages(chat_id=-100, local_date=DAY),
            ("Alice is the CHAD", "Bob is the CHUD"),
        )

    def test_an_old_posted_days_table_is_upgraded(self) -> None:
        path = Path(self.temp_dir.name) / "old-posts.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE posted_days (
                    chat_id INTEGER NOT NULL,
                    local_date TEXT NOT NULL,
                    PRIMARY KEY (chat_id, local_date)
                ) WITHOUT ROWID"""
            )
            connection.execute("INSERT INTO posted_days VALUES (-100, ?)", (DAY,))
        with LeaderboardStore(path) as store:
            self.assertEqual(
                store.posted_messages(chat_id=-100, local_date=DAY), (None, None)
            )

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

    def test_each_game_heading_has_its_emoji(self) -> None:
        boards = [
            GameStandings(
                game=name,
                puzzle_id="42",
                entries=(Entry(display_name="Alice", score="1", rank_key=1),),
            )
            for name in ("Connections", "Fermi", "Krillion", "Wordle")
        ]
        text = format_standings(boards, local_date=DAY)

        for heading in (
            "🧩 Connections 42",
            "🧮 Fermi 42",
            "🦐 Krillion 42",
            "🆆 Wordle 42",
        ):
            self.assertIn(heading, text)
        self.assertEqual(text.count("1. 👑 Alice - 1"), 4)

    def test_a_detail_sits_between_the_name_and_the_score(self) -> None:
        board = GameStandings(
            game="Connections",
            puzzle_id="1204",
            entries=(
                Entry(
                    display_name="Alice",
                    score="perfect",
                    rank_key=0.04,
                    detail="🟪🟦🟩🟨",
                ),
                Entry(display_name="Bob", score="perfect", rank_key=0.07),
            ),
        )
        text = format_standings([board], local_date=DAY)

        self.assertIn("1. 👑 Alice 🟪🟦🟩🟨 - perfect", text)
        self.assertIn("2. Bob - perfect", text)


class FormatChudTests(unittest.TestCase):
    @staticmethod
    def board(game: str, *names: str) -> GameStandings:
        return GameStandings(
            game=game,
            puzzle_id="42",
            entries=tuple(
                Entry(display_name=name, score=str(rank), rank_key=rank)
                for rank, name in enumerate(names, start=1)
            ),
        )

    def test_a_day_without_results_has_no_chud(self) -> None:
        self.assertIsNone(format_chud([]))

    def test_the_worst_total_placement_is_the_chud(self) -> None:
        boards = [
            self.board("Wordle", "Alice", "Bob", "Cara"),
            self.board("Fermi", "Bob", "Cara", "Alice"),
            self.board("Krillion", "Alice", "Cara", "Bob"),
            self.board("Connections", "Alice", "Bob", "Cara"),
        ]
        # Alice 6, Bob 8, Cara 10 - one bad game is not enough to sink Alice.
        self.assertEqual(
            format_chud(boards), "🚽 Ding ding ding! Cara is the CHUD of the day!"
        )

    def test_sitting_a_game_out_is_worse_than_losing_it(self) -> None:
        boards = [
            self.board("Wordle", "Alice", "Bob", "Cara"),
            self.board("Fermi", "Alice", "Bob"),
        ]
        # Cara skipped Fermi, which costs more than Bob's last place on it.
        self.assertEqual(
            format_chud(boards), "🚽 Ding ding ding! Cara is the CHUD of the day!"
        )

    def test_a_shared_placement_spares_nobody(self) -> None:
        tied = GameStandings(
            game="Wordle",
            puzzle_id="42",
            entries=(
                Entry(display_name="Alice", score="3/6", rank_key=3),
                Entry(display_name="Bob", score="5/6", rank_key=5),
                Entry(display_name="Cara", score="5/6", rank_key=5),
            ),
        )
        self.assertEqual(
            format_chud([tied]),
            "Ding ding ding! Bob and Cara are the CHUDs of the day!",
        )

    def test_three_chuds_are_listed_with_commas(self) -> None:
        boards = [self.board("Wordle", "Alice"), self.board("Fermi", "Bob")]
        boards.append(self.board("Krillion", "Cara"))
        # Everyone played one game and skipped two, so nobody is spared.
        self.assertEqual(
            format_chud(boards),
            "Ding ding ding! Alice, Bob and Cara are the CHUDs of the day!",
        )


class FormatChadTests(unittest.TestCase):
    board = staticmethod(FormatChudTests.board)

    def test_a_day_without_results_has_no_chad(self) -> None:
        self.assertIsNone(format_chad([]))

    def test_the_best_total_placement_is_the_chad(self) -> None:
        boards = [
            self.board("Wordle", "Alice", "Bob", "Cara"),
            self.board("Fermi", "Bob", "Cara", "Alice"),
            self.board("Krillion", "Alice", "Cara", "Bob"),
            self.board("Connections", "Alice", "Bob", "Cara"),
        ]
        self.assertTrue(
            format_chad(boards, variation=0).startswith(
                "👑 Ding ding ding! Alice is the CHAD of the day!\n"
            )
        )

    def test_skipping_a_game_costs_more_than_last_place(self) -> None:
        boards = [
            self.board("Wordle", "Alice", "Bob"),
            self.board("Fermi", "Bob"),
            self.board("Krillion", "Bob"),
        ]
        self.assertTrue(
            format_chad(boards).startswith(
                "👑 Ding ding ding! Bob is the CHAD of the day!\n"
            )
        )

    def test_ties_name_every_winner(self) -> None:
        boards = [self.board("Wordle", "Alice", "Bob")]
        boards.append(self.board("Fermi", "Bob", "Alice"))
        self.assertTrue(
            format_chad(boards).startswith(
                "👑 Ding ding ding! Alice and Bob are the CHADs of the day!\n"
            )
        )

    def test_all_requested_lines_fit_the_winner_count(self) -> None:
        solo = [self.board("Wordle", "Alice")]
        tied = [self.board("Wordle", "Alice", "Bob"), self.board("Fermi", "Bob", "Alice")]
        singular_lines = [
            format_chad(solo, variation=i).split("\n", 1)[1] for i in range(16)
        ]
        plural_lines = [
            format_chad(tied, variation=i).split("\n", 1)[1] for i in range(14)
        ]
        self.assertEqual(len(set(singular_lines)), 16)
        self.assertEqual(len(set(plural_lines)), 14)
        self.assertEqual(
            set(singular_lines) | set(plural_lines),
            {
                "we gotta audit this guy. 🔎",
                "i hope your parents are proud of you! 🥹",
                "you're the alpha of the pack! 🐺",
                "the big leagues are calling! 📞",
                "are you guys poly? 👀",
                "awwwww",
                "chill out! it's just a game!",
                "touch grass! 🌱",
                "i'm from tel aviv and this is my favourite quizzer!",
                "cool.",
                "congratulations! 🎉",
                "wahoo!",
                "yippee!",
                "that's super hot! 🔥",
                "everybody clap. 👏",
                "it's like everyone else didn't even try!",
                "*feels the aura* 😈",
            },
        )
        self.assertIn("we gotta audit this guy. 🔎", singular_lines)
        self.assertIn("you're the alpha of the pack! 🐺", singular_lines)
        self.assertIn("i'm from tel aviv and this is my favourite quizzer!", singular_lines)
        self.assertIn("are you guys poly? 👀", plural_lines)
        self.assertNotIn("are you guys poly? 👀", singular_lines)
        self.assertNotIn("we gotta audit this guy. 🔎", plural_lines)


if __name__ == "__main__":
    unittest.main()
