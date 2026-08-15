import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

with patch("dotenv.load_dotenv"):
    from scripts import weekly_screener


FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "weekly_screener"
)
EASTERN = ZoneInfo("America/New_York")


class FakeResponse:
    def __init__(self, body=b"{}", status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def weekly_from_fixture(name: str) -> tuple[list[dict], str]:
    payload = load_fixture(name)
    weekly = []
    for candle in payload["weekly"]:
        item = dict(candle)
        item["date"] = datetime.fromisoformat(item["date"])
        weekly.append(item)
    return weekly, payload["expected"]


SAMPLE_DAILY = [
    {
        "timestamp": 1.0,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "volume": 400000,
    }
    for _ in range(25)
]
SAMPLE_DAILY[-1]["close"] = 12.5


SAMPLE_COINS = [
    {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
    },
    {
        "id": "tether",
        "symbol": "usdt",
        "name": "Tether",
    },
    {
        "id": "wrapped-bitcoin",
        "symbol": "wbtc",
        "name": "Wrapped Bitcoin",
    },
    {
        "id": "solana",
        "symbol": "sol",
        "name": "Solana",
    },
]


def sample_watch(symbol="AAPL", side="gain", level=12.0) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "market": "us",
        "chart_symbol": symbol,
        "side": side,
        "week_id": "2026-W33",
        "level": level,
        "close": 12.6,
        "level_date": "2026-08-07T20:00:00+00:00",
    }


def make_weekly(rows: list[tuple[str, float, float, float, float]]) -> list[dict]:
    weekly = []
    for date, open_, high, low, close in rows:
        weekly.append(
            {
                "date": datetime.fromisoformat(date),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000_000,
            }
        )
    return weekly


def lnsr_style_gain(*, close: float) -> list[dict]:
    """Containment: the week that made the ~5 low has body 6.42, then close through."""
    rows = [
        ("2026-04-06T20:00:00+00:00", 6.10, 6.30, 5.80, 6.00),
        ("2026-04-13T20:00:00+00:00", 6.00, 6.35, 5.85, 6.20),
        ("2026-04-20T20:00:00+00:00", 6.20, 6.40, 5.90, 6.10),
        ("2026-04-27T20:00:00+00:00", 6.10, 6.45, 5.95, 6.30),
        ("2026-05-04T20:00:00+00:00", 6.30, 6.50, 6.00, 6.20),
        ("2026-05-11T20:00:00+00:00", 6.42, 6.55, 5.00, 5.80),
        ("2026-05-18T20:00:00+00:00", 5.80, 5.90, 5.20, 5.40),
        ("2026-05-25T20:00:00+00:00", 5.40, 5.60, 5.10, 5.20),
        ("2026-06-01T20:00:00+00:00", 5.20, 5.80, 5.10, 5.60),
        ("2026-06-08T20:00:00+00:00", 5.60, 6.10, 5.50, 5.90),
        ("2026-06-15T20:00:00+00:00", 5.90, close + 0.2, 5.80, close),
    ]
    return make_weekly(rows)


