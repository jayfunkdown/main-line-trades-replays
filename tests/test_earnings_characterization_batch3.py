import asyncio
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

from tests import test_earnings_characterization_batch2 as batch2

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


class PrivateReviewPersistenceTests(NoNetworkTestCase):
    def candidate(self):
        return {
            "symbol": "ACME",
            "report": {
                "date": "2026-08-06",
                "symbol": "ACME",
                "year": 2026,
                "quarter": 2,
            },
        }

    def empty_state(self):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    def test_private_discord_post_precedes_signal_queue_persistence(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return b'{"id":"987654321"}'

        state = self.empty_state()

        with tempfile.TemporaryDirectory() as temp_dir:
            chart_path = Path(temp_dir) / "chart.png"
            chart_path.write_bytes(b"test-chart")

            with (
                patch.object(
                    earnings_reactions,
                    "required_env",
                    side_effect=lambda name: {
                        "DISCORD_BOT_TOKEN": "test-token",
                        "EARNINGS_REVIEW_WEBHOOK": "test-webhook",
                    }[name],
                ),
                patch.object(
                    earnings_reactions,
                    "resolve_webhook_channel_id",
                    return_value="200",
                ),
                patch.object(
                    earnings_reactions,
                    "generate_weekly_chart",
                    return_value=chart_path,
                ),
                patch.object(
                    earnings_reactions,
                    "build_private_message",
                    return_value="private review",
                ),
                patch.object(
                    earnings_reactions.urllib.request,
                    "urlopen",
                    return_value=FakeResponse(),
                ) as discord_post,
                patch.object(
                    earnings_reactions,
                    "set_state_record",
                    side_effect=OSError("simulated state save failure"),
                ) as set_record,
            ):
                with self.assertRaisesRegex(
                    earnings_reactions.AmbiguousDeliveryError,
                    "review state could not be confirmed",
                ):
                    earnings_reactions.send_private_review_with_chart(
                        self.candidate(),
                        1,
                        state,
                    )

        discord_post.assert_called_once()
        set_record.assert_called_once()
        self.assertEqual(state["signal_queue"], {})


class CorruptNestedExecutionTests(NoNetworkTestCase):
    TARGET_DATE = "2026-08-06"

    def report(self):
        return {
            "date": self.TARGET_DATE,
            "symbol": "ACME",
            "year": 2026,
            "quarter": 2,
        }

    def candidate(self):
        return {
            "symbol": "ACME",
            "score": 100.0,
            "move_percent": 10.0,
            "report": self.report(),
        }

    def empty_state(self):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    def base_main_patches(self, state_path, arguments):
        return (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", arguments),
            patch.object(earnings_reactions, "STATE_FILE", state_path),
            patch.object(
                earnings_reactions,
                "get_completed_reports",
                return_value=[self.report()],
            ),
        )

    def test_corrupt_private_section_fails_before_posting(self):
        state = self.empty_state()
        state["private"] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            send_private = Mock()
            patches = self.base_main_patches(
                state_path,
                [
                    "earnings_reactions.py",
                    "--post",
                    "--date",
                    self.TARGET_DATE,
                ],
            )

            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {"EARNINGS_REVIEW_WEBHOOK": "review-webhook"},
                        clear=True,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "build_candidates_optimized",
                        return_value=([self.candidate()], 0, 0),
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "qualifies_for_private",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "qualifies_for_public",
                        return_value=False,
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "required_env",
                        return_value="public-webhook",
                    )
                )
                stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "send_private_review_with_chart",
                        send_private,
                    )
                )
                stack.enter_context(patch.object(earnings_reactions.time, "sleep"))
                stack.enter_context(redirect_stdout(StringIO()))

                with self.assertRaises(EarningsStateValidationError):
                    earnings_reactions.main()

        send_private.assert_not_called()

    def test_corrupt_quotes_section_is_reset_and_preview_continues(self):
        state = self.empty_state()
        state["quotes"] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            patches = self.base_main_patches(
                state_path,
                [
                    "earnings_reactions.py",
                    "--preview",
                    "--date",
                    self.TARGET_DATE,
                ],
            )

            with ExitStack() as stack:
                for manager in patches:
                    stack.enter_context(manager)
                quote_request = stack.enter_context(
                    patch.object(
                        earnings_reactions,
                        "get_quote_with_retry",
                        return_value={"c": 10.0, "dp": 5.0},
                    )
                )
                stack.enter_context(redirect_stdout(StringIO()))

                earnings_reactions.main()

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        quote_request.assert_called_once_with("ACME")
        self.assertIsInstance(persisted["quotes"], dict)
        self.assertEqual(persisted["public"], {})
        self.assertEqual(persisted["private"], {})
        self.assertEqual(persisted["signal_queue"], {})


