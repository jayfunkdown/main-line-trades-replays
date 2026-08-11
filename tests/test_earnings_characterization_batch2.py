import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

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


class StateWriteCharacterizationTests(NoNetworkTestCase):
    def empty_state(self):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    def test_transactional_record_updates_preserve_review_bot_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            original = self.empty_state()
            original["signal_queue"]["token"] = {
                "review_message_id": "123",
                "sent_to_signals": False,
            }

            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                earnings_reactions.earnings_state_store().replace(original)

                def mark_signal_sent(state):
                    state["signal_queue"]["token"][
                        "sent_to_signals"
                    ] = True

                earnings_reactions.update_state(mark_signal_sent)
                earnings_reactions.set_state_record(
                    "public",
                    "report-key",
                    {"symbol": "ACME"},
                )

                final_state = earnings_reactions.load_state()

            self.assertTrue(
                final_state["signal_queue"]["token"]["sent_to_signals"]
            )
            self.assertIn("report-key", final_state["public"])

    def test_load_state_rejects_corrupt_safety_sections(self):
        corrupt_state = {
            "public": [],
            "private": "invalid",
            "quotes": None,
            "signal_queue": ["invalid"],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(corrupt_state), encoding="utf-8")

            with patch.object(earnings_reactions, "STATE_FILE", state_path):
                with self.assertRaises(EarningsStateValidationError):
                    earnings_reactions.load_state()

    def test_corrupt_signal_queue_fails_closed_during_message_lookup(self):
        state = self.empty_state()
        state["signal_queue"] = []

        self.assertIsNone(
            earnings_reactions.find_signal_item_by_review_message(state, "123")
        )


class PostingFailureCharacterizationTests(NoNetworkTestCase):
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

    def empty_state(self):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    def main_patches(self, state_path, send_private, send_public, *, force=False):
        arguments = [
            "earnings_reactions.py",
            "--post",
            "--date",
            self.TARGET_DATE,
        ]
        if force:
            arguments.append("--force")

        return (
            patch.dict(os.environ, {}, clear=True),
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

    def test_successful_public_post_then_confirmation_failure_blocks_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps(self.empty_state()),
                encoding="utf-8",
            )
            send_private = Mock()
            send_public = Mock()
            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
            )
            original_transition = earnings_reactions.transition_feed_delivery

            def fail_confirmation(feed, key, attempt_id, status, **kwargs):
                if status == earnings_reactions.FEED_DELIVERY_CONFIRMED:
                    raise OSError("simulated state save failure")
                return original_transition(
                    feed,
                    key,
                    attempt_id,
                    status,
                    **kwargs,
                )

            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "transition_feed_delivery",
                        side_effect=fail_confirmation,
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))
                with self.assertRaisesRegex(OSError, "state save failure"):
                    earnings_reactions.main()

            self.assertEqual(send_public.call_count, 1)
            persisted_after_failure = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted_after_failure["public"][self.key]["delivery_status"],
                earnings_reactions.FEED_DELIVERY_RESERVED,
            )

            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
            )
            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                stack.enter_context(redirect_stdout(StringIO()))
                earnings_reactions.main()

            self.assertEqual(send_public.call_count, 1)
            persisted_after_retry = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                persisted_after_retry["public"][self.key]["delivery_status"],
                earnings_reactions.FEED_DELIVERY_RESERVED,
            )

    def test_force_reposts_saved_private_and_public_candidates(self):
        state = self.empty_state()
        legacy_record = {
            "symbol": "ACME",
            "posted_at": "2026-08-07T08:00:00-04:00",
        }
        state["private"][self.key] = copy.deepcopy(legacy_record)
        state["public"][self.key] = copy.deepcopy(legacy_record)

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            send_private = Mock()
            send_public = Mock()
            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
                force=True,
            )

            with ExitStack() as stack:
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {"EARNINGS_REVIEW_WEBHOOK": "review-webhook"},
                        clear=True,
                    )
                )
                for manager in patches[1:]:
                    stack.enter_context(manager)
                stack.enter_context(redirect_stdout(StringIO()))
                earnings_reactions.main()

        send_private.assert_called_once()
        send_public.assert_called_once_with(
            "public-webhook",
            "public message",
            earnings_reactions.PUBLIC_WEBHOOK_USERNAME,
            chart_symbol="ACME",
        )

    def test_corrupt_public_section_fails_before_posting(self):
        state = self.empty_state()
        state["public"] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            send_private = Mock()
            send_public = Mock()
            patches = self.main_patches(
                state_path,
                send_private,
                send_public,
            )

            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                stack.enter_context(redirect_stdout(StringIO()))
                with self.assertRaises(EarningsStateValidationError):
                    earnings_reactions.main()

        send_public.assert_not_called()