class WeeklyScreenerClassifierTests(unittest.TestCase):
    def test_too_few_weeks_is_none(self):
        weekly = make_weekly(
            [
                ("2026-06-01T20:00:00+00:00", 5.0, 6.0, 4.5, 5.5),
                ("2026-06-08T20:00:00+00:00", 5.5, 6.5, 5.0, 6.2),
            ]
        )
        result = weekly_screener.classify_weekly_structure(weekly)
        self.assertEqual(result["side"], "none")
        self.assertEqual(result["reason"], "not_enough_weeks")

    def test_origin_body_gain_matches_lnsr_style(self):
        weekly = lnsr_style_gain(close=8.27)
        result = weekly_screener.classify_weekly_structure(weekly)
        self.assertEqual(result["side"], "gain")
        self.assertAlmostEqual(result["level"], 6.42, places=2)
        self.assertFalse(
            weekly_screener.is_retest(result["close"], result["level"], proximity=0.01)
        )

    def test_bounce_before_the_low_is_not_the_weekly(self):
        """IMXI: 14.54 bounce did not make the low; the 13.06 week did."""
        weekly = make_weekly(
            [
                ("2026-04-10T20:00:00+00:00", 15.90, 16.00, 15.70, 15.80),
                ("2026-04-17T20:00:00+00:00", 15.80, 15.95, 15.60, 15.70),
                ("2026-04-24T20:00:00+00:00", 15.70, 15.80, 15.40, 15.50),
                ("2026-05-01T20:00:00+00:00", 15.50, 15.60, 15.20, 15.30),
                ("2026-05-08T20:00:00+00:00", 15.30, 15.40, 14.80, 15.00),
                ("2026-05-15T20:00:00+00:00", 14.23, 14.75, 14.01, 14.54),
                ("2026-05-22T20:00:00+00:00", 14.51, 14.67, 14.03, 14.50),
                ("2026-05-29T20:00:00+00:00", 14.50, 14.50, 13.35, 13.42),
                ("2026-06-05T20:00:00+00:00", 13.43, 13.53, 12.90, 13.03),
                ("2026-06-12T20:00:00+00:00", 13.06, 13.50, 11.15, 11.86),
                ("2026-06-19T20:00:00+00:00", 12.20, 12.72, 12.06, 12.19),
                ("2026-06-26T20:00:00+00:00", 12.20, 14.61, 11.26, 14.59),
            ]
        )
        result = weekly_screener.classify_weekly_structure(weekly)
        self.assertEqual(result["side"], "gain")
        self.assertAlmostEqual(result["level"], 13.06, places=2)
        self.assertNotAlmostEqual(result["level"], 14.54, places=2)

    def test_scan_save_cannot_drop_posted_names(self):
        disk = weekly_screener.empty_state()
        disk["seeded"] = True
        disk["posted"] = {"NFLX:gain:77.65": {"symbol": "NFLX"}}
        disk["watchlist"] = {"OLD:gain:1": {"symbol": "OLD"}}
        memory = weekly_screener.empty_state()
        memory["watchlist"] = {"IMXI:gain:13.06": {"symbol": "IMXI"}}
        memory["scan"] = {"week_id": "2026-W33"}
        merged = weekly_screener.merge_persistent_fields(disk, memory)
        self.assertIn("NFLX:gain:77.65", merged["posted"])
        self.assertIn("IMXI:gain:13.06", merged["watchlist"])
        self.assertIn("OLD:gain:1", merged["watchlist"])
        self.assertTrue(merged["seeded"])

    def test_already_posted_name_ignores_level_changes(self):
        state = weekly_screener.empty_state()
        state["posted"] = {"VTRS:loss:16.27": {"symbol": "VTRS"}}
        self.assertTrue(weekly_screener.already_posted_name(state, "VTRS", "loss"))
        self.assertFalse(weekly_screener.already_posted_name(state, "VTRS", "gain"))

    def test_same_week_close_within_one_percent_is_immediate_signal(self):
        weekly = lnsr_style_gain(close=6.47)
        result = weekly_screener.classify_weekly_structure(weekly)
        self.assertEqual(result["side"], "gain")
        self.assertTrue(
            weekly_screener.is_retest(result["close"], result["level"], proximity=0.01)
        )

    def test_loss_uses_open_of_the_impulse_week(self):
        pivot = 10.0
        gain_weekly = lnsr_style_gain(close=8.27)
        loss_weekly = []
        for candle in gain_weekly:
            loss_weekly.append(
                {
                    **candle,
                    "open": pivot * 2 - candle["open"],
                    "close": pivot * 2 - candle["close"],
                    "high": pivot * 2 - candle["low"],
                    "low": pivot * 2 - candle["high"],
                }
            )
        gain = weekly_screener.classify_weekly_structure(gain_weekly)
        loss = weekly_screener.classify_weekly_structure(loss_weekly)
        self.assertEqual(gain["side"], "gain")
        self.assertEqual(loss["side"], "loss")
        self.assertAlmostEqual(gain["level"], 6.42, places=2)
        origin = int(loss["origin_index"])
        self.assertAlmostEqual(loss["level"], loss_weekly[origin]["open"], places=2)

    def test_sitting_at_the_high_is_not_the_weekly(self):
        """SAFT: spike from ~74 to 103, then tiny weeks at 103. Weekly is the spike open."""
        weekly = make_weekly(
            [
                ("2026-04-10T20:00:00+00:00", 72.0, 73.0, 71.0, 72.5),
                ("2026-04-17T20:00:00+00:00", 72.5, 74.0, 71.5, 73.0),
                ("2026-04-24T20:00:00+00:00", 73.0, 74.0, 72.0, 72.8),
                ("2026-05-01T20:00:00+00:00", 72.8, 73.5, 71.0, 71.5),
                ("2026-05-08T20:00:00+00:00", 71.5, 72.0, 70.0, 70.5),
                ("2026-05-15T20:00:00+00:00", 70.5, 72.0, 70.0, 71.0),
                ("2026-05-22T20:00:00+00:00", 71.0, 73.0, 70.5, 72.0),
                ("2026-05-29T20:00:00+00:00", 72.0, 75.0, 71.0, 74.5),
                ("2026-06-05T20:00:00+00:00", 73.77, 103.42, 70.81, 103.20),
                ("2026-06-12T20:00:00+00:00", 103.15, 103.57, 102.90, 103.39),
                ("2026-06-19T20:00:00+00:00", 103.37, 103.75, 103.15, 103.50),
                ("2026-06-26T20:00:00+00:00", 103.48, 103.75, 103.35, 103.35),
            ]
        )
        result = weekly_screener.classify_weekly_structure(weekly)
        self.assertNotAlmostEqual(float(result.get("level") or 0), 103.37, places=2)
        if result["side"] == "loss":
            self.assertAlmostEqual(result["level"], 73.77, places=2)
        elif result["side"] == "gain":
            self.assertLess(result["level"], 80.0)
            self.assertFalse(
                weekly_screener.is_retest(result["close"], result["level"], proximity=0.01)
            )
        else:
            self.assertEqual(result["side"], "none")

    def test_dump_week_close_is_not_the_weekly(self):
        """VTRS/DBD: a crash week that tags a new high is not drawn on its close."""
        weekly = make_weekly(
            [
                ("2026-04-10T20:00:00+00:00", 16.0, 16.2, 15.7, 16.1),
                ("2026-04-17T20:00:00+00:00", 16.1, 16.4, 15.8, 16.3),
                ("2026-04-24T20:00:00+00:00", 16.3, 16.5, 16.0, 16.4),
                ("2026-05-01T20:00:00+00:00", 16.4, 16.8, 16.2, 16.6),
                ("2026-05-08T20:00:00+00:00", 16.6, 17.0, 16.4, 16.9),
                ("2026-05-15T20:00:00+00:00", 16.9, 17.3, 16.7, 17.2),
                ("2026-05-22T20:00:00+00:00", 17.2, 17.5, 17.0, 17.4),
                ("2026-05-29T20:00:00+00:00", 17.35, 18.07, 17.28, 17.56),
                ("2026-06-05T20:00:00+00:00", 17.74, 18.39, 16.20, 16.43),
                ("2026-06-12T20:00:00+00:00", 16.30, 16.71, 15.88, 16.23),
            ]
        )
        result = weekly_screener.classify_weekly_structure(weekly)
        self.assertNotAlmostEqual(float(result.get("level") or 0), 16.43, places=2)
        if result["side"] == "loss":
            self.assertAlmostEqual(result["level"], 17.74, places=2)
            self.assertFalse(
                weekly_screener.is_retest(result["close"], result["level"], proximity=0.01)
            )

    def test_retest_fixtures(self):
        for name in ("retest_within_1_percent.json", "retest_too_far.json"):
            payload = load_fixture(name)
            result = weekly_screener.is_retest(
                payload["last_price"],
                payload["level"],
                proximity=payload["proximity"],
            )
            self.assertEqual(result, payload["expected"], name)

    def test_exact_1_percent_is_a_hit(self):
        self.assertTrue(weekly_screener.is_retest(10.1, 10.0, proximity=0.01))

    def test_first_test_of_the_level_is_the_only_signal(self):
        weekly = lnsr_style_gain(close=8.27)
        weekly.extend(
            make_weekly(
                [
                    ("2026-06-22T20:00:00+00:00", 7.20, 7.40, 6.38, 6.50),
                    ("2026-06-29T20:00:00+00:00", 6.50, 8.10, 6.40, 7.90),
                    ("2026-07-06T20:00:00+00:00", 7.90, 8.20, 6.35, 6.48),
                ]
            )
        )
        self.assertTrue(
            weekly_screener.first_visit_already_used(
                weekly,
                side="gain",
                level=6.42,
                proximity=0.01,
            )
        )

    def test_current_week_first_test_is_still_valid(self):
        weekly = lnsr_style_gain(close=8.27)
        weekly.extend(
            make_weekly(
                [
                    ("2026-06-22T20:00:00+00:00", 7.20, 7.40, 6.38, 6.50),
                ]
            )
        )
        self.assertFalse(
            weekly_screener.first_visit_already_used(
                weekly,
                side="gain",
                level=6.42,
                proximity=0.01,
            )
        )

    def test_us_scan_waits_for_friday_cash_close(self):
        friday_open = datetime(2026, 8, 14, 15, 59, tzinfo=EASTERN)
        friday_close = datetime(2026, 8, 14, 16, 0, tzinfo=EASTERN)
        saturday = datetime(2026, 8, 15, 12, 0, tzinfo=EASTERN)
        monday = datetime(2026, 8, 17, 10, 0, tzinfo=EASTERN)
        self.assertFalse(weekly_screener.us_scan_window(friday_open))
        self.assertTrue(weekly_screener.us_scan_window(friday_close))
        self.assertTrue(weekly_screener.us_scan_window(saturday))
        self.assertFalse(weekly_screener.us_scan_window(monday))

    def test_crypto_scan_waits_for_monday_utc(self):
        sunday = datetime(2026, 8, 16, 23, 59, tzinfo=weekly_screener.UTC)
        monday = datetime(2026, 8, 17, 0, 0, tzinfo=weekly_screener.UTC)
        saturday = datetime(2026, 8, 15, 12, 0, tzinfo=EASTERN)
        self.assertFalse(weekly_screener.crypto_scan_window(sunday))
        self.assertTrue(weekly_screener.crypto_scan_window(monday))
        self.assertFalse(weekly_screener.crypto_scan_window(saturday))

    def test_weekend_scan_batch_skips_crypto(self):
        saturday = datetime(2026, 8, 15, 12, 0, tzinfo=EASTERN)
        batch = weekly_screener.next_scan_batch(
            [
                {"symbol": "AAPL", "market": "us"},
                {"symbol": "BTC", "market": "crypto"},
            ],
            weekly_screener.empty_state(),
            batch_size=10,
            week_id="2026-W33",
            now=saturday,
        )
        self.assertEqual([item["symbol"] for item in batch], ["AAPL"])

    def test_monday_utc_scan_batch_skips_us(self):
        monday = datetime(2026, 8, 17, 12, 0, tzinfo=weekly_screener.UTC)
        batch = weekly_screener.next_scan_batch(
            [
                {"symbol": "AAPL", "market": "us"},
                {"symbol": "BTC", "market": "crypto"},
            ],
            weekly_screener.empty_state(),
            batch_size=10,
            week_id="2026-W34",
            now=monday,
        )
        self.assertEqual([item["symbol"] for item in batch], ["BTC"])

    def test_crypto_scan_drops_forming_monday_week(self):
        weekly = lnsr_style_gain(close=8.27)
        weekly.append(
            {
                "date": datetime(2026, 8, 17, 12, 0, tzinfo=weekly_screener.UTC),
                "open": 9.0,
                "high": 9.5,
                "low": 8.8,
                "close": 9.2,
            }
        )
        closed = weekly_screener.closed_weekly_candles(
            weekly,
            market="crypto",
            now=datetime(2026, 8, 17, 12, 0, tzinfo=weekly_screener.UTC),
        )
        self.assertEqual(closed[-1]["close"], 8.27)


