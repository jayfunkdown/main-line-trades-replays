#!/usr/bin/env python3
from __future__ import annotations

from dotenv import load_dotenv

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.discord_embeds import BRAND_ELECTRIC_BLUE
except ModuleNotFoundError:
    from discord_embeds import BRAND_ELECTRIC_BLUE


# ============================================================
# Configuration
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
USER_AGENT = "MainLineTrades-ReplayBot/4.0"

STATE_PATH = PROJECT_ROOT / "data" / "posted_ids.json"

DEFAULT_TITLE_PREFIX = "🔴 Live Trading"
DEFAULT_FETCH_LIMIT = 25
DEFAULT_MAX_AGE_HOURS = 72

MAX_STATE_IDS = 500
MAX_DISCORD_ATTEMPTS = 4
POST_DELAY_SECONDS = 2.0

# Exact Discord embed appearance from the approved screenshot.
EMBED_COLOR = BRAND_ELECTRIC_BLUE
WEBHOOK_USERNAME = "Main Line Trades Replays"
EMBED_AUTHOR = "Main Line Trades"
EMBED_DESCRIPTION = (
    "Missed the live session? Watch the complete trading replay below."
)
EMBED_FOOTER = "Main Line Trades • Live Trading Replay"

# Used only by --test so the test has the same full visual layout,
# including the large image.
TEST_VIDEO_ID = "rGpqe4WquHM"
TEST_TITLE = "🔴 Live Trading Crypto Futures Forex Stocks - NY Open"
TEST_DURATION = "1h 54m"


# ============================================================
# Environment helpers
# ============================================================

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


# ============================================================
# State handling
# ============================================================

def load_processed_ids() -> list[str]:
    if not STATE_PATH.exists():
        return []

    try:
        value = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(value, list):
        return []

    return [str(item) for item in value]