class SendToSignalsRaceAndProvenanceTests(unittest.IsolatedAsyncioTestCase):
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
    def transactional_update(persisted_state, event=None):
        def update(mutation):
            latest = json.loads(json.dumps(persisted_state))
            mutation(latest)
            persisted_state.clear()
            persisted_state.update(json.loads(json.dumps(latest)))
            if event is not None:
                event()
            return json.loads(json.dumps(latest))

        return update

    async def start_fake_bot(self, signals_channel, review_channel):
        client = batch2.FakeDiscordClient(signals_channel, review_channel)
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
                batch2.FakeCommandTree,
            ),
            redirect_stdout(StringIO()),
        ):
            await earnings_reactions.run_review_button_bot()

        return client

    async def open_modal(
        self,
        client,
        *,
        user=None,
        channel_id=200,
        attachment=None,
    ):
        modal, _interaction = await self.click_button(
            client,
            user=user,
            channel_id=channel_id,
        )
        self.assertIsNotNone(modal)
        modal.trade_direction._values = ["long"]
        modal.trade_thesis._value = "Trade above resistance."
        modal.trade_chart._values = [
            attachment
            or SimpleNamespace(
                filename="chart.png",
                content_type="image/png",
                to_file=AsyncMock(return_value="discord-file"),
            )
        ]
        return modal

    async def click_button(
        self,
        client,
        *,
        user=None,
        channel_id=200,
    ):
        captured = SimpleNamespace(modal=None)

        async def send_modal(modal):
            captured.modal = modal

        reviewer = user or SimpleNamespace(
            id=1,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_messages=False,
            ),
        )

        interaction = SimpleNamespace(
            message=SimpleNamespace(
                id=321,
                content="**ACME**",
                author=client.user,
                channel=SimpleNamespace(id=channel_id),
                type=discord.MessageType.default,
            ),
            channel_id=channel_id,
            guild=SimpleNamespace(owner_id=1),
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
            await button.callback(interaction)

        return captured.modal, interaction

    def submit_interaction(self, *, channel_id=200, user=None):
        reviewer = user or SimpleNamespace(
            id=1,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_messages=False,
            ),
        )
        return SimpleNamespace(
            channel_id=channel_id,
            guild=SimpleNamespace(owner_id=1),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=reviewer,
            delete_original_response=AsyncMock(),
        )

    async def test_two_simultaneous_submissions_post_signals_once(self):
        signals_channel = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=555))
        )
        review_message = SimpleNamespace(edit=AsyncMock())
        review_channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=review_message)
        )
        client = await self.start_fake_bot(signals_channel, review_channel)
        modal_one = await self.open_modal(client)
        modal_two = await self.open_modal(client)
        persisted_state = self.signal_state(sent=False)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                earnings_reactions,
                "load_state",
                side_effect=lambda: json.loads(json.dumps(persisted_state)),
            ) as load_state,
            patch.object(
                earnings_reactions,
                "update_state",
                side_effect=self.transactional_update(persisted_state),
            ) as update_state,
            redirect_stdout(StringIO()),
        ):
            await asyncio.gather(
                modal_one.on_submit(self.submit_interaction()),
                modal_two.on_submit(self.submit_interaction()),
            )

        self.assertEqual(load_state.call_count, 2)
        self.assertEqual(signals_channel.send.await_count, 1)
        self.assertEqual(update_state.call_count, 2)
        item = persisted_state["signal_queue"]["token"]
        self.assertEqual(item["delivery_status"], "sent")
        self.assertEqual(item["signals_message_id"], "555")

    async def test_review_update_failure_occurs_after_saved_signal_state(self):
        events = []

        async def send_signal(**kwargs):
            events.append("signal-posted")
            return SimpleNamespace(id=555)

        signals_channel = SimpleNamespace(send=AsyncMock(side_effect=send_signal))
        review_channel = SimpleNamespace(
            fetch_message=AsyncMock()
        )
        client = await self.start_fake_bot(signals_channel, review_channel)
        valid_review_message = review_channel.fetch_message.return_value

        fetch_count = 0

        async def fetch_review_message(message_id):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 2:
                events.append("review-update-attempted")
                raise RuntimeError("simulated review update failure")
            return valid_review_message

        review_channel.fetch_message.side_effect = fetch_review_message
        modal = await self.open_modal(client)
        state = self.signal_state(sent=False)

        def state_saved():
            events.append("state-saved")

        first_interaction = self.submit_interaction()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                earnings_reactions,
                "load_state",
                return_value=state,
            ),
            patch.object(
                earnings_reactions,
                "update_state",
                side_effect=self.transactional_update(state, state_saved),
            ) as update_state,
            redirect_stdout(StringIO()),
        ):
            await modal.on_submit(first_interaction)
            await modal.on_submit(self.submit_interaction())

        self.assertEqual(
            events,
            [
                "state-saved",
                "signal-posted",
                "state-saved",
                "review-update-attempted",
            ],
        )
        self.assertTrue(state["signal_queue"]["token"]["sent_to_signals"])
        signals_channel.send.assert_awaited_once()
        self.assertEqual(update_state.call_count, 2)
        first_interaction.delete_original_response.assert_awaited_once()

    async def test_button_rejects_wrong_channel_and_unprivileged_user(self):
        signals_channel = SimpleNamespace(send=AsyncMock())
        review_channel = SimpleNamespace(fetch_message=AsyncMock())
        client = await self.start_fake_bot(signals_channel, review_channel)
        unprivileged_user = SimpleNamespace(
            id=55,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_messages=False,
            ),
        )

        modal, interaction = await self.click_button(
            client,
            user=unprivileged_user,
            channel_id=999,
        )

        self.assertIsNone(modal)
        interaction.response.send_message.assert_awaited_once()
        signals_channel.send.assert_not_awaited()

    async def test_modal_rejects_wrong_channel_and_unprivileged_user(self):
        signals_channel = SimpleNamespace(send=AsyncMock())
        review_message = SimpleNamespace(edit=AsyncMock())
        review_channel = SimpleNamespace(
            fetch_message=AsyncMock(return_value=review_message)
        )
        client = await self.start_fake_bot(signals_channel, review_channel)
        unprivileged_user = SimpleNamespace(
            id=55,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_messages=False,
            ),
        )
        modal = await self.open_modal(client)
        state = self.signal_state(sent=False)
        interaction = self.submit_interaction(
            channel_id=999,
            user=unprivileged_user,
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                earnings_reactions,
                "load_state",
                return_value=state,
            ),
            patch.object(
                earnings_reactions,
                "update_state",
                side_effect=self.transactional_update(state),
            ) as update_state,
            redirect_stdout(StringIO()),
        ):
            await modal.on_submit(interaction)

        interaction.followup.send.assert_awaited_once()
        signals_channel.send.assert_not_awaited()
        update_state.assert_not_called()
        self.assertFalse(state["signal_queue"]["token"]["sent_to_signals"])


if __name__ == "__main__":
    unittest.main()
