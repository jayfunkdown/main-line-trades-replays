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
BRAND_NEON_PINK = 0xFF2BD6
USER_AGENT = "MainLineTrades-SignalCardMigration/1.0"
SIGNAL_PREFIX = "# 📈 Trade Signal"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def discord_json(token: str, path: str, parameters=None):
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
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_channel_history(token: str, channel_id: str):
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
            break
        before = str(batch[-1]["id"])

    return messages


def embed_descriptions(message):
    return [
        str(embed.get("description") or "")
        for embed in message.get("embeds") or []
        if isinstance(embed, dict)
    ]


def is_trade_signal(message) -> bool:
    text = "\n".join(
        [
            str(message.get("content") or ""),
            *embed_descriptions(message),
        ]
    )
    return "Trade Signal" in text


def is_pink_card(message) -> bool:
    return any(
        embed.get("color") == BRAND_NEON_PINK
        for embed in message.get("embeds") or []
        if isinstance(embed, dict)
    )


def chart_source_count(message) -> int:
    attachment_count = len(message.get("attachments") or [])
    embed_image_count = sum(
        bool((embed.get("image") or {}).get("url"))
        for embed in message.get("embeds") or []
        if isinstance(embed, dict)
    )
    return attachment_count + embed_image_count


def legacy_signal_description(message) -> str | None:
    content = str(message.get("content") or "")
    if content.startswith(SIGNAL_PREFIX):
        return content
    return None


def classification(message) -> str:
    author = message.get("author") or {}
    automated = bool(author.get("bot")) or bool(message.get("webhook_id"))

    if not is_trade_signal(message):
        return "non_signal"
    if not automated:
        return "human_signal"
    if is_pink_card(message):
        return "already_carded"
    if legacy_signal_description(message) is None:
        return "automated_unrecognized_format"
    if chart_source_count(message) < 1:
        return "automated_missing_chart"
    return "migratable_automated_signal"


def migration_candidates(messages, author_id: str, through_message_id: str):
    cutoff = int(through_message_id)
    candidates = []

    for message in messages:
        message_id = str(message.get("id") or "")
        author = message.get("author") or {}

        if classification(message) != "migratable_automated_signal":
            continue
        if str(author.get("id") or "") != author_id:
            continue
        if not message_id.isdigit() or int(message_id) > cutoff:
            continue

        candidates.append(message)

    return sorted(candidates, key=lambda item: int(item["id"]))


def validate_candidate(message):
    description = legacy_signal_description(message)
    if description is None:
        raise RuntimeError("Candidate does not use the recognized Trade Signal format.")
    if len(description) > 4096:
        raise RuntimeError("Candidate description exceeds Discord's embed limit.")
    if message.get("embeds"):
        raise RuntimeError("Candidate unexpectedly contains an existing embed.")

    attachments = message.get("attachments") or []
    if len(attachments) != 1:
        raise RuntimeError("Candidate must contain exactly one chart attachment.")

    attachment = attachments[0]
    content_type = str(attachment.get("content_type") or "")
    if not content_type.startswith("image/"):
        raise RuntimeError("Candidate attachment is not an image.")

    filename = Path(str(attachment.get("filename") or "")).name
    if not filename:
        raise RuntimeError("Candidate attachment filename is missing.")

    size = attachment.get("size")
    if isinstance(size, int) and size > MAX_ATTACHMENT_BYTES:
        raise RuntimeError("Candidate attachment exceeds the migration size limit.")

    attachment_url = str(attachment.get("url") or "")
    if not attachment_url.startswith("https://"):
        raise RuntimeError("Candidate attachment URL is invalid.")

    return {
        "description": description,
        "filename": filename,
        "content_type": content_type,
        "attachment_url": attachment_url,
    }


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
    records = value.get("records")
    if not isinstance(records, dict):
        raise RuntimeError("Migration state records are invalid.")
    return value


def ensure_backup(path: Path, candidates):
    expected_ids = [str(message["id"]) for message in candidates]
    if path.exists():
        with path.open("r", encoding="utf-8") as source:
            existing = json.load(source)
        existing_ids = [str(message.get("id") or "") for message in existing]
        if not set(expected_ids).issubset(existing_ids):
            raise RuntimeError("Existing migration backup does not cover the plan.")
        return existing
    atomic_private_json(path, candidates)
    return candidates


