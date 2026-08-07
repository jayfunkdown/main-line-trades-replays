import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scripts import earnings_reactions


class FakeHttpError(Exception):
    pass


class FakeNotFound(FakeHttpError):
    pass


class FakeMessage:
    def __init__(
        self,
        message_id,
        *,
        created_at,
        author_id=99,
        pinned=False,
        message_type="default",
        delete_error=None,
    ):
        self.id = message_id
        self.created_at = created_at
        self.author = SimpleNamespace(id=author_id)
        self.pinned = pinned
        self.type = message_type
        self.delete_error = delete_error
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error


class FakeChannel:
    def __init__(self, bulk_error=None):
        self.bulk_error = bulk_error
        self.bulk_calls = []

    async def delete_messages(self, messages):
        self.bulk_calls.append(list(messages))
        if self.bulk_error is not None:
            raise self.bulk_error


class ReviewSelectionTests(unittest.TestCase):
    def test_selects_only_handled_messages_for_configured_channel(self):
        state = {
            "signal_queue": {
                "handled": {
                    "review_message_id": "101",
                    "review_channel_id": "500",
                    "sent_to_signals": True,
                },
                "unhandled": {
                    "review_message_id": "102",
                    "review_channel_id": "500",
                    "sent_to_signals": False,
                },
                "wrong_channel": {
                    "review_message_id": "103",
                    "review_channel_id": "501",
                    "sent_to_signals": True,
                },
                "missing_id": {
                    "review_channel_id": "500",
                    "sent_to_signals": True,
                },
            }
        }
        original_state = copy.deepcopy(state)

        result = earnings_reactions.handled_review_message_ids(
            state,
            500,
        )

        self.assertEqual(result, [101])
        self.assertEqual(state, original_state)

    def test_channel_check_requires_exact_configured_channel(self):
        self.assertTrue(
            earnings_reactions.is_configured_review_channel("500", 500)
        )
        self.assertFalse(
            earnings_reactions.is_configured_review_channel("501", 500)
        )


class AuthorizationTests(unittest.TestCase):
    def member(self, user_id, *, administrator=False, manage=False):
        return SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(
                administrator=administrator,
                manage_messages=manage,
            ),
        )

    def test_allows_owner_administrator_and_message_manager(self):
        guild = SimpleNamespace(owner_id=1)

        self.assertTrue(
            earnings_reactions.can_clear_earnings_review(
                self.member(1), guild
            )
        )
        self.assertTrue(
            earnings_reactions.can_clear_earnings_review(
                self.member(2, administrator=True), guild
            )
        )
        self.assertTrue(
            earnings_reactions.can_clear_earnings_review(
                self.member(3, manage=True), guild
            )
        )

    def test_rejects_regular_member_and_direct_message(self):
        guild = SimpleNamespace(owner_id=1)

        self.assertFalse(
            earnings_reactions.can_clear_earnings_review(
                self.member(4), guild
            )
        )
        self.assertFalse(
            earnings_reactions.can_clear_earnings_review(
                self.member(1), None
            )
        )


class MessageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def message(self, **overrides):
        values = {
            "message_id": 101,
            "created_at": self.now,
            "author_id": 99,
            "pinned": False,
            "message_type": "default",
        }
        values.update(overrides)
        return FakeMessage(**values)

    def test_requires_bot_author_normal_type_and_unpinned_message(self):
        valid = self.message()
        wrong_author = self.message(author_id=100)
        pinned = self.message(pinned=True)
        system_message = self.message(message_type="system")

        self.assertTrue(
            earnings_reactions.is_safe_review_message(
                valid, 99, "default"
            )
        )
        for message in (wrong_author, pinned, system_message):
            self.assertFalse(
                earnings_reactions.is_safe_review_message(
                    message, 99, "default"
                )
            )

    def test_partitions_old_messages_for_individual_deletion(self):
        recent = self.message()
        old = self.message(
            message_id=102,
            created_at=self.now - timedelta(days=14),
        )

        bulk, individual = (
            earnings_reactions.partition_review_messages_for_deletion(
                [recent, old],
                self.now,
            )
        )

        self.assertEqual(bulk, [recent])
        self.assertEqual(individual, [old])


class DeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_deletes_recent_and_individually_deletes_old(self):
        now = datetime.now(timezone.utc)
        recent = [
            FakeMessage(101, created_at=now),
            FakeMessage(102, created_at=now),
        ]
        old = FakeMessage(
            103,
            created_at=now - timedelta(days=14),
        )
        channel = FakeChannel()

        result = await earnings_reactions.delete_review_messages_safely(
            channel,
            recent + [old],
            now_utc=now,
            http_error_types=(FakeHttpError,),
            not_found_type=FakeNotFound,
        )

        self.assertEqual(channel.bulk_calls, [recent])
        self.assertEqual(old.delete_calls, 1)
        self.assertEqual(
            result,
            {"deleted": 3, "missing": 0, "failed": 0},
        )

    async def test_bulk_failure_falls_back_to_individual_deletes(self):
        now = datetime.now(timezone.utc)
        messages = [
            FakeMessage(101, created_at=now),
            FakeMessage(102, created_at=now),
        ]
        channel = FakeChannel(bulk_error=FakeHttpError())

        result = await earnings_reactions.delete_review_messages_safely(
            channel,
            messages,
            now_utc=now,
            http_error_types=(FakeHttpError,),
            not_found_type=FakeNotFound,
        )

        self.assertEqual([item.delete_calls for item in messages], [1, 1])
        self.assertEqual(
            result,
            {"deleted": 2, "missing": 0, "failed": 0},
        )

    async def test_reports_missing_and_failed_individual_deletes(self):
        now = datetime.now(timezone.utc)
        missing = FakeMessage(
            101,
            created_at=now - timedelta(days=14),
            delete_error=FakeNotFound(),
        )
        failed = FakeMessage(
            102,
            created_at=now - timedelta(days=14),
            delete_error=FakeHttpError(),
        )

        result = await earnings_reactions.delete_review_messages_safely(
            FakeChannel(),
            [missing, failed],
            now_utc=now,
            http_error_types=(FakeHttpError,),
            not_found_type=FakeNotFound,
        )

        self.assertEqual(
            result,
            {"deleted": 0, "missing": 1, "failed": 1},
        )


if __name__ == "__main__":
    unittest.main()
