#!/usr/bin/env python3

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


DISCORD_API = "https://discord.com/api/v10"

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"].strip()
RAW_CHANNEL_ID = os.environ["TRENDSPIDER_RAW_CHANNEL_ID"].strip()
PUBLIC_WEBHOOK_URL = os.environ["TRENDSPIDER_WEBHOOK"].strip()

STATE_PATH = Path("data/trendspider_processed.json")

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


def discord_get(endpoint):
    request = urllib.request.Request(
        f"{DISCORD_API}{endpoint}",
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "User-Agent": "MainLineTrades-FeedFilter/1.0",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_recent_messages():
    query = urllib.parse.urlencode({"limit": 50})

    return discord_get(
        f"/channels/{RAW_CHANNEL_ID}/messages?{query}"
    )


def load_processed_ids():
    if not STATE_PATH.exists():
        return []

    try:
        data = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return [str(item) for item in data]

    except (json.JSONDecodeError, OSError):
        pass

    return []


def save_processed_ids(message_ids):
    STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE_PATH.write_text(
        json.dumps(message_ids[-500:], indent=2) + "\n",
        encoding="utf-8",
    )


def collect_message_text(message):
    parts = []

    content = str(
        message.get("content") or ""
    ).strip()

    if content:
        parts.append(content)

    for embed in message.get("embeds", []):
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
        author_name = str(
            author.get("name") or ""
        ).strip()

        if author_name:
            parts.append(author_name)

        for field in embed.get("fields", []):
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


def extract_image_urls(message):
    image_urls = []

    for attachment in message.get("attachments", []):
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
        image = embed.get("image") or {}
        thumbnail = embed.get("thumbnail") or {}

        image_url = str(
            image.get("url") or ""
        ).strip()

        thumbnail_url = str(
            thumbnail.get("url") or ""
        ).strip()

        if image_url:
            image_urls.append(image_url)

        if thumbnail_url:
            image_urls.append(thumbnail_url)

    unique_urls = []
    seen = set()

    for url in image_urls:
        if url and url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return unique_urls


def extract_original_url(message, text):
    for embed in message.get("embeds", []):
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


def is_promotional(text):
    lowered = text.lower()

    return any(
        phrase in lowered
        for phrase in PROMOTIONAL_PHRASES
    )


def clean_text(text, original_url):
    cleaned = text

    if original_url:
        cleaned = cleaned.replace(
            original_url,
            "",
        )

    cleaned = re.sub(
        r"\n{3,}",
        "\n\n",
        cleaned,
    )

    return cleaned.strip()


def build_public_message(text, original_url):
    lines = [
        "# 📊 TrendSpider Chart",
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
                "🔗 **View the original post**",
                f"<{original_url}>",
            ]
        )

    return "\n".join(lines).strip()


def post_to_public_channel(
    message_text,
    image_url,
):
    payload = {
        "username": "Main Line Trades Charts",
        "content": message_text,
        "embeds": [
            {
                "image": {
                    "url": image_url,
                }
            }
        ],
        "allowed_mentions": {
            "parse": [],
        },
    }

    request = urllib.request.Request(
        PUBLIC_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MainLineTrades-FeedFilter/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord webhook returned HTTP "
                f"{response.status}"
            )


def main():
    messages = get_recent_messages()

    processed_ids = load_processed_ids()
    processed_set = set(processed_ids)

    messages = sorted(
        messages,
        key=lambda item: int(item["id"]),
    )

    # On the first run, remember existing raw messages
    # without posting all of them publicly.
    if not STATE_PATH.exists():
        initial_ids = [
            str(message["id"])
            for message in messages
        ]

        save_processed_ids(initial_ids)

        print(
            f"Initialized with {len(initial_ids)} "
            "existing raw message(s). Nothing posted."
        )
        return

    checked = 0
    posted = 0
    skipped_no_image = 0
    skipped_promotional = 0

    for message in messages:
        message_id = str(message["id"])

        if message_id in processed_set:
            continue

        checked += 1

        text = collect_message_text(message)
        image_urls = extract_image_urls(message)
        original_url = extract_original_url(
            message,
            text,
        )

        if not image_urls:
            skipped_no_image += 1
            processed_ids.append(message_id)
            processed_set.add(message_id)
            continue

        if is_promotional(text):
            skipped_promotional += 1
            processed_ids.append(message_id)
            processed_set.add(message_id)
            continue

        cleaned_text = clean_text(
            text,
            original_url,
        )

        public_message = build_public_message(
            cleaned_text,
            original_url,
        )

        post_to_public_channel(
            public_message,
            image_urls[0],
        )

        posted += 1
        processed_ids.append(message_id)
        processed_set.add(message_id)

        print(
            f"Posted TrendSpider chart from "
            f"message {message_id}"
        )

    save_processed_ids(processed_ids)

    print(
        f"Finished. Checked {checked}, "
        f"posted {posted}, "
        f"skipped {skipped_no_image} without images, "
        f"and skipped {skipped_promotional} "
        "promotional post(s)."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        raise