def download_attachment(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise RuntimeError("Downloaded attachment exceeds the migration size limit.")
    return data


def multipart_body(payload, filename, content_type, file_data):
    boundary = f"----MainLineTrades{uuid.uuid4().hex}"
    safe_filename = filename.replace('"', "_")
    json_data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    chunks = [
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json_data,
        b"\r\n",
        f"--{boundary}\r\n".encode("ascii"),
        (
            'Content-Disposition: form-data; name="files[0]"; '
            f'filename="{safe_filename}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
        file_data,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return boundary, b"".join(chunks)


def post_card(token, channel_id, candidate, file_data):
    payload = {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "description": candidate["description"],
                "color": BRAND_NEON_PINK,
                "image": {"url": f"attachment://{candidate['filename']}"},
            }
        ],
        "attachments": [
            {
                "id": 0,
                "filename": candidate["filename"],
            }
        ],
    }
    boundary, body = multipart_body(
        payload,
        candidate["filename"],
        candidate["content_type"],
        file_data,
    )
    request = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f"Discord create returned HTTP {response.status}.")
        return json.loads(response.read().decode("utf-8"))


def verify_new_message(message, candidate):
    embeds = message.get("embeds") or []
    attachments = message.get("attachments") or []
    if len(embeds) != 1 or len(attachments) != 1:
        raise RuntimeError("Created card response has an unexpected structure.")
    if embeds[0].get("description") != candidate["description"]:
        raise RuntimeError("Created card description does not match the original.")
    if embeds[0].get("color") != BRAND_NEON_PINK:
        raise RuntimeError("Created card color is incorrect.")
    if attachments[0].get("filename") != candidate["filename"]:
        raise RuntimeError("Created card attachment name is incorrect.")


def fetch_message(token, channel_id, message_id):
    return discord_json(token, f"/channels/{channel_id}/messages/{message_id}")


def delete_message(token, channel_id, message_id):
    request = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": USER_AGENT,
        },
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


def dry_run_plan(messages, author_id, through_message_id):
    candidates = migration_candidates(messages, author_id, through_message_id)
    entries = []
    for message in candidates:
        candidate = validate_candidate(message)
        entries.append(
            {
                "message_id": str(message["id"]),
                "timestamp": message.get("timestamp"),
                "content_sha256": hashlib.sha256(
                    candidate["description"].encode("utf-8")
                ).hexdigest(),
                "filename": candidate["filename"],
            }
        )
    return {"candidate_count": len(entries), "candidates": entries}


