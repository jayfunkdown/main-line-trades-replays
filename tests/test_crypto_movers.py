import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from scripts import crypto_movers


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


SAMPLE_COINS = [
    {
        "id": "bitcoin",
        "symbol": "btc",
        "name": "Bitcoin",
        "current_price": 100000.0,
        "market_cap": 2_000_000_000_000,
        "market_cap_rank": 1,
        "total_volume": 50_000_000_000,
        "price_change_percentage_24h": 2.0,
    },
    {
        "id": "ethereum",
        "symbol": "eth",
        "name": "Ethereum",
        "current_price": 5000.0,
        "market_cap": 600_000_000_000,
        "market_cap_rank": 2,
        "total_volume": 20_000_000_000,
        "price_change_percentage_24h": -12.0,
    },
    {
        "id": "solana",
        "symbol": "sol",
        "name": "Solana",
        "current_price": 200.0,
        "market_cap": 90_000_000_000,
        "market_cap_rank": 5,
        "total_volume": 8_000_000_000,
        "price_change_percentage_24h": 18.0,
    },
    {
        "id": "smallcap",
        "symbol": "smol",
        "name": "Small Cap Coin",
        "current_price": 0.25,
        "market_cap": 500_000_000,
        "market_cap_rank": 88,
        "total_volume": 120_000_000,
        "price_change_percentage_24h": 3.0,
    },
]


