import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from scripts import crypto_movers


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

    def test_priority_coin_qualifies_at_lower_move_threshold(self):
        coin = dict(SAMPLE_COINS[0])
        coin["price_change_percentage_24h"] = 4.0
        candidate = crypto_movers.calculate_candidate(coin)

        with patch.dict(
            os.environ,
            {
                "CRYPTO_MOVERS_MIN_MOVE_PCT": "5",
                "CRYPTO_MOVERS_PRIORITY_MIN_MOVE_PCT": "3",
            },
            clear=False,
        ):
            self.assertTrue(crypto_movers.qualifies_for_public(candidate))

    def test_non_priority_coin_requires_default_move_threshold(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[3])

        with patch.dict(
            os.environ,
            {
                "CRYPTO_MOVERS_MIN_MOVE_PCT": "5",
                "CRYPTO_MOVERS_PRIORITY_MIN_MOVE_PCT": "3",
            },
            clear=False,
        ):
            self.assertFalse(crypto_movers.qualifies_for_public(candidate))

    def test_rank_candidates_prefers_higher_score_and_priority(self):
        candidates = [
            crypto_movers.calculate_candidate(coin)
            for coin in SAMPLE_COINS
        ]

        ranked = crypto_movers.rank_candidates(candidates)

        self.assertEqual(ranked[0]["symbol"], "SOL")
        self.assertEqual(ranked[1]["symbol"], "ETH")

    def test_build_public_message_matches_earnings_mover_structure(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[2])
        message = crypto_movers.build_public_message(candidate)

        self.assertTrue(message.startswith("# 🪙 Crypto Mover"))
        self.assertIn("## SOL", message)
        self.assertIn("**24h move:", message)
        self.assertIn("*Market data — not a trade signal.*", message)

    def test_build_webhook_payload_uses_neon_pink_border(self):
        candidate = crypto_movers.calculate_candidate(SAMPLE_COINS[2])
        payload = crypto_movers.build_webhook_payload(candidate)

        self.assertEqual(payload["username"], "Main Line Trades Crypto Movers")
        self.assertEqual(payload["embeds"][0]["color"], 0xFF2BD6)

    def test_daily_cap_limits_posting_and_state_prevents_duplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "crypto_movers_state.json"
            posted: list[str] = []

            def capture_post(webhook_url, payload):
                posted.append(payload["embeds"][0]["description"])
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
            self.assertIn("SOL", posted[0])
            self.assertIn("ETH", posted[1])

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
