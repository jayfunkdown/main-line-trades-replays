import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)


class ScoringAndThresholdTests(NoNetworkTestCase):
    def test_priority_tickers_match_the_earnings_calendar_starred_list(self):
        from scripts import morning_brief

        self.assertEqual(
            earnings_reactions.PRIORITY_TICKERS,
            morning_brief.PRIORITY_TICKERS,
        )

    def qualification_candidate(
        self,
        move,
        *,
        priority=False,
        eps_surprise=None,
        revenue_surprise=None,
    ):
        return {
            "move_percent": move,
            "priority": priority,
            "eps_surprise": eps_surprise,
            "revenue_surprise": revenue_surprise,
        }

    def test_movement_score_bonus_boundaries_are_inclusive(self):
        cases = (
            (6.999, 34.995),
            (7.0, 43.0),
            (9.999, 57.995),
            (10.0, 65.0),
            (14.999, 89.995),
            (15.0, 100.0),
            (19.999, 124.995),
            (20.0, 135.0),
            (-20.0, 135.0),
        )

        for move, expected in cases:
            with self.subTest(move=move):
                self.assertAlmostEqual(
                    earnings_reactions.movement_score(move),
                    expected,
                )

    def test_candidate_score_adds_every_exact_boundary_bonus(self):
        report = {
            "symbol": "AAPL",
            "epsActual": 1.5,
            "epsEstimate": 1.0,
            "revenueActual": 115.0,
            "revenueEstimate": 100.0,
        }

        candidate = earnings_reactions.calculate_candidate(
            report,
            {"dp": 20.0, "c": 200.0},
        )

        self.assertTrue(candidate["priority"])
        self.assertEqual(candidate["eps_surprise"], 50.0)
        self.assertEqual(candidate["revenue_surprise"], 15.0)
        self.assertEqual(candidate["score"], 188.0)

    def test_private_thresholds_include_each_exact_boundary(self):
        cases = (
            (self.qualification_candidate(5.0), True),
            (self.qualification_candidate(4.999), False),
            (self.qualification_candidate(-5.0), True),
            (self.qualification_candidate(3.0, priority=True), True),
            (self.qualification_candidate(2.999, priority=True), False),
            (
                self.qualification_candidate(2.0, eps_surprise=75.0),
                True,
            ),
            (
                self.qualification_candidate(1.999, eps_surprise=75.0),
                False,
            ),
            (
                self.qualification_candidate(2.0, eps_surprise=74.999),
                False,
            ),
            (
                self.qualification_candidate(2.0, revenue_surprise=20.0),
                True,
            ),
            (
                self.qualification_candidate(2.0, revenue_surprise=19.999),
                False,
            ),
            (self.qualification_candidate(None, priority=True), False),
        )

        with patch.dict(os.environ, {}, clear=True):
            for candidate, expected in cases:
                with self.subTest(candidate=candidate):
                    self.assertEqual(
                        earnings_reactions.qualifies_for_private(candidate),
                        expected,
                    )

    def test_public_thresholds_include_each_exact_boundary(self):
        cases = (
            (self.qualification_candidate(15.0), True),
            (self.qualification_candidate(14.999), False),
            (self.qualification_candidate(-15.0), True),
            (self.qualification_candidate(-14.999), False),
            (self.qualification_candidate(15.0, priority=True), True),
            (self.qualification_candidate(14.999, priority=True), True),
            (self.qualification_candidate(None, priority=True), False),
        )

        with patch.dict(os.environ, {}, clear=True):
            for candidate, expected in cases:
                with self.subTest(candidate=candidate):
                    self.assertEqual(
                        earnings_reactions.qualifies_for_public(candidate),
                        expected,
                    )

    def test_priority_ticker_gets_score_and_feed_threshold_overrides(self):
        report = {
            "symbol": "AAPL",
            "epsActual": None,
            "epsEstimate": None,
            "revenueActual": None,
            "revenueEstimate": None,
        }
        priority = earnings_reactions.calculate_candidate(
            report,
            {"dp": 3.0, "c": 200.0},
        )
        ordinary = earnings_reactions.calculate_candidate(
            {**report, "symbol": "ACME"},
            {"dp": 3.0, "c": 20.0},
        )

        self.assertEqual(priority["score"] - ordinary["score"], 30.0)

        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(earnings_reactions.qualifies_for_private(priority))
            self.assertFalse(earnings_reactions.qualifies_for_private(ordinary))

            priority["move_percent"] = 14.999
            ordinary["move_percent"] = 14.999
            self.assertTrue(earnings_reactions.qualifies_for_public(priority))
            self.assertFalse(earnings_reactions.qualifies_for_public(ordinary))

            priority["move_percent"] = 15.0
            ordinary["move_percent"] = 15.0
            self.assertTrue(earnings_reactions.qualifies_for_public(priority))
            self.assertTrue(earnings_reactions.qualifies_for_public(ordinary))

    def test_public_threshold_cannot_be_configured_below_fifteen_percent(self):
        candidate = self.qualification_candidate(14.999)

        with patch.dict(
            os.environ,
            {"EARNINGS_PUBLIC_MOVE_PCT": "8"},
            clear=True,
        ):
            self.assertFalse(
                earnings_reactions.qualifies_for_public(candidate)
            )

    def test_priority_sorting_falls_back_to_the_candidate_symbol(self):
        self.assertTrue(
            earnings_reactions.is_priority_candidate({"symbol": "AAPL"})
        )
        self.assertFalse(
            earnings_reactions.is_priority_candidate({"symbol": "ACME"})
        )


class SelectionAndLimitTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"

    def candidate(
        self,
        symbol,
        score,
        move,
        index,
        *,
        private=True,
        public=True,
        report=None,
        priority=False,
    ):
        return {
            "symbol": symbol,
            "score": score,
            "move_percent": move,
            "private_ok": private,
            "public_ok": public,
            "priority": priority,
            "report": report
            or {
                "date": self.TARGET_DATE,
                "symbol": symbol,
                "year": 2026,
                "quarter": index,
            },
        }

    def empty_state(self):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    def run_main(
        self,
        candidates,
        *,
        mode="preview",
        environment=None,
        limit=None,
        state=None,
    ):
        environment = dict(environment or {})
        if mode == "post" and "EARNINGS_REVIEW_WEBHOOK" not in environment:
            environment["EARNINGS_REVIEW_WEBHOOK"] = "review-webhook"

        arguments = [
            "earnings_reactions.py",
            f"--{mode}",
            "--date",
            self.TARGET_DATE,
        ]
        if limit is not None:
            arguments.extend(["--limit", str(limit)])

        state = state or self.empty_state()
        preview_list = Mock()
        send_private = Mock()
        send_public = Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            managers = (
                patch.dict(os.environ, environment, clear=True),
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
                    return_value=(candidates, 0, 0),
                ),
                patch.object(
                    earnings_reactions,
                    "qualifies_for_private",
                    side_effect=lambda item: item["private_ok"],
                ),
                patch.object(
                    earnings_reactions,
                    "qualifies_for_public",
                    side_effect=lambda item: item["public_ok"],
                ),
                patch.object(
                    earnings_reactions,
                    "required_env",
                    return_value="public-webhook",
                ),
                patch.object(
                    earnings_reactions,
                    "print_preview_list",
                    preview_list,
                ),
                patch.object(
                    earnings_reactions,
                    "send_private_review_with_chart",
                    send_private,
                ),
                patch.object(
                    earnings_reactions,
                    "send_discord_message",
                    send_public,
                ),
                patch.object(
                    earnings_reactions,
                    "build_public_message",
                    side_effect=lambda item: f"public:{item['symbol']}",
                ),
                patch.object(
                    earnings_reactions,
                    "build_private_message",
                    side_effect=lambda item, rank: f"private:{item['symbol']}",
                ),
                patch.object(earnings_reactions.time, "sleep"),
            )

            with ExitStack() as stack:
                for manager in managers:
                    stack.enter_context(manager)
                stack.enter_context(redirect_stdout(StringIO()))
                earnings_reactions.main()

            persisted_state = json.loads(state_path.read_text(encoding="utf-8"))

        return {
            "preview_list": preview_list,
            "send_private": send_private,
            "send_public": send_public,
            "state": persisted_state,
        }

    def preview_symbols(self, result):
        private_candidates = result["preview_list"].call_args_list[0].args[1]
        public_candidates = result["preview_list"].call_args_list[1].args[1]
        return (
            [item["symbol"] for item in private_candidates],
            [item["symbol"] for item in public_candidates],
        )

    def test_ranking_uses_score_then_absolute_move_then_stable_input_order(self):
        candidates = [
            self.candidate("LOWMOVE", 100.0, 5.0, 1),
            self.candidate("TIEONE", 100.0, 7.0, 2),
            self.candidate("TIETWO", 100.0, -7.0, 3),
            self.candidate("HIGHSCORE", 110.0, 1.0, 4),
        ]

        result = self.run_main(candidates)

        expected = ["HIGHSCORE", "TIEONE", "TIETWO", "LOWMOVE"]
        self.assertEqual(self.preview_symbols(result), (expected, expected))

    def test_feed_maximums_truncate_after_ranking(self):
        candidates = [
            self.candidate(f"S{index}", 100.0 - index, 10.0, index)
            for index in range(5)
        ]

        result = self.run_main(
            candidates,
            environment={
                "EARNINGS_PRIVATE_MAX": "3",
                "EARNINGS_PUBLIC_MAX": "2",
            },
        )

        self.assertEqual(
            self.preview_symbols(result),
            (["S0", "S1", "S2"], ["S0", "S1"]),
        )

    def test_public_cap_prioritizes_starred_names_within_fifteen_total(self):
        candidates = [
            self.candidate(
                "STARRED",
                1.0,
                2.0,
                0,
                private=False,
                priority=True,
            ),
            *[
                self.candidate(
                    f"MOVE{index}",
                    100.0 - index,
                    15.0 + index,
                    index + 1,
                )
                for index in range(15)
            ],
        ]

        result = self.run_main(
            candidates,
            environment={"EARNINGS_PUBLIC_MAX": "20"},
        )
        private_symbols, public_symbols = self.preview_symbols(result)

        self.assertNotIn("STARRED", private_symbols)
        self.assertEqual(len(public_symbols), 15)
        self.assertEqual(public_symbols[0], "STARRED")
        self.assertNotIn("MOVE14", public_symbols)

    def test_public_feed_is_selected_before_private_feed_is_capped(self):
        candidates = [
            self.candidate(
                "PRIVATEONLY",
                100.0,
                10.0,
                1,
                public=False,
            ),
            self.candidate("PUBLIC", 90.0, 9.0, 2),
        ]

        result = self.run_main(
            candidates,
            environment={
                "EARNINGS_PRIVATE_MAX": "1",
                "EARNINGS_PUBLIC_MAX": "1",
            },
        )

        self.assertEqual(
            self.preview_symbols(result),
            (["PRIVATEONLY"], ["PUBLIC"]),
        )

    def test_limit_one_allows_one_post_per_feed(self):
        candidates = [
            self.candidate("FIRST", 100.0, 10.0, 1),
            self.candidate("SECOND", 90.0, 9.0, 2),
        ]

        result = self.run_main(candidates, mode="post", limit=1)

        result["send_private"].assert_called_once()
        result["send_public"].assert_called_once()
        self.assertEqual(len(result["state"]["private"]), 1)
        self.assertEqual(len(result["state"]["public"]), 1)

    def test_feed_caps_bound_larger_limit_and_leave_others_unmarked(self):
        candidates = [
            self.candidate(f"S{index}", 100.0 - index, 10.0, index)
            for index in range(4)
        ]

        result = self.run_main(
            candidates,
            mode="post",
            limit=5,
            environment={
                "EARNINGS_PRIVATE_MAX": "2",
                "EARNINGS_PUBLIC_MAX": "1",
            },
        )

        self.assertEqual(result["send_private"].call_count, 2)
        self.assertEqual(result["send_public"].call_count, 1)
        self.assertEqual(len(result["state"]["private"]), 2)
        self.assertEqual(len(result["state"]["public"]), 1)
        self.assertNotIn(
            earnings_reactions.report_key(candidates[2]["report"]),
            result["state"]["private"],
        )

    def test_limit_is_applied_before_duplicates_and_does_not_backfill(self):
        top = self.candidate("DUPLICATE", 100.0, 10.0, 1)
        next_candidate = self.candidate("NEXT", 90.0, 9.0, 2)
        state = self.empty_state()
        state["public"][earnings_reactions.report_key(top["report"])] = {
            "symbol": "DUPLICATE"
        }

        result = self.run_main(
            [top, next_candidate],
            mode="post",
            limit=1,
            environment={"EARNINGS_REVIEW_WEBHOOK": ""},
            state=state,
        )

        result["send_public"].assert_not_called()
        self.assertNotIn(
            earnings_reactions.report_key(next_candidate["report"]),
            result["state"]["public"],
        )

    def test_preview_preserves_exact_duplicate_candidates(self):
        report = {
            "date": self.TARGET_DATE,
            "symbol": "DUP",
            "year": 2026,
            "quarter": 2,
        }
        duplicate_one = self.candidate("DUP", 100.0, 10.0, 1, report=report)
        duplicate_two = self.candidate("DUP", 100.0, 10.0, 2, report=report)

        result = self.run_main([duplicate_one, duplicate_two])

        self.assertEqual(
            self.preview_symbols(result),
            (["DUP", "DUP"], ["DUP", "DUP"]),
        )

    def test_posting_suppresses_second_exact_duplicate_within_each_feed(self):
        report = {
            "date": self.TARGET_DATE,
            "symbol": "DUP",
            "year": 2026,
            "quarter": 2,
        }
        duplicate_one = self.candidate("DUP", 100.0, 10.0, 1, report=report)
        duplicate_two = self.candidate("DUP", 100.0, 10.0, 2, report=report)

        result = self.run_main(
            [duplicate_one, duplicate_two],
            mode="post",
        )

        result["send_private"].assert_called_once()
        result["send_public"].assert_called_once()
        self.assertEqual(len(result["state"]["private"]), 1)
        self.assertEqual(len(result["state"]["public"]), 1)

    def test_same_ticker_and_date_with_different_quarters_are_distinct(self):
        first = self.candidate("ACME", 100.0, 10.0, 1)
        second = self.candidate("ACME", 90.0, 9.0, 2)

        self.assertNotEqual(
            earnings_reactions.report_key(first["report"]),
            earnings_reactions.report_key(second["report"]),
        )

        result = self.run_main([first, second], mode="post")

        self.assertEqual(result["send_private"].call_count, 2)
        self.assertEqual(result["send_public"].call_count, 2)


if __name__ == "__main__":
    unittest.main()
