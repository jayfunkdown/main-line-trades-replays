#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

DISCORD_API_BASE = "https://discord.com/api/v10"
BRAND_ELECTRIC_BLUE = 0x00CFFF
USER_AGENT = "MainLineTrades-MorningBriefCardMigration/1.0"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def request_json(request, timeout=30):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def webhook_metadata(webhook_url):
    request = urllib.request.Request(
        webhook_url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    value = request_json(request)
    webhook_id = str(value.get("id") or "")
    channel_id = str(value.get("channel_id") or "")
    if not webhook_id.isdigit() or not channel_id.isdigit():
        raise RuntimeError("Morning Brief webhook metadata is invalid.")
    return {"webhook_id": webhook_id, "channel_id": channel_id}


def discord_json(token, path, parameters=None):
    query = ""
    if parameters:
        query = "?" + urllib.parse.urlencode(parameters)
    request = urllib.request.Request(
        f"{DISCORD_API_BASE}{path}{query}",
        headers={
            "Authorization": f"Bot {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    return request_json(request)


def fetch_channel_history(token, channel_id):
    messages = []
    before = None
    while True:
        parameters = {"limit": 100}
        if before:
            parameters["before"] = before
        batch = discord_json(
            token,
            f"/channels/{channel_id}/messages",
            parameters,
        )
        if not isinstance(batch, list):
            raise RuntimeError("Discord returned an unexpected message response.")
        messages.extend(batch)
        if len(batch) < 100:
            return messages
        before = str(batch[-1]["id"])


def classification(message, webhook_id):
    if message.get("pinned"):
        return "protected_pinned"
    if str(message.get("webhook_id") or "") != webhook_id:
        return "other_message"

    content = str(message.get("content") or "")
    embeds = [
        embed
        for embed in message.get("embeds") or []
        if isinstance(embed, dict)
    ]
    attachments = message.get("attachments") or []
    if (
        not content
        and len(embeds) == 1
        and embeds[0].get("color") == BRAND_ELECTRIC_BLUE
        and str(embeds[0].get("description") or "")
    ):
        return "already_carded"
    if content and not embeds and not attachments:
        return "migratable_plain_brief"
    return "webhook_message_unrecognized"


def migration_candidates(messages, webhook_id, through_message_id):
    cutoff = int(through_message_id)
    return sorted(
        [
            message
            for message in messages
            if classification(message, webhook_id) == "migratable_plain_brief"
            and str(message.get("id") or "").isdigit()
            and int(message["id"]) <= cutoff
        ],
        key=lambda message: int(message["id"]),
    )


def validate_candidate(message):
    description = str(message.get("content") or "")
    if not description:
        raise RuntimeError("Candidate Morning Brief content is empty.")
    if len(description) > 4096:
        raise RuntimeError("Candidate Morning Brief exceeds the embed limit.")
    if message.get("pinned"):
        raise RuntimeError("Pinned Morning Brief messages cannot be migrated.")
    if message.get("embeds") or message.get("attachments"):
        raise RuntimeError("Candidate Morning Brief has unexpected rich content.")
    return {"description": description}


def atomic_private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def load_state(path: Path):
    if not path.exists():
        return {"version": 1, "records": {}}
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RuntimeError("Migration state is invalid.")
    if not isinstance(value.get("records"), dict):
        raise RuntimeError("Migration state records are invalid.")
    return value


def ensure_backup(path: Path, candidates):
    expected_ids = {str(message["id"]) for message in candidates}
    if path.exists():
        with path.open("r", encoding="utf-8") as source:
            existing = json.load(source)
        existing_ids = {str(message.get("id") or "") for message in existing}
        if not expected_ids.issubset(existing_ids):
            raise RuntimeError("Existing migration backup does not cover the plan.")
        return existing
    atomic_private_json(path, candidates)
    return candidates


def webhook_wait_url(webhook_url):
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}wait=true"


def post_card(webhook_url, candidate):
    payload = json.dumps(
        {
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "description": candidate["description"],
                    "color": BRAND_ELECTRIC_BLUE,
                }
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_wait_url(webhook_url),
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    return request_json(request)


def verify_new_message(message, candidate):
    embeds = message.get("embeds") or []
    if message.get("content") or len(embeds) != 1:
        raise RuntimeError("Created Morning Brief card has an unexpected structure.")
    if embeds[0].get("description") != candidate["description"]:
        raise RuntimeError("Created Morning Brief text does not match the original.")
    if embeds[0].get("color") != BRAND_ELECTRIC_BLUE:
        raise RuntimeError("Created Morning Brief card color is incorrect.")


def fetch_message(token, channel_id, message_id):
    return discord_json(token, f"/channels/{channel_id}/messages/{message_id}")


def delete_message(token, channel_id, message_id):
    request = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
        headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 204:
                raise RuntimeError(
                    f"Discord delete returned HTTP {response.status}."
                )
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


def dry_run_plan(messages, webhook_id, through_message_id):
    entries = []
    for message in migration_candidates(messages, webhook_id, through_message_id):
        candidate = validate_candidate(message)
        entries.append(
            {
                "message_id": str(message["id"]),
                "timestamp": message.get("timestamp"),
                "content_length": len(candidate["description"]),
                "content_sha256": hashlib.sha256(
                    candidate["description"].encode("utf-8")
                ).hexdigest(),
            }
        )
    return {"candidate_count": len(entries), "candidates": entries}


def apply_migration(
    *, token, webhook_url, channel_id, candidates, state_path, backup_path, limit
):
    ensure_backup(backup_path, candidates)
    state = load_state(state_path)
    records = state["records"]
    completed = 0
    for message in candidates:
        if completed >= limit:
            break
        message_id = str(message["id"])
        candidate = validate_candidate(message)
        content_hash = hashlib.sha256(
            candidate["description"].encode("utf-8")
        ).hexdigest()
        record = records.get(message_id)
        if record and record.get("content_sha256") != content_hash:
            raise RuntimeError(f"State hash mismatch for message {message_id}.")
        if record and record.get("status") == "complete":
            continue
        if record and record.get("status") in {"reserved", "unknown"}:
            raise RuntimeError(f"Message {message_id} requires reconciliation.")

        if record and record.get("status") == "posted":
            new_message_id = str(record.get("new_message_id") or "")
            verify_new_message(
                fetch_message(token, channel_id, new_message_id), candidate
            )
        else:
            records[message_id] = {
                "status": "reserved",
                "content_sha256": content_hash,
                "new_message_id": None,
            }
            atomic_private_json(state_path, state)
            try:
                new_message = post_card(webhook_url, candidate)
                verify_new_message(new_message, candidate)
            except urllib.error.HTTPError:
                records[message_id]["status"] = "failed"
                atomic_private_json(state_path, state)
                raise
            except (urllib.error.URLError, TimeoutError):
                records[message_id]["status"] = "unknown"
                atomic_private_json(state_path, state)
                raise
            except Exception:
                records[message_id]["status"] = "unknown"
                atomic_private_json(state_path, state)
                raise
            new_message_id = str(new_message.get("id") or "")
            if not new_message_id.isdigit():
                records[message_id]["status"] = "unknown"
                atomic_private_json(state_path, state)
                raise RuntimeError("Created Morning Brief message ID is invalid.")
            records[message_id].update(
                {"status": "posted", "new_message_id": new_message_id}
            )
            atomic_private_json(state_path, state)

        delete_message(token, channel_id, message_id)
        records[message_id]["status"] = "complete"
        atomic_private_json(state_path, state)
        completed += 1
        print(f"Migrated Morning Brief {message_id} to {new_message_id}.")
    return completed


def audit(messages, webhook_id):
    counts = Counter(classification(message, webhook_id) for message in messages)
    candidates = []
    cards = []
    for message in messages:
        kind = classification(message, webhook_id)
        if kind == "migratable_plain_brief":
            content = str(message.get("content") or "")
            candidates.append(
                {
                    "message_id": str(message.get("id") or ""),
                    "timestamp": message.get("timestamp"),
                    "content_length": len(content),
                    "content_sha256": hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                }
            )
        elif kind == "already_carded":
            description = str(message["embeds"][0].get("description") or "")
            cards.append(
                {
                    "message_id": str(message.get("id") or ""),
                    "timestamp": message.get("timestamp"),
                    "description_sha256": hashlib.sha256(
                        description.encode("utf-8")
                    ).hexdigest(),
                }
            )
    return {
        "total_messages": len(messages),
        "classifications": dict(sorted(counts.items())),
        "migration_candidates": sorted(
            candidates, key=lambda item: item["timestamp"] or ""
        ),
        "already_carded_messages": sorted(
            cards, key=lambda item: item["timestamp"] or ""
        ),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Audit or migrate historical Morning Briefs into cards."
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--audit", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--through-message-id")
    parser.add_argument(
        "--webhook-env",
        choices=("MORNING_BRIEF_WEBHOOK", "ECONOMIC_CALENDAR_WEBHOOK"),
        default="MORNING_BRIEF_WEBHOOK",
    )
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--confirm-channel-id")
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--limit", type=int, default=1)
    return parser


def main():
    args = build_parser().parse_args()
    token = required_env("DISCORD_BOT_TOKEN")
    webhook_url = required_env(args.webhook_env)
    metadata = webhook_metadata(webhook_url)
    channel_id = metadata["channel_id"]
    webhook_id = metadata["webhook_id"]
    messages = fetch_channel_history(token, channel_id)

    if args.audit:
        print(json.dumps(audit(messages, webhook_id), indent=2))
        return 0
    if not args.through_message_id or not args.through_message_id.isdigit():
        raise RuntimeError("A numeric --through-message-id is required.")

    plan = dry_run_plan(messages, webhook_id, args.through_message_id)
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0

    if args.confirm_channel_id != channel_id:
        raise RuntimeError("--confirm-channel-id does not match the webhook channel.")
    if args.expect_count is None:
        raise RuntimeError("--expect-count is required with --apply.")
    if args.state_path is None or args.backup_path is None:
        raise RuntimeError("State and backup paths are required with --apply.")
    if not args.state_path.is_absolute() or not args.backup_path.is_absolute():
        raise RuntimeError("Migration state and backup paths must be absolute.")
    if args.limit < 1:
        raise RuntimeError("--limit must be at least 1.")

    comparison_count = plan["candidate_count"]
    if args.backup_path.exists():
        with args.backup_path.open("r", encoding="utf-8") as source:
            existing_backup = json.load(source)
        comparison_count = len(existing_backup)
    if comparison_count != args.expect_count:
        raise RuntimeError(
            f"Expected {args.expect_count} candidates, found {comparison_count}."
        )

    candidates = migration_candidates(
        messages, webhook_id, args.through_message_id
    )
    completed = apply_migration(
        token=token,
        webhook_url=webhook_url,
        channel_id=channel_id,
        candidates=candidates,
        state_path=args.state_path,
        backup_path=args.backup_path,
        limit=args.limit,
    )
    print(f"Completed {completed} Morning Brief migration(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
