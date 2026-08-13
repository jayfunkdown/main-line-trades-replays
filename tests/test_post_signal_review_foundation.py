import copy
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


class PostSignalReviewRecordTests(unittest.TestCase):
    def build_record(self, **updates):
        values = {
            "source": earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_EARNINGS,
            "source_record_id": "earnings-token",
            "signals_channel_id": "100",
            "signals_message_id": "900",
            "symbol": "ROAD",
            "trade_direction": "short",
            "trade_thesis": "Weekly resistance rejection.",
            "original_chart_filename": "charts/ROAD_weekly.png",
            "sent_at": "2026-08-11T09:30:00-04:00",
            "reference_level": 120.0,
        }
        values.update(updates)
        return earnings_reactions.build_post_signal_review_record(**values)

    def test_builder_creates_complete_scheduled_record(self):
        record = self.build_record()

        self.assertTrue(
            earnings_reactions.is_valid_post_signal_review_record(record)
        )
        self.assertEqual(record["review_id"], "900")
        self.assertEqual(record["signals_message_id"], "900")
        self.assertEqual(record["trade_direction"], "short")
        self.assertEqual(record["original_chart_filename"], "ROAD_weekly.png")
        self.assertEqual(record["reference_level"], 120.0)
        self.assertEqual(record["review_status"], "scheduled")
        self.assertEqual(record["review_cycle"], 1)
        self.assertEqual(
            record["review_due_at"],
            "2026-09-11T09:30:00-04:00",
        )

    def test_calendar_month_clamps_month_end_and_preserves_timezone(self):
        cases = (
            ("2026-01-31T15:45:00-05:00", "2026-02-28T15:45:00-05:00"),
            ("2028-01-31T15:45:00-05:00", "2028-02-29T15:45:00-05:00"),
            ("2026-12-31T15:45:00-05:00", "2027-01-31T15:45:00-05:00"),
        )

        for sent_at, expected in cases:
            with self.subTest(sent_at=sent_at):
                self.assertEqual(
                    earnings_reactions.one_calendar_month_after(sent_at),
                    expected,
                )

    def test_builder_rejects_incomplete_or_unsafe_metadata(self):
        invalid_updates = (
            {"source": "legacy"},
            {"source_record_id": ""},
            {"signals_channel_id": True},
            {"signals_message_id": "not-a-snowflake"},
            {"symbol": ""},
            {"trade_direction": None},
            {"trade_direction": "buy"},
            {"trade_thesis": ""},
            {"original_chart_filename": ""},
            {"sent_at": "not-a-date"},
        )

        for updates in invalid_updates:
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    self.build_record(**updates)

    def test_store_is_idempotent_and_conflicts_fail_closed(self):
        record = self.build_record()
        state = {"post_signal_reviews": {}}

        self.assertTrue(
            earnings_reactions.store_post_signal_review(state, record)
        )
        self.assertTrue(
            earnings_reactions.store_post_signal_review(state, record)
        )
        self.assertEqual(len(state["post_signal_reviews"]), 1)

        conflict = copy.deepcopy(record)
        conflict["trade_thesis"] = "Conflicting thesis"
        self.assertFalse(
            earnings_reactions.store_post_signal_review(state, conflict)
        )
        self.assertEqual(
            state["post_signal_reviews"]["900"]["trade_thesis"],
            "Weekly resistance rejection.",
        )

    def test_due_check_requires_scheduled_valid_record_and_aware_time(self):
        record = self.build_record()
        self.assertFalse(
            earnings_reactions.is_due_post_signal_review(
                record,
                earnings_reactions.datetime.fromisoformat(
                    "2026-09-11T09:29:59-04:00"
                ),
            )
        )
        self.assertTrue(
            earnings_reactions.is_due_post_signal_review(
                record,
                earnings_reactions.datetime.fromisoformat(
                    "2026-09-11T09:30:00-04:00"
                ),
            )
        )
        record["review_status"] = earnings_reactions.POST_SIGNAL_REVIEW_DRAFT_READY
        self.assertFalse(
            earnings_reactions.is_due_post_signal_review(
                record,
                earnings_reactions.datetime.fromisoformat(
                    "2026-09-12T09:30:00-04:00"
                ),
            )
        )

    def test_review_card_preserves_direction_thesis_and_verification_gate(self):
        record = self.build_record()
        record["current_price"] = 108.0
        message = earnings_reactions.build_post_signal_review_message(
            record,
            earnings_reactions.datetime.fromisoformat(
                "2026-09-11T09:30:00-04:00"
            ),
            private=True,
        )
        self.assertIn("ROAD — Short", message)
        self.assertIn("Weekly resistance rejection.", message)
        self.assertIn("Still Active", message)
        self.assertIn("Original level", message)
        self.assertIn("Gain:** +10.00%", message)
        self.assertIn("requires staff verification", message)
        self.assertIn("not a claim of realized profit", message)

    def test_performance_is_direction_adjusted_from_the_single_reference_level(self):
        self.assertEqual(
            earnings_reactions.direction_adjusted_performance(100, 90, "short"),
            10.0,
        )
        self.assertEqual(
            earnings_reactions.direction_adjusted_performance(100, 90, "long"),
            -10.0,
        )
        self.assertIsNone(
            earnings_reactions.direction_adjusted_performance(None, 90, "long")
        )

    def test_combined_review_chart_is_one_stacked_png(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.png"
            updated = root / "updated.png"
            output = root / "combined.png"
            Image.new("RGB", (800, 400), "#112233").save(original)
            Image.new("RGB", (400, 300), "#334455").save(updated)

            result = earnings_reactions.combine_post_signal_review_charts(
                original,
                updated,
                output_path=output,
            )

            self.assertEqual(result, output)
            with Image.open(output) as combined:
                self.assertEqual(combined.format, "PNG")
                self.assertEqual(combined.width, 1600)
                self.assertLess(combined.height, combined.width)

    def test_weekly_aggregation_supports_matching_long_history(self):
        daily = []
        start = earnings_reactions.datetime(2024, 1, 1, tzinfo=earnings_reactions.timezone.utc)
        for week in range(140):
            daily.append(
                {
                    "timestamp": (start + earnings_reactions.timedelta(days=week * 7)).timestamp(),
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "volume": 100.0,
                }
            )
        self.assertEqual(
            len(earnings_reactions.aggregate_weekly_candles(daily, max_weeks=130)),
            130,
        )

    def test_publish_acknowledgement_happens_before_claim_in_source(self):
        source = Path(earnings_reactions.__file__).read_text(encoding="utf-8")
        publish = source[source.index("        async def publish("):source.index("        @discord.ui.button(\n            label=\"Review in 1 Month\"")]
        self.assertLess(
            publish.index("await defer_ephemeral_response(interaction)"),
            publish.index("claim_post_signal_review_action("),
        )
        self.assertIn("await draft_message.delete()", publish)


class PostSignalReviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.record = PostSignalReviewRecordTests().build_record()
        self.state = {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
            "manual_signal_drafts": {},
            "post_signal_reviews": {"900": copy.deepcopy(self.record)},
        }

    def transactional_update(self, mutation):
        latest = copy.deepcopy(self.state)
        result = mutation(latest)
        self.state = latest
        return copy.deepcopy(latest), result

    def patch_update(self):
        return patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update,
        )

    def claim_due(self):
        with self.patch_update():
            _state, outcome = earnings_reactions.claim_due_post_signal_review(
                "900",
                "draft-attempt",
                "2026-09-11T09:30:00-04:00",
            )
        self.assertEqual(outcome, "claimed")

    def mark_draft_ready(self, verified=False):
        self.claim_due()
        with self.patch_update():
            _state, outcome = earnings_reactions.transition_post_signal_review(
                "900",
                "draft-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_DRAFT_READY,
                "2026-09-11T09:31:00-04:00",
                updates={
                    "draft_channel_id": "700",
                    "draft_message_id": "800",
                    "comparison_chart_filename": "ROAD_review.png",
                    "comparison_chart_verified": verified,
                },
            )
        self.assertEqual(outcome, "transitioned")

    def test_due_claim_is_single_winner(self):
        self.claim_due()
        with self.patch_update():
            _state, outcome = earnings_reactions.claim_due_post_signal_review(
                "900",
                "second-attempt",
                "2026-09-11T09:30:01-04:00",
            )
        self.assertEqual(outcome, "not_due")
        self.assertEqual(
            self.state["post_signal_reviews"]["900"]["review_attempt_id"],
            "draft-attempt",
        )

    def test_publish_requires_comparison_chart_verification(self):
        self.mark_draft_ready(verified=False)
        with self.patch_update():
            _state, outcome = earnings_reactions.claim_post_signal_review_action(
                "900",
                "800",
                "publish-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
                "2026-09-11T09:35:00-04:00",
            )
        self.assertEqual(outcome, "verification_required")
        self.assertEqual(
            self.state["post_signal_reviews"]["900"]["review_status"],
            earnings_reactions.POST_SIGNAL_REVIEW_DRAFT_READY,
        )

    def test_verified_combined_chart_can_publish_without_edit_control(self):
        self.mark_draft_ready(verified=True)
        with self.patch_update():
            _state, outcome = earnings_reactions.claim_post_signal_review_action(
                "900",
                "800",
                "publish-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
                "2026-09-11T09:35:00-04:00",
            )
        self.assertEqual(outcome, "claimed")
        self.assertEqual(
            self.state["post_signal_reviews"]["900"]["review_status"],
            earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
        )

    def test_edit_can_verify_chart_and_set_outcome(self):
        self.mark_draft_ready()
        with self.patch_update():
            _state, outcome = earnings_reactions.edit_post_signal_review(
                "900",
                "800",
                "2026-09-11T09:35:00-04:00",
                outcome="worked",
                summary="The original rejection level held and price moved lower.",
                comparison_chart_verified=True,
            )
        self.assertEqual(outcome, "updated")
        record = self.state["post_signal_reviews"]["900"]
        self.assertEqual(record["proposed_outcome"], "worked")
        self.assertTrue(record["comparison_chart_verified"])
        self.assertEqual(record["review_history"][-1]["action"], "edited")

    def test_publish_and_defer_cannot_both_win(self):
        self.mark_draft_ready(verified=True)
        with self.patch_update():
            _state, publish = earnings_reactions.claim_post_signal_review_action(
                "900",
                "800",
                "publish-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
                "2026-09-11T09:35:00-04:00",
            )
            _state, deferred = earnings_reactions.defer_post_signal_review(
                "900",
                "800",
                "2026-09-11T09:35:01-04:00",
            )
        self.assertEqual(publish, "claimed")
        self.assertEqual(deferred, "unavailable")
        self.assertEqual(
            self.state["post_signal_reviews"]["900"]["review_history"][-1]["action"],
            "publish_started",
        )

    def test_deferral_schedules_one_calendar_month_and_preserves_audit(self):
        self.mark_draft_ready()
        with self.patch_update():
            _state, outcome = earnings_reactions.defer_post_signal_review(
                "900",
                "800",
                "2026-09-30T10:00:00-04:00",
            )
        self.assertEqual(outcome, "deferred")
        record = self.state["post_signal_reviews"]["900"]
        self.assertEqual(record["review_due_at"], "2026-10-30T10:00:00-04:00")
        self.assertEqual(record["review_cycle"], 2)
        self.assertEqual(record["deferral_count"], 1)
        self.assertEqual(record["review_history"][-1]["action"], "deferred")
        self.assertNotIn("draft_message_id", record)

    def test_stale_attempt_cannot_confirm_publication(self):
        self.mark_draft_ready(verified=True)
        with self.patch_update():
            earnings_reactions.claim_post_signal_review_action(
                "900",
                "800",
                "publish-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
                "2026-09-11T09:35:00-04:00",
            )
            _state, outcome = earnings_reactions.transition_post_signal_review(
                "900",
                "stale-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHED,
                "2026-09-11T09:36:00-04:00",
                updates={"public_channel_id": "600", "public_message_id": "601"},
            )
        self.assertEqual(outcome, "stale")
        self.assertEqual(
            self.state["post_signal_reviews"]["900"]["review_status"],
            earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
        )

    def test_publication_confirmation_requires_a_message_id(self):
        self.mark_draft_ready(verified=True)
        with self.patch_update():
            earnings_reactions.claim_post_signal_review_action(
                "900",
                "800",
                "publish-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
                "2026-09-11T09:35:00-04:00",
            )
            _state, outcome = earnings_reactions.transition_post_signal_review(
                "900",
                "publish-attempt",
                earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHED,
                "2026-09-11T09:36:00-04:00",
                updates={"public_channel_id": "600", "public_message_id": None},
            )
        self.assertEqual(outcome, "invalid")
        self.assertEqual(
            self.state["post_signal_reviews"]["900"]["review_status"],
            earnings_reactions.POST_SIGNAL_REVIEW_PUBLISHING,
        )


class TransactionalReviewCaptureTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
            "manual_signal_drafts": {},
            "post_signal_reviews": {},
        }

    def transactional_update(self, mutation):
        latest = copy.deepcopy(self.state)
        mutation(latest)
        self.state = latest
        return copy.deepcopy(latest)

    def review_record(self, **updates):
        values = {
            "source": earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_MANUAL,
            "source_record_id": "draft",
            "signals_channel_id": "100",
            "signals_message_id": "900",
            "symbol": "ES",
            "trade_direction": "long",
            "trade_thesis": "Hold above weekly support.",
            "original_chart_filename": "chart.png",
            "sent_at": "2026-08-11T10:00:00-04:00",
        }
        values.update(updates)
        return earnings_reactions.build_post_signal_review_record(**values)

    @staticmethod
    def manual_draft():
        return {
            "draft_id": "draft",
            "draft_message_id": "400",
            "draft_channel_id": "300",
            "creator_user_id": "1",
            "instrument": "ES",
            "trade_thesis": "Hold above weekly support.",
            "trade_direction": "long",
            "timeframe": "1W",
            "setup_name": "Support retest",
            "chart": {
                "filename": "chart.png",
                "content_type": "image/png",
                "attachment_id": "500",
            },
            "created_at": "2026-08-11T09:55:00-04:00",
            "updated_at": "2026-08-11T09:59:00-04:00",
            "delivery_status": "sending",
            "delivery_attempt_id": "manual-attempt",
            "canceled": False,
        }

    def test_manual_confirmation_and_review_schedule_commit_together(self):
        self.state["manual_signal_drafts"]["draft"] = self.manual_draft()
        review = self.review_record()

        with patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update,
        ):
            _state, outcome = (
                earnings_reactions.transition_manual_signal_delivery(
                    "draft",
                    "manual-attempt",
                    earnings_reactions.MANUAL_SIGNAL_SENT,
                    "2026-08-11T10:00:00-04:00",
                    signals_message_id="900",
                    post_signal_review=review,
                )
            )

        self.assertEqual(outcome, "transitioned")
        draft = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(draft["delivery_status"], "sent")
        self.assertEqual(draft["signals_message_id"], "900")
        self.assertEqual(self.state["post_signal_reviews"], {"900": review})

    def test_invalid_manual_review_does_not_confirm_delivery(self):
        self.state["manual_signal_drafts"]["draft"] = self.manual_draft()
        invalid_review = self.review_record()
        invalid_review["trade_direction"] = None

        with patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update,
        ):
            _state, outcome = (
                earnings_reactions.transition_manual_signal_delivery(
                    "draft",
                    "manual-attempt",
                    earnings_reactions.MANUAL_SIGNAL_SENT,
                    "2026-08-11T10:00:00-04:00",
                    signals_message_id="900",
                    post_signal_review=invalid_review,
                )
            )

        self.assertEqual(outcome, "invalid")
        self.assertEqual(
            self.state["manual_signal_drafts"]["draft"]["delivery_status"],
            "sending",
        )
        self.assertEqual(self.state["post_signal_reviews"], {})

    def test_manual_review_must_match_delivered_message_and_source(self):
        mismatches = (
            {"source": earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_EARNINGS},
            {"source_record_id": "another-draft"},
            {"signals_message_id": "901"},
        )

        for updates in mismatches:
            with self.subTest(updates=updates):
                self.state["manual_signal_drafts"]["draft"] = (
                    self.manual_draft()
                )
                self.state["post_signal_reviews"] = {}
                review = self.review_record(**updates)
                with patch.object(
                    earnings_reactions,
                    "update_state",
                    side_effect=self.transactional_update,
                ):
                    _state, outcome = (
                        earnings_reactions.transition_manual_signal_delivery(
                            "draft",
                            "manual-attempt",
                            earnings_reactions.MANUAL_SIGNAL_SENT,
                            "2026-08-11T10:00:00-04:00",
                            signals_message_id="900",
                            post_signal_review=review,
                        )
                    )

                self.assertEqual(outcome, "invalid")
                self.assertEqual(
                    self.state["manual_signal_drafts"]["draft"]["delivery_status"],
                    "sending",
                )
                self.assertEqual(self.state["post_signal_reviews"], {})

    def test_earnings_confirmation_and_review_schedule_commit_together(self):
        self.state["signal_queue"]["earnings-token"] = {
            "sent_to_signals": False,
            "delivery_status": "sending",
            "delivery_attempt_id": "earnings-attempt",
        }
        review = self.review_record(
            source=earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_EARNINGS,
            source_record_id="earnings-token",
            symbol="ROAD",
            trade_direction="short",
            trade_thesis="Weekly resistance rejection.",
            original_chart_filename="ROAD.png",
        )

        with patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update,
        ):
            _state, outcome = earnings_reactions.transition_signal_delivery(
                "earnings-token",
                "earnings-attempt",
                earnings_reactions.SIGNAL_DELIVERY_SENT,
                "2026-08-11T10:00:00-04:00",
                updates={"signals_message_id": "900"},
                post_signal_review=review,
            )

        self.assertEqual(outcome, "transitioned")
        item = self.state["signal_queue"]["earnings-token"]
        self.assertTrue(item["sent_to_signals"])
        self.assertEqual(item["delivery_status"], "sent")
        self.assertEqual(item["signals_message_id"], "900")
        self.assertEqual(self.state["post_signal_reviews"], {"900": review})

    def test_conflicting_review_blocks_confirmation_and_duplicate_window(self):
        self.state["signal_queue"]["earnings-token"] = {
            "sent_to_signals": False,
            "delivery_status": "sending",
            "delivery_attempt_id": "earnings-attempt",
        }
        original = self.review_record(
            source=earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_EARNINGS,
            source_record_id="another-token",
        )
        self.state["post_signal_reviews"]["900"] = original
        conflict = self.review_record(
            source=earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_EARNINGS,
            source_record_id="earnings-token",
        )

        with patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update,
        ):
            _state, outcome = earnings_reactions.transition_signal_delivery(
                "earnings-token",
                "earnings-attempt",
                earnings_reactions.SIGNAL_DELIVERY_SENT,
                "2026-08-11T10:00:00-04:00",
                updates={"signals_message_id": "900"},
                post_signal_review=conflict,
            )

        self.assertEqual(outcome, "invalid")
        item = self.state["signal_queue"]["earnings-token"]
        self.assertFalse(item["sent_to_signals"])
        self.assertEqual(item["delivery_status"], "sending")
        self.assertEqual(self.state["post_signal_reviews"]["900"], original)

    def test_earnings_review_must_match_queue_token_and_delivered_message(self):
        mismatches = (
            {"source": earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_MANUAL},
            {"source_record_id": "another-token"},
            {"signals_message_id": "901"},
        )

        for updates in mismatches:
            with self.subTest(updates=updates):
                self.state["signal_queue"]["earnings-token"] = {
                    "sent_to_signals": False,
                    "delivery_status": "sending",
                    "delivery_attempt_id": "earnings-attempt",
                }
                self.state["post_signal_reviews"] = {}
                review_values = {
                    "source": (
                        earnings_reactions.POST_SIGNAL_REVIEW_SOURCE_EARNINGS
                    ),
                    "source_record_id": "earnings-token",
                }
                review_values.update(updates)
                review = self.review_record(**review_values)
                with patch.object(
                    earnings_reactions,
                    "update_state",
                    side_effect=self.transactional_update,
                ):
                    _state, outcome = (
                        earnings_reactions.transition_signal_delivery(
                            "earnings-token",
                            "earnings-attempt",
                            earnings_reactions.SIGNAL_DELIVERY_SENT,
                            "2026-08-11T10:00:00-04:00",
                            updates={"signals_message_id": "900"},
                            post_signal_review=review,
                        )
                    )

                self.assertEqual(outcome, "invalid")
                item = self.state["signal_queue"]["earnings-token"]
                self.assertFalse(item["sent_to_signals"])
                self.assertEqual(item["delivery_status"], "sending")
                self.assertEqual(self.state["post_signal_reviews"], {})


if __name__ == "__main__":
    unittest.main()
