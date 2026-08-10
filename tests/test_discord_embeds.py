import unittest

from scripts.discord_embeds import bordered_webhook_payload


class DiscordEmbedTests(unittest.TestCase):
    def test_builds_one_bordered_embed_with_mentions_disabled(self):
        payload = bordered_webhook_payload(
            "Main Line Trades",
            "# Heading\nUseful content",
        )

        self.assertEqual(payload["username"], "Main Line Trades")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("content", payload)
        self.assertEqual(
            payload["embeds"],
            [
                {
                    "description": "# Heading\nUseful content",
                    "color": 0x5865F2,
                }
            ],
        )

    def test_rejects_empty_or_oversized_descriptions(self):
        for description in ("", "   ", "x" * 4097):
            with self.subTest(length=len(description)):
                with self.assertRaises(ValueError):
                    bordered_webhook_payload("Main Line Trades", description)

    def test_accepts_discord_maximum_description_length(self):
        payload = bordered_webhook_payload(
            "Main Line Trades",
            "x" * 4096,
        )

        self.assertEqual(
            len(payload["embeds"][0]["description"]),
            4096,
        )

    def test_rejects_invalid_colors(self):
        for color in (-1, True, 0x1000000, "blue"):
            with self.subTest(color=color):
                with self.assertRaises(ValueError):
                    bordered_webhook_payload(
                        "Main Line Trades",
                        "content",
                        color=color,
                    )


if __name__ == "__main__":
    unittest.main()
