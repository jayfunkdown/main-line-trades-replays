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

from tests.test_earnings_characterization_batch2 import FakeCommandTree

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


class Response:
    def __init__(self):
        self.done = False
        self.modal = None
        self.send_message = AsyncMock(side_effect=self._send)
        self.send_modal = AsyncMock(side_effect=self._modal)
        self.defer = AsyncMock(side_effect=self._defer)

    def is_done(self):
        return self.done

    async def _send(self, *args, **kwargs):
        self.done = True

    async def _modal(self, modal):
        self.modal = modal
        self.done = True

    async def _defer(self, *args, **kwargs):
        self.done = True


class ComposerClient:
    def __init__(self, signals, review, drafts):
        self.channels = {100: signals, 200: review, 300: drafts}
        self.guilds = []
        self.user = SimpleNamespace(id=999)
        self.views = []
        self.started_with = None

    def event(self, function):
        setattr(self, function.__name__, function)
        return function

    def add_view(self, view):
        self.views.append(view)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    async def start(self, token):
        self.started_with = token


class ManualSignalPureBehaviorTests(unittest.TestCase):
    def test_exact_format_with_optional_fields(self):
        self.assertEqual(
            earnings_reactions.build_manual_signal_message(
                "ES futures",
                "Hold above VWAP.",
                timeframe="15m",
                setup_name="Opening drive",
            ),
            "# 📈 Trade Signal\n\n"
            "## ES futures\n\n"
            "🕒 **Timeframe:** 15m\n"
            "🎯 **Setup:** Opening drive\n\n"
            "## 🧠 Trade Thesis\n\n"
            f"Hold above VWAP.\n\n{earnings_reactions.DIVIDER}\n\n"
            "## 📊 Trade Chart\n\n"
            "*Chart and thesis provided by Main Line Trades.*\n\n"
            "⚠️ **Manage risk. This is not financial advice.**",
        )

    def test_exact_format_omits_blank_optional_fields(self):
        content = earnings_reactions.build_manual_signal_message(
            "BTC/USD", "Breakout retest."
        )
        self.assertNotIn("Timeframe", content)
        self.assertNotIn("Setup:", content)
        self.assertIn("## BTC/USD", content)
        self.assertIn(earnings_reactions.DIVIDER, content)
        self.assertNotIn("\n---\n", content)

    def test_non_stock_instruments_and_message_boundary(self):
        self.assertFalse(
            earnings_reactions.is_valid_manual_signal_fields("", "Thesis")
        )
        self.assertFalse(
            earnings_reactions.is_valid_manual_signal_fields("ES", "")
        )
        for instrument in (
            "SPY 2026-09-18 600C",
            "ESU6",
            "EUR/USD",
            "Gold futures",
            "BTC-PERP",
        ):
            self.assertTrue(
                earnings_reactions.is_valid_manual_signal_fields(
                    instrument, "Synthetic thesis"
                )
            )
        base = earnings_reactions.build_manual_signal_message("ES", "")
        max_thesis = "x" * (
            earnings_reactions.MANUAL_SIGNAL_MAX_CONTENT_LENGTH - len(base)
        )
        self.assertTrue(
            earnings_reactions.is_valid_manual_signal_fields("ES", max_thesis)
        )
        self.assertFalse(
            earnings_reactions.is_valid_manual_signal_fields(
                "ES", max_thesis + "x"
            )
        )

    def test_attachment_validation_is_strict(self):
        for filename, content_type in (
            ("chart.png", "image/png"),
            ("chart.JPG", "image/jpeg"),
            ("chart.jpeg", "image/jpeg"),
            ("chart.webp", "image/webp"),
        ):
            self.assertTrue(
                earnings_reactions.is_valid_manual_chart_attachment(
                    SimpleNamespace(filename=filename, content_type=content_type)
                )
            )
        for filename, content_type in (
            ("chart.gif", "image/gif"),
            ("chart.png", "text/plain"),
            ("chart.exe", "image/png"),
        ):
            self.assertFalse(
                earnings_reactions.is_valid_manual_chart_attachment(
                    SimpleNamespace(filename=filename, content_type=content_type)
                )
            )

    def test_manual_signal_log_labels_are_safe_and_bounded(self):
        value = earnings_reactions.safe_manual_signal_log_value(
            "@everyone\n" + ("x" * 100)
        )
        self.assertNotIn("\n", value)
        self.assertNotIn("@everyone", value)
        self.assertLessEqual(len(value), 80)


class ManualSignalStateTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
            "manual_signal_drafts": {"draft": self.record()},
        }

    @staticmethod
    def record(**updates):
        value = {
            "draft_id": "draft",
            "draft_message_id": "400",
            "draft_channel_id": "300",
            "creator_user_id": "1",
            "instrument": "ES",
            "trade_thesis": "Hold above VWAP.",
            "timeframe": "15m",
            "setup_name": "Breakout",
            "chart": {
                "filename": "chart.png",
                "content_type": "image/png",
                "attachment_id": "500",
            },
            "created_at": "2026-08-08T10:00:00+00:00",
            "updated_at": "2026-08-08T10:00:00+00:00",
            "delivery_status": "ready",
            "canceled": False,
        }
        value.update(updates)
        return value

    def transactional_update(self, mutation):
        latest = copy.deepcopy(self.state)
        mutation(latest)
        self.state = latest
        return copy.deepcopy(latest)

    def test_claim_transition_and_attempt_guard(self):
        with patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            _state, outcome = earnings_reactions.claim_manual_signal_delivery(
                "draft", "400", "300", "attempt", "2026-08-08T10:01:00+00:00"
            )
            self.assertEqual(outcome, "claimed")
            _state, mismatch = earnings_reactions.transition_manual_signal_delivery(
                "draft",
                "old",
                "sent",
                "2026-08-08T10:02:00+00:00",
                signals_message_id="600",
            )
            self.assertEqual(mismatch, "mismatch")
            _state, outcome = earnings_reactions.transition_manual_signal_delivery(
                "draft",
                "attempt",
                "sent",
                "2026-08-08T10:02:00+00:00",
                signals_message_id="600",
            )
        self.assertEqual(outcome, "transitioned")
        self.assertEqual(self.state["manual_signal_drafts"]["draft"]["signals_message_id"], "600")

    def test_canceled_and_malformed_records_fail_closed(self):
        malformed = self.record(chart={"filename": "chart.gif", "content_type": "image/gif"})
        self.assertIsNone(earnings_reactions.manual_signal_delivery_status(malformed))
        self.state["manual_signal_drafts"]["draft"]["canceled"] = True
        with patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            _state, outcome = earnings_reactions.claim_manual_signal_delivery(
                "draft", "400", "300", "attempt", "2026-08-08T10:01:00+00:00"
            )
        self.assertEqual(outcome, "canceled")


class ManualSignalWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
            "manual_signal_drafts": {},
        }
        self.signals = SimpleNamespace(
            send=AsyncMock(return_value=SimpleNamespace(id=600))
        )
        self.review = SimpleNamespace(fetch_message=AsyncMock())
        self.drafts = SimpleNamespace(
            send=AsyncMock(),
            fetch_message=AsyncMock(),
        )
        self.bot_log = SimpleNamespace(send=AsyncMock())
        self.bot_log_env_patcher = patch.dict(
            os.environ,
            {"BOT_LOG_CHANNEL_ID": "900"},
        )
        self.bot_log_env_patcher.start()
        self.addCleanup(self.bot_log_env_patcher.stop)
        self.urlopen_patcher = patch.object(
            earnings_reactions.urllib.request,
            "urlopen",
            side_effect=AssertionError("Tests must not make network requests"),
        )
        self.urlopen_patcher.start()
        self.addCleanup(self.urlopen_patcher.stop)

    @staticmethod
    def user(user_id=1, *, admin=False, manage=False):
        return SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(
                administrator=admin,
                manage_messages=manage,
            ),
        )

    @staticmethod
    def guild(owner_id=1):
        return SimpleNamespace(owner_id=owner_id)

    def transactional_update(self, mutation):
        latest = copy.deepcopy(self.state)
        mutation(latest)
        self.state.clear()
        self.state.update(copy.deepcopy(latest))
        return copy.deepcopy(latest)

    def set_record(self, section, key, value):
        latest = copy.deepcopy(self.state)
        latest.setdefault(section, {})[key] = copy.deepcopy(value)
        self.state.clear()
        self.state.update(latest)
        return copy.deepcopy(latest)

    async def start_bot(self):
        client = ComposerClient(self.signals, self.review, self.drafts)
        client.channels[900] = self.bot_log
        tree_holder = {}

        class CapturingTree(FakeCommandTree):
            def __init__(self, client_value):
                super().__init__(client_value)
                tree_holder["tree"] = self

        def required(name):
            return {
                "DISCORD_BOT_TOKEN": "synthetic-token",
                "SIGNALS_CHANNEL_ID": "100",
                "EARNINGS_REVIEW_WEBHOOK": "synthetic-webhook",
            }[name]

        with patch.dict(
            os.environ,
            {
                "SIGNAL_DRAFTS_CHANNEL_ID": "300",
                "BOT_LOG_CHANNEL_ID": "900",
            },
            clear=True,
        ), patch.object(
            earnings_reactions, "required_env", side_effect=required
        ), patch.object(
            earnings_reactions, "resolve_webhook_channel_id", return_value="200"
        ), patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            discord, "Client", return_value=client
        ), patch.object(
            discord.app_commands, "CommandTree", CapturingTree
        ), redirect_stdout(StringIO()):
            await earnings_reactions.run_review_button_bot()
        manual_view = next(
            view
            for view in client.views
            if any(
                getattr(child, "custom_id", None) == "manual_signal_publish"
                for child in view.children
            )
        )
        return client, tree_holder["tree"], manual_view

    def interaction(self, client, *, user=None, channel_id=300, message=None, guild=None):
        if message is not None:
            self.drafts.fetch_message.return_value = message
        return SimpleNamespace(
            user=user or self.user(),
            guild=self.guild() if guild is None else guild,
            channel_id=channel_id,
            message=message,
            response=Response(),
            followup=SimpleNamespace(send=AsyncMock()),
            delete_original_response=AsyncMock(),
        )

    @staticmethod
    def attachment(filename="chart.png", content_type="image/png", attachment_id=500):
        return SimpleNamespace(
            id=attachment_id,
            filename=filename,
            content_type=content_type,
            to_file=AsyncMock(return_value=f"file:{filename}"),
        )

    def draft_record(self, **updates):
        record = ManualSignalStateTests.record(**updates)
        self.state["manual_signal_drafts"]["draft"] = record
        return record

    @staticmethod
    def button(view, custom_id):
        return next(child for child in view.children if child.custom_id == custom_id)

    async def test_command_authorization_channel_and_persistent_registration(self):
        client, tree, _view = await self.start_bot()
        self.assertEqual(len(client.views), 2)
        command = tree.commands["new-signal"]
        good = self.interaction(client)
        await command(good)
        self.assertIsNotNone(good.response.modal)
        for staff in (
            self.user(2, admin=True),
            self.user(3, manage=True),
        ):
            allowed = self.interaction(client, user=staff, guild=self.guild(99))
            await command(allowed)
            self.assertIsNotNone(allowed.response.modal)
        nonguild = self.interaction(client)
        nonguild.guild = None
        for bad in (
            self.interaction(client, user=self.user(2), guild=self.guild(1)),
            self.interaction(client, channel_id=301),
            nonguild,
        ):
            await command(bad)
            self.assertIsNone(bad.response.modal)
            self.assertTrue(bad.response.send_message.await_args.kwargs["ephemeral"])

    async def test_draft_creation_persists_preview_and_chart(self):
        client, tree, _view = await self.start_bot()
        draft_message = SimpleNamespace(id=400, edit=AsyncMock())
        self.drafts.send.return_value = draft_message
        interaction = self.interaction(client)
        await tree.commands["new-signal"](interaction)
        modal = interaction.response.modal
        modal.instrument._value = "EUR/USD"
        modal.trade_thesis._value = "Hold the breakout."
        modal.timeframe._value = "4h"
        modal.setup_name._value = "Retest"
        modal.trade_chart._values = [self.attachment()]
        submit = self.interaction(client)
        with patch.object(
            earnings_reactions, "set_state_record", side_effect=self.set_record
        ):
            await modal.on_submit(submit)
        sent = self.drafts.send.await_args.kwargs
        self.assertEqual(
            sent["content"],
            earnings_reactions.build_manual_signal_message(
                "EUR/USD", "Hold the breakout.", timeframe="4h", setup_name="Retest"
            ),
        )
        self.assertIsInstance(sent["allowed_mentions"], discord.AllowedMentions)
        record = next(iter(self.state["manual_signal_drafts"].values()))
        self.assertEqual(record["draft_message_id"], "400")
        self.assertEqual(record["delivery_status"], "ready")

    async def test_edit_retains_or_replaces_chart_and_cancel_deletes(self):
        client, _tree, view = await self.start_bot()
        original = self.attachment()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[original],
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.draft_record()
        unauthorized_edit = self.interaction(
            client,
            user=self.user(2, manage=True),
            guild=self.guild(99),
            message=message,
        )
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_edit").callback(unauthorized_edit)
        self.assertIsNone(unauthorized_edit.response.modal)
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_edit").callback(interaction)
        modal = interaction.response.modal
        modal.trade_thesis._value = "Edited thesis"
        modal.trade_chart._values = []
        submit = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await modal.on_submit(submit)
        self.assertNotIn("attachments", message.edit.await_args.kwargs)
        self.assertEqual(
            self.state["manual_signal_drafts"]["draft"]["chart"]["filename"],
            "chart.png",
        )
        message.edit.reset_mock()
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_edit").callback(interaction)
        replacement_modal = interaction.response.modal
        replacement_modal.trade_chart._values = [
            self.attachment("replacement.webp", "image/webp", 501)
        ]
        submit = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await replacement_modal.on_submit(submit)
        self.assertIn("attachments", message.edit.await_args.kwargs)
        self.assertEqual(
            self.state["manual_signal_drafts"]["draft"]["chart"]["filename"],
            "replacement.webp",
        )
        message.edit.reset_mock()
        unauthorized_cancel = self.interaction(
            client,
            user=self.user(2, manage=True),
            guild=self.guild(99),
            message=message,
        )
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_cancel").callback(unauthorized_cancel)
        self.assertFalse(self.state["manual_signal_drafts"]["draft"]["canceled"])

        async def delete_after_persistence():
            self.assertTrue(
                self.state["manual_signal_drafts"]["draft"]["canceled"]
            )

        message.delete.side_effect = delete_after_persistence
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await self.button(view, "manual_signal_cancel").callback(interaction)
        self.assertTrue(self.state["manual_signal_drafts"]["draft"]["canceled"])
        message.delete.assert_awaited_once()
        message.edit.assert_not_awaited()
        log_message = self.bot_log.send.await_args.args[0]
        self.assertIn("ES", log_message)
        self.assertIn("canceled", log_message)
        self.assertNotIn("Edited thesis", log_message)
        self.assertIsInstance(
            self.bot_log.send.await_args.kwargs["allowed_mentions"],
            discord.AllowedMentions,
        )

    async def test_publish_success_and_concurrent_double_click(self):
        client, _tree, view = await self.start_bot()
        attachment = self.attachment()

        async def delete_after_confirmation():
            record = self.state["manual_signal_drafts"]["draft"]
            self.assertEqual(record["delivery_status"], "sent")
            self.assertEqual(record["signals_message_id"], "600")

        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[attachment],
            edit=AsyncMock(),
            delete=AsyncMock(side_effect=delete_after_confirmation),
        )
        self.draft_record()
        one = self.interaction(
            client,
            user=self.user(2, manage=True),
            guild=self.guild(99),
            message=message,
        )
        two = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await asyncio.gather(
                self.button(view, "manual_signal_publish").callback(one),
                self.button(view, "manual_signal_publish").callback(two),
            )
        self.signals.send.assert_awaited_once()
        record = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(record["delivery_status"], "sent")
        self.assertEqual(record["signals_message_id"], "600")
        message.delete.assert_awaited_once()
        message.edit.assert_not_awaited()
        log_message = self.bot_log.send.await_args.args[0]
        self.assertIn("ES", log_message)
        self.assertIn("published successfully", log_message)
        self.assertNotIn("Hold above VWAP", log_message)
        self.assertNotIn("chart.png", log_message)

    async def test_publish_treats_already_deleted_draft_as_success(self):
        client, _tree, view = await self.start_bot()
        not_found = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found", text=""),
            "Unknown Message",
        )
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(side_effect=not_found),
        )
        self.draft_record()
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await self.button(view, "manual_signal_publish").callback(interaction)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(record["delivery_status"], "sent")
        self.assertEqual(record["signals_message_id"], "600")
        message.edit.assert_not_awaited()
        interaction.delete_original_response.assert_awaited_once()

    async def test_publish_delete_failure_stays_sent_and_blocks_retry(self):
        client, _tree, view = await self.start_bot()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(side_effect=RuntimeError("synthetic delete failure")),
        )
        self.draft_record()
        first = self.interaction(client, message=message)
        second = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await self.button(view, "manual_signal_publish").callback(first)
            await self.button(view, "manual_signal_publish").callback(second)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(record["delivery_status"], "sent")
        self.assertEqual(record["signals_message_id"], "600")
        self.signals.send.assert_awaited_once()
        fallback_view = message.edit.await_args.kwargs["view"]
        self.assertEqual(fallback_view.children[0].label, "Published")
        self.assertTrue(fallback_view.children[0].disabled)
        warning = first.followup.send.await_args.args[0]
        self.assertIn("published", warning)
        self.assertIn("manual cleanup", warning)
        self.assertNotIn("synthetic", warning)
        self.assertIn("deletion failed", self.bot_log.send.await_args.args[0])

    async def test_bot_log_failure_does_not_change_successful_publication(self):
        client, _tree, view = await self.start_bot()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.draft_record()
        self.bot_log.send.side_effect = RuntimeError("synthetic log failure")
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ), redirect_stdout(StringIO()):
            await self.button(view, "manual_signal_publish").callback(interaction)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(record["delivery_status"], "sent")
        self.assertEqual(record["signals_message_id"], "600")
        self.signals.send.assert_awaited_once()
        message.delete.assert_awaited_once()
        interaction.delete_original_response.assert_awaited_once()

    async def test_cancel_treats_already_deleted_message_as_success(self):
        client, _tree, view = await self.start_bot()
        not_found = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found", text=""),
            "Unknown Message",
        )
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(side_effect=not_found),
        )
        self.draft_record()
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await self.button(view, "manual_signal_cancel").callback(interaction)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertTrue(record["canceled"])
        self.assertEqual(record["delivery_status"], "ready")
        message.edit.assert_not_awaited()
        self.assertIn("canceled", self.bot_log.send.await_args.args[0])

    async def test_bot_log_failure_does_not_change_cancellation_result(self):
        client, _tree, view = await self.start_bot()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.draft_record()
        self.bot_log.send.side_effect = RuntimeError("synthetic log failure")
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ), redirect_stdout(StringIO()):
            await self.button(view, "manual_signal_cancel").callback(interaction)

        self.assertTrue(self.state["manual_signal_drafts"]["draft"]["canceled"])
        message.delete.assert_awaited_once()
        self.assertIn("canceled", interaction.response.send_message.await_args.args[0])

    async def test_cancel_delete_failure_keeps_canceled_and_disables_controls(self):
        client, _tree, view = await self.start_bot()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(side_effect=RuntimeError("synthetic delete failure")),
        )
        self.draft_record()
        interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            await self.button(view, "manual_signal_cancel").callback(interaction)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertTrue(record["canceled"])
        self.assertEqual(record["delivery_status"], "ready")
        fallback_view = message.edit.await_args.kwargs["view"]
        self.assertTrue(fallback_view.children[0].disabled)
        warning = interaction.response.send_message.await_args.args[0]
        self.assertIn("could not be removed", warning)
        self.assertNotIn("synthetic", warning)
        self.assertIn("deletion failed", self.bot_log.send.await_args.args[0])

    async def test_edit_and_publish_serialize_with_edit_winning(self):
        client, _tree, view = await self.start_bot()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.draft_record()
        open_interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_edit").callback(open_interaction)
        modal = open_interaction.response.modal
        modal.trade_thesis._value = "Completely updated thesis"
        replacement = self.attachment(
            "updated.webp", "image/webp", 502
        )
        modal.trade_chart._values = [replacement]

        edit_started = asyncio.Event()
        release_edit = asyncio.Event()

        async def edit_message(**kwargs):
            if "content" in kwargs and not edit_started.is_set():
                edit_started.set()
                await release_edit.wait()
                message.attachments = [replacement]
            return message

        message.edit.side_effect = edit_message
        edit_submit = self.interaction(client, message=message)
        publish_interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            edit_task = asyncio.create_task(modal.on_submit(edit_submit))
            await edit_started.wait()
            publish_task = asyncio.create_task(
                self.button(view, "manual_signal_publish").callback(
                    publish_interaction
                )
            )
            await asyncio.sleep(0)
            self.signals.send.assert_not_awaited()
            release_edit.set()
            await asyncio.gather(edit_task, publish_task)

        self.signals.send.assert_awaited_once()
        self.assertIn(
            "Completely updated thesis",
            self.signals.send.await_args.kwargs["content"],
        )
        self.assertEqual(
            self.signals.send.await_args.kwargs["file"],
            "file:updated.webp",
        )
        record = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(record["trade_thesis"], "Completely updated thesis")
        self.assertEqual(record["chart"]["filename"], "updated.webp")
        self.assertEqual(record["delivery_status"], "sent")

    async def test_edit_and_publish_serialize_with_publish_winning(self):
        client, _tree, view = await self.start_bot()
        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        self.draft_record()
        open_interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_edit").callback(open_interaction)
        modal = open_interaction.response.modal
        modal.trade_thesis._value = "Losing edit"
        modal.trade_chart._values = []

        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def send_signal(**kwargs):
            send_started.set()
            await release_send.wait()
            return SimpleNamespace(id=600)

        self.signals.send.side_effect = send_signal
        publish_interaction = self.interaction(client, message=message)
        edit_submit = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            publish_task = asyncio.create_task(
                self.button(view, "manual_signal_publish").callback(
                    publish_interaction
                )
            )
            await send_started.wait()
            edit_task = asyncio.create_task(modal.on_submit(edit_submit))
            await asyncio.sleep(0)
            release_send.set()
            await asyncio.gather(publish_task, edit_task)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertEqual(record["delivery_status"], "sent")
        self.assertEqual(record["trade_thesis"], "Hold above VWAP.")
        content_edits = [
            call
            for call in message.edit.await_args_list
            if "content" in call.kwargs
        ]
        self.assertEqual(content_edits, [])

    async def test_edit_and_cancel_serialize_with_cancel_winning(self):
        client, _tree, view = await self.start_bot()
        cancel_started = asyncio.Event()
        release_cancel = asyncio.Event()

        async def delete_message():
            cancel_started.set()
            await release_cancel.wait()

        message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
            delete=AsyncMock(side_effect=delete_message),
        )
        self.draft_record()
        open_interaction = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            await self.button(view, "manual_signal_edit").callback(open_interaction)
        modal = open_interaction.response.modal
        modal.trade_thesis._value = "Losing edit"
        modal.trade_chart._values = []
        cancel_interaction = self.interaction(client, message=message)
        edit_submit = self.interaction(client, message=message)
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            cancel_task = asyncio.create_task(
                self.button(view, "manual_signal_cancel").callback(
                    cancel_interaction
                )
            )
            await cancel_started.wait()
            edit_task = asyncio.create_task(modal.on_submit(edit_submit))
            await asyncio.sleep(0)
            release_cancel.set()
            await asyncio.gather(cancel_task, edit_task)

        record = self.state["manual_signal_drafts"]["draft"]
        self.assertTrue(record["canceled"])
        self.assertEqual(record["trade_thesis"], "Hold above VWAP.")
        self.assertFalse(
            any("content" in call.kwargs for call in message.edit.await_args_list)
        )

    async def test_edits_for_different_drafts_do_not_block_each_other(self):
        client, _tree, view = await self.start_bot()
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first_edit(**kwargs):
            if "content" in kwargs:
                first_started.set()
                await release_first.wait()
            return first_message

        first_message = SimpleNamespace(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(side_effect=first_edit),
        )
        second_message = SimpleNamespace(
            id=401,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment("second.png", attachment_id=501)],
            edit=AsyncMock(return_value=None),
        )
        self.draft_record()
        self.state["manual_signal_drafts"]["draft2"] = ManualSignalStateTests.record(
            draft_id="draft2",
            draft_message_id="401",
            chart={
                "filename": "second.png",
                "content_type": "image/png",
                "attachment_id": "501",
            },
        )
        messages = {400: first_message, 401: second_message}
        self.drafts.fetch_message.side_effect = lambda message_id: messages[message_id]

        async def open_edit(message):
            interaction = self.interaction(client, message=message)
            await self.button(view, "manual_signal_edit").callback(interaction)
            interaction.response.modal.trade_chart._values = []
            return interaction.response.modal

        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ):
            first_modal = await open_edit(first_message)
            second_modal = await open_edit(second_message)
        first_modal.trade_thesis._value = "First updated"
        second_modal.trade_thesis._value = "Second updated"
        first_submit = self.interaction(client, message=first_message)
        second_submit = self.interaction(client, message=second_message)
        self.drafts.fetch_message.side_effect = lambda message_id: messages[message_id]
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(
            earnings_reactions, "update_state", side_effect=self.transactional_update
        ):
            first_task = asyncio.create_task(first_modal.on_submit(first_submit))
            await first_started.wait()
            second_task = asyncio.create_task(second_modal.on_submit(second_submit))
            await asyncio.wait_for(second_task, timeout=1)
            self.assertEqual(
                self.state["manual_signal_drafts"]["draft2"]["trade_thesis"],
                "Second updated",
            )
            release_first.set()
            await first_task

        self.assertEqual(
            self.state["manual_signal_drafts"]["draft"]["trade_thesis"],
            "First updated",
        )

    async def test_publish_rejects_wrong_author_channel_message_and_staff(self):
        client, _tree, view = await self.start_bot()
        self.draft_record()
        base = dict(
            id=400,
            author=client.user,
            channel=SimpleNamespace(id=300),
            type=discord.MessageType.default,
            attachments=[self.attachment()],
            edit=AsyncMock(),
        )
        cases = (
            self.interaction(client, user=self.user(2), message=SimpleNamespace(**base)),
            self.interaction(client, channel_id=301, message=SimpleNamespace(**base)),
            self.interaction(client, message=SimpleNamespace(**{**base, "id": 401})),
            self.interaction(
                client,
                message=SimpleNamespace(**{**base, "author": SimpleNamespace(id=998)}),
            ),
            self.interaction(
                client,
                message=SimpleNamespace(
                    **{**base, "type": discord.MessageType.pins_add}
                ),
            ),
        )
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(earnings_reactions, "update_state") as update:
            for interaction in cases:
                await self.button(view, "manual_signal_publish").callback(interaction)
        self.signals.send.assert_not_awaited()
        update.assert_not_called()
        self.state["manual_signal_drafts"]["draft"]["chart"] = {
            "filename": "chart.gif",
            "content_type": "image/gif",
        }
        malformed = self.interaction(client, message=SimpleNamespace(**base))
        with patch.object(
            earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
        ), patch.object(earnings_reactions, "update_state") as update:
            await self.button(view, "manual_signal_publish").callback(malformed)
        self.signals.send.assert_not_awaited()
        update.assert_not_called()

    async def test_definite_ambiguous_and_confirmation_failures(self):
        async def run_failure(send_effect, update_effect=None):
            self.state["manual_signal_drafts"] = {}
            self.draft_record()
            self.signals.send.reset_mock()
            self.signals.send.side_effect = send_effect
            client, _tree, view = await self.start_bot()
            message = SimpleNamespace(
                id=400,
                author=client.user,
                channel=SimpleNamespace(id=300),
                type=discord.MessageType.default,
                attachments=[self.attachment()],
                edit=AsyncMock(),
            )
            interaction = self.interaction(client, message=message)
            updater = update_effect or self.transactional_update
            with patch.object(
                earnings_reactions, "load_state", side_effect=lambda: copy.deepcopy(self.state)
            ), patch.object(
                earnings_reactions, "update_state", side_effect=updater
            ):
                await self.button(view, "manual_signal_publish").callback(interaction)
            return self.state["manual_signal_drafts"]["draft"]

        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", text=""),
            "Forbidden",
        )
        record = await run_failure(forbidden)
        self.assertEqual(record["delivery_status"], "ready")
        record = await run_failure(TimeoutError("uncertain"))
        self.assertEqual(record["delivery_status"], "unknown")

        calls = 0

        def fail_confirmation(mutation):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise earnings_reactions.EarningsStateError("synthetic")
            return self.transactional_update(mutation)

        record = await run_failure(None, fail_confirmation)
        self.assertEqual(record["delivery_status"], "sending")


if __name__ == "__main__":
    unittest.main()
