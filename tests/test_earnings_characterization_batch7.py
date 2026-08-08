import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


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


def make_http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError(
        "https://example.invalid/api",
        code,
        "synthetic failure",
        headers or {},
        io.BytesIO(body),
    )


def empty_state():
    return {
        "public": {},
        "private": {},
        "quotes": {},
        "signal_queue": {},
    }


def candidate(symbol="ACME"):
    return {
        "symbol": symbol,
        "score": 100.0,
        "move_percent": 10.0,
        "report": {
            "symbol": symbol,
            "date": "2026-08-06",
            "year": 2026,
            "quarter": 2,
        },
    }


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    @contextmanager
    def temporary_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(empty_state()), encoding="utf-8")
            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                yield state_path

    @contextmanager
    def private_post_context(self, state, *, chart_exists=True):
        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            if chart_exists:
                chart_path.write_bytes(b"synthetic-chart")

            with patch.object(
                earnings_reactions,
                "required_env",
                side_effect=lambda name: {
                    "DISCORD_BOT_TOKEN": "synthetic-token",
                    "EARNINGS_REVIEW_WEBHOOK": (
                        "https://example.invalid/review-webhook"
                    ),
                }[name],
            ), patch.object(
                earnings_reactions,
                "resolve_webhook_channel_id",
                return_value="review-channel",
            ), patch.object(
                earnings_reactions,
                "generate_weekly_chart",
                return_value=chart_path,
            ), patch.object(
                earnings_reactions,
                "build_private_message",
                return_value="private review",
            ), patch.object(
                earnings_reactions,
                "datetime",
                FixedDateTime,
            ):
                yield chart_path


class NetworkExceptionTests(NoNetworkTestCase):
    def test_finnhub_urlerror_becomes_runtime_error_without_retry(self):
        with patch.dict(
            os.environ,
            {"FINNHUB_API_KEY": "synthetic-key"},
            clear=True,
        ), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("synthetic DNS failure"),
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(
                RuntimeError,
                "Could not reach.*synthetic DNS failure",
            ):
                earnings_reactions.get_quote_with_retry("ACME")

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_finnhub_timeout_propagates_without_retry(self):
        with patch.dict(
            os.environ,
            {"FINNHUB_API_KEY": "synthetic-key"},
            clear=True,
        ), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=TimeoutError("synthetic timeout"),
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(TimeoutError, "synthetic timeout"):
                earnings_reactions.get_quote_with_retry("ACME")

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_discord_urlerror_and_timeout_propagate_without_retry(self):
        failures = (
            urllib.error.URLError("synthetic DNS failure"),
            TimeoutError("synthetic timeout"),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                side_effect=failure,
            ) as urlopen, patch.object(
                earnings_reactions.time,
                "sleep",
            ) as sleep:
                with self.assertRaises(type(failure)):
                    earnings_reactions.send_discord_message(
                        "https://example.invalid/webhook",
                        "message",
                        "bot",
                    )

                urlopen.assert_called_once()
                sleep.assert_not_called()


class DiscordRetryMetadataTests(NoNetworkTestCase):
    def test_invalid_retry_after_header_uses_valid_json_body(self):
        error = make_http_error(
            429,
            body=b'{"retry_after": 3.25}',
            headers={"Retry-After": "not-a-number"},
        )

        self.assertEqual(
            earnings_reactions.discord_retry_seconds(error, 1),
            3.25,
        )

    def test_missing_malformed_and_incomplete_bodies_use_exponential_fallback(self):
        cases = (
            (b"", 1, 2.0),
            (b"{not-json", 2, 4.0),
            (b"{}", 3, 8.0),
            (b'{"retry_after": "bad"}', 2, 4.0),
            (b'{"retry_after": 0}', 1, 2.0),
        )

        for body, attempt, expected in cases:
            with self.subTest(body=body, attempt=attempt):
                error = make_http_error(
                    429,
                    body=body,
                    headers={"Retry-After": "invalid"},
                )
                self.assertEqual(
                    earnings_reactions.discord_retry_seconds(error, attempt),
                    expected,
                )

    def test_unexpected_json_list_body_raises_attribute_error(self):
        error = make_http_error(
            429,
            body=b"[]",
            headers={"Retry-After": "invalid"},
        )

        with self.assertRaises(AttributeError):
            earnings_reactions.discord_retry_seconds(error, 1)

    def test_unusable_retry_metadata_uses_two_four_eight_then_fails(self):
        def rate_limited(*_args, **_kwargs):
            raise make_http_error(
                429,
                body=b"{not-json",
                headers={"Retry-After": "invalid"},
            )

        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=rate_limited,
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "Discord returned HTTP 429"):
                earnings_reactions.send_discord_message(
                    "https://example.invalid/webhook",
                    "message",
                    "bot",
                )

        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(
            sleep.call_args_list,
            [call(2.0), call(4.0), call(8.0)],
        )


