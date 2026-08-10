import hashlib
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

with patch("dotenv.load_dotenv"), patch.dict(
    os.environ,
    {"FINNHUB_API_KEY": "synthetic-key"},
    clear=True,
):
    from scripts import morning_brief


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 10, 7, 0)
        return value.replace(tzinfo=tz) if tz is not None else value


class MorningBriefRoutingTests(unittest.TestCase):
    MORNING_DESTINATION = "morning-destination"
    EARNINGS_DESTINATION = "earnings-destination"

    def setUp(self):
        self.urlopen_patcher = patch.object(
            morning_brief.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    def environment(self):
        return {
            "FINNHUB_API_KEY": "synthetic-key",
            "MORNING_BRIEF_WEBHOOK": self.MORNING_DESTINATION,
            "EARNINGS_CALENDAR_WEBHOOK": self.EARNINGS_DESTINATION,
        }

    def test_main_routes_each_unchanged_builder_output_once(self):
        events = []
        earnings = [
            {
                "symbol": "ACME",
                "name": "",
                "hour": "bmo",
                "priority": True,
            }
        ]
        market_snapshot = []
        global_markets = []
        key_markets = []

        with patch.dict(os.environ, self.environment(), clear=True), patch.object(
            morning_brief,
            "get_high_impact_usd_events",
            return_value=events,
        ) as get_events, patch.object(
            morning_brief,
            "get_all_earnings",
            return_value=earnings,
        ) as get_earnings, patch.object(
            morning_brief,
            "get_market_snapshot",
            return_value=market_snapshot,
        ) as get_market, patch.object(
            morning_brief,
            "get_global_market_snapshot",
            return_value=global_markets,
        ) as get_global_markets, patch.object(
            morning_brief,
            "get_key_markets",
            return_value=key_markets,
        ) as get_key_markets, patch.object(
            morning_brief,
            "build_market_message",
            return_value="MORNING-CONTENT",
        ) as build_market, patch.object(
            morning_brief,
            "build_earnings_message",
            return_value="EARNINGS-CONTENT",
        ) as build_earnings, patch.object(
            morning_brief,
            "send_webhook_message",
        ) as send, patch.object(
            morning_brief.time,
            "sleep",
        ) as sleep, redirect_stdout(StringIO()) as output:
            morning_brief.main()

        get_events.assert_called_once_with()
        get_earnings.assert_called_once_with()
        get_market.assert_called_once_with()
        get_global_markets.assert_called_once_with()
        get_key_markets.assert_called_once_with()
        build_market.assert_called_once_with(
            events,
            market_snapshot,
            global_markets,
            key_markets,
        )
        build_earnings.assert_called_once_with(earnings)
        send.assert_has_calls(
            [
                call(
                    self.MORNING_DESTINATION,
                    "Main Line Trades Morning Brief",
                    "MORNING-CONTENT",
                ),
                call(
                    self.EARNINGS_DESTINATION,
                    "Main Line Trades Earnings Calendar",
                    "EARNINGS-CONTENT",
                ),
            ]
        )
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(10)
        self.assertIn("1 total earnings report(s)", output.getvalue())

    def test_splitting_and_allowed_mentions_apply_to_each_destination(self):
        captured_requests = []

        def make_request(destination, *, data, headers, method):
            request = SimpleNamespace(
                destination=destination,
                data=data,
                headers=headers,
                method=method,
            )
            captured_requests.append(request)
            return request

        deliveries = [
            (
                self.MORNING_DESTINATION,
                morning_brief.MORNING_BRIEF_WEBHOOK_USERNAME,
                "morning one\nmorning two",
            ),
            (
                self.EARNINGS_DESTINATION,
                morning_brief.EARNINGS_CALENDAR_WEBHOOK_USERNAME,
                "earnings one\nearnings two",
            ),
        ]

        real_splitter = morning_brief.split_discord_message

        with patch.object(
            morning_brief,
            "split_discord_message",
            side_effect=lambda message: real_splitter(message, limit=12),
        ), patch.object(
            morning_brief.urllib.request,
            "Request",
            side_effect=make_request,
        ), patch.object(
            morning_brief.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen, patch.object(
            morning_brief.time,
            "sleep",
        ) as sleep:
            count = morning_brief.post_messages(deliveries)

        self.assertEqual(count, 4)
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(
            [request.destination for request in captured_requests],
            [
                self.MORNING_DESTINATION,
                self.MORNING_DESTINATION,
                self.EARNINGS_DESTINATION,
                self.EARNINGS_DESTINATION,
            ],
        )
        payloads = [json.loads(request.data.decode("utf-8")) for request in captured_requests]
        self.assertEqual(
            [payload["username"] for payload in payloads],
            [
                morning_brief.MORNING_BRIEF_WEBHOOK_USERNAME,
                morning_brief.MORNING_BRIEF_WEBHOOK_USERNAME,
                morning_brief.EARNINGS_CALENDAR_WEBHOOK_USERNAME,
                morning_brief.EARNINGS_CALENDAR_WEBHOOK_USERNAME,
            ],
        )
        self.assertTrue(all(payload["allowed_mentions"] == {"parse": []} for payload in payloads))
        self.assertTrue(all("content" not in payload for payload in payloads))
        self.assertTrue(all(len(payload["embeds"]) == 1 for payload in payloads))
        descriptions = [payload["embeds"][0]["description"] for payload in payloads]
        self.assertTrue(all(len(description) <= 12 for description in descriptions))
        self.assertTrue(all("earnings" not in description for description in descriptions[:2]))
        self.assertTrue(all("morning" not in description for description in descriptions[2:]))
        self.assertTrue(all(payload["embeds"][0]["color"] == 0x00CFFF for payload in payloads))
        sleep.assert_called_once_with(10)

    def test_missing_or_empty_destination_fails_before_fetch_or_network(self):
        cases = (
            {},
            {"MORNING_BRIEF_WEBHOOK": ""},
            {"MORNING_BRIEF_WEBHOOK": self.MORNING_DESTINATION},
            {
                "MORNING_BRIEF_WEBHOOK": self.MORNING_DESTINATION,
                "EARNINGS_CALENDAR_WEBHOOK": "   ",
            },
        )

        for values in cases:
            environment = {"FINNHUB_API_KEY": "synthetic-key", **values}
            with self.subTest(values=tuple(sorted(values))), patch.dict(
                os.environ,
                environment,
                clear=True,
            ), patch.object(
                morning_brief,
                "get_high_impact_usd_events",
            ) as get_events, patch.object(
                morning_brief,
                "get_all_earnings",
            ) as get_earnings, patch.object(
                morning_brief,
                "get_market_snapshot",
            ) as get_market, patch.object(
                morning_brief,
                "get_global_market_snapshot",
            ) as get_global_markets, patch.object(
                morning_brief,
                "get_key_markets",
            ) as get_key_markets:
                with self.assertRaisesRegex(RuntimeError, "_WEBHOOK is required"):
                    morning_brief.main()

                get_events.assert_not_called()
                get_earnings.assert_not_called()
                get_market.assert_not_called()
                get_global_markets.assert_not_called()
                get_key_markets.assert_not_called()

    def test_corrected_builder_outputs_route_to_expected_destinations(self):
        with patch.dict(
            os.environ,
            self.environment(),
            clear=True,
        ), patch.object(
            morning_brief,
            "get_high_impact_usd_events",
            return_value=[],
        ), patch.object(
            morning_brief,
            "get_all_earnings",
            return_value=[],
        ), patch.object(
            morning_brief,
            "get_market_snapshot",
            return_value=[],
        ), patch.object(
            morning_brief,
            "get_global_market_snapshot",
            return_value=[],
        ), patch.object(
            morning_brief,
            "get_key_markets",
            return_value=[],
        ), patch.object(
            morning_brief,
            "datetime",
            FixedDateTime,
        ), patch.object(
            morning_brief.random,
            "choice",
            return_value=morning_brief.TRADING_QUOTES[0],
        ) as choose_quote, patch.object(
            morning_brief,
            "send_webhook_message",
        ) as send, patch.object(
            morning_brief.time,
            "sleep",
        ), redirect_stdout(StringIO()):
            morning_brief.main()

        market_message = send.call_args_list[0].args[2]
        earnings_message = send.call_args_list[1].args[2]
        choose_quote.assert_called_once_with(morning_brief.TRADING_QUOTES)
        self.assertEqual(market_message.count("## 🌍 Global Markets"), 1)
        self.assertEqual(market_message.count("## 🧠 Trading Focus"), 1)
        self.assertEqual(market_message.count("## 🎥 Live Today"), 1)
        self.assertNotIn("## 🧠 Trading Focus", earnings_message)
        self.assertNotIn("## 🎥 Live Today", earnings_message)
        self.assertNotIn("## 🌍 Global Markets", earnings_message)
        self.assertIn("**8:30 AM Eastern**", market_message)
        self.assertIn("**📢︱announcements**", market_message)
        self.assertTrue(earnings_message.endswith("━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))

        self.assertEqual(
            hashlib.sha256(market_message.encode("utf-8")).hexdigest(),
            "4e3cb6eacd4f0d8bff54081ffa7b3b2515152888e3df59a9127dd621defd46d0",
        )
        self.assertEqual(
            hashlib.sha256(earnings_message.encode("utf-8")).hexdigest(),
            "109988dce4249a21afea9235039a43f282d7c704a621ea502ce696fc51cd307a",
        )

    def test_global_snapshot_fetches_each_named_index_once(self):
        quotes = [
            {"price": 1000.0 + index, "percent_change": float(index)}
            for index in range(len(morning_brief.GLOBAL_MARKET_SYMBOLS))
        ]

        with patch.object(
            morning_brief,
            "get_yahoo_index_quote",
            side_effect=quotes,
        ) as get_quote:
            snapshot = morning_brief.get_global_market_snapshot()

        self.assertEqual(
            [call.args[0] for call in get_quote.call_args_list],
            [symbol for symbol, _ in morning_brief.GLOBAL_MARKET_SYMBOLS],
        )
        self.assertEqual(
            [item["name"] for item in snapshot],
            [name for _, name in morning_brief.GLOBAL_MARKET_SYMBOLS],
        )
        self.assertEqual([item["quote"] for item in snapshot], quotes)

    def test_yahoo_index_quote_uses_previous_close(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 105.0,
                            "chartPreviousClose": 100.0,
                        },
                        "indicators": {"quote": [{"close": [99.0, 105.0]}]},
                    }
                ]
            }
        }

        with patch.object(
            morning_brief,
            "get_json",
            return_value=payload,
        ) as get_json:
            quote = morning_brief.get_yahoo_index_quote("^N225")

        self.assertEqual(quote, {"price": 105.0, "percent_change": 5.0})
        requested_url = get_json.call_args.args[0]
        self.assertIn("%5EN225", requested_url)
        self.assertIn("range=5d", requested_url)
        self.assertIn("interval=1d", requested_url)

    def test_yahoo_index_failure_does_not_abort_morning_brief(self):
        with patch.object(
            morning_brief,
            "get_json",
            side_effect=TimeoutError("synthetic timeout"),
        ), redirect_stdout(StringIO()) as output:
            quote = morning_brief.get_yahoo_index_quote("^N225")

        self.assertIsNone(quote)
        self.assertIn("Global index unavailable for ^N225", output.getvalue())

    def test_global_section_preserves_spacious_card_layout(self):
        global_markets = [
            {
                "symbol": "^N225",
                "name": "Nikkei 225",
                "quote": {"price": 42000.0, "percent_change": 0.8},
            },
            {
                "symbol": "^HSI",
                "name": "Hang Seng",
                "quote": None,
            },
        ]

        with patch.object(
            morning_brief,
            "datetime",
            FixedDateTime,
        ), patch.object(
            morning_brief.random,
            "choice",
            return_value=morning_brief.TRADING_QUOTES[0],
        ):
            message = morning_brief.build_market_message(
                [],
                [],
                global_markets,
                [],
            )

        self.assertIn("\n\n## 🌍 Global Markets\n\n", message)
        self.assertIn("▲ **Nikkei 225:** 42,000.00 (+0.80%)", message)
        self.assertIn("• **Hang Seng:** Unavailable", message)
        self.assertLess(
            message.index("## 📈 U.S. Market Snapshot"),
            message.index("## 🌍 Global Markets"),
        )
        self.assertLess(
            message.index("## 🌍 Global Markets"),
            message.index("## 💰 Key Markets"),
        )

    def test_later_delivery_failure_preserves_sequential_order(self):
        failure = RuntimeError("synthetic later delivery failure")
        send = Mock(side_effect=[None, failure])
        deliveries = [
            (
                self.MORNING_DESTINATION,
                morning_brief.MORNING_BRIEF_WEBHOOK_USERNAME,
                "MORNING-CONTENT",
            ),
            (
                self.EARNINGS_DESTINATION,
                morning_brief.EARNINGS_CALENDAR_WEBHOOK_USERNAME,
                "EARNINGS-CONTENT",
            ),
        ]

        with patch.object(
            morning_brief,
            "send_webhook_message",
            send,
        ), patch.object(morning_brief.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "later delivery failure"):
                morning_brief.post_messages(deliveries)

        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[0], self.MORNING_DESTINATION)
        self.assertEqual(send.call_args_list[1].args[0], self.EARNINGS_DESTINATION)
        sleep.assert_called_once_with(10)

    def test_first_delivery_failure_prevents_calendar_delivery(self):
        send = Mock(side_effect=RuntimeError("synthetic first delivery failure"))
        deliveries = [
            (
                self.MORNING_DESTINATION,
                morning_brief.MORNING_BRIEF_WEBHOOK_USERNAME,
                "MORNING-CONTENT",
            ),
            (
                self.EARNINGS_DESTINATION,
                morning_brief.EARNINGS_CALENDAR_WEBHOOK_USERNAME,
                "EARNINGS-CONTENT",
            ),
        ]

        with patch.object(
            morning_brief,
            "send_webhook_message",
            send,
        ), patch.object(morning_brief.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "first delivery failure"):
                morning_brief.post_messages(deliveries)

        send.assert_called_once_with(
            self.MORNING_DESTINATION,
            morning_brief.MORNING_BRIEF_WEBHOOK_USERNAME,
            "MORNING-CONTENT",
        )
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