def save_processed_ids(video_ids: list[str]) -> None:
    STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    unique_ids = list(
        dict.fromkeys(str(item) for item in video_ids)
    )

    STATE_PATH.write_text(
        json.dumps(
            unique_ids[-MAX_STATE_IDS:],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def mark_processed(
    video_id: str,
    processed_ids: list[str],
    processed_set: set[str],
) -> None:
    if video_id not in processed_set:
        processed_ids.append(video_id)
        processed_set.add(video_id)

    save_processed_ids(processed_ids)


# ============================================================
# YouTube API
# ============================================================

def youtube_api_get(
    endpoint: str,
    parameters: dict[str, str | int],
    api_key: str,
) -> dict[str, Any]:
    query = dict(parameters)
    query["key"] = api_key

    url = (
        f"{YOUTUBE_API_BASE}/{endpoint}?"
        f"{urllib.parse.urlencode(query)}"
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8")
        except UnicodeDecodeError:
            body = ""

        raise RuntimeError(
            f"YouTube API returned HTTP {error.code}: "
            f"{body or error.reason}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not reach YouTube API: {error.reason}"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError(
            "YouTube API returned an unexpected response."
        )

    return payload


def get_uploads_playlist_id(
    channel_id: str,
    api_key: str,
) -> str:
    response = youtube_api_get(
        "channels",
        {
            "part": "contentDetails",
            "id": channel_id,
            "maxResults": 1,
        },
        api_key,
    )

    items = response.get("items", [])

    if not items:
        raise RuntimeError(
            "YouTube channel was not found. "
            "Check YOUTUBE_CHANNEL_ID."
        )

    try:
        playlist_id = str(
            items[0]["contentDetails"]
            ["relatedPlaylists"]["uploads"]
        ).strip()
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(
            "YouTube did not return the uploads playlist."
        ) from error

    if not playlist_id:
        raise RuntimeError(
            "YouTube uploads playlist ID is empty."
        )

    return playlist_id


def parse_youtube_datetime(value: str) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def format_stream_date(value: str) -> str:
    parsed = parse_youtube_datetime(value)

    if parsed is None:
        return "Unknown"

    return parsed.astimezone(
        timezone.utc
    ).strftime("%A, %B %d, %Y")


def parse_iso_duration(value: str) -> str:
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        value or "",
    )

    if not match:
        return "Unknown"

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)

    if hours:
        return f"{hours}h {minutes:02d}m"

    if minutes:
        return f"{minutes}m {seconds:02d}s"

    return f"{seconds}s"


def best_thumbnail(
    thumbnails: dict[str, Any],
) -> str:
    for size_name in (
        "maxres",
        "standard",
        "high",
        "medium",
        "default",
    ):
        item = thumbnails.get(size_name) or {}

        if isinstance(item, dict):
            url = str(
                item.get("url") or ""
            ).strip()

            if url:
                return url

    return ""


def normalize_video_item(
    item: dict[str, Any],
) -> dict[str, str] | None:
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}

    if not isinstance(snippet, dict):
        return None

    if not isinstance(content_details, dict):
        content_details = {}

    video_id = str(item.get("id") or "").strip()
    title = str(snippet.get("title") or "").strip()

    if not video_id or not title:
        return None

    published = str(
        snippet.get("publishedAt") or ""
    ).strip()

    thumbnails = snippet.get("thumbnails") or {}

    if not isinstance(thumbnails, dict):
        thumbnails = {}

    return {
        "id": video_id,
        "title": title,
        "url": (
            "https://www.youtube.com/watch"
            f"?v={video_id}"
        ),
        "published": published,
        "duration": parse_iso_duration(
            str(
                content_details.get("duration")
                or ""
            )
        ),
        "thumbnail": best_thumbnail(thumbnails),
    }


def fetch_video_details(
    video_ids: list[str],
    api_key: str,
) -> list[dict[str, str]]:
    if not video_ids:
        return []

    response = youtube_api_get(
        "videos",
        {
            "part": "snippet,contentDetails",
            "id": ",".join(video_ids[:50]),
            "maxResults": min(len(video_ids), 50),
        },
        api_key,
    )

    videos: list[dict[str, str]] = []

    for item in response.get("items", []):
        if not isinstance(item, dict):
            continue

        video = normalize_video_item(item)

        if video is not None:
            videos.append(video)

    return videos


def fetch_video_by_id(
    video_id: str,
    api_key: str,
) -> dict[str, str]:
    videos = fetch_video_details(
        [video_id],
        api_key,
    )

    if not videos:
        raise RuntimeError(
            f"YouTube video {video_id} was not found "
            "or is not public."
        )

    return videos[0]


def fetch_matching_uploads(
    channel_id: str,
    api_key: str,
    title_prefix: str,
    fetch_limit: int,
) -> list[dict[str, str]]:
    playlist_id = get_uploads_playlist_id(
        channel_id,
        api_key,
    )

    response = youtube_api_get(
        "playlistItems",
        {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": max(
                1,
                min(fetch_limit, 50),
            ),
        },
        api_key,
    )

    video_ids: list[str] = []

    for item in response.get("items", []):
        if not isinstance(item, dict):
            continue

        content_details = item.get("contentDetails") or {}

        if not isinstance(content_details, dict):
            continue

        video_id = str(
            content_details.get("videoId")
            or ""
        ).strip()

        if video_id:
            video_ids.append(video_id)

    normalized_prefix = title_prefix.casefold()

    matches = [
        video
        for video in fetch_video_details(
            video_ids,
            api_key,
        )
        if video["title"].casefold().startswith(
            normalized_prefix
        )
    ]

    return sorted(
        matches,
        key=lambda item: item["published"],
    )


# ============================================================
# Exact approved Discord embed
# ============================================================

