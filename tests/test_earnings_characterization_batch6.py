import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from datetime import datetime
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
            state_path.write_text(
                json.dumps(
                    {
                        "public": {},
                        "private": {},
                        "quotes": {},
                        "signal_queue": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                yield state_path


class FinnhubFailureTests(NoNetworkTestCase):
    def test_quote_429_recovers_on_third_attempt_with_linear_delays(self):
        quote = {"c": 42.0, "dp": 8.5}

        with patch.object(
            earnings_reactions,
            "finnhub_get",
            side_effect=[
                RuntimeError("HTTP 429 returned by Finnhub"),
                RuntimeError("HTTP 429 returned by Finnhub"),
                quote,
            ],
        ) as finnhub_get, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            result = earnings_reactions.get_quote_with_retry("ACME")

        self.assertEqual(result, quote)
        self.assertEqual(finnhub_get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(10), call(20)])

    def test_quote_429_stops_after_four_attempts_and_reraises(self):
        error = RuntimeError("HTTP 429 returned by Finnhub")

        with patch.object(
            earnings_reactions,
            "finnhub_get",
            side_effect=error,
        ) as finnhub_get, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                earnings_reactions.get_quote_with_retry("ACME")

        self.assertEqual(finnhub_get.call_count, 4)
        self.assertEqual(
            sleep.call_args_list,
            [call(10), call(20), call(30)],
        )

    def test_quote_non_429_runtime_error_is_not_retried(self):
        with patch.dict(
            os.environ,
            {"FINNHUB_API_KEY": "synthetic-test-key"},
            clear=True,
        ), patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=make_http_error(500, body=b"Finnhub failed"),
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(
                RuntimeError,
                "HTTP 500.*Finnhub failed",
            ):
                earnings_reactions.get_quote_with_retry("ACME")

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_get_json_propagates_malformed_json_decode_error(self):
        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=FakeResponse(b"{not-json"),
        ):
            with self.assertRaises(json.JSONDecodeError):
                earnings_reactions.get_json("https://example.invalid/json")

    def test_calendar_fetch_raises_attribute_error_for_top_level_list(self):
        with patch.object(
            earnings_reactions,
            "finnhub_get",
            return_value=[],
        ):
            with self.assertRaises(AttributeError):
                earnings_reactions.fetch_completed_reports_from_finnhub(
                    "2026-08-06"
                )

    def test_calendar_fetch_treats_non_list_calendar_as_empty(self):
        with patch.object(
            earnings_reactions,
            "finnhub_get",
            return_value={"earningsCalendar": {"symbol": "ACME"}},
        ):
            reports = earnings_reactions.fetch_completed_reports_from_finnhub(
                "2026-08-06"
            )

        self.assertEqual(reports, [])


class InvalidConfigurationTests(NoNetworkTestCase):
    REPORT = {
        "symbol": "ACME",
        "date": "2026-08-06",
        "year": 2026,
        "quarter": 2,
    }

    def test_invalid_quote_budget_values_raise_before_quote_requests(self):
        for value, message in (
            ("not-an-integer", "must be an integer"),
            ("0", "must be at least 1"),
            ("-1", "must be at least 1"),
        ):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"EARNINGS_MAX_QUOTE_CALLS_PER_RUN": value},
                clear=True,
            ), patch.object(
                earnings_reactions,
                "get_quote_with_retry",
            ) as get_quote:
                with self.assertRaisesRegex(RuntimeError, message):
                    earnings_reactions.build_candidates_optimized(
                        [dict(self.REPORT)],
                        "2026-08-06",
                        {"quotes": {}},
                    )

                get_quote.assert_not_called()

    def test_invalid_quote_cache_duration_values_raise_before_requests(self):
        for value, message in (
            ("not-a-number", "must be a number"),
            ("-0.01", "cannot be negative"),
        ):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"EARNINGS_QUOTE_CACHE_MINUTES": value},
                clear=True,
            ), patch.object(
                earnings_reactions,
                "get_quote_with_retry",
            ) as get_quote:
                with self.assertRaisesRegex(RuntimeError, message):
                    earnings_reactions.build_candidates_optimized(
                        [dict(self.REPORT)],
                        "2026-08-06",
                        {"quotes": {}},
                    )

                get_quote.assert_not_called()