class WeeklyScreenerUniverseTests(unittest.TestCase):
    def test_crypto_universe_skips_stables_and_wrapped(self):
        with patch.object(
            weekly_screener,
            "fetch_crypto_markets",
            return_value=SAMPLE_COINS,
        ):
            universe = weekly_screener.build_crypto_universe(100)

        symbols = [item["symbol"] for item in universe]
        self.assertEqual(symbols, ["BTC", "SOL"])
        self.assertEqual(universe[0]["chart_symbol"], "BTC-USD")
        self.assertEqual(universe[0]["market"], "crypto")

    def test_yahoo_us_symbol_maps_class_shares(self):
        self.assertEqual(weekly_screener.yahoo_us_symbol("BRK.B"), "BRK-B")
        self.assertEqual(weekly_screener.yahoo_us_symbol("AAPL"), "AAPL")

    def test_normalize_skips_warrants_and_units(self):
        self.assertIsNone(weekly_screener.normalize_us_symbol("ABC.W"))
        self.assertIsNone(weekly_screener.normalize_us_symbol("ABC.U"))
        self.assertEqual(weekly_screener.normalize_us_symbol("AAPL"), "AAPL")

    def test_parse_sp500_csv_symbols(self):
        csv_text = "Symbol,Name,Sector\nAAPL,Apple,Tech\nMSFT,Microsoft,Tech\n"
        symbols = weekly_screener.parse_csv_symbols(
            csv_text,
            ticker_headers=("symbol", "ticker"),
        )
        self.assertEqual(symbols, ["AAPL", "MSFT"])

    def test_parse_ishares_holdings_csv(self):
        csv_text = "\n".join(
            [
                "Fund Holdings",
                "Ticker,Name,Asset Class",
                "AAPL,Apple Inc,Equity",
                "BRK.B,Berkshire,Equity",
                "XYZ.WS,Warrant,Equity",
            ]
        )
        symbols = weekly_screener.parse_csv_symbols(
            csv_text,
            ticker_headers=("ticker", "symbol"),
        )
        self.assertEqual(symbols, ["AAPL", "BRK.B"])

    def test_parse_nasdaq100_wiki_symbols(self):
        html = """
        <table class="wikitable sortable">
          <tr><th>Company</th><th>Ticker</th></tr>
          <tr><td>Apple</td><td><a href="/wiki/AAPL">AAPL</a></td></tr>
          <tr><td>Microsoft</td><td><a href="/wiki/MSFT">MSFT</a></td></tr>
        </table>
        """
        self.assertEqual(
            weekly_screener.parse_nasdaq100_wiki_symbols(html),
            ["AAPL", "MSFT"],
        )

    def test_us_universe_prefers_finnhub_listings(self):
        with patch.object(
            weekly_screener,
            "fetch_finnhub_us_symbols",
            return_value=["AAPL", "MSFT", "NVDA"],
        ), patch.object(
            weekly_screener,
            "save_universe_cache",
        ):
            symbols = weekly_screener.build_us_universe(use_cache=False)
        self.assertEqual(symbols, ["AAPL", "MSFT", "NVDA"])

    def test_us_universe_falls_back_to_indexes(self):
        with patch.object(
            weekly_screener,
            "fetch_finnhub_us_symbols",
            side_effect=RuntimeError("403"),
        ), patch.object(
            weekly_screener,
            "fetch_sp500_symbols",
            return_value=["AAPL", "MSFT"],
        ), patch.object(
            weekly_screener,
            "fetch_nasdaq100_symbols",
            return_value=["AAPL", "NVDA"],
        ), patch.object(
            weekly_screener,
            "fetch_russell1000_symbols",
            return_value=["MSFT", "IWM"],
        ), patch.object(
            weekly_screener,
            "save_universe_cache",
        ):
            symbols = weekly_screener.build_us_universe(use_cache=False)

        self.assertEqual(symbols, ["AAPL", "MSFT", "NVDA", "IWM"])

    def test_us_universe_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "universe.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "saved_at": datetime.now(EASTERN).isoformat(),
                        "us_symbols": ["AAPL", "MSFT"],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                weekly_screener,
                "UNIVERSE_CACHE_PATH",
                cache_path,
            ), patch.object(
                weekly_screener,
                "fetch_finnhub_us_symbols",
                side_effect=RuntimeError("offline"),
            ):
                symbols = weekly_screener.build_us_universe()

        self.assertEqual(symbols, ["AAPL", "MSFT"])

    def test_us_liquidity_rejects_thin_names(self):
        thin = [
            {
                "timestamp": 1.0,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
                "volume": 1000,
            }
        ]
        self.assertFalse(
            weekly_screener.passes_us_liquidity(
                thin,
                min_price=2.0,
                min_avg_volume=200000,
            )
        )
        self.assertTrue(
            weekly_screener.passes_us_liquidity(
                SAMPLE_DAILY,
                min_price=2.0,
                min_avg_volume=200000,
            )
        )


