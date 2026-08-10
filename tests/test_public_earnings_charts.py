import asyncio
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


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


def http_error(code, body=b""):
    return urllib.error.HTTPError(
        "https://example.invalid/webhook",
        code,
        "synthetic failure",
        {},
        io.BytesIO(body),
    )


class PublicEarningsChartTests(unittest.TestCase):
    TARGET_DATE = "2026-08-06"

    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    @staticmethod
    def candidate(symbol="ACME", *, score=100.0, move=10.0, public=True):
        return {
            "report": {
                "date": PublicEarningsChartTests.TARGET_DATE,
                "symbol": symbol,
                "year": 2026,
                "quarter": 2,
                "hour": "amc",
                "epsActual": 1.5,
                "epsEstimate": 1.0,
                "revenueActual": 120_000_000,
                "revenueEstimate": 100_000_000,
            },
            "quote": {"c": 25.0, "dp": move},
            "symbol": symbol,
            "move_percent": move,
            "current_price": 25.0,
            "eps_surprise": 50.0,
            "revenue_surprise": 20.0,
            "eps_direction": "beat",
            "revenue_direction": "beat",
            "priority": False,
            "score": score,
            "public_ok": public,
        }

    @staticmethod
    def empty_state():
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
            "manual_signal_drafts": {},
        }

    def write_state(self, path):
        path.write_text(json.dumps(self.empty_state()), encoding="utf-8")

    def main_managers(
        self,
        state_path,
        candidates,
        *,
        preview=False,
        private=False,
    ):
        arguments = [
            "earnings_reactions.py",
            "--preview" if preview else "--post",
            "--date",
            self.TARGET_DATE,
        ]
        return (
            patch.dict(
                os.environ,
                {
                    "EARNINGS_REACTIONS_WEBHOOK": (
                        "https://example.invalid/public-webhook"
                    ),
                    "EARNINGS_REVIEW_WEBHOOK": "review-webhook" if private else "",
                },
                clear=True,
            ),
            patch.object(sys, "argv", arguments),
            patch.object(earnings_reactions, "STATE_FILE", state_path),
            patch.object(
                earnings_reactions,
                "get_completed_reports",
                return_value=[item["report"] for item in candidates],
            ),
            patch.object(
                earnings_reactions,
                "build_candidates_optimized",
                return_value=(copy.deepcopy(candidates), 0, 0),
            ),
            patch.object(
                earnings_reactions,
                "qualifies_for_private",
                return_value=True,
            ),
            patch.object(
                earnings_reactions,
                "qualifies_for_public",
                side_effect=lambda item: item["public_ok"],
            ),
            patch.object(earnings_reactions.time, "sleep"),
        )

    def run_main(self, managers):
        with ExitStack() as stack:
            for manager in managers:
                stack.enter_context(manager)
            stack.enter_context(redirect_stdout(StringIO()))
            earnings_reactions.main()

    @staticmethod
    def render_chart(symbol, *, output_path=None):
        if output_path is None:
            raise AssertionError("Public charts must use a unique output path")
        output_path.write_bytes(f"chart:{symbol}".encode("utf-8"))
        return output_path

    def test_public_divider_leads_the_next_reaction(self):
        message = earnings_reactions.build_public_message(self.candidate())

        self.assertTrue(
            message.startswith(
                f"{earnings_reactions.DIVIDER}\n\n# "
            )
        )
        self.assertEqual(message.count(earnings_reactions.DIVIDER), 1)
        self.assertLess(
            message.index("**Session:**"),
            message.index("*Reported earnings data"),
        )

    def test_private_divider_keeps_its_existing_position(self):
        message = earnings_reactions.build_private_message(
            self.candidate(),
            1,
        )

        self.assertFalse(message.startswith(earnings_reactions.DIVIDER))
        self.assertIn(
            (
                f"\n\n{earnings_reactions.DIVIDER}\n\n"
                "*Reported earnings data"
            ),
            message,
        )

    def test_selection_ranking_content_and_private_delivery_are_unchanged(self):
        low = self.candidate("LOW", score=90.0)
        excluded = self.candidate("EXCLUDED", score=200.0, public=False)
        high = self.candidate("HIGH", score=110.0)
        candidates = [low, excluded, high]

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            send_public = Mock(side_effect=["high-id", "low-id"])
            send_private = Mock(side_effect=["private-1", "private-2", "private-3"])
            managers = self.main_managers(
                state_path,
                candidates,
                private=True,
            )
            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "send_discord_message",
                        send_public,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "send_private_review_with_chart",
                        send_private,
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))
                earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [item.args[0]["symbol"] for item in send_private.call_args_list],
            ["EXCLUDED", "HIGH", "LOW"],
        )
        self.assertEqual(
            [item.args[1] for item in send_private.call_args_list],
            [1, 2, 3],
        )
        send_public.assert_has_calls(
            [
                call(
                    "https://example.invalid/public-webhook",
                    earnings_reactions.build_public_message(high),
                    earnings_reactions.PUBLIC_WEBHOOK_USERNAME,
                    chart_symbol="HIGH",
                ),
                call(
                    "https://example.invalid/public-webhook",
                    earnings_reactions.build_public_message(low),
                    earnings_reactions.PUBLIC_WEBHOOK_USERNAME,
                    chart_symbol="LOW",
                ),
            ]
        )
        self.assertEqual(len(state["public"]), 2)
        self.assertEqual(len(state["private"]), 3)

    def test_public_multipart_reuses_weekly_renderer_and_cleans_after_success(self):
        candidate = self.candidate()
        message = earnings_reactions.build_public_message(candidate)
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / ".ACME_unique.tmp.png"
            response = FakeResponse(b'{"id":"public-message"}')
            with patch.object(
                earnings_reactions,
                "temporary_weekly_chart_path",
                return_value=chart_path,
            ), patch.object(
                earnings_reactions,
                "generate_weekly_chart",
                side_effect=self.render_chart,
            ) as generate, patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen:
                message_id = earnings_reactions.send_discord_message(
                    "https://example.invalid/public-webhook",
                    message,
                    earnings_reactions.PUBLIC_WEBHOOK_USERNAME,
                    chart_symbol="ACME",
                )

            request = urlopen.call_args.args[0]
            body = request.data
            content_type = request.headers["Content-type"]
            chart_exists_after = chart_path.exists()
            payload_marker = b"Content-Type: application/json\r\n\r\n"
            payload_bytes = body.split(payload_marker, 1)[1].split(b"\r\n--", 1)[0]
            payload = json.loads(payload_bytes.decode("utf-8"))

        self.assertEqual(message_id, "public-message")
        generate.assert_called_once_with("ACME", output_path=chart_path)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertEqual(payload["content"], message)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["attachments"][0]["filename"], "ACME_weekly.png")
        self.assertIn(b"chart:ACME", body)
        self.assertIn(b'filename="ACME_weekly.png"', body)
        self.assertNotIn(chart_path.name.encode("utf-8"), body)
        self.assertFalse(chart_exists_after)

    def assert_preparation_failure(
        self,
        original_exception,
        *,
        fail_multipart=False,
    ):
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            chart_path = Path(temp_dir) / ".partial.tmp.png"
            self.write_state(state_path)

            def fail_render(symbol, *, output_path=None):
                output_path.write_bytes(b"partial")
                raise original_exception

            managers = self.main_managers(state_path, [candidate])
            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                urlopen = stack.enter_context(
                    patch.object(
                        earnings_reactions.urllib.request,
                        "urlopen",
                        side_effect=AssertionError("Discord must not be contacted"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "temporary_weekly_chart_path",
                        return_value=chart_path,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "generate_weekly_chart",
                        side_effect=(self.render_chart if fail_multipart else fail_render),
                    )
                )
                if fail_multipart:
                    stack.enter_context(
                        patch.object(
                            earnings_reactions,
                            "multipart_body",
                            side_effect=original_exception,
                        )
                    )
                stack.enter_context(redirect_stdout(StringIO()))
                expected_type = (
                    earnings_reactions.PublicChartPreparationCancelled
                    if isinstance(original_exception, asyncio.CancelledError)
                    else earnings_reactions.PublicChartPreparationError
                )
                with self.assertRaises(expected_type) as raised:
                    earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            chart_exists_after = chart_path.exists()

        key = earnings_reactions.report_key(candidate["report"])
        urlopen.assert_not_called()
        self.assertEqual(
            state["public"][key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_FAILED,
        )
        self.assertNotIn("discord_message_id", state["public"][key])
        self.assertFalse(chart_exists_after)
        self.assertIs(raised.exception.__cause__, original_exception)

    def test_chart_url_error_is_definite_pre_delivery_failure(self):
        self.assert_preparation_failure(
            urllib.error.URLError("synthetic chart network failure")
        )

    def test_chart_timeout_is_definite_pre_delivery_failure(self):
        self.assert_preparation_failure(TimeoutError("synthetic chart timeout"))

    def test_chart_cancellation_is_definite_pre_delivery_failure(self):
        self.assert_preparation_failure(asyncio.CancelledError())

    def test_multipart_failure_is_definite_pre_delivery_failure(self):
        self.assert_preparation_failure(
            OSError("synthetic multipart file read failure"),
            fail_multipart=True,
        )

    def test_discord_failure_preserves_ledger_classification_and_cleans(self):
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            chart_path = Path(temp_dir) / ".discord-failure.tmp.png"
            self.write_state(state_path)
            managers = self.main_managers(state_path, [candidate])
            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "temporary_weekly_chart_path",
                        return_value=chart_path,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "generate_weekly_chart",
                        side_effect=self.render_chart,
                    )
                )
                urlopen = stack.enter_context(
                    patch.object(
                        earnings_reactions.urllib.request,
                        "urlopen",
                        side_effect=http_error(500, b"synthetic rejection"),
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))
                with self.assertRaises(earnings_reactions.DefiniteDeliveryError):
                    earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            chart_exists_after = chart_path.exists()

        key = earnings_reactions.report_key(candidate["report"])
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(
            state["public"][key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_FAILED,
        )
        self.assertFalse(chart_exists_after)

    def test_success_is_confirmed_duplicate_protected_and_cleans(self):
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            chart_path = Path(temp_dir) / ".success.tmp.png"
            self.write_state(state_path)
            managers = self.main_managers(state_path, [candidate])
            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "temporary_weekly_chart_path",
                        return_value=chart_path,
                    )
                )
                generate = stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "generate_weekly_chart",
                        side_effect=self.render_chart,
                    )
                )
                urlopen = stack.enter_context(
                    patch.object(
                        earnings_reactions.urllib.request,
                        "urlopen",
                        return_value=FakeResponse(b'{"id":"public-message"}'),
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))
                earnings_reactions.main()
                earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))
            chart_exists_after = chart_path.exists()

        key = earnings_reactions.report_key(candidate["report"])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(
            state["public"][key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_CONFIRMED,
        )
        self.assertEqual(state["public"][key]["discord_message_id"], "public-message")
        self.assertFalse(chart_exists_after)

    def test_cleanup_failure_cannot_change_successful_confirmation(self):
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            chart_path = Path(temp_dir) / ".cleanup-failure.tmp.png"
            self.write_state(state_path)
            managers = self.main_managers(state_path, [candidate])
            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "temporary_weekly_chart_path",
                        return_value=chart_path,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "generate_weekly_chart",
                        side_effect=self.render_chart,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions.urllib.request,
                        "urlopen",
                        return_value=FakeResponse(b'{"id":"public-message"}'),
                    )
                )
                stack.enter_context(
                    patch.object(Path, "unlink", side_effect=OSError("synthetic cleanup"))
                )
                stack.enter_context(redirect_stdout(StringIO()))
                earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))

        key = earnings_reactions.report_key(candidate["report"])
        self.assertEqual(
            state["public"][key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_CONFIRMED,
        )
        self.assertEqual(state["public"][key]["discord_message_id"], "public-message")

    def test_preview_does_not_prepare_or_deliver_charts(self):
        candidate = self.candidate()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            managers = self.main_managers(
                state_path,
                [candidate],
                preview=True,
                private=True,
            )
            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                generate = stack.enter_context(
                    patch.object(earnings_reactions, "generate_weekly_chart")
                )
                send_public = stack.enter_context(
                    patch.object(earnings_reactions, "send_discord_message")
                )
                send_private = stack.enter_context(
                    patch.object(earnings_reactions, "send_private_review_with_chart")
                )
                output = StringIO()
                stack.enter_context(redirect_stdout(output))
                earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))

        generate.assert_not_called()
        send_public.assert_not_called()
        send_private.assert_not_called()
        self.assertIn(earnings_reactions.build_public_message(candidate), output.getvalue())
        self.assertEqual(state["public"], {})
        self.assertEqual(state["private"], {})


if __name__ == "__main__":
    unittest.main()