class FakeCommandTree:
    def __init__(self, client):
        self.client = client
        self.commands = {}

    def command(self, *, name, description):
        def decorator(function):
            self.commands[name] = function
            return function

        return decorator

    async def sync(self):
        return list(self.commands.values())


class FakeDiscordClient:
    def __init__(self, signals_channel, review_channel):
        self.signals_channel = signals_channel
        self.review_channel = review_channel
        self.guilds = []
        self.user = SimpleNamespace(id=999, __str__=lambda self: "Test Bot")
        self.persistent_view = None
        self.started_with = None

    def event(self, function):
        setattr(self, function.__name__, function)
        return function

    def add_view(self, view):
        self.persistent_view = view

    def get_channel(self, channel_id):
        if channel_id == 100:
            return self.signals_channel
        if channel_id == 200:
            return self.review_channel
        return None

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    async def start(self, token):
        self.started_with = token


class SendToSignalsCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    def signal_state(self, *, sent=False):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {
                "token": {
                    "review_message_id": "321",
                    "review_channel_id": "200",
                    "sent_to_signals": sent,
                    "trade_direction": "long",
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

    @staticmethod
    def transactional_update(persisted_state, failure=None):
        def update(mutation):
            latest = copy.deepcopy(persisted_state)
            mutation(latest)
            if failure is not None:
                raise failure
            persisted_state.clear()
            persisted_state.update(copy.deepcopy(latest))
            return copy.deepcopy(latest)

        return update

    async def build_modal(self, signals_channel, review_channel):
        client = FakeDiscordClient(signals_channel, review_channel)
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

        def required_environment(name):
            return {
                "DISCORD_BOT_TOKEN": "test-token",
                "SIGNALS_CHANNEL_ID": "100",
                "EARNINGS_REVIEW_WEBHOOK": "test-webhook",
            }[name]

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                earnings_reactions,
                "required_env",
                side_effect=required_environment,
            ),
            patch.object(
                earnings_reactions,
                "resolve_webhook_channel_id",
                return_value="200",
            ),
            patch.object(
                earnings_reactions,
                "load_state",
                return_value=self.signal_state(sent=False),
            ),
            patch.object(discord, "Client", return_value=client),
            patch.object(
                discord.app_commands,
                "CommandTree",
                FakeCommandTree,
            ),
            redirect_stdout(StringIO()),
        ):
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
        button = client.persistent_view.children[0]

        with patch.object(
            earnings_reactions,
            "load_state",
            return_value=self.signal_state(sent=False),
        ), redirect_stdout(StringIO()):
            await button.callback(button_interaction)

        modal = captured.modal
        self.assertIsNotNone(modal)
        modal.trade_direction._values = ["long"]
        modal.trade_thesis._value = "Trade above resistance."
        modal.trade_chart._values = [
            SimpleNamespace(
                filename="chart.png",
                content_type="image/png",
                to_file=AsyncMock(return_value="discord-file"),
            )
        ]
        return modal, client, reviewer, guild

    def submit_interaction(self, reviewer, guild):
        return SimpleNamespace(
            channel_id=200,
            guild=guild,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=reviewer,
            delete_original_response=AsyncMock(),
        )

    async def test_already_sent_state_prevents_second_signal_post(self):
        signals_channel = SimpleNamespace(send=AsyncMock())
        review_channel = SimpleNamespace(fetch_message=AsyncMock())
        modal, _client, reviewer, guild = await self.build_modal(
            signals_channel,
            review_channel,
        )
        interaction = self.submit_interaction(reviewer, guild)

        with (
            patch.object(
                earnings_reactions,
                "load_state",
                return_value=self.signal_state(sent=True),
            ),
            patch.object(earnings_reactions, "update_state") as update_state,
        ):
            await modal.on_submit(interaction)

        signals_channel.send.assert_not_awaited()
        update_state.assert_not_called()
        self.assertIn(
            "already",
            interaction.followup.send.await_args.args[0].lower(),
        )

    async def test_signal_post_then_confirmation_failure_blocks_retry(self):
        signals_channel = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=555))
        )
        review_channel = SimpleNamespace(fetch_message=AsyncMock())
        modal, _client, reviewer, guild = await self.build_modal(
            signals_channel,
            review_channel,
        )
        persisted_state = self.signal_state(sent=False)
        interaction_one = self.submit_interaction(reviewer, guild)
        interaction_two = self.submit_interaction(reviewer, guild)
        transactional_update = self.transactional_update(persisted_state)
        update_calls = 0

        def fail_confirmation(mutation):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 2:
                raise OSError("simulated state save failure")
            return transactional_update(mutation)

        with (
            patch.object(
                earnings_reactions,
                "load_state",
                side_effect=[
                    copy.deepcopy(persisted_state),
                    copy.deepcopy(persisted_state),
                ],
            ),
            patch.object(
                earnings_reactions,
                "update_state",
                side_effect=fail_confirmation,
            ) as update_state,
            redirect_stdout(StringIO()),
        ):
            await modal.on_submit(interaction_one)
            await modal.on_submit(interaction_two)

        self.assertEqual(signals_channel.send.await_count, 1)
        self.assertEqual(update_state.call_count, 3)
        self.assertEqual(review_channel.fetch_message.await_count, 2)
        item = persisted_state["signal_queue"]["token"]
        self.assertFalse(item["sent_to_signals"])
        self.assertEqual(item["delivery_status"], "sending")
        self.assertTrue(item["delivery_attempt_id"])
        self.assertNotIn("signals_message_id", item)
        self.assertIn(
            "do not retry",
            interaction_one.followup.send.await_args.args[0].lower(),
        )
        self.assertIn(
            "already being sent",
            interaction_two.followup.send.await_args.args[0].lower(),
        )

    async def test_signal_post_failure_does_not_mark_or_save_state(self):
        response = SimpleNamespace(
            status=500,
            reason="Server Error",
            headers={},
        )
        post_error = discord.HTTPException(response, "simulated failure")
        signals_channel = SimpleNamespace(
            send=AsyncMock(side_effect=post_error)
        )
        review_channel = SimpleNamespace(fetch_message=AsyncMock())
        modal, _client, reviewer, guild = await self.build_modal(
            signals_channel,
            review_channel,
        )
        state = self.signal_state(sent=False)
        interaction = self.submit_interaction(reviewer, guild)
        transactional_update = self.transactional_update(state)

        with (
            patch.object(
                earnings_reactions,
                "load_state",
                return_value=state,
            ),
            patch.object(
                earnings_reactions,
                "update_state",
                side_effect=transactional_update,
            ) as update_state,
            redirect_stdout(StringIO()),
        ):
            await modal.on_submit(interaction)

        signals_channel.send.assert_awaited_once()
        self.assertEqual(update_state.call_count, 2)
        review_channel.fetch_message.assert_awaited_once_with(321)
        self.assertFalse(
            state["signal_queue"]["token"]["sent_to_signals"]
        )
        self.assertEqual(
            state["signal_queue"]["token"]["delivery_status"],
            "ready",
        )
        self.assertEqual(
            state["signal_queue"]["token"]["delivery_error"],
            "discord_rejected",
        )
        self.assertNotIn("trade_thesis", state["signal_queue"]["token"])
        self.assertIn(
            "rejected",
            interaction.followup.send.await_args.args[0].lower(),
        )


if __name__ == "__main__":
    unittest.main()