class CalendarCacheFailureTests(NoNetworkTestCase):
    def test_malformed_calendar_cache_structures_are_treated_as_empty(self):
        values = (
            "not-json",
            json.dumps([{"date": "2026-08-06"}]),
            json.dumps({"2026-08-06": ["not-an-entry"]}),
            json.dumps({"2026-08-06": {"reports": "not-a-list"}}),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            with patch.object(
                earnings_reactions,
                "CALENDAR_CACHE_FILE",
                cache_path,
            ):
                for value in values:
                    with self.subTest(value=value):
                        cache_path.write_text(value, encoding="utf-8")
                        self.assertEqual(
                            earnings_reactions.get_cached_calendar_reports(
                                "2026-08-06"
                            ),
                            [],
                        )

    def test_calendar_cache_filters_non_dictionary_reports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "2026-08-06": {
                            "reports": [
                                {"symbol": "ACME"},
                                "bad",
                                None,
                                7,
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                earnings_reactions,
                "CALENDAR_CACHE_FILE",
                cache_path,
            ):
                reports = earnings_reactions.get_cached_calendar_reports(
                    "2026-08-06"
                )

        self.assertEqual(reports, [{"symbol": "ACME"}])

    def test_calendar_cache_read_oserror_is_silently_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            cache_path.write_text("{}", encoding="utf-8")
            with patch.object(
                earnings_reactions,
                "CALENDAR_CACHE_FILE",
                cache_path,
            ), patch.object(
                Path,
                "read_text",
                side_effect=OSError("synthetic read failure"),
            ):
                self.assertEqual(earnings_reactions.load_calendar_cache(), {})

    def test_calendar_cache_write_oserror_propagates_without_target_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            with patch.object(
                earnings_reactions,
                "CALENDAR_CACHE_FILE",
                cache_path,
            ), patch.object(
                Path,
                "write_text",
                side_effect=OSError("synthetic write failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic write failure"):
                    earnings_reactions.save_calendar_cache({"value": 1})

            self.assertFalse(cache_path.exists())
            self.assertFalse(cache_path.with_suffix(".tmp").exists())

    def test_calendar_cache_replace_failure_leaves_old_file_and_new_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            cache_path.write_text('{"old": true}', encoding="utf-8")
            with patch.object(
                earnings_reactions,
                "CALENDAR_CACHE_FILE",
                cache_path,
            ), patch.object(
                Path,
                "replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    earnings_reactions.save_calendar_cache({"new": True})

            self.assertEqual(
                cache_path.read_text(encoding="utf-8"),
                '{"old": true}',
            )
            self.assertEqual(
                json.loads(
                    cache_path.with_suffix(".tmp").read_text(encoding="utf-8")
                ),
                {"new": True},
            )


class DiscordWebhookFailureTests(NoNetworkTestCase):
    def test_discord_429_header_retries_once_with_minimum_one_second(self):
        responses = [
            make_http_error(429, headers={"Retry-After": "0.25"}),
            FakeResponse(status=204),
        ]

        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=responses,
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            earnings_reactions.send_discord_message(
                "https://example.invalid/webhook",
                "message",
                "bot",
            )

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_discord_429_body_retry_after_is_used_without_rounding(self):
        responses = [
            make_http_error(429, body=b'{"retry_after": 2.5}'),
            FakeResponse(status=204),
        ]

        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=responses,
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            earnings_reactions.send_discord_message(
                "https://example.invalid/webhook",
                "message",
                "bot",
            )

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.5)

    def test_discord_429_stops_after_four_attempts(self):
        def rate_limited(*_args, **_kwargs):
            return_error = make_http_error(
                429,
                body=b'{"retry_after": 1.5}',
            )
            raise return_error

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
            [call(1.5), call(1.5), call(1.5)],
        )

    def test_discord_non_429_error_is_not_retried(self):
        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=make_http_error(500, body=b"server failed"),
        ) as urlopen, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(
                RuntimeError,
                "Discord returned HTTP 500: server failed",
            ):
                earnings_reactions.send_discord_message(
                    "https://example.invalid/webhook",
                    "message",
                    "bot",
                )

        urlopen.assert_called_once()
        sleep.assert_not_called()


class PartialPostingFailureTests(NoNetworkTestCase):
    @staticmethod
    def candidate(symbol):
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

    def test_later_public_failure_preserves_only_earlier_success_in_state(self):
        first = self.candidate("FIRST")
        second = self.candidate("SECOND")

        with self.temporary_state() as state_path, patch.dict(
            os.environ,
            {
                "EARNINGS_REACTIONS_WEBHOOK": (
                    "https://example.invalid/public-webhook"
                ),
                "EARNINGS_REVIEW_WEBHOOK": "",
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
            return_value=[first["report"], second["report"]],
        ), patch.object(
            earnings_reactions,
            "build_candidates_optimized",
            return_value=([first, second], 0, 0),
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
            "build_public_message",
            side_effect=["first message", "second message"],
        ), patch.object(
            earnings_reactions,
            "send_discord_message",
            side_effect=[None, RuntimeError("synthetic Discord failure")],
        ) as send_message, patch.object(
            earnings_reactions,
            "send_private_review_with_chart",
        ) as send_private, patch.object(
            earnings_reactions.time,
            "sleep",
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "synthetic Discord failure"):
                earnings_reactions.main()

            state = json.loads(state_path.read_text(encoding="utf-8"))

        first_key = earnings_reactions.report_key(first["report"])
        second_key = earnings_reactions.report_key(second["report"])
        self.assertIn(first_key, state["public"])
        self.assertNotIn(second_key, state["public"])
        self.assertEqual(state["private"], {})
        self.assertEqual(send_message.call_count, 2)
        send_private.assert_not_called()
        sleep.assert_called_once_with(
            earnings_reactions.DISCORD_POST_DELAY_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
