import asyncio
import unittest
from types import SimpleNamespace

from sfbot.cleanup import sweep

GROUP = -100


class FakeMTProtoClient:
    """Holds a chat's messages by ID, the way a supergroup numbers them."""

    def __init__(self, texts: dict[int, str | None]) -> None:
        self.texts = texts
        self.fetched: list[list[int]] = []
        self.deleted: list[list[int]] = []

    async def get_input_entity(self, chat_id: int) -> int:
        return chat_id

    async def get_messages(self, chat: int, *, ids: list[int]) -> list[object]:
        self.fetched.append(ids)
        return [
            SimpleNamespace(id=message_id, raw_text=self.texts[message_id])
            if message_id in self.texts
            else None
            for message_id in ids
        ]

    async def delete_messages(self, chat: int, ids: list[int]) -> list[object]:
        self.deleted.append(ids)
        for message_id in ids:
            del self.texts[message_id]
        return [SimpleNamespace(pts_count=len(ids))]


class SweepTests(unittest.TestCase):
    def sweep(self, client: FakeMTProtoClient, needle: str, last: int) -> int:
        return asyncio.run(
            sweep(client, chat_id=GROUP, needle=needle, last_message_id=last)
        )

    def test_deletes_substring_matches_in_any_case(self) -> None:
        client = FakeMTProtoClient(
            {
                1: "foo",
                2: "unrelated",
                3: "a FOObar b",
                4: "fOo",
                5: "fo o",
                7: "/cleanup Foo",
            }
        )
        self.assertEqual(self.sweep(client, "Foo", 7), 4)
        self.assertEqual(client.deleted, [[1, 3, 4, 7]])
        self.assertEqual(client.texts, {2: "unrelated", 5: "fo o"})

    def test_skips_missing_and_textless_messages(self) -> None:
        client = FakeMTProtoClient({2: None, 3: "foo"})
        self.assertEqual(self.sweep(client, "foo", 4), 1)
        self.assertEqual(client.deleted, [[3]])

    def test_walks_every_id_in_batches_of_one_hundred(self) -> None:
        client = FakeMTProtoClient({1: "foo", 100: "foo", 101: "foo", 250: "foo"})
        self.assertEqual(self.sweep(client, "foo", 250), 4)
        self.assertEqual(
            client.fetched,
            [list(range(1, 101)), list(range(101, 201)), list(range(201, 251))],
        )
        self.assertEqual(client.deleted, [[1, 100], [101], [250]])

    def test_nothing_matching_deletes_nothing(self) -> None:
        client = FakeMTProtoClient({1: "bar"})
        self.assertEqual(self.sweep(client, "foo", 1), 0)
        self.assertEqual(client.deleted, [])


if __name__ == "__main__":
    unittest.main()
