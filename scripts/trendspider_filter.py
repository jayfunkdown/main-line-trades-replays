#!/usr/bin/env python3
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from scripts.discord_embeds import bordered_webhook_payload
except ModuleNotFoundError:
    from discord_embeds import bordered_webhook_payload
from typing import Any


DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "MainLineTrades-FeedFilter/1.1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "trendspider_processed.json"

DEFAULT_FETCH_LIMIT = 50
DEFAULT_MAX_NEW_MESSAGES = 10
MAX_STATE_IDS = 500
POST_DELAY_SECONDS = 2.0
MAX_POST_ATTEMPTS = 4

RETIRED_MESSAGE = (
    "TrendSpider public chart forwarding is retired. "
    "The charts public channel and filter automation were removed. "
    "TrendSpider raw posts remain visible in Discord without this script."
)

PROMOTIONAL_PHRASES = (
    "webinar",
    "register now",
    "register today",
    "save your seat",
    "reserve your seat",
    "free trial",
    "start your trial",
    "limited time",
    "discount",
    "coupon",
    "sale ends",
    "join us live",
    "sign up",
    "subscribe now",
    "giveaway",
    "podcast episode",
    "sponsored",
)

IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def discord_get(
    endpoint: str,
    bot_token: str,
) -> Any:
    request = urllib.request.Request(
        f"{DISCORD_API}{endpoint}",
        headers={
            "Authorization": f"Bot {bot_token}",
            "User-Agent": USER_AGENT,
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def get_recent_messages(
    bot_token: str,
    raw_channel_id: str,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "limit": max(
                1,
                min(fetch_limit, 100),
            )
        }
    )

    data = discord_get(
        (
            f"/channels/{raw_channel_id}"
            f"/messages?{query}"
        ),
        bot_token,
    )

    if not isinstance(data, list):
        raise RuntimeError(
            "Discord returned an unexpected messages response."
        )

    return [
        item
        for item in data
        if isinstance(item, dict)
    ]


def load_processed_ids() -> list[str]:
    if not STATE_PATH.exists():
        return []

    try:
        data = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return [
                str(item)
                for item in data
            ]

    except (json.JSONDecodeError, OSError) as error:
        print(
            f"Warning: could not read state file: {error}",
            file=sys.stderr,
        )

    return []


def save_processed_ids(
    message_ids: list[str],
) -> None:
    STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    deduplicated = list(
        dict.fromkeys(
            str(item)
            for item in message_ids
        )
    )

    STATE_PATH.write_text(
        json.dumps(
            deduplicated[-MAX_STATE_IDS:],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def mark_processed(
    message_id: str,
    processed_ids: list[str],
    processed_set: set[str],
) -> None:
    if message_id not in processed_set:
        processed_ids.append(message_id)
        processed_set.add(message_id)

    save_processed_ids(processed_ids)


def collect_message_text(
    message: dict[str, Any],
) -> str:
    parts: list[str] = []

    content = str(
        message.get("content") or ""
    ).strip()

    if content:
        parts.append(content)

    for embed in message.get("embeds", []):
        if not isinstance(embed, dict):
            continue

        title = str(
            embed.get("title") or ""
        ).strip()

        description = str(
            embed.get("description") or ""
        ).strip()

        if title:
            parts.append(title)

        if description:
            parts.append(description)

        author = embed.get("author") or {}

        if isinstance(author, dict):
            author_name = str(
                author.get("name") or ""
            ).strip()

            if author_name:
                parts.append(author_name)

        for field in embed.get("fields", []):
            if not isinstance(field, dict):
                continue

            field_name = str(
                field.get("name") or ""
            ).strip()

            field_value = str(
                field.get("value") or ""
            ).strip()

            if field_name:
                parts.append(field_name)

            if field_value:
                parts.append(field_value)

    return "\n".join(parts).strip()


def extract_image_urls(
    message: dict[str, Any],
) -> list[str]:
    image_urls: list[str] = []

    for attachment in message.get(
        "attachments",
        [],
    ):
        if not isinstance(attachment, dict):
            continue

        url = str(
            attachment.get("url") or ""
        ).strip()

        content_type = str(
            attachment.get("content_type") or ""
        ).lower()

        clean_url = url.lower().split("?")[0]

        if (
            content_type.startswith("image/")
            or clean_url.endswith(IMAGE_EXTENSIONS)
        ):
            image_urls.append(url)

    for embed in message.get("embeds", []):
        if not isinstance(embed, dict):
            continue

        image = embed.get("image") or {}
        thumbnail = embed.get("thumbnail") or {}

        if isinstance(image, dict):
            image_url = str(
                image.get("url") or ""
            ).strip()

            if image_url:
                image_urls.append(image_url)

        if isinstance(thumbnail, dict):
            thumbnail_url = str(
                thumbnail.get("url") or ""
            ).strip()

            if thumbnail_url:
                image_urls.append(thumbnail_url)

    unique_urls: list[str] = []
    seen: set[str] = set()

    for url in image_urls:
        if url and url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return unique_urls


def extract_original_url(
    message: dict[str, Any],
    text: str,
) -> str:
    for embed in message.get("embeds", []):
        if not isinstance(embed, dict):
            continue

        url = str(
            embed.get("url") or ""
        ).strip()

        if url:
            return url

    urls = re.findall(
        r"https?://[^\s<>]+",
        text,
    )

    if urls:
        return urls[0].rstrip(".,)]")

    return ""


def is_promotional(text: str) -> bool:
    lowered = text.lower()

    return any(
        phrase in lowered
        for phrase in PROMOTIONAL_PHRASES
    )


def clean_text(
    text: str,
    original_url: str,
) -> str:
    lines: list[str] = []

    unwanted_exact_lines = {
        "TrendSpider",
        "@TrendSpider",
        "TrendSpider (@TrendSpider)",
        "TrendSpider (@TrendSpider) on X",
        "@TrendSpider quoted @TrendSpider",
    }

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line in unwanted_exact_lines:
            continue

        if original_url:
            line = line.replace(
                original_url,
                "",
            ).strip()

        line = re.sub(
            r"https?://t\.co/\S+",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()

        if line and line not in lines:
            lines.append(line)

    return "\n\n".join(lines).strip()


def build_public_message(
    text: str,
    original_url: str,
) -> str:
    lines = [
        "📊 **TrendSpider**",
        "",
    ]

    if text:
        lines.extend(
            [
                text,
                "",
            ]
        )

    if original_url:
        lines.extend(
            [
                "━━━━━━━━━━━━━━━━━━━━━━",
                "",
                "🔗 **Original Post**",
                f"<{original_url}>",
            ]
        )

    return "\n".join(lines).strip()


def _retry_after_seconds(
    error: urllib.error.HTTPError,
    attempt: int,
) -> float:
    header_value = error.headers.get(
        "Retry-After",
        "",
    )

    try:
        if header_value:
            return max(
                float(header_value),
                1.0,
            )
    except ValueError:
        pass

    try:
        body = error.read().decode("utf-8")
        payload = json.loads(body)
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


def post_to_public_channel(
    webhook_url: str,
    message_text: str,
    image_url: str,
) -> None:
    payload = bordered_webhook_payload(
        "Main Line Trades Charts",
        message_text,
    )
    payload["embeds"][0]["image"] = {
        "url": image_url,
    }

    encoded_payload = json.dumps(
        payload
    ).encode("utf-8")

    for attempt in range(
        1,
        MAX_POST_ATTEMPTS + 1,
    ):
        request = urllib.request.Request(
            webhook_url,
            data=encoded_payload,
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
                if response.status not in (
                    200,
                    204,
                ):
                    raise RuntimeError(
                        "Discord webhook returned HTTP "
                        f"{response.status}"
                    )

                return

        except urllib.error.HTTPError as error:
            if (
                error.code != 429
                or attempt >= MAX_POST_ATTEMPTS
            ):
                raise

            wait_seconds = _retry_after_seconds(
                error,
                attempt,
            )

            print(
                "Discord rate limit reached. "
                f"Waiting {wait_seconds:.1f} seconds "
                "before retrying..."
            )
            time.sleep(wait_seconds)

    raise RuntimeError(
        "Discord post failed after all retry attempts."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Legacy TrendSpider raw-channel filter "
            "(public chart forwarding retired)."
        )
    )

    mode_group = parser.add_mutually_exclusive_group()

    mode_group.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Show qualifying new messages without "
            "posting or changing processed state."
        ),
    )

    mode_group.add_argument(
        "--post",
        action="store_true",
        help=(
            "Publish qualifying new messages to "
            "Discord and update processed state."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_MAX_NEW_MESSAGES,
        help=(
            "Maximum number of unprocessed raw "
            "messages checked per run "
            f"(default: {DEFAULT_MAX_NEW_MESSAGES})."
        ),
    )

    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=DEFAULT_FETCH_LIMIT,
        help=(
            "Number of recent raw Discord messages "
            f"to fetch (default: {DEFAULT_FETCH_LIMIT}, "
            "maximum: 100)."
        ),
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.preview and not args.post:
        parser.print_help()
        return 0

    if args.post:
        print(RETIRED_MESSAGE)
        return 0

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    if not 1 <= args.fetch_limit <= 100:
        parser.error(
            "--fetch-limit must be between 1 and 100"
        )

    bot_token = required_env(
        "DISCORD_BOT_TOKEN"
    )
    raw_channel_id = required_env(
        "TRENDSPIDER_RAW_CHANNEL_ID"
    )

    messages = get_recent_messages(
        bot_token,
        raw_channel_id,
        args.fetch_limit,
    )

    messages = sorted(
        messages,
        key=lambda item: int(item["id"]),
    )

    processed_ids = load_processed_ids()
    processed_set = set(processed_ids)

    checked = 0
    previewed = 0
    skipped_no_image = 0
    skipped_promotional = 0

    for message in messages:
        message_id = str(message["id"])

        if message_id in processed_set:
            continue

        if checked >= args.limit:
            print(
                "Safety limit reached: "
                f"{args.limit} new message(s)."
            )
            break

        checked += 1

        text = collect_message_text(message)
        image_urls = extract_image_urls(message)
        original_url = extract_original_url(
            message,
            text,
        )

        if not image_urls:
            skipped_no_image += 1

            print(
                f"Skipped message {message_id}: "
                "no image."
            )
            continue

        if is_promotional(text):
            skipped_promotional += 1

            print(
                f"Skipped message {message_id}: "
                "promotional content."
            )
            continue

        cleaned_text = clean_text(
            text,
            original_url,
        )

        public_message = build_public_message(
            cleaned_text,
            original_url,
        )

        previewed += 1
        print(
            "\n"
            "===== TRENDSPIDER PREVIEW =====\n"
            f"Message ID: {message_id}\n"
            f"{public_message}\n"
            f"Image: {image_urls[0]}\n"
            "================================\n"
        )

    print(
        f"Finished. Checked {checked}, "
        f"previewed {previewed}, "
        f"skipped {skipped_no_image} without images, "
        f"and skipped {skipped_promotional} "
        "promotional post(s)."
    )

    print(
        "Preview mode did not post anything "
        "or change the processed-state file."
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
