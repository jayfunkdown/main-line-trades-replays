import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from tests import test_earnings_characterization_batch2 as batch2

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions, earnings_state


FIXED_NOW = datetime(
    2026,
    8,
    6,
    12,
    0,
    tzinfo=earnings_reactions.EASTERN,
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FIXED_NOW.replace(tzinfo=None)
        return FIXED_NOW.astimezone(tz)


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


def make_http_error(code, body=b""):
    return urllib.error.HTTPError(
        "https://example.invalid/api",
        code,
        "synthetic failure",
        {},
        io.BytesIO(body),
    )


def empty_state():
    return {
        "public": {},
        "private": {},
        "quotes": {},
        "signal_queue": {},
    }


def weekly_source_candles():
    candles = []
    for day, price in ((2, 10.0), (9, 11.0), (16, 12.0), (23, 13.0)):
        candles.append(
            {
                "timestamp": datetime(
                    2026,
                    1,
                    day,
                    tzinfo=timezone.utc,
                ).timestamp(),
                "open": price,
                "high": price + 1.0,
                "low": price - 1.0,
                "close": price + 0.5,
                "volume": 1000.0,
            }
        )
    return candles


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)


class YahooAndWebhookTests(NoNetworkTestCase):
    def test_yahoo_transport_and_json_failures_are_wrapped(self):
        failures = (
            make_http_error(500, b"server failed"),
            urllib.error.URLError("synthetic DNS failure"),
            FakeResponse(b"{not-json"),
        )

        for failure in failures:
            kwargs = (
                {"return_value": failure}
                if isinstance(failure, FakeResponse)
                else {"side_effect": failure}
            )
            with self.subTest(failure=type(failure).__name__), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                **kwargs,
            ) as urlopen:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Could not load chart data for ACME",
                ):
                    earnings_reactions.fetch_daily_candles("ACME")

                urlopen.assert_called_once()

    def test_yahoo_malformed_structure_and_empty_candles_fail_differently(self):
        malformed = FakeResponse(b'{"chart":{"result":[]}}')
        empty = FakeResponse(
            json.dumps(
                {
                    "chart": {
                        "result": [
                            {
                                "timestamp": [1],
                                "indicators": {
                                    "quote": [
                                        {
                                            "open": [None],
                                            "high": [2],
                                            "low": [1],
                                            "close": [2],
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            ).encode("utf-8")
        )

        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=malformed,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "incomplete chart data",
            ):
                earnings_reactions.fetch_daily_candles("ACME")

        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=empty,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "No usable chart candles",
            ):
                earnings_reactions.fetch_daily_candles("ACME")

    def test_webhook_resolution_wraps_transport_and_json_failures(self):
        failures = (
            make_http_error(403, b"forbidden"),
            urllib.error.URLError("synthetic DNS failure"),
            FakeResponse(b"{not-json"),
        )

        for failure in failures:
            kwargs = (
                {"return_value": failure}
                if isinstance(failure, FakeResponse)
                else {"side_effect": failure}
            )
            with self.subTest(failure=type(failure).__name__), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                **kwargs,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Could not resolve the private earnings-review channel",
                ):
                    earnings_reactions.resolve_webhook_channel_id(
                        "https://example.invalid/webhook"
                    )

    def test_webhook_resolution_rejects_missing_id_but_list_raises_attribute_error(self):
        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=FakeResponse(b"{}"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "did not return a channel_id",
            ):
                earnings_reactions.resolve_webhook_channel_id(
                    "https://example.invalid/webhook"
                )

        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=FakeResponse(b"[]"),
        ):
            with self.assertRaises(AttributeError):
                earnings_reactions.resolve_webhook_channel_id(
                    "https://example.invalid/webhook"
                )


class ChartAndMultipartTests(NoNetworkTestCase):
    def test_generated_chart_remains_at_persistent_output_path(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            earnings_reactions,
            "PROJECT_ROOT",
            Path(temp_dir),
        ), patch.object(
            earnings_reactions,
            "fetch_daily_candles",
            return_value=weekly_source_candles(),
        ):
            output_path = earnings_reactions.generate_weekly_chart("acme")

            self.assertEqual(
                output_path,
                Path(temp_dir) / "data" / "earnings_charts" / "ACME_weekly.png",
            )
            self.assertTrue(output_path.exists())
            self.assertTrue(output_path.read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(list(output_path.parent.iterdir()), [output_path])

    def test_chart_save_failure_propagates_and_does_not_close_figure(self):
        import matplotlib.figure
        import matplotlib.pyplot as plt

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            earnings_reactions,
            "PROJECT_ROOT",
            Path(temp_dir),
        ), patch.object(
            earnings_reactions,
            "fetch_daily_candles",
            return_value=weekly_source_candles(),
        ), patch.object(
            matplotlib.figure.Figure,
            "savefig",
            side_effect=OSError("synthetic chart write failure"),
        ), patch.object(
            plt,
            "close",
        ) as close:
            with self.assertRaisesRegex(OSError, "synthetic chart write failure"):
                earnings_reactions.generate_weekly_chart("ACME")

            close.assert_not_called()
            output_path = (
                Path(temp_dir) / "data" / "earnings_charts" / "ACME_weekly.png"
            )
            self.assertTrue(output_path.parent.exists())
            self.assertFalse(output_path.exists())

        plt.close("all")

    def test_multipart_body_matches_exact_discord_contract(self):
        payload = {
            "content": "Hello",
            "allowed_mentions": {"parse": []},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"PNG-DATA")

            with patch.object(
                earnings_reactions.time,
                "time_ns",
                return_value=123456789,
            ):
                body, boundary = earnings_reactions.multipart_body(
                    payload=payload,
                    file_path=chart_path,
                )

        expected_boundary = (
            "----MainLineTrades"
            + hashlib.sha256(b"123456789").hexdigest()[:24]
        )
        expected = (
            f"--{expected_boundary}\r\n"
            'Content-Disposition: form-data; name="payload_json"\r\n'
            "Content-Type: application/json\r\n"
            "\r\n"
            f"{json.dumps(payload, ensure_ascii=False)}\r\n"
            f"--{expected_boundary}\r\n"
            'Content-Disposition: form-data; name="files[0]"; '
            'filename="chart.png"\r\n'
            "Content-Type: image/png\r\n"
            "\r\n"
        ).encode("utf-8") + b"PNG-DATA\r\n" + (
            f"--{expected_boundary}--\r\n"
        ).encode("utf-8")

        self.assertEqual(boundary, expected_boundary)
        self.assertEqual(body, expected)


class ConfigurationAndMessageBoundaryTests(NoNetworkTestCase):
    def test_each_execution_mode_reports_its_first_missing_required_variable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            cache_path = Path(temp_dir) / "calendar.json"
            state_path.write_text(json.dumps(empty_state()), encoding="utf-8")

            common = (
                patch.dict(os.environ, {}, clear=True),
                patch.object(earnings_reactions, "STATE_FILE", state_path),
                patch.object(
                    earnings_reactions,
                    "CALENDAR_CACHE_FILE",
                    cache_path,
                ),
                patch.object(earnings_reactions, "datetime", FixedDateTime),
            )

            with self.subTest(mode="preview"):
                with common[0], common[1], common[2], common[3], patch.object(
                    sys,
                    "argv",
                    ["earnings_reactions.py", "--preview", "--date", "2026-08-06"],
                ), redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "FINNHUB_API_KEY",
                    ):
                        earnings_reactions.main()

            with self.subTest(mode="post"):
                with patch.dict(os.environ, {}, clear=True), patch.object(
                    earnings_reactions,
                    "STATE_FILE",
                    state_path,
                ), patch.object(
                    sys,
                    "argv",
                    ["earnings_reactions.py", "--post", "--date", "2026-08-06"],
                ), patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ), patch.object(
                    earnings_reactions,
                    "get_completed_reports",
                    return_value=[],
                ), patch.object(
                    earnings_reactions,
                    "build_candidates_optimized",
                    return_value=([], 0, 0),
                ), redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "EARNINGS_REACTIONS_WEBHOOK",
                    ):
                        earnings_reactions.main()

            with self.subTest(mode="review-bot"):
                with patch.dict(os.environ, {}, clear=True), patch.object(
                    earnings_reactions,
                    "STATE_FILE",
                    state_path,
                ), patch.object(
                    sys,
                    "argv",
                    ["earnings_reactions.py", "--review-bot"],
                ), redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "DISCORD_BOT_TOKEN",
                    ):
                        earnings_reactions.main()

            with self.subTest(mode="private-test"):
                with patch.dict(os.environ, {}, clear=True), patch.object(
                    earnings_reactions,
                    "STATE_FILE",
                    state_path,
                ), patch.object(
                    sys,
                    "argv",
                    ["earnings_reactions.py", "--private-test"],
                ), patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ), redirect_stdout(StringIO()):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "DISCORD_BOT_TOKEN",
                    ):
                        earnings_reactions.main()

    def test_discord_webhook_has_no_local_2000_character_boundary(self):
        for length in (2000, 2001):
            with self.subTest(length=length), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                return_value=FakeResponse(status=204),
            ) as urlopen:
                earnings_reactions.send_discord_message(
                    "https://example.invalid/webhook",
                    "x" * length,
                    "bot",
                )

                request = urlopen.call_args.args[0]
                payload = json.loads(request.data.decode("utf-8"))
                self.assertNotIn("content", payload)
                self.assertEqual(
                    len(payload["embeds"][0]["description"]),
                    length,
                )
                self.assertEqual(payload["embeds"][0]["color"], 0xFF2BD6)


class ReviewModalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    async def build_modal(self, attachment):
        signals_channel = SimpleNamespace(send=AsyncMock())
        review_channel = SimpleNamespace(fetch_message=AsyncMock())
        client = batch2.FakeDiscordClient(signals_channel, review_channel)
        reviewer = SimpleNamespace(
            id=1,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_messages=False,
            ),
        )
        guild = SimpleNamespace(owner_id=1)
        review_channel.fetch_message.return_value = SimpleNamespace(
            id=321,
            author=client.user,
            channel=SimpleNamespace(id=200),
            type=discord.MessageType.default,
            edit=AsyncMock(),
        )
        button_state = empty_state()
        button_state["signal_queue"]["token"] = {
            "review_message_id": "321",
            "review_channel_id": "200",
            "sent_to_signals": False,
            "candidate": {
                "symbol": "ACME",
                "eps_direction": "beat",
                "revenue_direction": "beat",
            },
        }

        def required_environment(name):
            return {
                "DISCORD_BOT_TOKEN": "synthetic-token",
                "SIGNALS_CHANNEL_ID": "100",
                "EARNINGS_REVIEW_WEBHOOK": (
                    "https://example.invalid/review-webhook"
                ),
            }[name]

        with patch.dict(os.environ, {}, clear=True), patch.object(
            earnings_reactions,
            "required_env",
            side_effect=required_environment,
        ), patch.object(
            earnings_reactions,
            "resolve_webhook_channel_id",
            return_value="200",
        ), patch.object(
            earnings_reactions,
            "load_state",
            return_value=button_state,
        ), patch.object(
            discord,
            "Client",
            return_value=client,
        ), patch.object(
            discord.app_commands,
            "CommandTree",
            batch2.FakeCommandTree,
        ), redirect_stdout(StringIO()):
            await earnings_reactions.run_review_button_bot()

        captured = SimpleNamespace(modal=None)

        async def send_modal(modal):
            captured.modal = modal

        button_interaction = SimpleNamespace(
            message=SimpleNamespace(
                id=321,
                content="**ACME**",
                author=client.user,
                channel=SimpleNamespace(id=200),
                type=discord.MessageType.default,
            ),
            channel_id=200,
            guild=guild,
            user=reviewer,
            response=SimpleNamespace(
                send_modal=send_modal,
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=button_state,
        ), redirect_stdout(StringIO()):
            await client.persistent_view.children[0].callback(button_interaction)

        captured.modal.trade_thesis._value = "Trade above resistance."
        captured.modal.trade_chart._values = [attachment]
        return captured.modal, signals_channel, reviewer, guild

    @staticmethod
    def interaction(reviewer, guild):
        return SimpleNamespace(
            channel_id=200,
            guild=guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=reviewer,
            delete_original_response=AsyncMock(),
        )

    async def test_modal_missing_state_stops_before_attachment_or_signal(self):
        attachment = SimpleNamespace(
            filename="chart.png",
            content_type="image/png",
            to_file=AsyncMock(),
        )
        modal, signals_channel, reviewer, guild = await self.build_modal(
            attachment
        )
        interaction = self.interaction(reviewer, guild)

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=empty_state(),
        ), patch.object(
            earnings_reactions,
            "update_state",
        ) as update_state:
            await modal.on_submit(interaction)

        interaction.response.defer.assert_not_awaited()
        self.assertIn(
            "no longer available",
            interaction.followup.send.await_args.args[0],
        )
        attachment.to_file.assert_not_awaited()
        signals_channel.send.assert_not_awaited()
        update_state.assert_not_called()

    async def test_modal_invalid_state_fails_before_discord_defer(self):
        attachment = SimpleNamespace(
            filename="chart.png",
            content_type="image/png",
            to_file=AsyncMock(),
        )
        modal, signals_channel, reviewer, guild = await self.build_modal(
            attachment
        )
        interaction = self.interaction(reviewer, guild)

        with patch.object(
            earnings_reactions,
            "load_state",
            side_effect=earnings_state.EarningsStateValidationError(
                "synthetic corrupt state"
            ),
        ):
            await modal.on_submit(interaction)

        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        attachment.to_file.assert_not_awaited()
        signals_channel.send.assert_not_awaited()

    async def test_modal_invalid_candidate_stops_before_attachment_or_signal(self):
        attachment = SimpleNamespace(
            filename="chart.png",
            content_type="image/png",
            to_file=AsyncMock(),
        )
        modal, signals_channel, reviewer, guild = await self.build_modal(
            attachment
        )
        interaction = self.interaction(reviewer, guild)
        state = empty_state()
        state["signal_queue"]["token"] = {
            "review_message_id": "321",
            "review_channel_id": "200",
            "sent_to_signals": False,
            "candidate": ["not-a-dictionary"],
        }

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=state,
        ), patch.object(
            earnings_reactions,
            "update_state",
        ) as update_state:
            await modal.on_submit(interaction)

        self.assertIn(
            "no longer available",
            interaction.followup.send.await_args.args[0],
        )
        attachment.to_file.assert_not_awaited()
        signals_channel.send.assert_not_awaited()
        update_state.assert_not_called()

    async def test_modal_rejects_unsupported_attachment_before_conversion(self):
        attachment = SimpleNamespace(
            filename="chart.txt",
            content_type="text/plain",
            to_file=AsyncMock(),
        )
        modal, signals_channel, reviewer, guild = await self.build_modal(
            attachment
        )
        interaction = self.interaction(reviewer, guild)
        state = empty_state()
        state["signal_queue"]["token"] = {
            "review_message_id": "321",
            "review_channel_id": "200",
            "sent_to_signals": False,
            "candidate": {
                "symbol": "ACME",
                "eps_direction": "beat",
                "revenue_direction": "beat",
            },
        }

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=state,
        ), patch.object(
            earnings_reactions,
            "update_state",
        ) as update_state:
            await modal.on_submit(interaction)

        self.assertIn(
            "chart must be a PNG",
            interaction.followup.send.await_args.args[0],
        )
        attachment.to_file.assert_not_awaited()
        signals_channel.send.assert_not_awaited()
        update_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
