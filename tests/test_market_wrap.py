import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch("dotenv.load_dotenv"), patch.dict(
    os.environ,
    {"FINNHUB_API_KEY": "synthetic-key"},
    clear=True,
):
    from scripts import market_wrap


def quote(price, change):
    return {
        "price": price,
        "percent_change": change,
    }


def item(symbol, name, price, change, *, is_crypto=False):
    return {
        "symbol": symbol,
        "name": name,
        "is_crypto": is_crypto,
        "quote": quote(price, change),
    }


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class JsonResponse(FakeResponse):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class WindowsConsole:
    encoding = "cp1252"

    def __init__(self):
        self.output = ""

    def reconfigure(self, *, encoding):
        self.encoding = encoding

    def write(self, value):
        value.encode(self.encoding)
        self.output += value
        return len(value)

    def flush(self):
        pass


class MarketWrapTests(unittest.TestCase):
    def setUp(self):
        self.market = [
            item("SPY", "S&P 500 ETF", 700.0, 1.0),
            item("QQQ", "Nasdaq-100 ETF", 600.0, 1.5),
            item("DIA", "Dow ETF", 500.0, 0.4),
            item("IWM", "Russell 2000 ETF", 250.0, -0.2),
        ]
        self.crypto = [
            item("BTC", "Bitcoin", 100000.0, 2.5, is_crypto=True),
            item("ETH", "Ethereum", 5000.0, -1.0, is_crypto=True),
        ]
        self.cross_market = [
            item("GLD", "Gold ETF", 300.0, 0.8),
            item("USO", "Oil ETF", 80.0, -0.5),
            item("UUP", "U.S. Dollar ETF", 30.0, 0.1),
        ]
        self.global_markets = [
            item("Nikkei 225", "Nikkei 225", 42000.0, 0.8),
            item("Hang Seng", "Hang Seng", 25000.0, -0.4),
            item("Shanghai Composite", "Shanghai Composite", 3600.0, 0.2),
            item("Nifty 50", "Nifty 50", 24500.0, 1.1),
            item("FTSE 100", "FTSE 100", 9100.0, -0.1),
            item("DAX", "DAX", 23500.0, 0.5),
            item("CAC 40", "CAC 40", 7800.0, -0.3),
        ]
        self.economic_events = [
            {
                "title": "CPI m/m",
                "time": datetime(2026, 8, 10, 8, 30),
                "actual": "0.2%",
                "forecast": "0.3%",
                "previous": "0.1%",
            }
        ]

    def payload(self):
        return market_wrap.build_market_wrap_payload(
            self.market,
            self.crypto,
            self.cross_market,
            self.economic_events,
            self.global_markets,
            now=datetime(2026, 8, 10, 16, 15),
        )

    def test_payload_is_one_polished_embed_with_all_sections(self):
        payload = self.payload()

        self.assertEqual(payload["username"], "Main Line Trades Market Wrap")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)

        embed = payload["embeds"][0]
        self.assertEqual(embed["color"], 0x00CFFF)
        description = embed["description"]
        self.assertNotIn("fields", embed)
        self.assertIn("Monday, August 10, 2026", description)
        headings = [
            "📈 U.S. Market Close",
            "🌍 Global Markets",
            "📅 High-Impact Economic Results",
            "₿ Crypto Snapshot",
            "🌐 Key Markets",
            "🧠 Session Read",
            "🔭 Next Session",
        ]
        self.assertEqual(
            [description.index(f"## {heading}") for heading in headings],
            sorted(description.index(f"## {heading}") for heading in headings),
        )
        self.assertTrue(all(f"\n\n## {heading}\n" in description for heading in headings))
        self.assertIn("**SPY — S&P 500 ETF:** $700.00 (+1.00%)", description)
        self.assertIn("▲ **Nikkei 225:** 42,000.00 (+0.80%)", description)
        self.assertIn("▼ **Hang Seng:** 25,000.00 (-0.40%)", description)
        self.assertIn("**8:30 AM — CPI m/m**", description)
        self.assertIn("Actual: **0.2%**", description)
        self.assertIn("Forecast: **0.3%**", description)
        self.assertIn("Previous: **0.1%**", description)
        self.assertIn("**BTC — Bitcoin:** $100,000 (+2.50%)", description)
        self.assertIn("**GLD — Gold ETF:** $300.00 (+0.80%)", description)

    def test_quiet_day_omits_economic_results_section(self):
        payload = market_wrap.build_market_wrap_payload(
            self.market,
            self.crypto,
            self.cross_market,
            [],
            self.global_markets,
            now=datetime(2026, 8, 10, 16, 15),
        )

        self.assertNotIn(
            "## 📅 High-Impact Economic Results",
            payload["embeds"][0]["description"],
        )

    def test_economic_results_are_capped_with_omission_count(self):
        events = [
            {
                "title": f"Long Economic Event {index} " + ("x" * 250),
                "time": datetime(2026, 8, 10, 8, 30),
                "actual": "1.0%",
                "forecast": "0.9%",
                "previous": "0.8%",
            }
            for index in range(10)
        ]

        result = market_wrap.economic_results_block(events)

        self.assertLessEqual(len(result), 1024)
        self.assertIn("additional high-impact event(s)", result)

    def test_session_read_is_deterministic(self):
        self.assertEqual(
            market_wrap.session_read(self.market),
            "Broadly positive U.S. close across the major index ETFs.",
        )

        negative = [
            item("A", "A", 1, -1),
            item("B", "B", 1, -1),
            item("C", "C", 1, -1),
            item("D", "D", 1, 1),
        ]
        self.assertEqual(
            market_wrap.session_read(negative),
            "Broadly negative U.S. close across the major index ETFs.",
        )

    def test_unavailable_quotes_are_rendered_without_failure(self):
        payload = market_wrap.build_market_wrap_payload(
            [{"symbol": "SPY", "name": "S&P 500 ETF", "quote": None}],
            [],
            [],
            [],
            now=datetime(2026, 8, 10, 16, 15),
        )

        description = payload["embeds"][0]["description"]
        self.assertIn("Unavailable", description)
        self.assertEqual(description.count("• Data unavailable"), 3)

    def test_embed_validation_rejects_oversized_description_before_delivery(self):
        payload = self.payload()
        payload["embeds"][0]["description"] = "x" * 4097

        with self.assertRaisesRegex(ValueError, "description"):
            market_wrap.send_market_wrap("destination", payload)

    def test_send_posts_exactly_one_safe_json_payload(self):
        payload = self.payload()
        response = FakeResponse()

        with patch.object(
            market_wrap.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            market_wrap.send_market_wrap(
                "https://discord.example/webhook",
                payload,
            )

        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://discord.example/webhook")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 30})

    def test_post_fails_closed_before_fetching_when_webhook_missing(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            market_wrap.morning_brief,
            "get_market_snapshot",
        ) as get_market:
            with self.assertRaisesRegex(RuntimeError, "MARKET_WRAP_WEBHOOK"):
                market_wrap.main(["--post"])

        get_market.assert_not_called()

    def test_preview_never_requires_webhook_or_posts(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            market_wrap.morning_brief,
            "get_market_snapshot",
            return_value=self.market,
        ), patch.object(
            market_wrap.morning_brief,
            "get_high_impact_usd_events",
            return_value=[],
        ), patch.object(
            market_wrap,
            "get_crypto_snapshot",
            return_value=self.crypto,
        ), patch.object(
            market_wrap,
            "get_cross_market_snapshot",
            return_value=self.cross_market,
        ), patch.object(
            market_wrap,
            "get_global_market_snapshot",
            return_value=self.global_markets,
        ), patch.object(
            market_wrap,
            "send_market_wrap",
        ) as send, redirect_stdout(StringIO()) as output:
            market_wrap.main(["--preview"])

        send.assert_not_called()
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["username"], "Main Line Trades Market Wrap")
        self.assertEqual(len(preview["embeds"]), 1)

    def test_preview_reconfigures_windows_console_for_emoji(self):
        console = WindowsConsole()

        with patch.object(market_wrap.sys, "stdout", console):
            market_wrap.print_preview(self.payload())

        self.assertEqual(console.encoding, "utf-8")
        self.assertIn("🌆 Main Line Trades Market Wrap", console.output)

    def test_post_fetches_once_and_delivers_once(self):
        with patch.dict(
            os.environ,
            {"MARKET_WRAP_WEBHOOK": "destination"},
            clear=True,
        ), patch.object(
            market_wrap.morning_brief,
            "get_market_snapshot",
            return_value=self.market,
        ) as get_market, patch.object(
            market_wrap.morning_brief,
            "get_high_impact_usd_events",
            return_value=self.economic_events,
        ) as get_events, patch.object(
            market_wrap,
            "get_crypto_snapshot",
            return_value=self.crypto,
        ) as get_crypto, patch.object(
            market_wrap,
            "get_cross_market_snapshot",
            return_value=self.cross_market,
        ) as get_cross, patch.object(
            market_wrap,
            "get_global_market_snapshot",
            return_value=self.global_markets,
        ) as get_global, patch.object(
            market_wrap,
            "send_market_wrap",
        ) as send, redirect_stdout(StringIO()):
            market_wrap.main(["--post"])

        get_market.assert_called_once_with()
        get_events.assert_called_once_with()
        get_crypto.assert_called_once_with()
        get_cross.assert_called_once_with()
        get_global.assert_called_once_with()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "destination")
        self.assertEqual(len(send.call_args.args[1]["embeds"]), 1)

    def test_named_quote_fetch_preserves_display_symbols(self):
        get_quote = Mock(side_effect=[quote(100000, 1), quote(5000, 2)])

        with patch.object(market_wrap.morning_brief, "get_quote", get_quote):
            results = market_wrap.get_crypto_snapshot()

        self.assertEqual(
            get_quote.call_args_list,
            [
                unittest.mock.call("BINANCE:BTCUSDT"),
                unittest.mock.call("BINANCE:ETHUSDT"),
            ],
        )
        self.assertEqual([result["symbol"] for result in results], ["BTC", "ETH"])
        self.assertTrue(all(result["is_crypto"] for result in results))

    def test_global_snapshot_fetches_each_named_index_once(self):
        with patch.object(
            market_wrap,
            "get_yahoo_index_quote",
            return_value=quote(100.0, 1.0),
        ) as get_quote:
            results = market_wrap.get_global_market_snapshot()

        self.assertEqual(
            [call.args[0] for call in get_quote.call_args_list],
            [symbol for symbol, _ in market_wrap.GLOBAL_MARKET_SYMBOLS],
        )
        self.assertEqual(
            [result["name"] for result in results],
            [name for _, name in market_wrap.GLOBAL_MARKET_SYMBOLS],
        )

    def test_yahoo_index_quote_calculates_change_from_previous_close(self):
        response = JsonResponse(
            {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 105.0,
                                "chartPreviousClose": 100.0,
                            },
                            "indicators": {
                                "quote": [{"close": [99.0, 100.0, 105.0]}]
                            },
                        }
                    ]
                }
            }
        )

        with patch.object(
            market_wrap.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            result = market_wrap.get_yahoo_index_quote("^N225")

        self.assertEqual(result, {"price": 105.0, "percent_change": 5.0})
        request = urlopen.call_args.args[0]
        self.assertIn("%5EN225", request.full_url)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 30})

    def test_yahoo_index_failure_returns_unavailable_without_aborting_wrap(self):
        with patch.object(
            market_wrap.urllib.request,
            "urlopen",
            side_effect=TimeoutError("synthetic timeout"),
        ):
            self.assertIsNone(market_wrap.get_yahoo_index_quote("^N225"))

    def test_modes_are_explicit_and_mutually_exclusive(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                market_wrap.parse_args([])

            with self.assertRaises(SystemExit):
                market_wrap.parse_args(["--preview", "--post"])


if __name__ == "__main__":
    unittest.main()
