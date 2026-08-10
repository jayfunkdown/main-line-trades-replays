import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from scripts import trendspider_filter


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class BorderedTrendSpiderPostTests(unittest.TestCase):
    def test_caption_and_chart_share_one_visible_bordered_embed(self):
        captured = {}

        def make_request(destination, *, data, headers, method):
            captured["destination"] = destination
            captured["data"] = data
            captured["method"] = method
            return SimpleNamespace()

        with patch.object(
            trendspider_filter.urllib.request,
            "Request",
            side_effect=make_request,
        ), patch.object(
            trendspider_filter.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            trendspider_filter.post_to_public_channel(
                "chart-destination",
                "**SPY — Weekly Chart**",
                "https://example.com/chart.png",
            )

        payload = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(captured["destination"], "chart-destination")
        self.assertEqual(captured["method"], "POST")
        self.assertNotIn("content", payload)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)
        self.assertEqual(
            payload["embeds"][0],
            {
                "description": "**SPY — Weekly Chart**",
                "color": 0x00CFFF,
                "image": {"url": "https://example.com/chart.png"},
            },
        )
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
