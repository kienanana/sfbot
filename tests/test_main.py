import os
import unittest
from unittest import mock

from sfbot.__main__ import _nicknames


class NicknamesTests(unittest.TestCase):
    def parse(self, raw: str) -> dict[int, str]:
        with mock.patch.dict(os.environ, {"SFBOT_NICKNAMES": raw}):
            return _nicknames()

    def test_unset_means_no_nicknames(self) -> None:
        self.assertEqual(self.parse(""), {})

    def test_entries_are_user_id_to_nickname(self) -> None:
        self.assertEqual(self.parse("5:Juan, 6:Nikki"), {5: "Juan", 6: "Nikki"})

    def test_a_missing_nickname_is_rejected(self) -> None:
        for raw in ("5", "5:", "5:Juan,6"):
            with self.subTest(raw=raw), self.assertRaises(SystemExit):
                self.parse(raw)

    def test_a_non_numeric_user_id_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.parse("Bob:Juan")


if __name__ == "__main__":
    unittest.main()