class CryptoMoversTests(unittest.TestCase):
    def test_movement_score_matches_earnings_style_tiers(self):
        self.assertEqual(crypto_movers.movement_score(20), 20 * 5 + 35)
        self.assertEqual(crypto_movers.movement_score(-15), 15 * 5 + 25)
        self.assertEqual(crypto_movers.movement_score(7), 7 * 5 + 8)

    def test_priority_coin_still_requires_ten_percent_move(self):
        coin = dict(SAMPLE_COINS[0])
        coin["symbol"] = "avax"
        coin["price_change_percentage_24h"] = -3.9
        candidate = crypto_movers.calculate_candidate(coin)

        self.assertTrue(candidate["priority"])
        self.assertFalse(crypto_movers.qualifies_for_public(candidate))

        coin["price_change_percentage_24h"] = -10.0
        candidate = crypto_movers.calculate_candidate(coin)
        self.assertTrue(crypto_movers.qualifies_for_public(candidate))

    def test_env_cannot_lower_the_ten_percent_floor(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[3])

        with patch.dict(
            os.environ,
            {
                "CRYPTO_MOVERS_MIN_MOVE_PCT": "3",
                "CRYPTO_MOVERS_PRIORITY_MIN_MOVE_PCT": "1",
            },
            clear=False,
        ):
            self.assertFalse(crypto_movers.qualifies_for_public(candidate))

    def test_daily_max_cannot_exceed_ten(self):
        with patch.dict(
            os.environ,
            {"CRYPTO_MOVERS_DAILY_MAX": "50"},
            clear=False,
        ):
            self.assertEqual(crypto_movers.daily_post_max(), 10)

    def test_rank_candidates_prefers_higher_score_and_priority(self):
        candidates = [
            crypto_movers.calculate_candidate(coin)
            for coin in SAMPLE_COINS
        ]

        ranked = crypto_movers.rank_candidates(candidates)

        self.assertEqual(ranked[0]["symbol"], "SOL")
        self.assertEqual(ranked[1]["symbol"], "ETH")

    def test_crypto_chart_symbol_uses_yahoo_usd_ticker(self):
        self.assertEqual(crypto_movers.crypto_chart_symbol("btc"), "BTC-USD")
        self.assertEqual(crypto_movers.crypto_chart_symbol("LINK"), "LINK-USD")

    def test_send_discord_message_attaches_weekly_chart(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[2])
        message = crypto_movers.build_public_message(candidate)

        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / ".SOL_unique.tmp.png"

            def render_chart(symbol, *, output_path=None, **kwargs):
                if output_path is None:
                    raise AssertionError("Charts must use a unique output path")
                output_path.write_bytes(f"chart:{symbol}".encode("utf-8"))
                return output_path

            with patch.object(
                crypto_movers,
                "temporary_weekly_chart_path",
                return_value=chart_path,
            ), patch.object(
                crypto_movers,
                "generate_weekly_chart",
                side_effect=render_chart,
            ) as generate, patch.object(
                crypto_movers.urllib.request,
                "urlopen",
                return_value=FakeResponse(b'{"id":"crypto-message"}'),
            ) as urlopen:
                message_id = crypto_movers.send_discord_message(
                    "https://example.invalid/webhook",
                    message,
                    crypto_movers.WEBHOOK_USERNAME,
                    chart_symbol=candidate["symbol"],
                )

            request = urlopen.call_args.args[0]
            body = request.data
            content_type = request.headers["Content-type"]
            payload_marker = b"Content-Type: application/json\r\n\r\n"
            payload_bytes = body.split(payload_marker, 1)[1].split(b"\r\n--", 1)[0]
            payload = json.loads(payload_bytes.decode("utf-8"))

        self.assertEqual(message_id, "crypto-message")
        generate.assert_called_once_with(
            "SOL-USD",
            output_path=chart_path,
        )
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertEqual(
            payload["embeds"],
            [
                {
                    "description": message,
                    "color": 0xFF2BD6,
                    "image": {"url": "attachment://SOL_weekly.png"},
                }
            ],
        )
        self.assertEqual(
            payload["attachments"][0]["filename"],
            "SOL_weekly.png",
        )
        self.assertFalse(chart_path.exists())

    def test_build_public_message_matches_earnings_mover_structure(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[2])
        message = crypto_movers.build_public_message(candidate)

        self.assertTrue(message.startswith("# 🪙 Crypto Mover"))
        self.assertIn("## SOL", message)
        self.assertNotIn("**Solana**", message)
        self.assertIn("🟢 **24h move: +18.00%** at **$200.00**", message)
        self.assertIn("🏆 **Market cap rank:** #5", message)
        self.assertIn("💰 **Market cap:** $90.00B", message)
        self.assertIn("📊 **24h volume:** $8.00B", message)
        self.assertLess(
            message.index("📊 **24h volume:**"),
            message.index("*Market data — not a trade signal.*"),
        )
        self.assertIn("\n\n📊 **24h volume:**", message)
        self.assertIn("\n\n*Market data — not a trade signal.*", message)

    def test_build_webhook_payload_matches_earnings_embed_structure(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[2])
        payload = crypto_movers.build_webhook_payload(candidate)

        self.assertEqual(payload["username"], "Main Line Trades Crypto Movers")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)
        self.assertEqual(payload["embeds"][0]["color"], 0xFF2BD6)
        self.assertEqual(
            payload["embeds"][0]["description"],
            crypto_movers.build_public_message(candidate),
        )
        self.assertNotIn("fields", payload["embeds"][0])
        self.assertNotIn("title", payload["embeds"][0])

    def test_daily_cap_limits_posting_and_state_prevents_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "crypto_movers_state.json"
            posted: list[str] = []

            def capture_post(webhook_url, message, username, *, chart_symbol=None):
                posted.append((message, chart_symbol))
                return "message-id"

            with patch.object(
                crypto_movers,
                "STATE_PATH",
                state_path,
            ), patch.object(
                crypto_movers,
                "fetch_top_coins",
                return_value=SAMPLE_COINS,
            ), patch.object(
                crypto_movers,
                "send_discord_message",
                side_effect=capture_post,
            ), patch.dict(
                os.environ,
                {"CRYPTO_MOVERS_WEBHOOK": "https://example.invalid/webhook"},
                clear=True,
            ), patch.object(
                sys,
                "argv",
                ["crypto_movers.py", "--post"],
            ):
                crypto_movers.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            today = crypto_movers.eastern_today_label()

            self.assertEqual(len(posted), 2)
            self.assertEqual(len(state["posted"][today]), 2)
            self.assertIn("SOL", posted[0][0])
            self.assertEqual(posted[0][1], "SOL")
            self.assertIn("ETH", posted[1][0])
            self.assertEqual(posted[1][1], "ETH")

            with patch.object(
                crypto_movers,
                "STATE_PATH",
                state_path,
            ), patch.object(
                crypto_movers,
                "fetch_top_coins",
                return_value=SAMPLE_COINS,
            ), patch.object(
                crypto_movers,
                "send_discord_message",
                side_effect=capture_post,
            ), patch.dict(
                os.environ,
                {"CRYPTO_MOVERS_WEBHOOK": "https://example.invalid/webhook"},
                clear=True,
            ), patch.object(
                sys,
                "argv",
                ["crypto_movers.py", "--post"],
            ):
                crypto_movers.main()

            self.assertEqual(len(posted), 2)

    def test_preview_mode_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "crypto_movers_state.json"

            with patch.object(
                crypto_movers,
                "STATE_PATH",
                state_path,
            ), patch.object(
                crypto_movers,
                "fetch_top_coins",
                return_value=SAMPLE_COINS,
            ), patch.object(
                sys,
                "argv",
                ["crypto_movers.py", "--preview"],
            ):
                crypto_movers.main()

            self.assertFalse(state_path.exists())

    def test_systemd_unit_uses_preview_safe_post_command(self):
        systemd = (
            Path(__file__).resolve().parent.parent
            / "deploy"
            / "systemd"
        )
        service = (
            systemd / "mainline-crypto-movers.service"
        ).read_text(encoding="utf-8")
        timer = (
            systemd / "mainline-crypto-movers.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("scripts/crypto_movers.py --post", service)
        self.assertIn("Unit=mainline-crypto-movers.service", timer)


if __name__ == "__main__":
    unittest.main()
