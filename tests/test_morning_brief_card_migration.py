import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import migrate_morning_brief_cards as migration


WEBHOOK_ID = "700"
CHANNEL_ID = "800"


def plain_message(message_id="100", *, webhook_id=WEBHOOK_ID, pinned=False):
    return {
        "id": message_id,
        "timestamp": f"2026-08-10T10:00:{int(message_id) % 60:02d}+00:00",
        "content": "# 🌅 Main Line Trades Morning Brief\n\nOriginal historical text.",
        "webhook_id": webhook_id,
        "pinned": pinned,
        "embeds": [],
        "attachments": [],
    }


def card_response(message, new_id="900"):
    return {
        "id": new_id,
        "content": "",
        "embeds": [
            {
                "description": message["content"],
                "color": migration.BRAND_ELECTRIC_BLUE,
            }
        ],
        "attachments": [],
    }


class MorningBriefCardMigrationTests(unittest.TestCase):
    def test_audit_protects_other_and_pinned_messages(self):
        other = plain_message("101", webhook_id="other")
        pinned = plain_message("102", pinned=True)
        carded = card_response(plain_message("103"), "103")
        carded.update({"webhook_id": WEBHOOK_ID, "pinned": False})

        result = migration.audit(
            [plain_message("100"), other, pinned, carded], WEBHOOK_ID
        )

        self.assertEqual(
            result["classifications"],
            {
                "already_carded": 1,
                "migratable_plain_brief": 1,
                "other_message": 1,
                "protected_pinned": 1,
            },
        )
        self.assertEqual(
            [item["message_id"] for item in result["migration_candidates"]],
            ["100"],
        )

    def test_plan_preserves_exact_historical_content_hash(self):
        message = plain_message("100")
        plan = migration.dry_run_plan([message], WEBHOOK_ID, "100")

        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(
            plan["candidates"][0]["content_sha256"],
            hashlib.sha256(message["content"].encode("utf-8")).hexdigest(),
        )

    def test_validation_rejects_pinned_rich_and_oversized_messages(self):
        cases = [plain_message("100", pinned=True)]
        rich = plain_message("101")
        rich["embeds"] = [{"description": "existing"}]
        cases.append(rich)
        oversized = plain_message("102")
        oversized["content"] = "x" * 4097
        cases.append(oversized)

        for message in cases:
            with self.subTest(message_id=message["id"]):
                with self.assertRaises(RuntimeError):
                    migration.validate_candidate(message)

    def test_success_posts_verified_card_before_deleting_original(self):
        message = plain_message("100")
        events = []

        def post(*args):
            events.append("post")
            return card_response(message)

        def delete(*args):
            events.append("delete")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration, "post_card", side_effect=post
        ), patch.object(migration, "delete_message", side_effect=delete):
            root = Path(directory)
            completed = migration.apply_migration(
                token="token",
                webhook_url="https://discord.test/webhook",
                channel_id=CHANNEL_ID,
                candidates=[message],
                state_path=root / "state.json",
                backup_path=root / "backup.json",
                limit=1,
            )
            state = json.loads((root / "state.json").read_text("utf-8"))
            backup = json.loads((root / "backup.json").read_text("utf-8"))

        self.assertEqual(completed, 1)
        self.assertEqual(events, ["post", "delete"])
        self.assertEqual(state["records"]["100"]["status"], "complete")
        self.assertEqual(state["records"]["100"]["new_message_id"], "900")
        self.assertEqual(backup[0]["content"], message["content"])

    def test_ambiguous_post_blocks_retry_and_never_deletes(self):
        message = plain_message("100")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration,
            "post_card",
            side_effect=TimeoutError("synthetic timeout"),
        ), patch.object(migration, "delete_message") as delete:
            root = Path(directory)
            with self.assertRaises(TimeoutError):
                migration.apply_migration(
                    token="token",
                    webhook_url="https://discord.test/webhook",
                    channel_id=CHANNEL_ID,
                    candidates=[message],
                    state_path=root / "state.json",
                    backup_path=root / "backup.json",
                    limit=1,
                )
            state = json.loads((root / "state.json").read_text("utf-8"))

        self.assertEqual(state["records"]["100"]["status"], "unknown")
        delete.assert_not_called()

    def test_delete_failure_resumes_without_second_post(self):
        message = plain_message("100")
        new_message = card_response(message)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration, "post_card", return_value=new_message
        ) as post, patch.object(
            migration,
            "delete_message",
            side_effect=[RuntimeError("delete failed"), None],
        ), patch.object(
            migration, "fetch_message", return_value=new_message
        ) as fetch:
            root = Path(directory)
            arguments = {
                "token": "token",
                "webhook_url": "https://discord.test/webhook",
                "channel_id": CHANNEL_ID,
                "candidates": [message],
                "state_path": root / "state.json",
                "backup_path": root / "backup.json",
                "limit": 1,
            }
            with self.assertRaisesRegex(RuntimeError, "delete failed"):
                migration.apply_migration(**arguments)
            completed = migration.apply_migration(**arguments)

        self.assertEqual(completed, 1)
        post.assert_called_once()
        fetch.assert_called_once_with("token", CHANNEL_ID, "900")


if __name__ == "__main__":
    unittest.main()
