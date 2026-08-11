import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from scripts import migrate_signal_cards as migration


BOT_ID = "1533400472508039245"
CHANNEL_ID = "123"


def legacy_message(message_id="100", *, author_id=BOT_ID, content=None):
    description = content or (
        "# 📈 Trade Signal\n\n"
        "ACME\n\n"
        "🧠 **Trade Thesis**\n\n"
        "Weekly continuation."
    )
    return {
        "id": message_id,
        "timestamp": f"2026-08-10T11:00:{int(message_id) % 60:02d}.000000+00:00",
        "type": 0,
        "content": description,
        "author": {
            "id": author_id,
            "username": "Main Line Trades Feed Filter",
            "bot": author_id == BOT_ID,
        },
        "webhook_id": None,
        "embeds": [],
        "attachments": [
            {
                "filename": f"ACME_{message_id}.png",
                "content_type": "image/png",
                "size": 100,
                "url": f"https://cdn.discordapp.com/attachments/{message_id}.png",
            }
        ],
    }


def card_response(message, new_id="900"):
    candidate = migration.validate_candidate(message)
    return {
        "id": new_id,
        "embeds": [
            {
                "description": candidate["description"],
                "color": migration.BRAND_NEON_PINK,
                "image": {"url": "https://cdn.discordapp.com/new.png"},
            }
        ],
        "attachments": [{"filename": candidate["filename"]}],
    }


def hidden_attachment_card_response(message, new_id="900"):
    response = card_response(message, new_id)
    response["attachments"] = []
    response["embeds"][0]["image"]["url"] = (
        "https://cdn.discordapp.com/attachments/channel/message/ACME.png"
    )
    return response