def clean_display_title(title: str) -> str:
    cleaned = title.strip()

    cleaned = re.sub(
        r"^\s*🔴\s*",
        "",
        cleaned,
    )

    cleaned = re.sub(
        r"^\s*live\s+trading\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = cleaned.lstrip(" -–—|:")

    return cleaned.strip() or title.strip()


def build_replay_embed(
    video: dict[str, str],
) -> dict[str, Any]:
    display_title = clean_display_title(
        video["title"]
    )

    embed: dict[str, Any] = {
        "color": EMBED_COLOR,
        "author": {
            "name": EMBED_AUTHOR,
        },
        "title": f"📹 {display_title}",
        "url": video["url"],
        "description": EMBED_DESCRIPTION,
        "fields": [
            {
                "name": "📅 Stream Date",
                "value": format_stream_date(
                    video.get("published", "")
                ),
                "inline": True,
            },
            {
                "name": "⏱️ Duration",
                "value": video.get(
                    "duration",
                    "Unknown",
                ),
                "inline": True,
            },
            {
                "name": "▶️ Watch Replay",
                "value": (
                    "[Open the full replay]"
                    f"({video['url']})"
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text": EMBED_FOOTER,
        },
    }

    thumbnail = video.get("thumbnail", "").strip()

    if thumbnail:
        embed["image"] = {
            "url": thumbnail,
        }

    return embed


# ============================================================
# Discord webhook
# ============================================================

def discord_retry_seconds(
    error: urllib.error.HTTPError,
    attempt: int,
) -> float:
    header_value = error.headers.get(
        "Retry-After",
        "",
    )

    try:
        if header_value:
            return max(float(header_value), 1.0)
    except ValueError:
        pass

    try:
        payload = json.loads(
            error.read().decode("utf-8")
        )

        retry_after = float(
            payload.get("retry_after", 0)
        )

        if retry_after > 0:
            return retry_after
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        pass

    return float(2 ** attempt)


def post_replay_embed(
    webhook_url: str,
    video: dict[str, str],
) -> None:
    payload = {
        "username": WEBHOOK_USERNAME,
        "embeds": [
            build_replay_embed(video)
        ],
        "allowed_mentions": {
            "parse": [],
        },
    }

    encoded = json.dumps(
        payload
    ).encode("utf-8")

    for attempt in range(
        1,
        MAX_DISCORD_ATTEMPTS + 1,
    ):
        request = urllib.request.Request(
            webhook_url,
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=30,
            ) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(
                        "Discord webhook returned HTTP "
                        f"{response.status}"
                    )

                return

        except urllib.error.HTTPError as error:
            if (
                error.code != 429
                or attempt >= MAX_DISCORD_ATTEMPTS
            ):
                raise

            wait_seconds = discord_retry_seconds(
                error,
                attempt,
            )

            print(
                "Discord rate limit reached. "
                f"Waiting {wait_seconds:.1f} seconds..."
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        "Discord post failed after all retry attempts."
    )


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Post new Main Line Trades YouTube replays "
            "to Discord using the approved embed format."
        )
    )

    mode = parser.add_mutually_exclusive_group()

    mode.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Preview eligible replay data without "
            "posting or changing state."
        ),
    )

    mode.add_argument(
        "--post",
        action="store_true",
        help=(
            "Post eligible replay(s) and update state."
        ),
    )

    mode.add_argument(
        "--test",
        action="store_true",
        help=(
            "Post one full styled test embed."
        ),
    )

    parser.add_argument(
        "--video-id",
        help=(
            "Use one exact YouTube video ID."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help=(
            "Maximum automatic posts per run "
            "(default: 1)."
        ),
    )

    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=DEFAULT_FETCH_LIMIT,
        help=(
            "Recent uploads inspected "
            f"(default: {DEFAULT_FETCH_LIMIT}, max: 50)."
        ),
    )

    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=DEFAULT_MAX_AGE_HOURS,
        help=(
            "Ignore automatic matches older than this "
            f"(default: {DEFAULT_MAX_AGE_HOURS} hours)."
        ),
    )

    parser.add_argument(
        "--title-prefix",
        default=os.getenv(
            "TITLE_PREFIX",
            DEFAULT_TITLE_PREFIX,
        ).strip(),
        help=(
            "Automatic replay title prefix. "
            "Matching ignores case."
        ),
    )

    return parser