class DiscordStatusTests(NoNetworkTestCase):
    def test_status_200_and_204_are_both_accepted_without_reading_body(self):
        for status in (200, 204):
            with self.subTest(status=status), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"not-json", status=status),
            ) as urlopen, patch.object(
                earnings_reactions.time,
                "sleep",
            ) as sleep:
                result = earnings_reactions.send_discord_message(
                    "https://example.invalid/webhook",
                    "message",
                    "bot",
                )

                self.assertIsNone(result)
                urlopen.assert_called_once()
                sleep.assert_not_called()

    def test_other_returned_statuses_fail_without_retry(self):
        for status in (201, 202, 429):
            with self.subTest(status=status), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                return_value=FakeResponse(status=status),
            ) as urlopen, patch.object(
                earnings_reactions.time,
                "sleep",
            ) as sleep:
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"Discord returned HTTP {status}",
                ):
                    earnings_reactions.send_discord_message(
                        "https://example.invalid/webhook",
                        "message",
                        "bot",
                    )

                urlopen.assert_called_once()
                sleep.assert_not_called()


class PrivateDeliveryFailureTests(NoNetworkTestCase):
    def test_chart_generation_failure_prevents_upload_and_state_change(self):
        state = empty_state()

        with patch.object(
            earnings_reactions,
            "required_env",
            return_value="synthetic-value",
        ), patch.object(
            earnings_reactions,
            "resolve_webhook_channel_id",
            return_value="review-channel",
        ), patch.object(
            earnings_reactions,
            "generate_weekly_chart",
            side_effect=RuntimeError("synthetic chart failure"),
        ), patch.object(
            earnings_reactions,
            "save_state",
        ) as save_state:
            with self.assertRaisesRegex(RuntimeError, "synthetic chart failure"):
                earnings_reactions.send_private_review_with_chart(
                    candidate(),
                    1,
                    state,
                )

        self.assertEqual(state, empty_state())
        save_state.assert_not_called()

    def test_missing_chart_file_fails_multipart_before_upload_or_state_change(self):
        state = empty_state()

        with self.private_post_context(state, chart_exists=False), patch.object(
            earnings_reactions,
            "save_state",
        ) as save_state:
            with self.assertRaises(FileNotFoundError):
                earnings_reactions.send_private_review_with_chart(
                    candidate(),
                    1,
                    state,
                )

        self.assertEqual(state, empty_state())
        save_state.assert_not_called()

    def test_private_upload_429_is_not_retried_and_state_stays_empty(self):
        state = empty_state()

        with self.private_post_context(state), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=make_http_error(429, body=b"rate limited"),
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep, patch.object(
            earnings_reactions,
            "save_state",
        ) as save_state:
            with self.assertRaisesRegex(
                RuntimeError,
                "private earnings review: HTTP 429: rate limited",
            ):
                earnings_reactions.send_private_review_with_chart(
                    candidate(),
                    1,
                    state,
                )

        urlopen.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(state, empty_state())
        save_state.assert_not_called()

    def test_private_upload_urlerror_propagates_and_state_stays_empty(self):
        state = empty_state()

        with self.private_post_context(state), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("synthetic upload failure"),
        ) as urlopen, patch.object(
            earnings_reactions,
            "save_state",
        ) as save_state:
            with self.assertRaises(urllib.error.URLError):
                earnings_reactions.send_private_review_with_chart(
                    candidate(),
                    1,
                    state,
                )

        urlopen.assert_called_once()
        self.assertEqual(state, empty_state())
        save_state.assert_not_called()

    def test_private_success_with_malformed_json_is_not_persisted(self):
        state = empty_state()

        with self.private_post_context(state), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=FakeResponse(b"{not-json"),
        ) as urlopen, patch.object(
            earnings_reactions,
            "save_state",
        ) as save_state:
            with self.assertRaises(json.JSONDecodeError):
                earnings_reactions.send_private_review_with_chart(
                    candidate(),
                    1,
                    state,
                )

        urlopen.assert_called_once()
        self.assertEqual(state, empty_state())
        save_state.assert_not_called()

    def test_private_success_without_message_id_is_saved_with_empty_id(self):
        state = empty_state()

        with self.private_post_context(state), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=FakeResponse(b"{}"),
        ) as urlopen, patch.object(
            earnings_reactions,
            "save_state",
        ) as save_state:
            message_id = earnings_reactions.send_private_review_with_chart(
                candidate(),
                1,
                state,
            )

        token = earnings_reactions.candidate_button_token(candidate())
        self.assertEqual(message_id, "")
        self.assertEqual(
            state["signal_queue"][token]["review_message_id"],
            "",
        )
        self.assertEqual(
            state["signal_queue"][token]["review_channel_id"],
            "review-channel",
        )
        self.assertFalse(state["signal_queue"][token]["sent_to_signals"])
        urlopen.assert_called_once()
        save_state.assert_called_once_with(state)


