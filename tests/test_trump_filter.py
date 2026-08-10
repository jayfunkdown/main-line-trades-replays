import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from scripts import trump_filter


class OversizedMessageTests(unittest.TestCase):
    def setUp(self):
        self.message = {
            "id": "123456789",
            "content": "x" * (trump_filter.DISCORD_CONTENT_LIMIT + 1),
            "attachments": [],
            "embeds": [],
        }

    def test_post_mode_marks_oversized_message_without_posting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "trump_processed.json"
            state_path.write_text("[]\n", encoding="utf-8")
            output = StringIO()

            with (
                patch.object(trump_filter, "STATE_PATH", state_path),
                patch.object(
                    trump_filter,
                    "required_env",
                    side_effect=lambda name: name,
                ),
                patch.object(
                    trump_filter,
                    "get_recent_messages",
                    return_value=[self.message],
                ),
                patch.object(
                    trump_filter,
                    "post_to_public_channel",
                ) as post_message,
                patch.object(sys, "argv", ["trump_filter.py", "--post"]),
                redirect_stdout(output),
            ):
                result = trump_filter.main()

            self.assertEqual(result, 0)
            post_message.assert_not_called()
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                [self.message["id"]],
            )
            self.assertIn("oversized item(s)", output.getvalue())

    def test_preview_mode_does_not_change_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "trump_processed.json"
            output = StringIO()

            with (
                patch.object(trump_filter, "STATE_PATH", state_path),
                patch.object(
                    trump_filter,
                    "required_env",
                    side_effect=lambda name: name,
                ),
                patch.object(
                    trump_filter,
                    "get_recent_messages",
                    return_value=[self.message],
                ),
                patch.object(sys, "argv", ["trump_filter.py", "--preview"]),
                redirect_stdout(output),
            ):
                result = trump_filter.main()

            self.assertEqual(result, 0)
            self.assertFalse(state_path.exists())
            self.assertIn("oversized item(s)", output.getvalue())


class BorderedPublicPayloadTests(unittest.TestCase):
    def test_text_uses_one_bordered_embed(self):
        payload = trump_filter.build_public_payload(
            "👤 **Truth Social**\n\nMarket update",
            [],
        )

        self.assertNotIn("content", payload)
        self.assertEqual(payload["username"], "Main Line Trades Truth Social")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(
            payload["embeds"],
            [
                {
                    "description": "👤 **Truth Social**\n\nMarket update",
                    "color": 0x00CFFF,
                }
            ],
        )

    def test_first_image_shares_the_text_card(self):
        payload = trump_filter.build_public_payload(
            "Truth Social post",
            [
                "https://example.com/one.png",
                "https://example.com/two.png",
            ],
        )

        self.assertEqual(len(payload["embeds"]), 2)
        self.assertEqual(
            payload["embeds"][0],
            {
                "description": "Truth Social post",
                "color": 0x00CFFF,
                "image": {
                    "url": "https://example.com/one.png",
                },
            },
        )
        self.assertEqual(
            payload["embeds"][1],
            {
                "color": 0x00CFFF,
                "image": {
                    "url": "https://example.com/two.png",
                },
            },
        )

    def test_image_count_remains_capped_at_ten(self):
        payload = trump_filter.build_public_payload(
            "Truth Social post",
            [
                f"https://example.com/{index}.png"
                for index in range(12)
            ],
        )

        self.assertEqual(len(payload["embeds"]), 10)
        self.assertEqual(
            payload["embeds"][-1]["image"]["url"],
            "https://example.com/9.png",
        )


if __name__ == "__main__":
    unittest.main()
