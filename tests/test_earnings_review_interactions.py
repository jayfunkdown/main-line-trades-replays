import asyncio
import copy
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from tests import test_earnings_characterization_batch2 as batch2

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


class StatefulResponse:
    def __init__(self, *, acknowledged=False):
        self.acknowledged = acknowledged
        self.modal = None
        self.send_message = AsyncMock(side_effect=self._send_message)
        self.send_modal = AsyncMock(side_effect=self._send_modal)
        self.defer = AsyncMock(side_effect=self._defer)

    def is_done(self):
        return self.acknowledged

    async def _send_message(self, *args, **kwargs):
        self.acknowledged = True

    async def _send_modal(self, modal):
        self.modal = modal
        self.acknowledged = True

    async def _defer(self, *args, **kwargs):
        self.acknowledged = True


class ConcurrentAttachmentBarrier:
    filename = "chart.png"
    content_type = "image/png"

    def __init__(self):
        self.arrivals = 0
        self.ready = asyncio.Event()

    async def to_file(self, *, filename):
        self.arrivals += 1
        if self.arrivals == 2:
            self.ready.set()
        await self.ready.wait()
        return f"synthetic-discord-file:{filename}"


class EarningsSignalMessageFormattingTests(unittest.TestCase):
    @staticmethod
    def candidate():
        return {
            "symbol": "PAA",
            "move_percent": -3.02,
            "current_price": 22.81,
            "eps_surprise": 12.9,
            "revenue_surprise": 40.2,
            "eps_direction": "beat",
            "revenue_direction": "beat",
        }

    def test_signal_divider_leads_the_next_trade_signal(self):
        message = earnings_reactions.build_signal_message(
            self.candidate(),
            "Weekly continuation coming up",
        )

        self.assertTrue(
            message.startswith(
                f"{earnings_reactions.DIVIDER}\n\n# 📈 Trade Signal"
            )
        )
        self.assertEqual(message.count(earnings_reactions.DIVIDER), 1)

    def test_signal_divider_is_not_clumped_above_the_chart(self):
        message = earnings_reactions.build_signal_message(
            self.candidate(),
            "Weekly continuation coming up",
        )

        self.assertIn(
            "Weekly continuation coming up\n\n📊 **Trade Chart**",
            message,
        )
        self.assertNotIn(
            (
                "Weekly continuation coming up\n\n"
                f"{earnings_reactions.DIVIDER}\n\n📊 **Trade Chart**"
            ),
            message,
        )


class EarningsReviewInteractionAuthorizationTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    @staticmethod
    def user(user_id, *, administrator=False, manage_messages=False):
        return SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(
                administrator=administrator,
                manage_messages=manage_messages,
            ),
        )

    @staticmethod
    def guild(owner_id=1):
        return SimpleNamespace(owner_id=owner_id)

    @staticmethod
    def signal_state(*, message_id="321", channel_id="200"):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {
                "token": {
                    "review_message_id": message_id,
                    "review_channel_id": channel_id,
                    "sent_to_signals": False,
                    "candidate": {
                        "symbol": "ACME",
                        "move_percent": 10.0,
                        "current_price": 25.0,
                        "eps_surprise": 5.0,
                        "revenue_surprise": 4.0,
                        "eps_direction": "beat",
                        "revenue_direction": "beat",
                    },
                }
            },
        }

    async def start_bot(self):
        signals_channel = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=555))
        )
        review_channel = SimpleNamespace(fetch_message=AsyncMock())
        client = batch2.FakeDiscordClient(signals_channel, review_channel)
        review_channel.fetch_message.return_value = self.review_message(client)

        def required_environment(name):
            return {
                "DISCORD_BOT_TOKEN": "synthetic-token",
                "SIGNALS_CHANNEL_ID": "100",
                "EARNINGS_REVIEW_WEBHOOK": "synthetic-webhook",
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
            return_value=self.signal_state(),
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

        return client, signals_channel, review_channel

    @staticmethod
    def review_message(
        client,
        *,
        message_id=321,
        channel_id=200,
        author_id=None,
        message_type=discord.MessageType.default,
    ):
        author = (
            client.user
            if author_id is None
            else SimpleNamespace(id=author_id)
        )
        return SimpleNamespace(
            id=message_id,
            content="**ACME**",
            author=author,
            channel=SimpleNamespace(id=channel_id),
            type=message_type,
            edit=AsyncMock(),
        )

    async def click_button(
        self,
        client,
        *,
        user,
        guild,
        state=None,
        channel_id=200,
        message_id=321,
        author_id=None,
        message_type=discord.MessageType.default,
        acknowledged=False,
        include_message=True,
    ):
        response = StatefulResponse(acknowledged=acknowledged)
        followup = SimpleNamespace(send=AsyncMock())
        interaction = SimpleNamespace(
            guild=guild,
            user=user,
            channel_id=channel_id,
            message=(
                self.review_message(
                    client,
                    message_id=message_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    message_type=message_type,
                )
                if include_message
                else None
            ),
            response=response,
            followup=followup,
        )

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=state if state is not None else self.signal_state(),
        ), patch.object(
            earnings_reactions,
            "update_state",
        ) as update_state, redirect_stdout(StringIO()):
            await client.persistent_view.children[0].callback(interaction)

        update_state.assert_not_called()

        return response.modal, interaction

    async def valid_modal(self, client, *, user):
        modal, _interaction = await self.click_button(
            client,
            user=user,
            guild=self.guild(),
        )
        self.assertIsNotNone(modal)
        attachment = SimpleNamespace(
            filename="chart.png",
            content_type="image/png",
            to_file=AsyncMock(return_value="synthetic-discord-file"),
        )
        modal.trade_thesis._value = "Trade above resistance."
        modal.trade_chart._values = [attachment]
        return modal, attachment

    @staticmethod
    def modal_interaction(user, guild, *, channel_id=200, acknowledged=False):
        return SimpleNamespace(
            guild=guild,
            user=user,
            channel_id=channel_id,
            response=StatefulResponse(acknowledged=acknowledged),
            followup=SimpleNamespace(send=AsyncMock()),
            delete_original_response=AsyncMock(),
        )

    @staticmethod
    def transactional_update(state):
        def update(mutation):
            latest = copy.deepcopy(state)
            mutation(latest)
            state.clear()
            state.update(copy.deepcopy(latest))
            return copy.deepcopy(latest)

        return update

    def assert_ephemeral_rejection(self, interaction):
        response_send = interaction.response.send_message
        followup_send = interaction.followup.send
        self.assertEqual(
            response_send.await_count + followup_send.await_count,
            1,
        )
        sent = (
            response_send.await_args
            if response_send.await_count
            else followup_send.await_args
        )
        self.assertIs(sent.kwargs.get("ephemeral"), True)

    async def test_owner_administrator_and_manager_complete_valid_flow(self):
        authorized_users = (
            self.user(1),
            self.user(2, administrator=True),
            self.user(3, manage_messages=True),
        )

        for reviewer in authorized_users:
            with self.subTest(user_id=reviewer.id):
                client, signals_channel, review_channel = await self.start_bot()
                modal, attachment = await self.valid_modal(
                    client,
                    user=reviewer,
                )
                interaction = self.modal_interaction(
                    reviewer,
                    self.guild(),
                )
                state = self.signal_state()

                with patch.object(
                    earnings_reactions,
                    "load_state",
                    return_value=state,
                ), patch.object(
                    earnings_reactions,
                    "update_state",
                    side_effect=self.transactional_update(state),
                ) as update_state, redirect_stdout(StringIO()):
                    await modal.on_submit(interaction)

                attachment.to_file.assert_awaited_once()
                signals_channel.send.assert_awaited_once()
                self.assertEqual(update_state.call_count, 2)
                self.assertTrue(
                    state["signal_queue"]["token"]["sent_to_signals"]
                )
                self.assertEqual(
                    state["signal_queue"]["token"]["delivery_status"],
                    "sent",
                )
                self.assertEqual(
                    state["signal_queue"]["token"]["signals_message_id"],
                    "555",
                )
                self.assertGreaterEqual(
                    review_channel.fetch_message.await_count,
                    2,
                )

    def test_candidate_validator_requires_delivery_safe_fields(self):
        candidate = self.signal_state()["signal_queue"]["token"]["candidate"]
        self.assertTrue(
            earnings_reactions.is_valid_signal_candidate(candidate)
        )

        for field in ("eps_direction", "revenue_direction"):
            with self.subTest(missing_field=field):
                malformed = copy.deepcopy(candidate)
                malformed.pop(field)
                self.assertFalse(
                    earnings_reactions.is_valid_signal_candidate(malformed)
                )

        for field in (
            "move_percent",
            "current_price",
            "eps_surprise",
            "revenue_surprise",
        ):
            for invalid_value in ("10.5", True):
                with self.subTest(field=field, value=invalid_value):
                    malformed = copy.deepcopy(candidate)
                    malformed[field] = invalid_value
                    self.assertFalse(
                        earnings_reactions.is_valid_signal_candidate(
                            malformed
                        )
                    )

    def test_legacy_status_and_successful_state_transitions(self):
        self.assertEqual(
            earnings_reactions.signal_delivery_status(
                {"sent_to_signals": False}
            ),
            "ready",
        )
        self.assertEqual(
            earnings_reactions.signal_delivery_status(
                {"sent_to_signals": True}
            ),
            "sent",
        )
        self.assertEqual(
            earnings_reactions.signal_delivery_status(
                {
                    "sent_to_signals": False,
                    "delivery_status": "unknown",
                }
            ),
            "unknown",
        )

        state = self.signal_state()
        with patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update(state),
        ):
            claimed_state, claim_outcome = (
                earnings_reactions.claim_signal_delivery(
                    "token",
                    "321",
                    "200",
                    "attempt-one",
                    "2026-08-06T10:00:00-04:00",
                )
            )
            sent_state, sent_outcome = (
                earnings_reactions.transition_signal_delivery(
                    "token",
                    "attempt-one",
                    "sent",
                    "2026-08-06T10:01:00-04:00",
                    updates={"signals_message_id": "555"},
                )
            )

        claimed_item = claimed_state["signal_queue"]["token"]
        self.assertEqual(claim_outcome, "claimed")
        self.assertEqual(claimed_item["delivery_status"], "sending")
        self.assertEqual(claimed_item["delivery_attempt_id"], "attempt-one")
        self.assertFalse(claimed_item["sent_to_signals"])

        sent_item = sent_state["signal_queue"]["token"]
        self.assertEqual(sent_outcome, "transitioned")
        self.assertEqual(sent_item["delivery_status"], "sent")
        self.assertTrue(sent_item["sent_to_signals"])
        self.assertEqual(sent_item["signals_message_id"], "555")

    def test_old_attempt_cannot_overwrite_a_new_claim(self):
        state = self.signal_state()
        with patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update(state),
        ):
            earnings_reactions.claim_signal_delivery(
                "token",
                "321",
                "200",
                "old-attempt",
                "2026-08-06T10:00:00-04:00",
            )
            earnings_reactions.transition_signal_delivery(
                "token",
                "old-attempt",
                "ready",
                "2026-08-06T10:01:00-04:00",
                error="definite_failure",
            )
            earnings_reactions.claim_signal_delivery(
                "token",
                "321",
                "200",
                "new-attempt",
                "2026-08-06T10:02:00-04:00",
            )
            final_state, outcome = (
                earnings_reactions.transition_signal_delivery(
                    "token",
                    "old-attempt",
                    "sent",
                    "2026-08-06T10:03:00-04:00",
                    updates={"signals_message_id": "stale"},
                )
            )

        item = final_state["signal_queue"]["token"]
        self.assertEqual(outcome, "mismatch")
        self.assertEqual(item["delivery_status"], "sending")
        self.assertEqual(item["delivery_attempt_id"], "new-attempt")
        self.assertNotIn("signals_message_id", item)

    async def test_async_lock_entries_cleanup_after_success_error_and_cancel(self):
        locks = earnings_reactions.ReviewMessageAsyncLocks()

        async with locks.hold("321"):
            self.assertEqual(locks.active_key_count, 1)
        self.assertEqual(locks.active_key_count, 0)

        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            async with locks.hold("321"):
                raise RuntimeError("synthetic failure")
        self.assertEqual(locks.active_key_count, 0)

        async with locks.hold("321"):
            waiting = asyncio.create_task(self._wait_for_lock(locks, "321"))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertEqual(locks.active_key_count, 1)

        self.assertEqual(locks.active_key_count, 0)

    @staticmethod
    async def _wait_for_lock(locks, key):
        async with locks.hold(key):
            return

    async def test_attachment_failure_returns_matching_attempt_to_ready(self):
        client, signals_channel, _review_channel = await self.start_bot()
        reviewer = self.user(1)
        modal, attachment = await self.valid_modal(client, user=reviewer)
        attachment.to_file.side_effect = OSError("synthetic conversion failure")
        interaction = self.modal_interaction(reviewer, self.guild())
        state = self.signal_state()

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=state,
        ), patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update(state),
        ) as update_state, redirect_stdout(StringIO()):
            await modal.on_submit(interaction)

        item = state["signal_queue"]["token"]
        attachment.to_file.assert_awaited_once()
        signals_channel.send.assert_not_awaited()
        self.assertEqual(update_state.call_count, 2)
        self.assertEqual(item["delivery_status"], "ready")
        self.assertFalse(item["sent_to_signals"])
        self.assertEqual(item["delivery_error"], "attachment_conversion_failed")
        self.assertEqual(len(item["delivery_attempt_id"]), 32)
        int(item["delivery_attempt_id"], 16)
        datetime.fromisoformat(item["delivery_started_at"])
        self.assertEqual(
            modal._submission_locks.active_key_count,
            0,
        )

    async def test_ambiguous_send_failures_become_unknown_and_block_retry(self):
        failures = (
            TimeoutError("synthetic timeout"),
            ConnectionError("synthetic connection loss"),
        )

        for failure in failures:
            with self.subTest(exception=type(failure).__name__):
                client, signals_channel, _review_channel = await self.start_bot()
                signals_channel.send.side_effect = failure
                reviewer = self.user(1)
                modal, attachment = await self.valid_modal(
                    client,
                    user=reviewer,
                )
                first_interaction = self.modal_interaction(
                    reviewer,
                    self.guild(),
                )
                second_interaction = self.modal_interaction(
                    reviewer,
                    self.guild(),
                )
                state = self.signal_state()

                with patch.object(
                    earnings_reactions,
                    "load_state",
                    side_effect=lambda: copy.deepcopy(state),
                ), patch.object(
                    earnings_reactions,
                    "update_state",
                    side_effect=self.transactional_update(state),
                ) as update_state, redirect_stdout(StringIO()):
                    await modal.on_submit(first_interaction)
                    await modal.on_submit(second_interaction)

                item = state["signal_queue"]["token"]
                attachment.to_file.assert_awaited_once()
                signals_channel.send.assert_awaited_once()
                self.assertEqual(update_state.call_count, 2)
                self.assertEqual(item["delivery_status"], "unknown")
                self.assertFalse(item["sent_to_signals"])
                self.assertEqual(
                    item["delivery_error"],
                    "discord_delivery_ambiguous",
                )
                self.assertIn(
                    "do not retry",
                    first_interaction.followup.send.await_args.args[0].lower(),
                )
                self.assertIn(
                    "reconciliation",
                    second_interaction.followup.send.await_args.args[0].lower(),
                )

    async def test_cancelled_discord_send_becomes_unknown_and_releases_lock(self):
        client, signals_channel, _review_channel = await self.start_bot()
        signals_channel.send.side_effect = asyncio.CancelledError()
        reviewer = self.user(1)
        modal, attachment = await self.valid_modal(client, user=reviewer)
        interaction = self.modal_interaction(reviewer, self.guild())
        state = self.signal_state()

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=state,
        ), patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update(state),
        ) as update_state, redirect_stdout(StringIO()):
            with self.assertRaises(asyncio.CancelledError):
                await modal.on_submit(interaction)

        item = state["signal_queue"]["token"]
        attachment.to_file.assert_awaited_once()
        signals_channel.send.assert_awaited_once()
        self.assertEqual(update_state.call_count, 2)
        self.assertEqual(item["delivery_status"], "unknown")
        self.assertEqual(item["delivery_error"], "discord_delivery_ambiguous")
        self.assertEqual(modal._submission_locks.active_key_count, 0)

    async def test_different_review_messages_deliver_independently(self):
        client, signals_channel, review_channel = await self.start_bot()
        reviewer = self.user(1)
        state = self.signal_state()
        second_item = copy.deepcopy(state["signal_queue"]["token"])
        second_item["review_message_id"] = "654"
        state["signal_queue"]["token-two"] = second_item

        modal_one, _interaction_one = await self.click_button(
            client,
            user=reviewer,
            guild=self.guild(),
            state=state,
            message_id=321,
        )
        modal_two, _interaction_two = await self.click_button(
            client,
            user=reviewer,
            guild=self.guild(),
            state=state,
            message_id=654,
        )
        barrier = ConcurrentAttachmentBarrier()
        for modal in (modal_one, modal_two):
            self.assertIsNotNone(modal)
            modal.trade_thesis._value = "Trade above resistance."
            modal.trade_chart._values = [barrier]

        async def fetch_review_message(message_id):
            return self.review_message(client, message_id=message_id)

        review_channel.fetch_message.side_effect = fetch_review_message
        signals_channel.send.side_effect = (
            SimpleNamespace(id=901),
            SimpleNamespace(id=902),
        )

        with patch.object(
            earnings_reactions,
            "load_state",
            side_effect=lambda: copy.deepcopy(state),
        ), patch.object(
            earnings_reactions,
            "update_state",
            side_effect=self.transactional_update(state),
        ) as update_state, redirect_stdout(StringIO()):
            await asyncio.gather(
                modal_one.on_submit(
                    self.modal_interaction(reviewer, self.guild())
                ),
                modal_two.on_submit(
                    self.modal_interaction(reviewer, self.guild())
                ),
            )

        self.assertEqual(barrier.arrivals, 2)
        self.assertEqual(signals_channel.send.await_count, 2)
        self.assertEqual(update_state.call_count, 4)
        self.assertEqual(
            {
                item["delivery_status"]
                for item in state["signal_queue"].values()
            },
            {"sent"},
        )
        self.assertEqual(
            {
                item["signals_message_id"]
                for item in state["signal_queue"].values()
            },
            {"901", "902"},
        )

    async def test_button_rejects_unauthorized_or_non_guild_interactions(self):
        client, signals_channel, _review_channel = await self.start_bot()
        cases = (
            ("unprivileged", self.user(4), self.guild()),
            ("direct-message", self.user(1), None),
            (
                "missing-permissions",
                SimpleNamespace(id=5),
                self.guild(),
            ),
            (
                "malformed-user-id",
                self.user("bad", administrator=True),
                self.guild(),
            ),
        )

        for label, user, guild in cases:
            with self.subTest(case=label):
                modal, interaction = await self.click_button(
                    client,
                    user=user,
                    guild=guild,
                )
                self.assertIsNone(modal)
                self.assert_ephemeral_rejection(interaction)
                signals_channel.send.assert_not_awaited()

    async def test_button_rejects_channel_message_and_state_mismatches(self):
        client, signals_channel, _review_channel = await self.start_bot()
        owner = self.user(1)
        malformed_state = self.signal_state()
        malformed_state["signal_queue"]["token"]["candidate"] = []
        missing_directions = self.signal_state()
        missing_directions["signal_queue"]["token"]["candidate"].pop(
            "eps_direction"
        )
        bad_numeric_type = self.signal_state()
        bad_numeric_type["signal_queue"]["token"]["candidate"][
            "move_percent"
        ] = "10.0"
        cases = (
            ("wrong-channel", {"channel_id": 999}),
            ("wrong-message-id", {"message_id": 999}),
            ("wrong-author", {"author_id": 777}),
            (
                "non-default-type",
                {"message_type": discord.MessageType.pins_add},
            ),
            ("missing-state", {"state": self.signal_state(message_id="999")}),
            ("malformed-state", {"state": malformed_state}),
            ("missing-direction", {"state": missing_directions}),
            ("bad-numeric-type", {"state": bad_numeric_type}),
            ("missing-message", {"include_message": False}),
        )

        for label, overrides in cases:
            with self.subTest(case=label):
                modal, interaction = await self.click_button(
                    client,
                    user=owner,
                    guild=self.guild(),
                    **overrides,
                )
                self.assertIsNone(modal)
                self.assert_ephemeral_rejection(interaction)
                signals_channel.send.assert_not_awaited()

    async def test_modal_rejects_candidate_corrupted_after_button_open(self):
        corruptions = (
            ("missing-direction", "eps_direction", None),
            ("numeric-string", "current_price", "25.0"),
            ("boolean-number", "revenue_surprise", True),
        )

        for label, field, invalid_value in corruptions:
            with self.subTest(case=label):
                client, signals_channel, _review_channel = await self.start_bot()
                opener = self.user(1)
                modal, attachment = await self.valid_modal(client, user=opener)
                state = self.signal_state()
                candidate = state["signal_queue"]["token"]["candidate"]
                if invalid_value is None:
                    candidate.pop(field)
                else:
                    candidate[field] = invalid_value
                interaction = self.modal_interaction(
                    opener,
                    self.guild(),
                )

                with patch.object(
                    earnings_reactions,
                    "load_state",
                    return_value=state,
                ), patch.object(
                    earnings_reactions,
                    "update_state",
                ) as update_state, redirect_stdout(StringIO()):
                    await modal.on_submit(interaction)

                self.assert_ephemeral_rejection(interaction)
                attachment.to_file.assert_not_awaited()
                signals_channel.send.assert_not_awaited()
                update_state.assert_not_called()

    async def test_modal_revalidates_every_authorization_and_provenance_boundary(
        self,
    ):
        cases = (
            ("unprivileged", {"submitter": self.user(4)}),
            ("direct-message", {"guild": None}),
            ("wrong-channel", {"channel_id": 999}),
            ("wrong-message-id", {"fetched_message_id": 999}),
            ("wrong-author", {"author_id": 777}),
            (
                "non-default-type",
                {"message_type": discord.MessageType.pins_add},
            ),
            ("missing-state", {"state": self.signal_state(message_id="999")}),
            ("malformed-state", {"state": {"signal_queue": {"token": []}}}),
            (
                "different-submitter",
                {"submitter": self.user(2, administrator=True)},
            ),
            ("missing-discord-message", {"missing_discord_message": True}),
        )

        for label, overrides in cases:
            with self.subTest(case=label):
                client, signals_channel, review_channel = await self.start_bot()
                opener = self.user(1)
                modal, attachment = await self.valid_modal(client, user=opener)
                submitter = overrides.get("submitter", opener)
                guild = overrides.get("guild", self.guild())
                interaction = self.modal_interaction(
                    submitter,
                    guild,
                    channel_id=overrides.get("channel_id", 200),
                )
                state = overrides.get("state", self.signal_state())
                review_channel.fetch_message.return_value = (
                    None
                    if overrides.get("missing_discord_message")
                    else self.review_message(
                        client,
                        message_id=overrides.get("fetched_message_id", 321),
                        author_id=overrides.get("author_id"),
                        message_type=overrides.get(
                            "message_type",
                            discord.MessageType.default,
                        ),
                    )
                )

                with patch.object(
                    earnings_reactions,
                    "load_state",
                    return_value=state,
                ), patch.object(
                    earnings_reactions,
                    "update_state",
                ) as update_state, redirect_stdout(StringIO()):
                    await modal.on_submit(interaction)

                attachment.to_file.assert_not_awaited()
                signals_channel.send.assert_not_awaited()
                update_state.assert_not_called()
                self.assert_ephemeral_rejection(interaction)

    async def test_acknowledged_rejection_uses_ephemeral_followup(self):
        client, signals_channel, _review_channel = await self.start_bot()
        cases = (
            (self.user(4), "You cannot use this earnings review action."),
            (self.user(1), "This earnings review is no longer available."),
        )

        for user, expected_message in cases:
            with self.subTest(user_id=user.id):
                modal, interaction = await self.click_button(
                    client,
                    user=user,
                    guild=self.guild(),
                    acknowledged=True,
                )

                self.assertIsNone(modal)
                interaction.response.send_message.assert_not_awaited()
                interaction.response.send_modal.assert_not_awaited()
                interaction.followup.send.assert_awaited_once_with(
                    expected_message,
                    ephemeral=True,
                )
        signals_channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