class CalendarDirectoryFailureTests(NoNetworkTestCase):
    def test_calendar_directory_creation_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "nested" / "calendar.json"
            cache_path.parent.mkdir()
            cache_path.write_text('{"old": true}', encoding="utf-8")

            with patch.object(
                earnings_reactions,
                "CALENDAR_CACHE_FILE",
                cache_path,
            ), patch.object(
                Path,
                "mkdir",
                side_effect=OSError("synthetic mkdir failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic mkdir failure"):
                    earnings_reactions.save_calendar_cache({"new": True})

            self.assertEqual(
                cache_path.read_text(encoding="utf-8"),
                '{"old": true}',
            )
            self.assertFalse(cache_path.with_suffix(".tmp").exists())


class PrivateThenPublicFailureTests(NoNetworkTestCase):
    def test_private_success_is_fully_saved_before_public_failure(self):
        item = candidate()
        original_save_state = earnings_reactions.save_state

        with self.temporary_state() as state_path, tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"synthetic-chart")

            responses = [
                FakeResponse(b'{"channel_id":"review-channel"}'),
                FakeResponse(b'{"id":"review-message"}'),
                make_http_error(500, body=b"public failed"),
            ]

            with patch.dict(
                os.environ,
                {
                    "DISCORD_BOT_TOKEN": "synthetic-token",
                    "EARNINGS_REVIEW_WEBHOOK": (
                        "https://example.invalid/review-webhook"
                    ),
                    "EARNINGS_REACTIONS_WEBHOOK": (
                        "https://example.invalid/public-webhook"
                    ),
                },
                clear=True,
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
                return_value=[item["report"]],
            ), patch.object(
                earnings_reactions,
                "build_candidates_optimized",
                return_value=([item], 0, 0),
            ), patch.object(
                earnings_reactions,
                "qualifies_for_private",
                return_value=True,
            ), patch.object(
                earnings_reactions,
                "qualifies_for_public",
                return_value=True,
            ), patch.object(
                earnings_reactions,
                "generate_weekly_chart",
                return_value=chart_path,
            ), patch.object(
                earnings_reactions,
                "build_private_message",
                return_value="private review",
            ), patch.object(
                earnings_reactions,
                "build_public_message",
                return_value="public review",
            ), patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                side_effect=responses,
            ) as urlopen, patch.object(
                earnings_reactions,
                "save_state",
                wraps=original_save_state,
            ) as save_state, patch.object(
                earnings_reactions.time,
                "sleep",
            ) as sleep, redirect_stdout(StringIO()):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Discord returned HTTP 500: public failed",
                ):
                    earnings_reactions.main()

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        report_key = earnings_reactions.report_key(item["report"])
        token = earnings_reactions.candidate_button_token(item)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(save_state.call_count, 3)
        sleep.assert_called_once_with(
            earnings_reactions.DISCORD_POST_DELAY_SECONDS
        )
        self.assertIn(report_key, persisted["private"])
        self.assertEqual(persisted["public"], {})
        self.assertEqual(
            persisted["signal_queue"][token]["review_message_id"],
            "review-message",
        )
        self.assertEqual(
            persisted["signal_queue"][token]["review_channel_id"],
            "review-channel",
        )
        self.assertFalse(
            persisted["signal_queue"][token]["sent_to_signals"]
        )


if __name__ == "__main__":
    unittest.main()
