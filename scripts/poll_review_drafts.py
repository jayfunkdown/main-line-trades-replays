#!/usr/bin/env python3
"""Print post-signal review draft status for chart test symbols."""

from __future__ import annotations

import json
from pathlib import Path

TEST_IDS = [
    "1537780212706840621",
    "1537781066247442472",
    "1537783363107168318",
    "1537784242237345914",
]


def main() -> int:
    state = json.loads(Path("data/earnings_reactions_state.json").read_text())
    reviews = state.get("post_signal_reviews", {})
    ready = 0
    for review_id in TEST_IDS:
        record = reviews.get(review_id, {})
        symbol = record.get("symbol", "?")
        status = record.get("review_status", "?")
        draft_id = record.get("draft_message_id") or "-"
        due_at = (record.get("review_due_at") or "-")[:19]
        print(f"{symbol}\t{status}\t{draft_id}\t{due_at}")
        if status == "draft_ready":
            ready += 1
    print(f"ready_count={ready}")
    return 0 if ready == len(TEST_IDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
