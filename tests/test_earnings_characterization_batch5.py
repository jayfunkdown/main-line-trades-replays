import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, call, patch

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
    def temporary_state(self, quotes=None):
        state = {
            "public": {},
            "private": {},
            "quotes": quotes or {},
            "signal_queue": {},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                yield earnings_reactions.load_state(), state_path


class QuoteCacheTimestampTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"
    KEY = "2026-08-06:ACME"
    QUOTE = {"c": 25.0, "dp": 10.0}

    def cache_item(self, fetched_at, quote=None):
        return {
            "fetched_at": fetched_at,
            "quote": self.QUOTE if quote is None else quote,
        }

    def test_exact_maximum_age_is_fresh_and_one_microsecond_older_expires(self):
        exact_boundary = FIXED_NOW - timedelta(minutes=20)
        expired = exact_boundary - timedelta(microseconds=1)

        for fetched_at, expected in (
            (exact_boundary, self.QUOTE),
            (expired, None),
        ):
            with self.subTest(fetched_at=fetched_at):
                with self.temporary_state(
                    {self.KEY: self.cache_item(fetched_at.isoformat())}
                ) as (state, _state_path):
                    with patch.object(
                        earnings_reactions,
                        "datetime",
                        FixedDateTime,
                    ):
                        result = earnings_reactions.cached_quote(
                            state,
                            self.TARGET_DATE,
                            "acme",
                            max_age_minutes=20,
                        )

                self.assertEqual(result, expected)

    def test_naive_timestamp_is_assumed_eastern_and_aware_utc_is_converted(self):
        eastern_time = FIXED_NOW - timedelta(minutes=10)
        naive_value = eastern_time.replace(tzinfo=None).isoformat()
        utc_value = eastern_time.astimezone(timezone.utc).isoformat()

        with self.temporary_state(
            {
                self.KEY: self.cache_item(naive_value),
                "2026-08-06:UTC": self.cache_item(utc_value),
            }
        ) as (state, _state_path):
            with patch.object(
                earnings_reactions,
                "datetime",
                FixedDateTime,
            ):
                naive = earnings_reactions.cached_quote(
                    state,
                    self.TARGET_DATE,
                    "ACME",
                    max_age_minutes=20,
                )
                aware = earnings_reactions.cached_quote(
                    state,
                    self.TARGET_DATE,
                    "UTC",
                    max_age_minutes=20,
                )

        self.assertEqual(naive, self.QUOTE)
        self.assertEqual(aware, self.QUOTE)

    def test_malformed_cache_entries_and_timestamps_are_misses(self):
        valid_time = FIXED_NOW.isoformat()
        entries = {
            "2026-08-06:NOTDICT": "invalid",
            "2026-08-06:NOQUOTE": {"fetched_at": valid_time},
            "2026-08-06:BADQUOTE": {
                "fetched_at": valid_time,
                "quote": [],
            },
            "2026-08-06:NOTIME": {"quote": self.QUOTE},
            "2026-08-06:BADTIME": {
                "fetched_at": "not-a-timestamp",
                "quote": self.QUOTE,
            },
        }

        with self.temporary_state(entries) as (state, _state_path):
            with patch.object(
                earnings_reactions,
                "datetime",
                FixedDateTime,
            ):
                for symbol in (
                    "NOTDICT",
                    "NOQUOTE",
                    "BADQUOTE",
                    "NOTIME",
                    "BADTIME",
                ):
                    with self.subTest(symbol=symbol):
                        self.assertIsNone(
                            earnings_reactions.cached_quote(
                                state,
                                self.TARGET_DATE,
                                symbol,
                                max_age_minutes=20,
                            )
                        )

    def test_future_timestamp_is_treated_as_fresh(self):
        future = FIXED_NOW + timedelta(days=30)

        with self.temporary_state(
            {self.KEY: self.cache_item(future.isoformat())}
        ) as (state, _state_path):
            with patch.object(
                earnings_reactions,
                "datetime",
                FixedDateTime,
            ):
                result = earnings_reactions.cached_quote(
                    state,
                    self.TARGET_DATE,
                    "ACME",
                    max_age_minutes=20,
                )

        self.assertEqual(result, self.QUOTE)

    def test_pruning_removes_older_and_malformed_but_keeps_exact_cutoff(self):
        cutoff = FIXED_NOW - timedelta(days=3)
        quotes = {
            "exact": self.cache_item(cutoff.isoformat()),
            "older": self.cache_item(
                (cutoff - timedelta(microseconds=1)).isoformat()
            ),
            "newer": self.cache_item(
                (cutoff + timedelta(microseconds=1)).isoformat()
            ),
            "future": self.cache_item(
                (FIXED_NOW + timedelta(days=1)).isoformat()
            ),
            "malformed-time": self.cache_item("invalid"),
            "malformed-entry": [],
        }

        with self.temporary_state(quotes) as (state, _state_path):
            with patch.object(
                earnings_reactions,
                "datetime",
                FixedDateTime,
            ):
                earnings_reactions.prune_quote_cache(state, keep_days=3)

        self.assertEqual(
            set(state["quotes"]),
            {"exact", "newer", "future"},
        )


class QuoteBudgetAndOrderingTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"

    def report(
        self,
        symbol,
        *,
        eps_actual=None,
        eps_estimate=None,
        revenue_actual=None,
        revenue_estimate=None,
    ):
        return {
            "date": self.TARGET_DATE,
            "symbol": symbol,
            "year": 2026,
            "quarter": 2,
            "epsActual": eps_actual,
            "epsEstimate": eps_estimate,
            "revenueActual": revenue_actual,
            "revenueEstimate": revenue_estimate,
        }

    def test_limited_budget_quotes_priority_then_largest_surprise(self):
        low = self.report("LOW", eps_actual=1.1, eps_estimate=1.0)
        surprise = self.report("SURPRISE", eps_actual=2.0, eps_estimate=1.0)
        priority = self.report("AAPL")
        quote = {"c": 10.0, "dp": 5.0}

        with self.temporary_state() as (state, _state_path):
            with (
                patch.dict(
                    os.environ,
                    {
                        "EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "2",
                        "EARNINGS_QUOTE_DELAY_SECONDS": "0.25",
                    },
                    clear=True,
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                    return_value=quote,
                ) as get_quote,
                patch.object(earnings_reactions.time, "sleep") as sleep,
                redirect_stdout(StringIO()),
            ):
                candidates, quote_calls, cache_hits = (
                    earnings_reactions.build_candidates_optimized(
                        [low, surprise, priority],
                        self.TARGET_DATE,
                        state,
                    )
                )

        self.assertEqual(
            [item.args[0] for item in get_quote.call_args_list],
            ["AAPL", "SURPRISE"],
        )
        self.assertEqual(
            [item["symbol"] for item in candidates],
            ["AAPL", "SURPRISE"],
        )
        self.assertEqual((quote_calls, cache_hits), (2, 0))
        sleep.assert_called_once_with(0.25)
        self.assertNotIn(f"{self.TARGET_DATE}:LOW", state["quotes"])

    def test_empty_and_runtime_failed_quotes_are_cached_and_reused(self):
        empty = self.report("EMPTY")
        failed = self.report("FAILED")

        with self.temporary_state() as (state, _state_path):
            with (
                patch.dict(
                    os.environ,
                    {"EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "2"},
                    clear=True,
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                    side_effect=[{}, RuntimeError("simulated failure")],
                ) as first_fetch,
                patch.object(earnings_reactions.time, "sleep"),
                redirect_stdout(StringIO()),
            ):
                first_candidates, first_calls, first_hits = (
                    earnings_reactions.build_candidates_optimized(
                        [empty, failed],
                        self.TARGET_DATE,
                        state,
                    )
                )

            with (
                patch.dict(
                    os.environ,
                    {"EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "2"},
                    clear=True,
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                ) as second_fetch,
                patch.object(earnings_reactions.time, "sleep") as second_sleep,
                redirect_stdout(StringIO()),
            ):
                second_candidates, second_calls, second_hits = (
                    earnings_reactions.build_candidates_optimized(
                        [empty, failed],
                        self.TARGET_DATE,
                        state,
                    )
                )

        self.assertEqual(first_fetch.call_count, 2)
        self.assertEqual((first_calls, first_hits), (2, 0))
        self.assertEqual(len(first_candidates), 2)
        self.assertEqual(
            state["quotes"][f"{self.TARGET_DATE}:EMPTY"]["quote"],
            {},
        )
        self.assertEqual(
            state["quotes"][f"{self.TARGET_DATE}:FAILED"]["quote"],
            {},
        )
        second_fetch.assert_not_called()
        second_sleep.assert_not_called()
        self.assertEqual((second_calls, second_hits), (0, 2))
        self.assertEqual(len(second_candidates), 2)

    def test_cache_hits_do_not_consume_budget_or_delay_requests(self):
        cached_report = self.report("CACHED", eps_actual=2.0, eps_estimate=1.0)
        requested_report = self.report("REQUESTED")
        cached_quote = {"c": 20.0, "dp": 8.0}
        requested_quote = {"c": 10.0, "dp": 4.0}
        cache_key = f"{self.TARGET_DATE}:CACHED"
        quotes = {
            cache_key: {
                "fetched_at": FIXED_NOW.isoformat(),
                "quote": cached_quote,
            }
        }

        with self.temporary_state(quotes) as (state, _state_path):
            with (
                patch.dict(
                    os.environ,
                    {
                        "EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "1",
                        "EARNINGS_QUOTE_DELAY_SECONDS": "0.5",
                    },
                    clear=True,
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                    return_value=requested_quote,
                ) as get_quote,
                patch.object(earnings_reactions.time, "sleep") as sleep,
                redirect_stdout(StringIO()),
            ):
                candidates, quote_calls, cache_hits = (
                    earnings_reactions.build_candidates_optimized(
                        [requested_report, cached_report],
                        self.TARGET_DATE,
                        state,
                    )
                )

        get_quote.assert_called_once_with("REQUESTED")
        sleep.assert_not_called()
        self.assertEqual((quote_calls, cache_hits), (1, 1))
        self.assertEqual(
            [item["symbol"] for item in candidates],
            ["CACHED", "REQUESTED"],
        )

    def test_cached_report_is_included_after_request_budget_is_exhausted(self):
        priority = self.report("AAPL")
        cached_report = self.report("CACHED")
        cached_quote = {"c": 20.0, "dp": 8.0}
        quotes = {
            f"{self.TARGET_DATE}:CACHED": {
                "fetched_at": FIXED_NOW.isoformat(),
                "quote": cached_quote,
            }
        }

        with self.temporary_state(quotes) as (state, _state_path):
            with (
                patch.dict(
                    os.environ,
                    {"EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "1"},
                    clear=True,
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                    return_value={"c": 200.0, "dp": 5.0},
                ) as get_quote,
                patch.object(earnings_reactions.time, "sleep") as sleep,
                redirect_stdout(StringIO()),
            ):
                candidates, quote_calls, cache_hits = (
                    earnings_reactions.build_candidates_optimized(
                        [cached_report, priority],
                        self.TARGET_DATE,
                        state,
                    )
                )

        get_quote.assert_called_once_with("AAPL")
        sleep.assert_not_called()
        self.assertEqual((quote_calls, cache_hits), (1, 1))
        self.assertEqual(
            [item["symbol"] for item in candidates],
            ["AAPL", "CACHED"],
        )

    def test_last_actual_request_sleeps_when_budget_remains(self):
        report = self.report("ONLY")

        with self.temporary_state() as (state, _state_path):
            with (
                patch.dict(
                    os.environ,
                    {
                        "EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "5",
                        "EARNINGS_QUOTE_DELAY_SECONDS": "0.75",
                    },
                    clear=True,
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                    return_value={"c": 10.0, "dp": 4.0},
                ),
                patch.object(earnings_reactions.time, "sleep") as sleep,
                redirect_stdout(StringIO()),
            ):
                earnings_reactions.build_candidates_optimized(
                    [report],
                    self.TARGET_DATE,
                    state,
                )

        sleep.assert_called_once_with(0.75)


class PreviewPersistenceTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"

    def test_preview_persists_fetched_quote_but_not_posting_state(self):
        report = {
            "date": self.TARGET_DATE,
            "symbol": "ACME",
            "year": 2026,
            "quarter": 2,
        }
        quote = {"c": 10.0, "dp": 1.0}

        with self.temporary_state() as (_state, state_path):
            send_public = Mock()
            send_private = Mock()

            with (
                patch.dict(
                    os.environ,
                    {
                        "EARNINGS_QUOTE_DELAY_SECONDS": "0",
                        "EARNINGS_MAX_QUOTE_CALLS_PER_RUN": "1",
                    },
                    clear=True,
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "earnings_reactions.py",
                        "--preview",
                        "--date",
                        self.TARGET_DATE,
                    ],
                ),
                patch.object(
                    earnings_reactions,
                    "datetime",
                    FixedDateTime,
                ),
                patch.object(
                    earnings_reactions,
                    "get_completed_reports",
                    return_value=[report],
                ),
                patch.object(
                    earnings_reactions,
                    "get_quote_with_retry",
                    return_value=quote,
                ),
                patch.object(
                    earnings_reactions,
                    "send_discord_message",
                    send_public,
                ),
                patch.object(
                    earnings_reactions,
                    "send_private_review_with_chart",
                    send_private,
                ),
                patch.object(earnings_reactions.time, "sleep"),
                redirect_stdout(StringIO()),
            ):
                earnings_reactions.main()

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["public"], {})
        self.assertEqual(persisted["private"], {})
        self.assertEqual(persisted["signal_queue"], {})
        self.assertEqual(
            persisted["quotes"][f"{self.TARGET_DATE}:ACME"]["quote"],
            quote,
        )
        send_public.assert_not_called()
        send_private.assert_not_called()


class CalendarCachePruningTests(NoNetworkTestCase):
    def test_storing_calendar_keeps_lexically_newest_seven_dates(self):
        initial_cache = {
            f"2026-08-{day:02d}": {
                "reports": [{"symbol": f"S{day}"}],
            }
            for day in range(1, 9)
        }
        new_report = {"symbol": "NEW"}

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar.json"
            cache_path.write_text(
                json.dumps(initial_cache),
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
                    "datetime",
                    FixedDateTime,
                ),
            ):
                earnings_reactions.store_calendar_reports(
                    "2026-08-09",
                    [new_report],
                )

            persisted = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(
            set(persisted),
            {f"2026-08-{day:02d}" for day in range(3, 10)},
        )
        self.assertEqual(persisted["2026-08-09"]["count"], 1)
        self.assertEqual(persisted["2026-08-09"]["reports"], [new_report])
        self.assertEqual(
            persisted["2026-08-09"]["fetched_at"],
            FIXED_NOW.isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
