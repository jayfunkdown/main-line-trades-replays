import unittest
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from scripts import staff_announcements


class StaffAnnouncementTests(unittest.TestCase):
    def test_signals_card_uses_headline_body_and_banner(self):
        description = staff_announcements.build_announcement_description(
            staff_announcements.SIGNALS_ANNOUNCEMENT_HEADLINE,
            staff_announcements.SIGNALS_ANNOUNCEMENT_BODY,
        )
        embed = staff_announcements.announcement_embed(description)
        self.assertIn("**A new Signals channel is live.**", description)
        self.assertIn("dedicated **Signals** channel", description)
        self.assertEqual(embed["color"], 0xFF2BD6)
        self.assertEqual(
            embed["image"]["url"],
            "attachment://announcement_banner.png",
        )
        self.assertTrue(staff_announcements.banner_path().is_file())

    def test_blank_headline_is_rejected(self):
        with self.assertRaises(ValueError):
            staff_announcements.build_announcement_description("  ", "Body")

    def test_banner_asset_lives_in_repo(self):
        path = Path(__file__).resolve().parent.parent / "assets" / "announcement_banner.png"
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
