#!/usr/bin/env python3
"""One-off helper: reset test post-signal reviews and clear draft channel."""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from dotenv import load_dotenv

from scripts import earnings_reactions as er

EASTERN = ZoneInfo("America/New_York")

DEFAULT_TEST_IDS = [
    "1537780212706840621",  # LNSR
    "1537781066247442472",  # MH
    "1537783363107168318",  # SWMR
    "1537784242237345914",  # OXBR
]

POP_FIELDS = [
    "draft_channel_id",
    "draft_message_id",
    "draft_created_at",
    "original_review_chart_filename",
    "updated_review_chart_filename",
    "review_attempt_id",
    "current_price",
    "last_error",
    "public_channel_id",
    "public_message_id",
]


def reset_reviews(review_ids: list[str], now_iso: str) -> str:
    def mutation(state: dict) -> str:
        reviews = state.setdefault("post_signal_reviews", {})
        changed: list[str] = []
        for review_id in review_ids:
            record = reviews.get(review_id)
            if not isinstance(record, dict):
                changed.append(f"{review_id}: missing")
                continue
            rec = copy.deepcopy(record)
            for field in POP_FIELDS:
                rec.pop(field, None)
            rec["review_status"] = er.POST_SIGNAL_REVIEW_SCHEDULED
            rec["review_due_at"] = now_iso
            rec["comparison_chart_verified"] = False
            rec["updated_at"] = now_iso
            history = copy.deepcopy(rec.get("review_history", []))
            history.append(
                {
                    "action": "test_reset",
                    "at": now_iso,
                    "review_cycle": rec.get("review_cycle", 1),
                }
            )
            rec["review_history"] = history
            if not er.is_valid_post_signal_review_record(rec):
                changed.append(f"{review_id}: invalid")
                continue
            reviews[review_id] = rec
            changed.append(f"{rec['symbol']}: scheduled")
        return "reset:" + ", ".join(changed)

    _state, outcome = er.update_state(mutation)
    return outcome


async def clear_draft_channel(channel_id: int, token: str) -> tuple[int, int]:
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    deleted = 0
    skipped = 0

    @client.event
    async def on_ready() -> None:
        nonlocal deleted, skipped
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        async for message in channel.history(limit=200):
            if message.author.id != client.user.id:
                skipped += 1
                continue
            await message.delete()
            deleted += 1
        await client.close()

    await client.start(token)
    return deleted, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-clear",
        action="store_true",
        help="Only reset state; do not delete draft-channel bot messages.",
    )
    parser.add_argument(
        "--review-id",
        action="append",
        dest="review_ids",
        help="Review ID to reset (repeatable). Defaults to chart test set.",
    )
    args = parser.parse_args()

    load_dotenv()
    review_ids = args.review_ids or DEFAULT_TEST_IDS
    now_iso = datetime.now(EASTERN).isoformat()

    state_path = Path("data/earnings_reactions_state.json")
    backup_path = state_path.with_suffix(
        f".json.bak-test-reset-{datetime.now(EASTERN).strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(state_path, backup_path)
    print(f"Backed up state to {backup_path.name}")

    outcome = reset_reviews(review_ids, now_iso)
    print("State update:", outcome)

    if not args.skip_clear:
        channel_id = int(os.environ["SIGNAL_REVIEW_DRAFTS_CHANNEL_ID"])
        token = os.environ["DISCORD_BOT_TOKEN"]
        deleted, skipped = asyncio.run(clear_draft_channel(channel_id, token))
        print(f"Draft channel: deleted={deleted} skipped_non_bot={skipped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
