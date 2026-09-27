import unittest

from sfbot.games import parse_result


class ParseResultTests(unittest.TestCase):
    def test_wordle_share_text_is_recognized(self) -> None:
        cases = [
            (
                "Wordle 1,234 3/6\n\n⬛\U0001f7e9\n\U0001f7e9\U0001f7e9",
                "1234",
                "3/6",
                3,
            ),
            ("Wordle 1234 6/6", "1234", "6/6", 6),
            ("Wordle 1.234 4/6*", "1234", "4/6", 4),
            ("wordle 999 x/6", "999", "X/6", 7),
            ("gg all\nWordle 1,234 2/6\n\U0001f7e9", "1234", "2/6", 2),
        ]
        for text, puzzle_id, score, rank_key in cases:
            result = parse_result(text)
            self.assertIsNotNone(result, text)
            assert result is not None
            self.assertEqual(result.game, "Wordle")
            self.assertEqual(result.puzzle_id, puzzle_id)
            self.assertEqual(result.score, score)
            self.assertEqual(result.rank_key, rank_key)

    def test_krillion_share_text_ranks_a_deeper_dive_first(self) -> None:
        text = (
            "Krillion #74 \U0001f990\n465\n\n"
            "\U0001f41f\U0001f3ee\U0001f991\U0001f3ee\U0001f991\U0001f991\U0001f3ee"
        )
        result = parse_result(text)
        assert result is not None
        self.assertEqual(result.game, "Krillion")
        self.assertEqual(result.puzzle_id, "74")
        self.assertEqual(result.score, "465")
        # A deeper dive scores higher, so it must sort ahead of a shallow one.
        shallow = parse_result("Krillion #74 \U0001f990\n120")
        assert shallow is not None
        self.assertLess(result.rank_key, shallow.rank_key)

    def test_fermi_share_text_ranks_a_smaller_error_first(self) -> None:
        text = (
            "Fermi · No. 63\n"
            "01  14.0×\n02  12,870×\n03  33,500,000,000×\n"
            "─────────\n"
            "241× score · top 99%\n"
            "<https://fermi.gg/s/daily>"
        )
        result = parse_result(text)
        assert result is not None
        self.assertEqual(result.game, "Fermi")
        self.assertEqual(result.puzzle_id, "63")
        self.assertEqual(result.score, "241×")
        self.assertEqual(result.rank_key, 241.0)

        # The per-question lines must not be mistaken for the total.
        near_perfect = parse_result("Fermi · No. 63\n01  1.1×\n1.4× score")
        assert near_perfect is not None
        self.assertEqual(near_perfect.rank_key, 1.4)
        self.assertLess(near_perfect.rank_key, result.rank_key)

    def test_connections_counts_mistakes_from_the_grid(self) -> None:
        yellow, green, blue, purple = (
            "\U0001f7e8",
            "\U0001f7e9",
            "\U0001f7e6",
            "\U0001f7ea",
        )
        solved = f"{yellow * 4}\n{green * 4}\n{blue * 4}\n{purple * 4}"
        header = "Connections\nPuzzle #1204\n"
        cases = [
            (f"{header}{solved}", "perfect", 0),
            (f"{header}{yellow * 3}{green}\n{solved}", "1 mistake", 1),
            (
                f"{header}{yellow * 3}{green}\n{yellow * 3}{blue}\n{solved}",
                "2 mistakes",
                2,
            ),
        ]
        for text, score, rank_key in cases:
            result = parse_result(text)
            assert result is not None
            self.assertEqual(result.game, "Connections")
            self.assertEqual(result.puzzle_id, "1204")
            self.assertEqual(result.score, score)
            self.assertEqual(result.rank_key, rank_key)

    def test_connections_reports_four_mistakes_as_a_loss(self) -> None:
        text = (
            "Connections\nPuzzle #1204\n"
            "\U0001f7e8\U0001f7e9\U0001f7e8\U0001f7e6\n"
            "\U0001f7e8\U0001f7e9\U0001f7e6\U0001f7e8\n"
            "\U0001f7e8\U0001f7e6\U0001f7e9\U0001f7e8\n"
            "\U0001f7e8\U0001f7e6\U0001f7e8\U0001f7e8"
        )
        result = parse_result(text)
        assert result is not None
        self.assertEqual(result.score, "lost")
        # The game ends at four mistakes, so a loss already sorts last.
        self.assertEqual(result.rank_key, 4)

    def test_unrelated_text_is_not_a_result(self) -> None:
        self.assertIsNone(parse_result(""))
        self.assertIsNone(parse_result("anyone done wordle yet"))
        self.assertIsNone(parse_result("Wordle 1,234 7/6"))
        self.assertIsNone(parse_result("Wordle 1,234 3/5"))
        self.assertIsNone(parse_result("I got Wordle 1,234 3/6 on my third try"))
        self.assertIsNone(parse_result("Wordle\n1,234 3/6"))
        self.assertIsNone(parse_result("Krillion #74 \U0001f990"))
        self.assertIsNone(parse_result("anyone playing krillion"))
        self.assertIsNone(parse_result("Fermi · No. 63\n01  14.0×"))
        self.assertIsNone(parse_result("Connections\nPuzzle #1204"))
        self.assertIsNone(parse_result("how did everyone do on connections"))


if __name__ == "__main__":
    unittest.main()
