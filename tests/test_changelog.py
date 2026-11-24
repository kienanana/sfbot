import tempfile
import unittest
from pathlib import Path

from sfbot.changelog import announce_changelog, changelog_message, read_changelog
from sfbot.leaderboard import LeaderboardStore

GROUP = -100


def record(commit: str, subject: str, body: str = "") -> str:
    return f"{commit}\x1f{subject}\x1f{body}\x1e\n"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class ChangelogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "changelog"
        self.store = LeaderboardStore(Path(self.temp_dir.name) / "sfbot.db")
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def write(self, *records: str) -> None:
        self.path.write_text("".join(records), encoding="utf-8")

    def announce(self) -> None:
        announce_changelog(
            self.client,  # type: ignore[arg-type]
            self.store,
            chat_id=GROUP,
            path=self.path,
        )

    def test_merge_commits_take_the_pull_request_title(self) -> None:
        self.write(
            record(
                "c3",
                "Merge pull request #25 from kienanana/fix/fermi-scores",
                "Fix/fermi scores\n",
            ),
            record("c2", "feat(games): add Fermi (#24)", "Body.\n\nCo-Authored-By: x"),
            record("c1", "Merge pull request #20 from kienanana/revert", ""),
        )
        self.assertEqual(
            read_changelog(self.path),
            [
                ("c3", "Fix/fermi scores (#25)"),
                ("c2", "feat(games): add Fermi (#24)"),
                ("c1", "Merge pull request #20 from kienanana/revert (#20)"),
            ],
        )

    def test_the_message_lists_what_is_newer_than_the_last_announcement(self) -> None:
        entries = [("c3", "three"), ("c2", "two"), ("c1", "one")]
        self.assertEqual(
            changelog_message(entries, "c1"), "sfbot has been updated:\n• three\n• two"
        )
        self.assertEqual(
            changelog_message(entries, "c0"),
            "sfbot has been updated:\n• three\n• two\n• one\n• …and earlier changes",
        )

    def test_a_first_run_only_records_the_version(self) -> None:
        self.write(record("c1", "one"))
        self.announce()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.store.announced_commit(chat_id=GROUP), "c1")

    def test_each_new_version_is_announced_once(self) -> None:
        self.write(record("c1", "one"))
        self.announce()
        self.write(record("c3", "three"), record("c2", "two"), record("c1", "one"))
        self.announce()
        self.announce()
        self.assertEqual(
            self.client.sent, [(GROUP, "sfbot has been updated:\n• three\n• two")]
        )
        self.assertEqual(self.store.announced_commit(chat_id=GROUP), "c3")


if __name__ == "__main__":
    unittest.main()
