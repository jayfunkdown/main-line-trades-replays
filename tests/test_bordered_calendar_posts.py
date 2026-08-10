import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch("dotenv.load_dotenv"), patch.dict(
    os.environ,
    {"ECONOMIC_CALENDAR_WEBHOOK": "calendar-destination"},
    clear=True,
):
    from scripts import economic_calendar
    from scripts import weekly_economic_calendar


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class BorderedCalendarPostTests(unittest.TestCase):
    def assert_bordered_post(self, module):
        captured = {}

        def make_request(destination, *, data, headers, method):
            captured["destination"] = destination
            captured["data"] = data
            captured["headers"] = headers
            captured["method"] = method
            return SimpleNamespace()

        with patch.object(
            module.urllib.request,
            "Request",
            side_effect=make_request,
        ), patch.object(
            module.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            module.post_to_discord("# Calendar\nCalendar details")

        payload = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(captured["destination"], "calendar-destination")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("content", payload)
        self.assertEqual(len(payload["embeds"]), 1)
        self.assertEqual(
            payload["embeds"][0]["description"],
            "# Calendar\nCalendar details",
        )
        self.assertEqual(payload["embeds"][0]["color"], 0x5865F2)
        urlopen.assert_called_once()

    def test_daily_calendar_uses_bordered_embed(self):
        self.assert_bordered_post(economic_calendar)

    def test_weekly_calendar_uses_bordered_embed(self):
        self.assert_bordered_post(weekly_economic_calendar)


if __name__ == "__main__":
    unittest.main()
