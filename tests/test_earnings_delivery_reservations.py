import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions
from scripts.earnings_state import EarningsStateError


class DeliveryReservationTests(unittest.TestCase):
    TARGET_DATE = "2026-08-06"

    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

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

    @staticmethod
    def empty_state():
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    def write_state(self, state_path, state=None):
        state_path.write_text(
            json.dumps(state or self.empty_state()),
            encoding="utf-8",
        )

    def main_patches(
        self,
        state_path,
        send_private,
        send_public,
        *,
        private=True,
        public=True,
        force=False,
    ):
        arguments = [
            "earnings_reactions.py",
            "--post",
            "--date",
            self.TARGET_DATE,
        ]
        if force:
            arguments.append("--force")

        environment = {
            "EARNINGS_REACTIONS_WEBHOOK": "public-webhook",
            "EARNINGS_REVIEW_WEBHOOK": "review-webhook" if private else "",
        }
        return (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", arguments),
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
                return_value=public,
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
                return_value="public message",
            ),
            patch.object(earnings_reactions.time, "sleep"),
        )

    def run_main(self, patches):
        with ExitStack() as stack:
            for manager in patches:
                stack.enter_context(manager)
            stack.enter_context(redirect_stdout(StringIO()))
            earnings_reactions.main()

    def test_valid_legacy_record_is_confirmed_and_force_reclaimable(self):
        legacy = {
            "symbol": "ACME",
            "posted_at": "2026-08-07T08:00:00-04:00",
        }
        self.assertEqual(
            earnings_reactions.feed_delivery_status(legacy),
            earnings_reactions.FEED_DELIVERY_CONFIRMED,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state = self.empty_state()
            state["public"][self.key] = legacy
            self.write_state(state_path, state)
            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                _state, status, attempt_id = (
                    earnings_reactions.reserve_feed_delivery(
                        "public",
                        self.key,
                        "ACME",
                        force=True,
                        attempt_id="new-attempt",
                    )
                )

        self.assertEqual(status, earnings_reactions.FEED_DELIVERY_RESERVED)
        self.assertEqual(attempt_id, "new-attempt")

    def test_empty_legacy_record_fails_closed(self):
        self.assertEqual(
            earnings_reactions.feed_delivery_status({}),
            earnings_reactions.FEED_DELIVERY_INVALID,
        )

    def test_symbol_only_legacy_record_fails_closed(self):
        self.assertEqual(
            earnings_reactions.feed_delivery_status({"symbol": "ACME"}),
            earnings_reactions.FEED_DELIVERY_INVALID,
        )

    def test_missing_or_invalid_legacy_posted_at_fails_closed(self):
        for posted_at in (None, 123, "", "not-a-timestamp"):
            with self.subTest(posted_at=posted_at):
                self.assertEqual(
                    earnings_reactions.feed_delivery_status(
                        {"symbol": "ACME", "posted_at": posted_at}
                    ),
                    earnings_reactions.FEED_DELIVERY_INVALID,
                )

    def test_explicit_null_delivery_status_fails_closed(self):
        self.assertEqual(
            earnings_reactions.feed_delivery_status(
                {
                    "symbol": "ACME",
                    "posted_at": "2026-08-07T08:00:00-04:00",
                    "delivery_status": None,
                }
            ),
            earnings_reactions.FEED_DELIVERY_INVALID,
        )

    def test_successful_reserve_and_confirm_preserves_metadata_and_message_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state = self.empty_state()
            state["future"] = {"keep": True}
            self.write_state(state_path, state)
            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                _state, status, attempt_id = (
                    earnings_reactions.reserve_feed_delivery(
                        "public",
                        self.key,
                        "ACME",
                        attempt_id="attempt-one",
                        reserved_at="2026-08-08T12:00:00-04:00",
                    )
                )
                final_state, transitioned = (
                    earnings_reactions.transition_feed_delivery(
                        "public",
                        self.key,
                        attempt_id,
                        earnings_reactions.FEED_DELIVERY_CONFIRMED,
                        discord_message_id="message-123",
                        finished_at="2026-08-08T12:01:00-04:00",
                    )
                )

        record = final_state["public"][self.key]
        self.assertEqual(status, earnings_reactions.FEED_DELIVERY_RESERVED)
        self.assertTrue(transitioned)
        self.assertEqual(
            record["delivery_status"],
            earnings_reactions.FEED_DELIVERY_CONFIRMED,
        )
        self.assertEqual(record["discord_message_id"], "message-123")
        self.assertEqual(final_state["future"], {"keep": True})

    def test_concurrent_same_key_claims_allow_exactly_one_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            delivered = []
            delivery_guard = threading.Lock()

            def attempt_delivery(number):
                _state, status, attempt_id = (
                    earnings_reactions.reserve_feed_delivery(
                        "public",
                        self.key,
                        "ACME",
                        attempt_id=f"attempt-{number}",
                    )
                )
                if (
                    status == earnings_reactions.FEED_DELIVERY_RESERVED
                    and attempt_id is not None
                ):
                    with delivery_guard:
                        delivered.append(number)
                    earnings_reactions.transition_feed_delivery(
                        "public",
                        self.key,
                        attempt_id,
                        earnings_reactions.FEED_DELIVERY_CONFIRMED,
                    )
                return status, attempt_id

            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = list(executor.map(attempt_delivery, (1, 2)))

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            sum(attempt_id is not None for _, attempt_id in outcomes),
            1,
        )
        self.assertEqual(
            persisted["public"][self.key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_CONFIRMED,
        )

    def test_independent_keys_and_feeds_reserve_independently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                outcomes = [
                    earnings_reactions.reserve_feed_delivery(
                        feed,
                        key,
                        symbol,
                        attempt_id=f"{feed}-{symbol}",
                    )[1]
                    for feed, key, symbol in (
                        ("public", "key-one", "ONE"),
                        ("public", "key-two", "TWO"),
                        ("private", "key-one", "ONE"),
                    )
                ]
                persisted = earnings_reactions.load_state()

        self.assertEqual(
            outcomes,
            [earnings_reactions.FEED_DELIVERY_RESERVED] * 3,
        )
        self.assertEqual(set(persisted["public"]), {"key-one", "key-two"})
        self.assertEqual(set(persisted["private"]), {"key-one"})

    def test_attempt_id_mismatch_cannot_overwrite_reservation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                earnings_reactions.reserve_feed_delivery(
                    "public",
                    self.key,
                    "ACME",
                    attempt_id="current-attempt",
                )
                final_state, transitioned = (
                    earnings_reactions.transition_feed_delivery(
                        "public",
                        self.key,
                        "stale-attempt",
                        earnings_reactions.FEED_DELIVERY_CONFIRMED,
                    )
                )

        self.assertFalse(transitioned)
        self.assertEqual(
            final_state["public"][self.key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_RESERVED,
        )

    def test_force_reclaims_confirmed_but_never_reserved_or_unknown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            for starting_status, expected in (
                (earnings_reactions.FEED_DELIVERY_CONFIRMED, "reserved"),
                (earnings_reactions.FEED_DELIVERY_RESERVED, "reserved"),
                (earnings_reactions.FEED_DELIVERY_UNKNOWN, "unknown"),
                (earnings_reactions.FEED_DELIVERY_FAILED, "reserved"),
            ):
                state = self.empty_state()
                if starting_status == earnings_reactions.FEED_DELIVERY_CONFIRMED:
                    state["public"][self.key] = {
                        "symbol": "ACME",
                        "posted_at": "2026-08-07T08:00:00-04:00",
                    }
                else:
                    state["public"][self.key] = {
                        "delivery_status": starting_status,
                        "feed": "public",
                        "report_key": self.key,
                        "symbol": "ACME",
                        "delivery_attempt_id": "old-attempt",
                        "reserved_at": "2026-08-08T12:00:00-04:00",
                    }
                self.write_state(state_path, state)
                with patch.object(earnings_reactions, "STATE_FILE", state_path):
                    _state, outcome, attempt_id = (
                        earnings_reactions.reserve_feed_delivery(
                            "public",
                            self.key,
                            "ACME",
                            force=True,
                            attempt_id="new-attempt",
                        )
                    )
                self.assertEqual(outcome, expected)
                self.assertEqual(
                    attempt_id,
                    "new-attempt"
                    if starting_status in {"confirmed", "failed"}
                    else None,
                )

    def test_malformed_records_never_reach_either_feed_even_with_force(self):
        malformed_records = (
            {},
            {"symbol": "ACME"},
            {"symbol": "ACME", "posted_at": "not-a-timestamp"},
            {
                "symbol": "ACME",
                "posted_at": "2026-08-07T08:00:00-04:00",
                "delivery_status": None,
            },
        )
        for malformed in malformed_records:
            with self.subTest(
                record=malformed
            ), tempfile.TemporaryDirectory() as temp_dir:
                state_path = Path(temp_dir) / "state.json"
                state = self.empty_state()
                state["private"][self.key] = dict(malformed)
                state["public"][self.key] = dict(malformed)
                self.write_state(state_path, state)
                send_private = Mock()
                send_public = Mock()
                self.run_main(
                    self.main_patches(
                        state_path,
                        send_private,
                        send_public,
                        force=True,
                    )
                )
                persisted = json.loads(state_path.read_text(encoding="utf-8"))

            send_private.assert_not_called()
            send_public.assert_not_called()
            self.assertEqual(
                persisted["private"][self.key],
                state["private"][self.key],
            )
            self.assertEqual(
                persisted["public"][self.key],
                state["public"][self.key],
            )

    def test_reservation_persistence_failure_prevents_discord_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            send_private = Mock()
            send_public = Mock()
            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
                private=False,
            )
            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "reserve_feed_delivery",
                        side_effect=EarningsStateError("reservation failed"),
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))
                with self.assertRaises(EarningsStateError):
                    earnings_reactions.main()

        send_private.assert_not_called()
        send_public.assert_not_called()

    def test_public_failures_persist_definite_and_ambiguous_outcomes(self):
        failures = (
            (
                RuntimeError("chart generation failed"),
                earnings_reactions.FEED_DELIVERY_FAILED,
            ),
            (
                earnings_reactions.DefiniteDeliveryError("rejected"),
                earnings_reactions.FEED_DELIVERY_FAILED,
            ),
            (
                urllib.error.URLError("connection lost"),
                earnings_reactions.FEED_DELIVERY_UNKNOWN,
            ),
            (
                TimeoutError("timed out"),
                earnings_reactions.FEED_DELIVERY_UNKNOWN,
            ),
            (
                asyncio.CancelledError(),
                earnings_reactions.FEED_DELIVERY_UNKNOWN,
            ),
        )
        for failure, expected_status in failures:
            with self.subTest(failure=type(failure).__name__):
                with tempfile.TemporaryDirectory() as temp_dir:
                    state_path = Path(temp_dir) / "state.json"
                    self.write_state(state_path)
                    send_public = Mock(side_effect=failure)
                    patches = self.main_patches(
                        state_path,
                        Mock(),
                        send_public,
                        private=False,
                    )
                    with self.assertRaises(type(failure)):
                        self.run_main(patches)
                    persisted = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )

                self.assertEqual(send_public.call_count, 1)
                self.assertEqual(
                    persisted["public"][self.key]["delivery_status"],
                    expected_status,
                )

    def test_successful_main_deliveries_confirm_both_feeds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            send_private = Mock(return_value="private-message")
            send_public = Mock(return_value="public-message")
            self.run_main(
                self.main_patches(state_path, send_private, send_public)
            )
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        send_private.assert_called_once()
        send_public.assert_called_once()
        for feed, message_id in (
            ("private", "private-message"),
            ("public", "public-message"),
        ):
            self.assertEqual(
                persisted[feed][self.key]["delivery_status"],
                earnings_reactions.FEED_DELIVERY_CONFIRMED,
            )
            self.assertEqual(
                persisted[feed][self.key]["discord_message_id"],
                message_id,
            )

    def test_public_webhook_returns_message_id_when_discord_provides_one(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"id":"public-message"}'
        response.__enter__.return_value = response
        with patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            return_value=response,
        ):
            message_id = earnings_reactions.send_discord_message(
                "https://example.invalid/webhook?wait=true",
                "message",
                "bot",
            )

        self.assertEqual(message_id, "public-message")

    def test_private_queue_persistence_failure_becomes_unknown_and_blocks_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            send_private = Mock(
                side_effect=EarningsStateError("queue persistence failed")
            )
            send_public = Mock()
            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
                public=False,
            )
            with self.assertRaises(EarningsStateError):
                self.run_main(patches)
            first_state = json.loads(state_path.read_text(encoding="utf-8"))

            self.run_main(
                self.main_patches(
                    state_path,
                    send_private,
                    send_public,
                    public=False,
                    force=True,
                )
            )
            final_state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(send_private.call_count, 1)
        self.assertEqual(
            first_state["private"][self.key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_UNKNOWN,
        )
        self.assertEqual(final_state, first_state)

    def test_private_success_confirmation_failure_leaves_reservation_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            send_private = Mock(return_value="private-message")
            send_public = Mock()
            original_transition = earnings_reactions.transition_feed_delivery

            def fail_private_confirmation(
                feed,
                key,
                attempt_id,
                status,
                **kwargs,
            ):
                if (
                    feed == "private"
                    and status == earnings_reactions.FEED_DELIVERY_CONFIRMED
                ):
                    raise EarningsStateError("confirmation failed")
                return original_transition(
                    feed,
                    key,
                    attempt_id,
                    status,
                    **kwargs,
                )

            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
                public=False,
            )
            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "transition_feed_delivery",
                        side_effect=fail_private_confirmation,
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))
                with self.assertRaises(EarningsStateError):
                    earnings_reactions.main()

            reserved_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.run_main(
                self.main_patches(
                    state_path,
                    send_private,
                    send_public,
                    public=False,
                    force=True,
                )
            )

        self.assertEqual(send_private.call_count, 1)
        self.assertEqual(
            reserved_state["private"][self.key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_RESERVED,
        )

    def test_private_test_uses_separate_key_and_blocks_ambiguous_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            self.write_state(state_path)
            send_private = Mock(
                side_effect=earnings_reactions.AmbiguousDeliveryError(
                    "unclear test delivery"
                )
            )
            with patch.object(
                earnings_reactions,
                "STATE_FILE",
                state_path,
            ), patch.object(
                earnings_reactions,
                "private_test_candidate",
                return_value=self.candidate,
            ), patch.object(
                earnings_reactions,
                "send_private_review_with_chart",
                send_private,
            ):
                with self.assertRaises(
                    earnings_reactions.AmbiguousDeliveryError
                ):
                    earnings_reactions.run_private_test()
                with self.assertRaisesRegex(RuntimeError, "reconcile"):
                    earnings_reactions.run_private_test()

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        test_key = "private-test:" + self.key
        self.assertEqual(send_private.call_count, 1)
        self.assertEqual(
            persisted["private"][test_key]["delivery_status"],
            earnings_reactions.FEED_DELIVERY_UNKNOWN,
        )
        self.assertNotIn(self.key, persisted["private"])


if __name__ == "__main__":
    unittest.main()