class WeeklyScreenerCardTests(unittest.TestCase):
    def test_gain_retest_card_uses_pink_action_layout(self):
        hit = {
            "symbol": "AAPL",
            "market": "us",
            "side": "gain",
            "week_id": "2026-W33",
            "close": 12.6,
            "level": 12.0,
            "last_price": 12.05,
        }
        message = weekly_screener.build_screener_message(hit)
        self.assertTrue(message.startswith("# 📈 Trade Signal"))
        self.assertIn("## AAPL", message)
        self.assertIn("🟢 **Direction:** Long", message)
        self.assertIn("🎯 **Reference level:** $12", message)
        self.assertIn("💰 **Price:** $12.05", message)
        self.assertIn("## 🧠 Trade Thesis", message)
        self.assertIn("Weekly gained", message)
        self.assertIn("📊 **Trade Chart**", message)
        self.assertIn("*Chart and thesis provided by Main Line Trades.*", message)
        self.assertIn("⚠️ **Manage risk. This is not financial advice.**", message)
        self.assertNotIn("🧪", message)

    def test_loss_retest_card_names_the_watched_low(self):
        hit = {
            "symbol": "SOL",
            "market": "crypto",
            "side": "loss",
            "week_id": "2026-W33",
            "close": 16.4,
            "level": 18.0,
            "last_price": 17.9,
        }
        message = weekly_screener.build_screener_message(hit)
        self.assertTrue(message.startswith("# 📈 Trade Signal"))
        self.assertIn("🔴 **Direction:** Short", message)
        self.assertIn("🎯 **Reference level:** $18", message)
        self.assertIn("## 🧠 Trade Thesis", message)
        self.assertIn("Weekly lost", message)
        self.assertNotIn("Weekly gained", message)

    def test_watch_uses_public_beta_webhook(self):
        with patch.dict(
            os.environ,
            {"WEEKLY_SCREENER_WEBHOOK": "https://example.invalid/beta"},
            clear=True,
        ):
            webhook = weekly_screener.resolve_webhook()
        self.assertEqual(webhook, "https://example.invalid/beta")

    def test_send_discord_attaches_weekly_chart_with_level(self):
        message = weekly_screener.build_screener_message(
            {
                "symbol": "AAPL",
                "market": "us",
                "side": "gain",
                "week_id": "2026-W33",
                "close": 12.6,
                "level": 12.0,
                "last_price": 12.05,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / ".AAPL_unique.tmp.png"

            def render_chart(symbol, *, output_path=None, **kwargs):
                if output_path is None:
                    raise AssertionError("Charts must use a unique output path")
                output_path.write_bytes(b"chart")
                return output_path

            with patch.object(
                weekly_screener,
                "temporary_weekly_chart_path",
                return_value=chart_path,
            ), patch.object(
                weekly_screener,
                "generate_weekly_chart",
                side_effect=render_chart,
            ) as generate, patch.object(
                weekly_screener.urllib.request,
                "urlopen",
                return_value=FakeResponse(b'{"id":"weekly-message"}'),
            ):
                message_id = weekly_screener.send_discord_message(
                    "https://example.invalid/webhook",
                    message,
                    chart_symbol="AAPL",
                    level=12.0,
                    level_date="2026-08-07T20:00:00+00:00",
                )

        self.assertEqual(message_id, "weekly-message")
        generate.assert_called_once()
        kwargs = generate.call_args.kwargs
        self.assertTrue(kwargs["full_width_levels"])
        self.assertEqual(kwargs["weeks"], 80)
        self.assertEqual(
            kwargs["level_segments"],
            [
                {
                    "price": 12.0,
                    "start_date": "2026-08-07T20:00:00+00:00",
                }
            ],
        )
        self.assertFalse(chart_path.exists())


class WeeklyScreenerRunTests(unittest.TestCase):
    def test_preview_does_not_write_state_or_post(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            posted = []
            with patch.object(
                weekly_screener,
                "STATE_PATH",
                state_path,
            ), patch.object(
                weekly_screener,
                "run_scan_batch",
                return_value=([sample_watch()], 1, 0, 0),
            ), patch.object(
                weekly_screener,
                "evaluate_watchlist",
                return_value=([], 0),
            ), patch.object(
                weekly_screener,
                "send_discord_message",
                side_effect=lambda *args, **kwargs: posted.append(True),
            ), patch.object(
                sys,
                "argv",
                ["weekly_screener.py", "--preview"],
            ):
                weekly_screener.main()

            self.assertFalse(state_path.exists())
            self.assertEqual(posted, [])

    def test_scan_admits_watchlist_without_discord(self):
        weekly = lnsr_style_gain(close=8.27)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            posted = []
            with patch.object(
                weekly_screener,
                "STATE_PATH",
                state_path,
            ), patch.object(
                weekly_screener,
                "eastern_now",
                return_value=datetime(2026, 8, 14, 17, 0, tzinfo=EASTERN),
            ), patch.object(
                weekly_screener,
                "build_scan_instruments",
                return_value=[{"symbol": "AAPL", "market": "us", "chart_symbol": "AAPL"}],
            ), patch.object(
                weekly_screener,
                "fetch_weekly_from_yahoo",
                return_value=(weekly, SAMPLE_DAILY),
            ), patch.object(
                weekly_screener,
                "send_discord_message",
                side_effect=lambda *args, **kwargs: posted.append(True),
            ), patch.object(
                sys,
                "argv",
                ["weekly_screener.py", "--scan"],
            ):
                weekly_screener.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(posted, [])
            key = weekly_screener.watch_key("AAPL", "gain", 6.42)
            self.assertIn(key, state["watchlist"])
            self.assertEqual(state["watchlist"][key]["side"], "gain")

    def test_first_watch_seeds_without_discord(self):
        watch = sample_watch()
        key = weekly_screener.watch_key("AAPL", "gain", 12.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "seeded": False,
                        "watchlist": {key: watch},
                        "posted": {},
                        "daily": {},
                        "scan": {},
                    }
                ),
                encoding="utf-8",
            )
            posted = []
            with patch.object(
                weekly_screener,
                "STATE_PATH",
                state_path,
            ), patch.object(
                weekly_screener,
                "latest_chart_close",
                return_value=12.05,
            ), patch.object(
                weekly_screener,
                "fetch_weekly_from_yahoo",
                return_value=(lnsr_style_gain(close=8.27), SAMPLE_DAILY),
            ), patch.object(
                weekly_screener,
                "first_visit_already_used",
                return_value=False,
            ), patch.object(
                weekly_screener,
                "send_discord_message",
                side_effect=lambda *args, **kwargs: posted.append(True) or "id",
            ), patch.dict(
                os.environ,
                {"WEEKLY_SCREENER_WEBHOOK": "https://example.invalid/beta"},
                clear=True,
            ), patch.object(
                sys,
                "argv",
                ["weekly_screener.py", "--watch"],
            ):
                weekly_screener.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(state["seeded"])
            self.assertEqual(posted, [])
            self.assertIn(key, state["posted"])

    def test_seeded_watch_posts_retest_and_skips_far_names(self):
        near = sample_watch("AAPL", "gain", 12.0)
        far = sample_watch("MSFT", "loss", 20.0)
        near_key = weekly_screener.watch_key("AAPL", "gain", 12.0)
        far_key = weekly_screener.watch_key("MSFT", "loss", 20.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "seeded": True,
                        "watchlist": {near_key: near, far_key: far},
                        "posted": {},
                        "daily": {},
                        "scan": {},
                    }
                ),
                encoding="utf-8",
            )
            posted = []

            def last_price(symbol):
                return 12.05 if symbol == "AAPL" else 22.5

            def capture(webhook_url, message, **kwargs):
                posted.append((webhook_url, message, kwargs["chart_symbol"]))
                return "message-id"

            with patch.object(
                weekly_screener,
                "STATE_PATH",
                state_path,
            ), patch.object(
                weekly_screener,
                "latest_chart_close",
                side_effect=last_price,
            ), patch.object(
                weekly_screener,
                "fetch_weekly_from_yahoo",
                return_value=(lnsr_style_gain(close=8.27), SAMPLE_DAILY),
            ), patch.object(
                weekly_screener,
                "first_visit_already_used",
                return_value=False,
            ), patch.object(
                weekly_screener,
                "send_discord_message",
                side_effect=capture,
            ), patch.object(
                weekly_screener,
                "DISCORD_POST_DELAY_SECONDS",
                0,
            ), patch.dict(
                os.environ,
                {"WEEKLY_SCREENER_WEBHOOK": "https://example.invalid/beta"},
                clear=True,
            ), patch.object(
                sys,
                "argv",
                ["weekly_screener.py", "--watch"],
            ):
                weekly_screener.main()

            self.assertEqual(len(posted), 1)
            self.assertEqual(posted[0][0], "https://example.invalid/beta")
            self.assertIn("# 📈 Trade Signal", posted[0][1])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn(near_key, state["posted"])
            self.assertNotIn(far_key, state["posted"])

    def test_already_posted_level_does_not_repost(self):
        watch = sample_watch()
        key = weekly_screener.watch_key("AAPL", "gain", 12.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "seeded": True,
                        "watchlist": {key: watch},
                        "posted": {key: {"symbol": "AAPL"}},
                        "daily": {},
                        "scan": {},
                    }
                ),
                encoding="utf-8",
            )
            posted = []
            with patch.object(
                weekly_screener,
                "STATE_PATH",
                state_path,
            ), patch.object(
                weekly_screener,
                "latest_chart_close",
                return_value=12.05,
            ), patch.object(
                weekly_screener,
                "send_discord_message",
                side_effect=lambda *args, **kwargs: posted.append(True) or "id",
            ), patch.dict(
                os.environ,
                {"WEEKLY_SCREENER_WEBHOOK": "https://example.invalid/beta"},
                clear=True,
            ), patch.object(
                sys,
                "argv",
                ["weekly_screener.py", "--watch"],
            ):
                weekly_screener.main()
            self.assertEqual(posted, [])

    def test_stale_watches_expire_after_eight_weeks(self):
        state = {
            "watchlist": {
                "OLD:gain:10": {
                    "symbol": "OLD",
                    "side": "gain",
                    "week_id": "2026-W20",
                    "level": 10,
                },
                "NEW:gain:10": {
                    "symbol": "NEW",
                    "side": "gain",
                    "week_id": "2026-W32",
                    "level": 10,
                },
            }
        }
        expired = weekly_screener.expire_stale_watches(
            state,
            now_week_id="2026-W33",
            expire_weeks=8,
        )
        self.assertEqual(expired, 1)
        self.assertNotIn("OLD:gain:10", state["watchlist"])
        self.assertIn("NEW:gain:10", state["watchlist"])

    def test_scan_instrument_uses_yahoo_weekly_and_spec(self):
        weekly = lnsr_style_gain(close=8.27)
        with patch.object(
            weekly_screener,
            "fetch_weekly_from_yahoo",
            return_value=(weekly, SAMPLE_DAILY),
        ):
            hit = weekly_screener.scan_instrument(
                {
                    "symbol": "AAPL",
                    "market": "us",
                    "chart_symbol": "AAPL",
                },
                min_price=2.0,
                min_avg_volume=200000,
            )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["side"], "gain")
        self.assertAlmostEqual(hit["level"], 6.42, places=2)
        self.assertEqual(hit["symbol"], "AAPL")

    def test_systemd_units_split_scan_and_watch_and_stay_oneshot(self):
        systemd = Path(__file__).resolve().parent.parent / "deploy" / "systemd"
        scan_service = (systemd / "mainline-weekly-screener-scan.service").read_text(
            encoding="utf-8"
        )
        scan_timer = (systemd / "mainline-weekly-screener-scan.timer").read_text(
            encoding="utf-8"
        )
        watch_service = (systemd / "mainline-weekly-screener-watch.service").read_text(
            encoding="utf-8"
        )
        watch_timer = (systemd / "mainline-weekly-screener-watch.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("scripts/weekly_screener.py --scan", scan_service)
        self.assertIn("Type=oneshot", scan_service)
        self.assertIn("Fri *-*-* 16:30:00 America/New_York", scan_timer)
        self.assertIn("Mon *-*-* 00..12:00:00 UTC", scan_timer)
        self.assertIn("scripts/weekly_screener.py --watch", watch_service)
        self.assertIn("00,15,30,45:00 America/New_York", watch_timer)


if __name__ == "__main__":
    unittest.main()
