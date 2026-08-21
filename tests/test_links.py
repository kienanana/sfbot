import unittest

from sfbot.links import extract_tweet_ids, tweet_id_from_url


class TweetIdFromUrlTests(unittest.TestCase):
    def test_accepts_twitter_and_x_variants(self) -> None:
        urls = [
            "https://twitter.com/alice/status/123456789?ref_src=twsrc%5Etfw",
            "https://x.com/bob/status/123456789?s=20",
            "https://mobile.twitter.com/alice/status/123456789/photo/1",
            "x.com/i/web/status/123456789",
        ]
        self.assertEqual([tweet_id_from_url(url) for url in urls], ["123456789"] * 4)

    def test_rejects_non_status_and_lookalike_hosts(self) -> None:
        self.assertIsNone(tweet_id_from_url("https://x.com/alice"))
        self.assertIsNone(tweet_id_from_url("https://notx.com/alice/status/123"))
        self.assertIsNone(tweet_id_from_url("https://evil-twitter.com/alice/status/123"))
        self.assertIsNone(tweet_id_from_url("https://example.com/alice/status/123"))

    def test_extracts_visible_caption_and_hidden_links_once(self) -> None:
        message = {
            "text": "See https://x.com/alice/status/11?s=20 and twitter.com/bob/status/22.",
            "entities": [
                {"type": "text_link", "url": "https://twitter.com/other/status/11"},
                {"type": "bold", "offset": 0, "length": 3},
            ],
            "caption": "https://mobile.x.com/c/status/33/photo/1",
        }
        self.assertEqual(extract_tweet_ids(message), ["11", "22", "33"])


if __name__ == "__main__":
    unittest.main()

