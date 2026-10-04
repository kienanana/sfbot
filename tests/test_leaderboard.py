import sqlite3
import tempfile
import unittest
from pathlib import Path

from sfbot.games import ParsedResult
from sfbot.leaderboard import (
    Entry,
    GameStandings,
    DailyRank,
    LeaderboardStore,
    PostedAnnouncement,
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

    def test_users_with_the_same_name_keep_separate_daily_totals(self) -> None:
        # Alex #5 wins one game and skips two; Alex #6 plays all three.
        self.record("Alex", wordle("1/6", 1), user_id=5)
        self.record("Alex", wordle("6/6", 6), user_id=6)
        for game in ("Fermi", "Krillion"):
            self.record("Alex", ParsedResult(game, "42", "1", 1), user_id=6)
        boards = self.store.standings(chat_id=-100, local_date=DAY)
        from sfbot.leaderboard import _total_placings
        self.assertEqual(_total_placings(boards), {5: 5, 6: 4})
        self.assertIn("Alex is the CHAD", format_chad(boards))
        self.assertIn("Alex is the CHUD", format_chud(boards))

    def test_daily_ranking_uses_title_points_and_marks_ties(self) -> None:
        self.record("Alice", wordle("1/6", 1), user_id=5)
        self.record("Bob", wordle("2/6", 2), user_id=6)
        self.record("Cara", wordle("2/6", 2), user_id=7)
        self.record("Alice", ParsedResult("Fermi", "42", "1", 1), user_id=5)
        boards = self.store.standings(chat_id=-100, local_date=DAY)
        ranks = daily_ranking(boards)
        self.assertEqual([(rank.user_id, rank.points) for rank in ranks],
                         [(5, 2), (6, 4), (7, 4)])
        shown = format_daily_ranking(
            ranks, local_date=DAY, chads=chad_winners(boards),
            chuds=chud_winners(boards), nicknames={5: "Juan"},
        )
        self.assertIn("1. 👑 Juan — 2 points", shown)
        self.assertIn("2. 🚽 Bob — 4 points", shown)
        self.assertIn("2. 🚽 Cara — 4 points", shown)

    def test_posted_ranking_survives_reopen_and_late_results(self) -> None:
        self.store.mark_posted(
            chat_id=-100, local_date=DAY,
            chad_winners=[(5, "Alice")], chud_winners=[(6, "Bob")],
            daily_ranking=[DailyRank(5, "Alice", 1), DailyRank(6, "Bob", 2)],
        )
        self.record("Cara", wordle("1/6", 1), user_id=7)
        self.store.close()
        self.store = LeaderboardStore(Path(self.temp_dir.name) / "sfbot.db")
        self.assertEqual(
            self.store.posted_daily_ranking(chat_id=-100, local_date=DAY),
            (DailyRank(5, "Alice", 1), DailyRank(6, "Bob", 2)),
        )

    def test_overall_counts_titles_and_calendar_streaks_by_user_id(self) -> None:
        self.store.mark_posted(
            chat_id=-100, local_date="2033-05-16",
            chad_winners=[(5, "Alex"), (6, "Alex")], chud_winners=[(7, "Cara")],
        )
        self.store.mark_posted(
            chat_id=-100, local_date="2033-05-17",
            chad_winners=[(5, "Alex")], chud_winners=[(7, "Cara")],
        )
        self.store.mark_posted(
            chat_id=-100, local_date=DAY,
            chad_winners=[(5, "Alex")], chud_winners=[(6, "Alex")],
        )
        ranks = self.store.overall_ranking(chat_id=-100, nicknames={5: "Juan"})
        self.assertEqual(
            [(r.user_id, r.chads, r.chuds, r.chad_streak, r.chud_streak) for r in ranks],
            [(5, 3, 0, 3, 0), (6, 1, 1, 0, 1), (7, 0, 2, 0, 0)],
        )
        shown = format_overall_ranking(ranks)
        self.assertIn("Juan — 👑 3  🚽 0  🔥 3", shown)
        self.assertNotIn("Juan — 👑 3  🚽 0  🔥 3  💩", shown)
        self.assertIn("Alex — 👑 1  🚽 1  💩 1", shown)
        self.assertIn("Cara — 👑 0  🚽 2", shown)
        self.assertNotIn("Cara — 👑 0  🚽 2  🔥", shown)
        self.assertEqual(
            self.store.title_streaks(
                chat_id=-100, local_date=DAY, winners=[(5, "Alex"), (6, "Alex")],
                is_chad=True,
            ),
            {5: 3, 6: 1},
        )
        self.store.mark_posted(
            chat_id=-100, local_date="2033-05-20", chad_winners=[(5, "Alex")]
        )
        self.assertEqual(self.store.overall_ranking(chat_id=-100)[0].chad_streak, 1)

    def test_a_name_change_does_not_split_a_player(self) -> None:
        self.record("Alice", wordle("1/6", 1), user_id=5, submitted_at=1)
        self.record("Bob", wordle("6/6", 6), user_id=6, submitted_at=2)
        self.record(
            "Alicia", ParsedResult("Fermi", "42", "1", 1),
            user_id=5, submitted_at=3,
        )
        boards = self.store.standings(chat_id=-100, local_date=DAY)
        self.assertEqual(
            [entry.display_name for board in boards for entry in board.entries
             if entry.user_id == 5], ["Alicia", "Alicia"],
        )
        self.assertIn("Alicia is the CHAD", format_chad(boards))
        self.assertIn("Bob is the CHUD", format_chud(boards))
        self.assertNotIn("Alice", format_chud(boards))

    def test_duplicate_nicknames_do_not_merge_tied_winners(self) -> None:
        self.record("Alice", wordle("3/6", 3), user_id=5)
        self.record("Bob", wordle("3/6", 3), user_id=6)
        boards = self.store.standings(
            chat_id=-100, local_date=DAY, nicknames={5: "Alex", 6: "Alex"},
        )
        self.assertIn("Alex and Alex are the CHADs", format_chad(boards))
        self.assertIn("Alex and Alex are the CHUDs", format_chud(boards))

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

    def test_a_posted_day_remembers_who_won(self) -> None:
        self.assertTrue(
            self.store.mark_posted(
                chat_id=-100,
                local_date=DAY,
                chad_message="Alice is the CHAD",
                chud_message="Bob is the CHUD",
                chad_winners=[(5, "Alice")],
                chud_winners=[(6, "Bob")],
            )
        )

        announcements = self.store.posted_announcements(chat_id=-100, local_date=DAY)
        assert announcements is not None
        self.assertEqual(
            announcements[0], PostedAnnouncement("Alice is the CHAD", ((5, "Alice"),))
        )
        self.assertEqual(
            announcements[1], PostedAnnouncement("Bob is the CHUD", ((6, "Bob"),))
        )

    def test_a_day_posted_without_winners_has_only_its_messages(self) -> None:
        self.store.mark_posted(
            chat_id=-100, local_date=DAY, chad_message="Alice is the CHAD"
        )

        announcements = self.store.posted_announcements(chat_id=-100, local_date=DAY)
        assert announcements is not None
        self.assertEqual(
            announcements,
            (
                PostedAnnouncement("Alice is the CHAD", None),
                PostedAnnouncement(None, None),
            ),
        )

    def test_unreadable_winners_fall_back_to_the_message(self) -> None:
        self.store.mark_posted(
            chat_id=-100, local_date=DAY, chad_message="Alice is the CHAD"
        )
        with sqlite3.connect(self.store.path) as connection:
            connection.execute(
                "UPDATE posted_days SET chad_winners = 'not json' WHERE chat_id = -100"
            )

        announcements = self.store.posted_announcements(chat_id=-100, local_date=DAY)
        assert announcements is not None
        self.assertIsNone(announcements[0].winners)
        self.assertEqual(announcements[0].message, "Alice is the CHAD")

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
            self.assertEqual(
                store.posted_announcements(chat_id=-100, local_date=DAY),
                (
                    PostedAnnouncement(None, None),
                    PostedAnnouncement(None, None),
                ),
            )

    def test_a_database_predating_the_winner_columns_is_upgraded(self) -> None:
        path = Path(self.temp_dir.name) / "old-winners.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE posted_days (
                    chat_id INTEGER NOT NULL,
                    local_date TEXT NOT NULL,
                    chad_message TEXT,
                    chud_message TEXT,
                    PRIMARY KEY (chat_id, local_date)
                ) WITHOUT ROWID"""
            )
            connection.execute(
                "INSERT INTO posted_days VALUES (-100, ?, ?, ?)",
                (DAY, "Alice is the CHAD", "Bob is the CHUD"),
            )
        with LeaderboardStore(path) as store:
            announcements = store.posted_announcements(chat_id=-100, local_date=DAY)
            assert announcements is not None
            self.assertEqual(announcements[0].message, "Alice is the CHAD")
            self.assertIsNone(announcements[0].winners)

    def test_a_days_players_keep_the_names_they_submitted_under(self) -> None:
        self.record("Alice", wordle("3/6", 3), user_id=5)
        self.record("Bob", wordle("4/6", 4), user_id=6)

        self.assertEqual(
            sorted(self.store.daily_names(chat_id=-100, local_date=DAY)),
            [(5, "Alice"), (6, "Bob")],
        )
        self.assertEqual(self.store.daily_names(chat_id=-200, local_date=DAY), [])

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
                entries=(
                    Entry(user_id=5, display_name="Alice", score="1", rank_key=1),
                ),
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
                    user_id=5,
                    display_name="Alice",
                    score="perfect",
                    rank_key=0.04,
                    detail="🟪🟦🟩🟨",
                ),
                Entry(user_id=6, display_name="Bob", score="perfect", rank_key=0.07),
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
                Entry(
                    user_id={"Alice": 1, "Bob": 2, "Cara": 3}[name],
                    display_name=name,
                    score=str(rank),
                    rank_key=rank,
                )
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
                Entry(user_id=5, display_name="Alice", score="3/6", rank_key=3),
                Entry(user_id=6, display_name="Bob", score="5/6", rank_key=5),
                Entry(user_id=7, display_name="Cara", score="5/6", rank_key=5),
            ),
        )
        self.assertEqual(
            format_chud([tied]),
            "🚽 Ding ding ding! Bob and Cara are the CHUDs of the day!",
        )

    def test_three_chuds_are_listed_with_commas(self) -> None:
        boards = [self.board("Wordle", "Alice"), self.board("Fermi", "Bob")]
        boards.append(self.board("Krillion", "Cara"))
        # Everyone played one game and skipped two, so nobody is spared.
        self.assertEqual(
            format_chud(boards),
            "🚽 Ding ding ding! Alice, Bob and Cara are the CHUDs of the day!",
        )

    def test_a_nickname_renames_the_chud(self) -> None:
        boards = [self.board("Wordle", "Alice", "Bob")]

        self.assertEqual(
            format_chud(boards, nicknames={2: "Diddy"}),
            "🚽 Ding ding ding! Diddy is the CHUD of the day!",
        )

    def test_the_winners_carry_the_ids_they_were_recorded_with(self) -> None:
        boards = [self.board("Wordle", "Alice", "Bob")]

        self.assertEqual(chad_winners(boards), [(1, "Alice")])
        self.assertEqual(chud_winners(boards), [(2, "Bob")])

    def test_saved_winners_render_exactly_as_the_standings_do(self) -> None:
        boards = [
            self.board("Wordle", "Alice", "Bob", "Cara"),
            self.board("Fermi", "Bob", "Cara", "Alice"),
        ]

        self.assertEqual(format_chud_winners(chud_winners(boards)), format_chud(boards))
        self.assertEqual(
            format_chad_winners(chad_winners(boards), variation=3),
            format_chad(boards, variation=3),
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

    def test_a_nickname_renames_the_chad(self) -> None:
        boards = [self.board("Wordle", "Alice", "Bob")]

        chad = format_chad(boards, nicknames={1: "Juan"})
        assert chad is not None
        self.assertTrue(
            chad.startswith("👑 Ding ding ding! Juan is the CHAD of the day!\n")
        )

    def test_a_tie_keeps_both_winners_and_their_nicknames(self) -> None:
        boards = [self.board("Wordle", "Alice", "Bob")]
        boards.append(self.board("Fermi", "Bob", "Alice"))

        chad = format_chad(boards, nicknames={1: "Juan", 2: "Diddy"})
        assert chad is not None
        self.assertTrue(
            chad.startswith(
                "👑 Ding ding ding! Diddy and Juan are the CHADs of the day!\n"
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


class ApplyNicknamesTests(unittest.TestCase):
    """A day that recorded only its message must still follow nicknames."""

    def test_a_recorded_name_becomes_its_nickname(self) -> None:
        self.assertEqual(
            apply_nicknames(
                "👑 Ding ding ding! Ethan is the CHAD of the day!\nwahoo!",
                players=[(5, "Ethan"), (6, "Aditya")],
                nicknames={5: "Big Ethan"},
            ),
            "👑 Ding ding ding! Big Ethan is the CHAD of the day!\nwahoo!",
        )

    def test_a_name_no_nickname_covers_is_left_alone(self) -> None:
        self.assertEqual(
            apply_nicknames(
                "🚽 Ding ding ding! Aditya is the CHUD of the day!",
                players=[(5, "Ethan"), (6, "Aditya")],
                nicknames={5: "Big Ethan"},
            ),
            "🚽 Ding ding ding! Aditya is the CHUD of the day!",
        )

    def test_a_name_two_nicknames_share_is_left_alone(self) -> None:
        # Two players called Bob go by different nicknames, so "Bob" in a
        # message recorded before the split cannot be attributed to either.
        self.assertEqual(
            apply_nicknames(
                "🚽 Ding ding ding! Bob is the CHUD of the day!",
                players=[(5, "Bob"), (6, "Bob")],
                nicknames={5: "Juan", 6: "Diddy"},
            ),
            "🚽 Ding ding ding! Bob is the CHUD of the day!",
        )

    def test_a_shorter_name_does_not_replace_part_of_a_longer_one(self) -> None:
        self.assertEqual(
            apply_nicknames(
                "Ding ding ding! Ann and Anna are the CHUDs of the day!",
                players=[(5, "Ann"), (6, "Anna")],
                nicknames={6: "Diddy"},
            ),
            "Ding ding ding! Ann and Diddy are the CHUDs of the day!",
        )

    def test_only_the_line_naming_the_winners_is_rewritten(self) -> None:
        # A closing line is a joke in its own right, so a name it happens to
        # mention is left as the joke's author wrote it.
        self.assertEqual(
            apply_nicknames(
                "👑 Ding ding ding! Alice is the CHAD of the day!\nAlice grass! 🌱",
                players=[(5, "Alice")],
                nicknames={5: "Juan"},
            ),
            "👑 Ding ding ding! Juan is the CHAD of the day!\nAlice grass! 🌱",
        )

    def test_two_names_are_replaced_longest_first(self) -> None:
        self.assertEqual(
            apply_nicknames(
                "Ding ding ding! Ann and Anna are the CHUDs of the day!",
                players=[(5, "Ann"), (6, "Anna")],
                nicknames={5: "Juan", 6: "Diddy"},
            ),
            "Ding ding ding! Juan and Diddy are the CHUDs of the day!",
        )


if __name__ == "__main__":
    unittest.main()
