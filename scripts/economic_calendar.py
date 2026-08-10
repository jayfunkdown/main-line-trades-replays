#!/usr/bin/env python3
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from scripts.discord_embeds import bordered_webhook_payload
except ModuleNotFoundError:
    from discord_embeds import bordered_webhook_payload

CALENDAR_URL = (
    "https://nfs.faireconomy.media/"
    "ff_calendar_thisweek.json"
)

WEBHOOK_URL = os.environ["ECONOMIC_CALENDAR_WEBHOOK"].strip()

EASTERN = ZoneInfo("America/New_York")

COUNTRY_FLAGS = {
    "USD": "🇺🇸",
    "CAD": "🇨🇦",
    "EUR": "🇪🇺",
    "GBP": "🇬🇧",
    "JPY": "🇯🇵",
    "AUD": "🇦🇺",
    "NZD": "🇳🇿",
    "CHF": "🇨🇭",
    "CNY": "🇨🇳",
}


def fetch_calendar():
    request = urllib.request.Request(
        CALENDAR_URL,
        headers={
            "User-Agent": "MainLineTrades-EconomicCalendar/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def todays_high_impact_events(events):
    today = datetime.now(EASTERN).date()
    results = []

    for event in events:
        if event.get("impact") != "High":
            continue

        event_time = datetime.fromisoformat(event["date"])
        event_time = event_time.astimezone(EASTERN)

        if event_time.date() != today:
            continue

        results.append(
            {
                "title": event.get("title", "Unknown Event"),
                "currency": event.get("country", ""),
                "time": event_time,
                "forecast": event.get("forecast", ""),
                "previous": event.get("previous", ""),
            }
        )

    return sorted(results, key=lambda item: item["time"])


def format_value(value):
    return value if value else "Not listed"


def build_message(events):
    today = datetime.now(EASTERN)

    lines = [
        "# 🗓️ High-Impact Economic Calendar",
        "",
        f"📅 **{today.strftime('%A, %B %d, %Y')}**",
        "",
    ]

    if not events:
        lines.extend(
            [
                "✅ There are no high-impact economic events "
                "scheduled for today.",
                "",
                "Stay disciplined and have a great trading session! 📈",
            ]
        )
        return "\n".join(lines)

    lines.append("## 🚨 Today's High-Impact Events")
    lines.append("")

    for event in events:
        currency = event["currency"]
        flag = COUNTRY_FLAGS.get(currency, "🌍")
        time_text = event["time"].strftime("%-I:%M %p ET")

        lines.extend(
            [
                f"### {flag} {time_text} — {event['title']}",
                f"💱 Currency: **{currency}**",
                f"📊 Forecast: **{format_value(event['forecast'])}**",
                f"📉 Previous: **{format_value(event['previous'])}**",
                "",
            ]
        )

    lines.extend(
        [
            "## ⚠️ Trading Reminder",
            "",
            "High-impact releases can cause rapid volatility, "
            "wider spreads, slippage, and sudden changes in price action.",
            "",
            "Know the release times, manage your risk, "
            "and avoid being caught by surprise. 📈",
        ]
    )

    return "\n".join(lines)


def post_to_discord(message):
    payload = json.dumps(
        bordered_webhook_payload(
            "Main Line Trades Economic Calendar",
            message,
        )
    ).encode("utf-8")

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MainLineTrades-EconomicCalendar/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord returned HTTP {response.status}"
            )


def main():
    events = fetch_calendar()
    todays_events = todays_high_impact_events(events)
    message = build_message(todays_events)
    post_to_discord(message)

    print(
        f"Posted {len(todays_events)} high-impact event(s) "
        "to Discord."
    )


if __name__ == "__main__":
    main()
