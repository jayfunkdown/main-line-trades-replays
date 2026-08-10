import unittest

from scripts.discord_embeds import (
    BRAND_ELECTRIC_BLUE,
    BRAND_NEON_PINK,
    bordered_embed,
    bordered_webhook_payload,
)


class DiscordEmbedTests(unittest.TestCase):
    def test_builds_reusable_embed_with_attachment_image(self):
        embed = bordered_embed(
            "A bordered post",
            color=BRAND_NEON_PINK,
            image_url="attachment://chart.png",
        )

        self.assertEqual(
            embed,
            {
                "description": "A bordered post",
                "color": 0xFF2BD6,
                "image": {"url": "attachment://chart.png"},
            },
        )

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
                    "color": BRAND_ELECTRIC_BLUE,
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

    def test_brand_palette_is_stable(self):
        self.assertEqual(BRAND_ELECTRIC_BLUE, 0x00CFFF)
        self.assertEqual(BRAND_NEON_PINK, 0xFF2BD6)


if __name__ == "__main__":
    unittest.main()
