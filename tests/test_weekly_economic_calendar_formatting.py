import os
import unittest
from datetime import datetime
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(
    os.environ,
    {"ECONOMIC_CALENDAR_WEBHOOK": "https://discord.test/webhook"},
    clear=True,
):
    from scripts import weekly_economic_calendar as calendar


class WeeklyEconomicCalendarFormattingTests(unittest.TestCase):
    def test_weekly_card_uses_spacious_heading_hierarchy(self):
        monday = datetime(2026, 8, 10, tzinfo=calendar.ET)
        friday = datetime(2026, 8, 14, tzinfo=calendar.ET)
        event = {
            "title": "Core CPI m/m",
            "datetime": datetime(2026, 8, 12, 8, 30, tzinfo=calendar.ET),
            "forecast": "0.2%",
            "previous": "0.0%",
        }

        with patch.object(
            calendar,
            "current_week_bounds",
            return_value=(monday.date(), friday.date()),
        ):
            message = calendar.build_message([event])

        self.assertTrue(message.startswith("# 🗓️ Weekly U.S. Economic Calendar"))
        self.assertIn("## ⚠️ Week Overview", message)
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            self.assertIn(f"## {day} — August", message)
        self.assertIn("## ⚠️ Trading Reminder", message)
        self.assertNotIn("🗓️ **Weekly U.S. Economic Calendar**", message)
        self.assertLess(len(message), 4096)


if __name__ == "__main__":
    unittest.main()