class FakeResponse:
    status = 200

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class SignalCardMigrationTests(unittest.TestCase):
    def test_audit_separates_human_legacy_and_carded_posts(self):
        human = legacy_message("101", author_id="human")
        human["author"]["bot"] = False
        carded = legacy_message("102")
        carded["content"] = ""
        carded["attachments"] = []
        carded["embeds"] = [
            {
                "description": "# 📈 Trade Signal\n\nAlready carded",
                "color": migration.BRAND_NEON_PINK,
            }
        ]
        other = legacy_message("103")
        other["content"] = "Member discussion"
        other["attachments"] = []

        result = migration.audit(
            [legacy_message("100"), human, carded, other]
        )

        self.assertEqual(
            result["classifications"],
            {
                "already_carded": 1,
                "human_signal": 1,
                "migratable_automated_signal": 1,
                "non_signal": 1,
            },
        )
        self.assertEqual(len(result["migration_candidates"]), 1)
        self.assertEqual(
            result["migration_candidates"][0]["message_id"],
            "100",
        )

    def test_plan_requires_exact_author_cutoff_and_safe_structure(self):
        later = legacy_message("300")
        wrong_author = legacy_message("150", author_id="other-bot")
        wrong_author["author"]["bot"] = True
        messages = [later, wrong_author, legacy_message("100")]

        plan = migration.dry_run_plan(messages, BOT_ID, "200")

        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(plan["candidates"][0]["message_id"], "100")
        self.assertEqual(plan["candidates"][0]["filename"], "ACME_100.png")
        self.assertEqual(len(plan["candidates"][0]["content_sha256"]), 64)

    def test_candidate_validation_fails_closed(self):
        cases = []

        missing_chart = legacy_message("100")
        missing_chart["attachments"] = []
        cases.append(missing_chart)

        multiple_charts = legacy_message("101")
        multiple_charts["attachments"] *= 2
        cases.append(multiple_charts)

        embedded = legacy_message("102")
        embedded["embeds"] = [{"description": "unexpected"}]
        cases.append(embedded)

        wrong_prefix = legacy_message("103", content="ACME trade idea")
        cases.append(wrong_prefix)

        for message in cases:
            with self.subTest(message_id=message["id"]):
                with self.assertRaises(RuntimeError):
                    migration.validate_candidate(message)

    def test_multipart_contains_exact_embed_and_chart(self):
        message = legacy_message("100")
        candidate = migration.validate_candidate(message)
        payload = {
            "embeds": [
                {
                    "description": candidate["description"],
                    "color": migration.BRAND_NEON_PINK,
                }
            ]
        }

        boundary, body = migration.multipart_body(
            payload,
            candidate["filename"],
            candidate["content_type"],
            b"synthetic-png",
        )

        self.assertIn(boundary.encode("ascii"), body)
        encoded_description = json.dumps(candidate["description"])[1:-1].encode(
            "utf-8"
        )
        self.assertIn(encoded_description, body)
        self.assertIn(b"ACME_100.png", body)
        self.assertIn(b"synthetic-png", body)

    def test_post_card_uses_one_pink_embed_and_reuploaded_chart(self):
        message = legacy_message("100")
        candidate = migration.validate_candidate(message)
        response = card_response(message)
        captured = {}

        def make_request(url, *, data, headers, method):
            captured.update(
                {
                    "url": url,
                    "data": data,
                    "headers": headers,
                    "method": method,
                }
            )
            return SimpleNamespace()

        with patch.object(
            migration.urllib.request,
            "Request",
            side_effect=make_request,
        ), patch.object(
            migration.urllib.request,
            "urlopen",
            return_value=FakeResponse(response),
        ):
            result = migration.post_card(
                "token",
                CHANNEL_ID,
                candidate,
                b"synthetic-png",
            )

        self.assertEqual(result, response)
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith(f"/channels/{CHANNEL_ID}/messages"))
        self.assertIn(b"attachment://ACME_100.png", captured["data"])
        self.assertIn(b'"color":16722902', captured["data"])
        self.assertIn(b'"allowed_mentions":{"parse":[]}', captured["data"])
        self.assertNotIn(b'"content":', captured["data"])

    def test_verification_accepts_discord_hidden_embed_attachment(self):
        message = legacy_message("100")
        candidate = migration.validate_candidate(message)

        migration.verify_new_message(
            hidden_attachment_card_response(message),
            candidate,
        )

    def test_verification_rejects_card_without_discord_chart_image(self):
        message = legacy_message("100")
        candidate = migration.validate_candidate(message)
        response = hidden_attachment_card_response(message)
        response["embeds"][0]["image"] = {}

        with self.assertRaisesRegex(RuntimeError, "chart image"):
            migration.verify_new_message(response, candidate)

    def test_unknown_reconciliation_verifies_existing_card_without_reposting(self):
        message = legacy_message("100")
        existing_card = hidden_attachment_card_response(message, "900")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration,
            "fetch_message",
            return_value=existing_card,
        ) as fetch:
            state_path = Path(directory) / "state.json"
            candidate = migration.validate_candidate(message)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "records": {
                            "100": {
                                "status": "unknown",
                                "content_sha256": migration.hashlib.sha256(
                                    candidate["description"].encode("utf-8")
                                ).hexdigest(),
                                "filename": candidate["filename"],
                                "new_message_id": None,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            migration.reconcile_unknown_message(
                token="token",
                channel_id=CHANNEL_ID,
                original_message=message,
                new_message_id="900",
                state_path=state_path,
            )
            state = json.loads(state_path.read_text("utf-8"))

        self.assertEqual(state["records"]["100"]["status"], "posted")
        self.assertEqual(state["records"]["100"]["new_message_id"], "900")
        fetch.assert_called_once_with("token", CHANNEL_ID, "900")

    def test_success_posts_before_delete_and_persists_complete_state(self):
        messages = [legacy_message("100"), legacy_message("101")]
        events = []

        def post(*args):
            message = messages[len(events) // 2]
            events.append(("post", message["id"]))
            return card_response(message, f"9{message['id']}")

        def delete(token, channel_id, message_id):
            events.append(("delete", message_id))

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration,
            "download_attachment",
            return_value=b"png",
        ), patch.object(
            migration,
            "post_card",
            side_effect=post,
        ), patch.object(
            migration,
            "delete_message",
            side_effect=delete,
        ):
            root = Path(directory) / "migration"
            completed = migration.apply_migration(
                token="token",
                channel_id=CHANNEL_ID,
                candidates=messages,
                state_path=root / "state.json",
                backup_path=root / "backup.json",
                limit=10,
            )
            state = json.loads((root / "state.json").read_text("utf-8"))
            backup = json.loads((root / "backup.json").read_text("utf-8"))

        self.assertEqual(completed, 2)
        self.assertEqual(
            events,
            [
                ("post", "100"),
                ("delete", "100"),
                ("post", "101"),
                ("delete", "101"),
            ],
        )
        self.assertEqual(len(backup), 2)
        self.assertTrue(
            all(record["status"] == "complete" for record in state["records"].values())
        )

    def test_definite_post_failure_is_retryable_and_does_not_delete(self):
        message = legacy_message("100")
        failure = urllib.error.HTTPError(
            "https://discord.test",
            400,
            "synthetic rejection",
            None,
            None,
        )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration,
            "download_attachment",
            return_value=b"png",
        ), patch.object(
            migration,
            "post_card",
            side_effect=failure,
        ), patch.object(migration, "delete_message") as delete:
            root = Path(directory) / "migration"
            with self.assertRaises(urllib.error.HTTPError):
                migration.apply_migration(
                    token="token",
                    channel_id=CHANNEL_ID,
                    candidates=[message],
                    state_path=root / "state.json",
                    backup_path=root / "backup.json",
                    limit=1,
                )
            state = json.loads((root / "state.json").read_text("utf-8"))

        self.assertEqual(state["records"]["100"]["status"], "failed")
        delete.assert_not_called()

    def test_ambiguous_post_failure_blocks_retry(self):
        message = legacy_message("100")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration,
            "download_attachment",
            return_value=b"png",
        ), patch.object(
            migration,
            "post_card",
            side_effect=urllib.error.URLError("synthetic connection loss"),
        ):
            root = Path(directory) / "migration"
            with self.assertRaises(urllib.error.URLError):
                migration.apply_migration(
                    token="token",
                    channel_id=CHANNEL_ID,
                    candidates=[message],
                    state_path=root / "state.json",
                    backup_path=root / "backup.json",
                    limit=1,
                )
            state = json.loads((root / "state.json").read_text("utf-8"))

        self.assertEqual(state["records"]["100"]["status"], "unknown")

    def test_delete_failure_resumes_without_second_post(self):
        message = legacy_message("100")
        new_message = card_response(message, "900")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration,
            "download_attachment",
            return_value=b"png",
        ), patch.object(
            migration,
            "post_card",
            return_value=new_message,
        ) as post, patch.object(
            migration,
            "delete_message",
            side_effect=[RuntimeError("synthetic delete failure"), None],
        ) as delete, patch.object(
            migration,
            "fetch_message",
            return_value=new_message,
        ) as fetch:
            root = Path(directory) / "migration"
            arguments = {
                "token": "token",
                "channel_id": CHANNEL_ID,
                "candidates": [message],
                "state_path": root / "state.json",
                "backup_path": root / "backup.json",
                "limit": 1,
            }

            with self.assertRaisesRegex(RuntimeError, "delete failure"):
                migration.apply_migration(**arguments)
            first_state = json.loads((root / "state.json").read_text("utf-8"))
            completed = migration.apply_migration(**arguments)

        self.assertEqual(first_state["records"]["100"]["status"], "posted")
        self.assertEqual(completed, 1)
        post.assert_called_once()
        fetch.assert_called_once_with("token", CHANNEL_ID, "900")
        self.assertEqual(delete.call_count, 2)

    def test_apply_confirmation_fails_before_history_fetch(self):
        args = SimpleNamespace(
            audit=False,
            dry_run=False,
            apply=True,
            reconcile_unknown=None,
            author_id=BOT_ID,
            through_message_id="200",
            expect_count=None,
            confirm_channel_id=CHANNEL_ID,
            state_path=Path("/tmp/state.json"),
            backup_path=Path("/tmp/backup.json"),
            limit=1,
        )
        parser = Mock()
        parser.parse_args.return_value = args

        with patch.object(
            migration,
            "build_parser",
            return_value=parser,
        ), patch.object(
            migration,
            "required_env",
            side_effect=["token", CHANNEL_ID],
        ), patch.object(
            migration,
            "fetch_channel_history",
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "expect-count"):
                migration.main()

        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
