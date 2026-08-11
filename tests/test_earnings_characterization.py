import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions
from scripts.earnings_state import EarningsStateValidationError


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)


class AutomaticTargetDateTests(NoNetworkTestCase):
    def resolve(self, value, cutoff=None):
        environment = {}
        if cutoff is not None:
            environment["EARNINGS_EARLY_MORNING_CUTOFF_HOUR"] = str(cutoff)

        with patch.dict(os.environ, environment, clear=True):
            return earnings_reactions.resolve_automatic_target_date(value)

    def eastern_datetime(self, year, month, day, hour, minute=0):
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=earnings_reactions.EASTERN,
        )

    def test_default_cutoff_uses_current_date_at_exactly_six(self):
        result = self.resolve(self.eastern_datetime(2026, 8, 6, 6))

        self.assertEqual(result, ("2026-08-06", "current Eastern date"))

    def test_production_cutoff_rolls_exactly_six_to_previous_weekday(self):
        result = self.resolve(
            self.eastern_datetime(2026, 8, 6, 6),
            cutoff=7,
        )

        self.assertEqual(
            result,
            (
                "2026-08-05",
                "early-morning rollover (before 07:00 ET)",
            ),
        )

    def test_production_cutoff_changes_at_exactly_seven(self):
        before = self.resolve(
            self.eastern_datetime(2026, 8, 6, 6, 59),
            cutoff=7,
        )
        boundary = self.resolve(
            self.eastern_datetime(2026, 8, 6, 7),
            cutoff=7,
        )

        self.assertEqual(before[0], "2026-08-05")
        self.assertEqual(boundary, ("2026-08-06", "current Eastern date"))

    def test_monday_early_morning_rolls_back_to_friday(self):
        result = self.resolve(
            self.eastern_datetime(2026, 8, 10, 6),
            cutoff=7,
        )

        self.assertEqual(result[0], "2026-08-07")

    def test_weekend_always_rolls_back_to_friday(self):
        for day in (8, 9):
            with self.subTest(day=day):
                result = self.resolve(
                    self.eastern_datetime(2026, 8, day, 12),
                    cutoff=7,
                )
                self.assertEqual(result, ("2026-08-07", "weekend rollover"))


class EarningsStateTests(NoNetworkTestCase):
    EMPTY_STATE = {
        "public": {},
        "private": {},
        "quotes": {},
        "signal_queue": {},
        "manual_signal_drafts": {},
        "post_signal_reviews": {},
    }

    def load_from(self, path):
        with patch.object(earnings_reactions, "STATE_FILE", path):
            return earnings_reactions.load_state()

    def test_missing_state_file_returns_empty_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "missing.json"

            self.assertEqual(self.load_from(state_path), self.EMPTY_STATE)
            self.assertFalse(state_path.exists())

    def test_empty_and_malformed_state_files_fail_closed(self):
        for content in ("", "{not-json"):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as temp_dir:
                    state_path = Path(temp_dir) / "state.json"
                    state_path.write_text(content, encoding="utf-8")

                    with self.assertRaises(EarningsStateValidationError):
                        self.load_from(state_path)

    def test_empty_json_object_is_normalized_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text("{}", encoding="utf-8")

            state = self.load_from(state_path)

            self.assertEqual(state, self.EMPTY_STATE)
            self.assertEqual(state_path.read_text(encoding="utf-8"), "{}")

    def test_valid_non_object_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text("[]", encoding="utf-8")

            with self.assertRaises(EarningsStateValidationError):
                self.load_from(state_path)


class FinnhubCalendarFallbackTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"
    CACHED_REPORT = {
        "date": TARGET_DATE,
        "symbol": "ACME",
        "epsActual": 1.0,
    }

    def test_empty_responses_retry_then_use_date_specific_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            cache_path.write_text(
                json.dumps(
                    {
                        self.TARGET_DATE: {
                            "reports": [self.CACHED_REPORT, "invalid"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(
                    earnings_reactions,
                    "CALENDAR_CACHE_FILE",
                    cache_path,
                ),
                patch.object(
                    earnings_reactions,
                    "fetch_completed_reports_from_finnhub",
                    return_value=[],
                ) as fetch_reports,
                patch.object(earnings_reactions.time, "sleep") as sleep,
                redirect_stdout(StringIO()),
            ):
                reports = earnings_reactions.get_completed_reports(
                    self.TARGET_DATE
                )

            self.assertEqual(reports, [self.CACHED_REPORT])
            self.assertEqual(fetch_reports.call_count, 4)
            self.assertEqual(sleep.call_args_list, [call(10), call(20), call(30)])

    def test_empty_responses_without_cache_fail_after_four_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "missing-cache.json"

            with (
                patch.object(
                    earnings_reactions,
                    "CALENDAR_CACHE_FILE",
                    cache_path,
                ),
                patch.object(
                    earnings_reactions,
                    "fetch_completed_reports_from_finnhub",
                    return_value=[],
                ) as fetch_reports,
                patch.object(earnings_reactions.time, "sleep") as sleep,
                redirect_stdout(StringIO()),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Nothing will be posted",
                ):
                    earnings_reactions.get_completed_reports(self.TARGET_DATE)

            self.assertEqual(fetch_reports.call_count, 4)
            self.assertEqual(sleep.call_args_list, [call(10), call(20), call(30)])
            self.assertFalse(cache_path.exists())

    def test_recovered_response_is_returned_and_cached(self):
        fresh_report = {
            "date": self.TARGET_DATE,
            "symbol": "FRESH",
            "epsActual": 2.0,
        }

        with (
            patch.object(
                earnings_reactions,
                "get_cached_calendar_reports",
                return_value=[],
            ),
            patch.object(
                earnings_reactions,
                "fetch_completed_reports_from_finnhub",
                side_effect=[[], [fresh_report]],
            ) as fetch_reports,
            patch.object(
                earnings_reactions,
                "store_calendar_reports",
            ) as store_reports,
            patch.object(earnings_reactions.time, "sleep") as sleep,
            redirect_stdout(StringIO()),
        ):
            reports = earnings_reactions.get_completed_reports(self.TARGET_DATE)

        self.assertEqual(reports, [fresh_report])
        self.assertEqual(fetch_reports.call_count, 2)
        sleep.assert_called_once_with(10)
        store_reports.assert_called_once_with(self.TARGET_DATE, [fresh_report])


class DuplicatePostingTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"

    def setUp(self):
        super().setUp()
        self.report = {
            "date": self.TARGET_DATE,
            "symbol": "ACME",
            "year": 2026,
            "quarter": 2,
        }
        self.candidate = {
            "symbol": "ACME",
            "score": 100.0,
            "move_percent": 10.0,
            "report": self.report,
        }
        self.key = earnings_reactions.report_key(self.report)

    def run_post(self, state):
        output = StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with (
                patch.dict(
                    os.environ,
                    {"EARNINGS_REVIEW_WEBHOOK": "review-webhook"},
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "earnings_reactions.py",
                        "--post",
                        "--date",
                        self.TARGET_DATE,
                    ],
                ),
                patch.object(earnings_reactions, "STATE_FILE", state_path),
                patch.object(
                    earnings_reactions,
                    "get_completed_reports",
                    return_value=[self.report],
                ),
                patch.object(
                    earnings_reactions,
                    "build_candidates_optimized",
                    return_value=([self.candidate], 0, 0),
                ),
                patch.object(
                    earnings_reactions,
                    "qualifies_for_private",
                    return_value=True,
                ),
                patch.object(
                    earnings_reactions,
                    "qualifies_for_public",
                    return_value=True,
                ),
                patch.object(
                    earnings_reactions,
                    "required_env",
                    return_value="public-webhook",
                ),
                patch.object(
                    earnings_reactions,
                    "send_private_review_with_chart",
                ) as send_private,
                patch.object(
                    earnings_reactions,
                    "send_discord_message",
                ) as send_public,
                patch.object(
                    earnings_reactions,
                    "build_public_message",
                    return_value="public message",
                ),
                patch.object(earnings_reactions.time, "sleep"),
                redirect_stdout(output),
            ):
                earnings_reactions.main()
                final_state = earnings_reactions.load_state()

        state.clear()
        state.update(final_state)
        return send_private, send_public, output.getvalue()

    def state(self):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
            "manual_signal_drafts": {},
            "post_signal_reviews": {},
        }

    def test_saved_keys_suppress_both_private_and_public_posts(self):
        state = self.state()
        state["private"][self.key] = {"symbol": "ACME"}
        state["public"][self.key] = {"symbol": "ACME"}
        original_state = copy.deepcopy(state)

        send_private, send_public, output = self.run_post(state)

        send_private.assert_not_called()
        send_public.assert_not_called()
        self.assertEqual(state, original_state)
        self.assertIn("0 private posted, 0 public posted", output)

    def test_private_duplicate_does_not_suppress_new_public_post(self):
        state = self.state()
        state["private"][self.key] = {"symbol": "ACME"}

        send_private, send_public, output = self.run_post(state)

        send_private.assert_not_called()
        send_public.assert_called_once_with(
            "public-webhook",
            "public message",
            earnings_reactions.PUBLIC_WEBHOOK_USERNAME,
            chart_symbol="ACME",
        )
        self.assertIn(self.key, state["public"])
        self.assertIn("0 private posted, 1 public posted", output)


if __name__ == "__main__":
    unittest.main()
