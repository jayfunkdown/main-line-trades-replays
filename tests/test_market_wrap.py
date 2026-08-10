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
            now=datetime(2026, 8, 10, 16, 15),
        )

    def test_payload_is_one_polished_embed_with_all_sections(self):
        payload = self.payload()

        self.assertEqual(payload["username"], "Main Line Trades Market Wrap")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)

        embed = payload["embeds"][0]
        self.assertEqual(embed["color"], 0x00CFFF)
        self.assertIn("Monday, August 10, 2026", embed["description"])
        self.assertEqual(
            [field["name"] for field in embed["fields"]],
            [
                "📈 U.S. Market Close",
                "📅 High-Impact Economic Results",
                "₿ Crypto Snapshot",
                "🌐 Key Markets",
                "🧠 Session Read",
                "🔭 Next Session",
            ],
        )
        self.assertIn("**SPY — S&P 500 ETF:** $700.00 (+1.00%)", embed["fields"][0]["value"])
        self.assertIn("**8:30 AM — CPI m/m**", embed["fields"][1]["value"])
        self.assertIn("Actual: **0.2%**", embed["fields"][1]["value"])
        self.assertIn("Forecast: **0.3%**", embed["fields"][1]["value"])
        self.assertIn("Previous: **0.1%**", embed["fields"][1]["value"])
        self.assertIn("**BTC — Bitcoin:** $100,000 (+2.50%)", embed["fields"][2]["value"])
        self.assertIn("**GLD — Gold ETF:** $300.00 (+0.80%)", embed["fields"][3]["value"])

    def test_quiet_day_omits_economic_results_section(self):
        payload = market_wrap.build_market_wrap_payload(
            self.market,
            self.crypto,
            self.cross_market,
            [],
            now=datetime(2026, 8, 10, 16, 15),
        )

        field_names = [
            field["name"]
            for field in payload["embeds"][0]["fields"]
        ]
        self.assertNotIn("📅 High-Impact Economic Results", field_names)

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
            now=datetime(2026, 8, 10, 16, 15),
        )

        fields = payload["embeds"][0]["fields"]
        self.assertIn("Unavailable", fields[0]["value"])
        self.assertEqual(fields[1]["value"], "• Data unavailable")
        self.assertEqual(fields[2]["value"], "• Data unavailable")

    def test_embed_validation_rejects_oversized_field_before_delivery(self):
        payload = self.payload()
        payload["embeds"][0]["fields"][0]["value"] = "x" * 1025

        with self.assertRaisesRegex(ValueError, "field value"):
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
            "send_market_wrap",
        ) as send, redirect_stdout(StringIO()):
            market_wrap.main(["--post"])

        get_market.assert_called_once_with()
        get_events.assert_called_once_with()
        get_crypto.assert_called_once_with()
        get_cross.assert_called_once_with()
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

    def test_modes_are_explicit_and_mutually_exclusive(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                market_wrap.parse_args([])

            with self.assertRaises(SystemExit):
                market_wrap.parse_args(["--preview", "--post"])


if __name__ == "__main__":
    unittest.main()