def preview_video(
    video: dict[str, str],
) -> None:
    print(
        "\n"
        "===== YOUTUBE REPLAY PREVIEW =====\n"
        f"Video ID: {video['id']}\n"
        f"Title: {clean_display_title(video['title'])}\n"
        f"Date: {format_stream_date(video['published'])}\n"
        f"Duration: {video['duration']}\n"
        f"Thumbnail: {video['thumbnail']}\n"
        f"URL: {video['url']}\n"
        "==================================\n"
    )


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not (
        args.preview
        or args.post
        or args.test
    ):
        parser.print_help()
        return 0

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    if not 1 <= args.fetch_limit <= 50:
        parser.error(
            "--fetch-limit must be between 1 and 50"
        )

    if args.max_age_hours < 1:
        parser.error(
            "--max-age-hours must be at least 1"
        )

    webhook_url = ""

    if args.post or args.test:
        webhook_url = required_env(
            "DISCORD_WEBHOOK_URL"
        )

    api_key = required_env(
        "YOUTUBE_API_KEY"
    )

    # --test posts the exact approved embed format, not a plain text message.
    if args.test:
        try:
            test_video = fetch_video_by_id(
                TEST_VIDEO_ID,
                api_key,
            )
        except RuntimeError:
            test_video = {
                "id": TEST_VIDEO_ID,
                "title": TEST_TITLE,
                "url": (
                    "https://www.youtube.com/watch"
                    f"?v={TEST_VIDEO_ID}"
                ),
                "published": datetime.now(
                    timezone.utc
                ).isoformat(),
                "duration": TEST_DURATION,
                "thumbnail": (
                    "https://i.ytimg.com/vi/"
                    f"{TEST_VIDEO_ID}/maxresdefault.jpg"
                ),
            }

        post_replay_embed(
            webhook_url,
            test_video,
        )

        print(
            "Full styled replay embed posted to Discord."
        )
        return 0

    processed_ids = load_processed_ids()
    processed_set = set(processed_ids)

    if args.video_id:
        videos = [
            fetch_video_by_id(
                args.video_id.strip(),
                api_key,
            )
        ]
    else:
        channel_id = required_env(
            "YOUTUBE_CHANNEL_ID"
        )

        videos = fetch_matching_uploads(
            channel_id,
            api_key,
            args.title_prefix,
            args.fetch_limit,
        )

        # Critical protection: never backfill an empty state.
        if args.post and not processed_ids:
            save_processed_ids(
                [
                    video["id"]
                    for video in videos
                ]
            )

            print(
                f"Initialized state with {len(videos)} "
                "existing matching replay(s). "
                "Nothing posted."
            )
            return 0

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(
                hours=args.max_age_hours
            )
        )

        recent: list[dict[str, str]] = []

        for video in videos:
            published = parse_youtube_datetime(
                video["published"]
            )

            if (
                published is not None
                and published >= cutoff
            ):
                recent.append(video)

        videos = recent

    # Exact video preview is always allowed, even if already processed.
    if args.video_id and args.preview:
        preview_video(videos[0])
        print(
            "Preview mode did not post anything "
            "or change state."
        )
        return 0

    new_videos = [
        video
        for video in videos
        if video["id"] not in processed_set
    ]

    if not new_videos:
        print("No new eligible replay found.")
        return 0

    handled = 0

    for video in new_videos:
        if handled >= args.limit:
            break

        handled += 1

        if args.preview:
            preview_video(video)
            continue

        post_replay_embed(
            webhook_url,
            video,
        )

        mark_processed(
            video["id"],
            processed_ids,
            processed_set,
        )

        print(
            f"Posted: {video['title']}"
        )

        time.sleep(POST_DELAY_SECONDS)

    if args.preview:
        print(
            f"Finished. Previewed {handled} replay(s)."
        )
        print(
            "Preview mode did not post anything "
            "or change state."
        )
    else:
        print(
            f"Finished. Posted {handled} replay(s)."
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            "\nCancelled by user.",
            file=sys.stderr,
        )
        raise SystemExit(130)
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)
