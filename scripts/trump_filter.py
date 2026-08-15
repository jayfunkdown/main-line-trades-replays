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
from typing import Any

try:
    from scripts.discord_embeds import BRAND_ELECTRIC_BLUE
except ModuleNotFoundError:
    from discord_embeds import BRAND_ELECTRIC_BLUE


DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "MainLineTrades-TruthSocialFilter/1.1"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "trump_processed.json"

DEFAULT_FETCH_LIMIT = 50
DEFAULT_MAX_NEW_MESSAGES = 10
MAX_STATE_IDS = 500
POST_DELAY_SECONDS = 2.0
MAX_POST_ATTEMPTS = 4
DISCORD_CONTENT_LIMIT = 2000

RETIRED_MESSAGE = (
    "Truth Social / Trump filter automation is retired. "
    "The staff raw channel and public Truth Social channel were removed."
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

    return unique_urls[:10]


def extract_original_url(
    message: dict[str, Any],
    text: str,
) -> str:
    preferred_domains = (
        "trumpstruth.org",
        "truthsocial.com",
    )

    possible_urls: list[str] = []

    for embed in message.get("embeds", []):
        if not isinstance(embed, dict):
            continue

        url = str(
            embed.get("url") or ""
        ).strip()

        if url:
            possible_urls.append(url)

    possible_urls.extend(
        re.findall(
            r"https?://[^\s<>]+",
            text,
        )
    )

    cleaned_urls = [
        url.rstrip(".,)]")
        for url in possible_urls
        if url
    ]

    for url in cleaned_urls:
        if any(
            domain in url.lower()
            for domain in preferred_domains
        ):
            return url

    return cleaned_urls[0] if cleaned_urls else ""


def clean_text(
    text: str,
    original_url: str,
) -> str:
    lines: list[str] = []

    unwanted_exact_lines = {
        "Truth Social",
        "Trump Truth",
        "Trump's Truth",
        "Trump's Truth Social",
        "Donald J. Trump",
        "@realDonaldTrump",
        "Trump's Truth (@TrumpDailyPosts)",
        "Trump's Truth on X",
    }

    unwanted_patterns = (
        r"^posted by\s+donald j\.?\s*trump$",
        r"^donald j\.?\s*trump posted:?$",
        r"^new truth social post$",
        r"^truth social post$",
        r"^view on truth social$",
        r"^read more$",
    )

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line in unwanted_exact_lines:
            continue

        if any(
            re.fullmatch(
                pattern,
                line,
                flags=re.IGNORECASE,
            )
            for pattern in unwanted_patterns
        ):
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

        line = re.sub(
            r"https?://(?:www\.)?trumpstruth\.org/\S+",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()

        line = re.sub(
            r"https?://(?:www\.)?truthsocial\.com/\S+",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()

        line = re.sub(
            r"<[^>]+>",
            "",
            line,
        ).strip()

        line = (
            line.replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )

        if line and line not in lines:
            lines.append(line)

    return "\n\n".join(lines).strip()


def build_public_message(
    text: str,
    original_url: str,
) -> str:
    lines = [
        "👤 **Truth Social**",
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


def build_public_payload(
    message_text: str,
    image_urls: list[str],
) -> dict[str, Any]:
    selected_images = image_urls[:10]
    primary_embed: dict[str, Any] = {
        "description": message_text,
        "color": BRAND_ELECTRIC_BLUE,
    }

    if selected_images:
        primary_embed["image"] = {
            "url": selected_images[0],
        }

    payload: dict[str, Any] = {
        "username": "Main Line Trades Truth Social",
        "embeds": [primary_embed],
        "allowed_mentions": {
            "parse": [],
        },
    }

    payload["embeds"].extend(
        [
            {
                "color": BRAND_ELECTRIC_BLUE,
                "image": {
                    "url": image_url,
                }
            }
            for image_url in selected_images[1:]
        ]
    )

    return payload


def post_to_public_channel(
    webhook_url: str,
    message_text: str,
    image_urls: list[str],
) -> None:
    payload = build_public_payload(
        message_text,
        image_urls,
    )

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
            "Legacy Truth Social raw-channel filter "
            "(public posting retired)."
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
        "TRUMP_RAW_CHANNEL_ID"
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
    skipped_empty = 0
    skipped_oversized = 0

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

        raw_text = collect_message_text(message)
        image_urls = extract_image_urls(message)

        original_url = extract_original_url(
            message,
            raw_text,
        )

        cleaned_text = clean_text(
            raw_text,
            original_url,
        )

        if not cleaned_text and not image_urls:
            skipped_empty += 1

            print(
                f"Skipped message {message_id}: empty item."
            )
            continue

        public_message = build_public_message(
            cleaned_text,
            original_url,
        )

        if len(public_message) > DISCORD_CONTENT_LIMIT:
            skipped_oversized += 1

            print(
                f"Skipped message {message_id}: content is "
                f"{len(public_message)} characters; Discord's "
                f"limit is {DISCORD_CONTENT_LIMIT}."
            )
            continue

        previewed += 1
        image_lines = "\n".join(
            f"Image {index + 1}: {url}"
            for index, url in enumerate(image_urls)
        )

        if not image_lines:
            image_lines = "Images: none"

        print(
            "\n"
            "===== TRUTH SOCIAL PREVIEW =====\n"
            f"Message ID: {message_id}\n"
            f"{public_message}\n"
            f"{image_lines}\n"
            "================================\n"
        )

    print(
        f"Finished. Checked {checked}, "
        f"previewed {previewed}, and skipped "
        f"{skipped_empty} empty and "
        f"{skipped_oversized} oversized item(s)."
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