def apply_migration(
    *,
    token,
    channel_id,
    candidates,
    state_path,
    backup_path,
    limit,
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
            raise RuntimeError(
                f"Message {message_id} requires manual reconciliation."
            )

        if record and record.get("status") == "posted":
            new_message_id = str(record.get("new_message_id") or "")
            new_message = fetch_message(token, channel_id, new_message_id)
            verify_new_message(new_message, candidate)
        else:
            file_data = download_attachment(candidate["attachment_url"])
            records[message_id] = {
                "status": "reserved",
                "content_sha256": content_hash,
                "filename": candidate["filename"],
                "new_message_id": None,
            }
            atomic_private_json(state_path, state)

            try:
                new_message = post_card(token, channel_id, candidate, file_data)
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

            new_message_id = str(new_message["id"])
            records[message_id].update(
                {
                    "status": "posted",
                    "new_message_id": new_message_id,
                }
            )
            atomic_private_json(state_path, state)

        delete_message(token, channel_id, message_id)
        records[message_id]["status"] = "complete"
        atomic_private_json(state_path, state)
        completed += 1
        print(f"Migrated signal {message_id} to {new_message_id}.")

    return completed


def audit(messages):
    counts = Counter(classification(message) for message in messages)
    author_counts = Counter(
        (
            str((message.get("author") or {}).get("username") or ""),
            str((message.get("author") or {}).get("id") or ""),
            str(message.get("webhook_id") or ""),
            classification(message),
        )
        for message in messages
    )

    candidates = []
    for message in messages:
        if classification(message) != "migratable_automated_signal":
            continue

        content = str(message.get("content") or "")
        attachments = message.get("attachments") or []
        embeds = [
            embed
            for embed in message.get("embeds") or []
            if isinstance(embed, dict)
        ]
        candidates.append(
            {
                "message_id": str(message.get("id") or ""),
                "timestamp": message.get("timestamp"),
                "content_length": len(content),
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "attachment_count": len(attachments),
                "attachment_names": [
                    str(attachment.get("filename") or "")
                    for attachment in attachments
                ],
                "attachment_types": [
                    str(attachment.get("content_type") or "")
                    for attachment in attachments
                ],
                "embed_count": len(embeds),
                "embed_description_lengths": [
                    len(str(embed.get("description") or ""))
                    for embed in embeds
                ],
                "embed_image_hosts": [
                    urllib.parse.urlparse(
                        str((embed.get("image") or {}).get("url") or "")
                    ).netloc
                    for embed in embeds
                    if (embed.get("image") or {}).get("url")
                ],
            }
        )

    return {
        "total_messages": len(messages),
        "oldest_timestamp": messages[-1].get("timestamp") if messages else None,
        "newest_timestamp": messages[0].get("timestamp") if messages else None,
        "classifications": dict(sorted(counts.items())),
        "authors": [
            {
                "count": count,
                "username": key[0],
                "author_id": key[1],
                "webhook_id": key[2] or None,
                "classification": key[3],
            }
            for key, count in sorted(
                author_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "migration_candidates": sorted(
            candidates,
            key=lambda item: item["timestamp"] or "",
        ),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Audit or migrate historical Signals posts into cards."
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--audit",
        action="store_true",
        help="Read and classify channel history without changing Discord.",
    )
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the exact migration plan without changes.",
    )
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Apply the confirmed recreate-before-delete migration.",
    )
    parser.add_argument("--author-id")
    parser.add_argument("--through-message-id")
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--confirm-channel-id")
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = required_env("DISCORD_BOT_TOKEN")
    channel_id = required_env("SIGNALS_CHANNEL_ID")

    if args.audit:
        messages = fetch_channel_history(token, channel_id)
        print(json.dumps(audit(messages), indent=2))
        return 0

    if not args.author_id or not args.through_message_id:
        raise RuntimeError(
            "--author-id and --through-message-id are required for migration."
        )
    if not args.author_id.isdigit() or not args.through_message_id.isdigit():
        raise RuntimeError("Migration author and cutoff IDs must be numeric.")

    if args.apply:
        if args.expect_count is None:
            raise RuntimeError("--expect-count is required with --apply.")
        if args.confirm_channel_id != channel_id:
            raise RuntimeError(
                "--confirm-channel-id does not match SIGNALS_CHANNEL_ID."
            )
        if args.state_path is None or args.backup_path is None:
            raise RuntimeError(
                "--state-path and --backup-path are required with --apply."
            )
        if not args.state_path.is_absolute() or not args.backup_path.is_absolute():
            raise RuntimeError("Migration state and backup paths must be absolute.")
        if args.state_path == args.backup_path:
            raise RuntimeError("Migration state and backup paths must be different.")
        if args.limit < 1:
            raise RuntimeError("--limit must be at least 1.")

    messages = fetch_channel_history(token, channel_id)

    plan = dry_run_plan(messages, args.author_id, args.through_message_id)
    comparison_count = plan["candidate_count"]
    if args.apply and args.backup_path is not None and args.backup_path.exists():
        with args.backup_path.open("r", encoding="utf-8") as source:
            existing_backup = json.load(source)
        if not isinstance(existing_backup, list):
            raise RuntimeError("Existing migration backup is invalid.")
        comparison_count = len(existing_backup)

    if args.expect_count is not None and comparison_count != args.expect_count:
        raise RuntimeError(
            f"Expected {args.expect_count} candidates, found "
            f"{comparison_count}."
        )

    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0

    candidates = migration_candidates(
        messages,
        args.author_id,
        args.through_message_id,
    )
    completed = apply_migration(
        token=token,
        channel_id=channel_id,
        candidates=candidates,
        state_path=args.state_path,
        backup_path=args.backup_path,
        limit=args.limit,
    )
    print(f"Completed {completed} signal migration(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
