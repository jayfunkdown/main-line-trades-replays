import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


class MarketWrapScheduleTests(unittest.TestCase):
    def test_service_runs_explicit_post_mode_from_production_checkout(self):
        service = (
            SYSTEMD / "mainline-market-wrap.service"
        ).read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", service)
        self.assertIn("User=jason", service)
        self.assertIn(
            "WorkingDirectory=/home/jason/main-line-trades-replays",
            service,
        )
        self.assertIn(
            "scripts/market_wrap.py --post",
            service,
        )
        self.assertNotIn("MARKET_WRAP_WEBHOOK", service)

    def test_timer_runs_weekdays_at_415_eastern(self):
        timer = (
            SYSTEMD / "mainline-market-wrap.timer"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "OnCalendar=Mon..Fri *-*-* 16:15:00 America/New_York",
            timer,
        )
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=mainline-market-wrap.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_installation_documentation_enables_timer(self):
        documentation = (
            SYSTEMD / "README.md"
        ).read_text(encoding="utf-8")

        self.assertIn("mainline-market-wrap.timer", documentation)


if __name__ == "__main__":
    unittest.main()
