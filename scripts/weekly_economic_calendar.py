import os
import json
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


WEBHOOK_URL = os.getenv("ECONOMIC_CALENDAR_WEBHOOK")

# Reuse the same public economic-calendar source currently used by your setup.
# This endpoint is compatible with the same event structure your daily script expects.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

ET = ZoneInfo("America/New_York")


def fetch_calendar():
    request = urllib.request.Request(
        CALENDAR_URL,
        headers={
            "User-Agent": "MainLineTradesWeeklyCalendar/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_event_datetime(event):
    raw = event.get("date")

    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(ET)
    except Exception:
        return None


def current_week_bounds():
    now = datetime.now(ET)

    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    friday = monday + timedelta(days=4, hours=23, minutes=59, seconds=59)

    return monday, friday


def clean(value, fallback="N/A"):
    if value is None:
        return fallback

    value = str(value).strip()

    if not value:
        return fallback

    return value


def high_impact_usd_events(events):
    monday, friday = current_week_bounds()
    filtered = []

    for event in events:
        impact = clean(event.get("impact"), "").lower()
        country = clean(event.get("country"), "").upper()

        if impact != "high":
            continue

        if country != "USD":
            continue

        dt = parse_event_datetime(event)

        if not dt:
            continue

        if monday <= dt <= friday:
            filtered.append(
                {
                    "datetime": dt,
                    "title": clean(event.get("title"), "Economic Event"),
                    "forecast": clean(event.get("forecast")),
                    "previous": clean(event.get("previous")),
                }
            )

    filtered.sort(key=lambda item: item["datetime"])

    return filtered


def group_events_by_day(events):
    grouped = {}

    for event in events:
        day_name = event["datetime"].strftime("%A")
        grouped.setdefault(day_name, []).append(event)

    return grouped


def risk_level(event_count):
    if event_count >= 8:
        return "HIGH"

    if event_count >= 4:
        return "MODERATE"

    return "LOW"


def highest_risk_day(grouped):
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    best_day = None
    best_count = 0

    for day in weekdays:
        count = len(grouped.get(day, []))

        if count > best_count:
            best_day = day
            best_count = count

    return best_day, best_count


def main_event(events):
    if not events:
        return "None scheduled"

    priority_keywords = [
        "Non-Farm",
        "Federal Funds",
        "FOMC",
        "CPI",
        "Consumer Price",
        "Core CPI",
        "GDP",
        "PCE",
        "Unemployment Rate",
        "Retail Sales",
        "Average Hourly Earnings",
        "ISM",
    ]

    for keyword in priority_keywords:
        for event in events:
            if keyword.lower() in event["title"].lower():
                return event["title"]

    return events[0]["title"]


def separator():
    return "────────────────────────────"


def build_message(events):
    monday, friday = current_week_bounds()

    grouped = group_events_by_day(events)

    risk = risk_level(len(events))
    highest_day, highest_count = highest_risk_day(grouped)
    featured_event = main_event(events)

    lines = []

    lines.append("🗓️ **Weekly U.S. Economic Calendar**")
    lines.append("")
    lines.append(
        f"**{monday.strftime('%B %-d')}–{friday.strftime('%-d, %Y')}**"
    )
    lines.append("")
    lines.append("**High-impact USD events only**")
    lines.append("*All times Eastern Time (ET)*")
    lines.append("")
    lines.append(separator())
    lines.append("")
    lines.append("⚠️ **Week Overview**")
    lines.append(f"• **Risk level:** {risk}")
    lines.append(f"• **High-impact events:** {len(events)}")

    if highest_day:
        lines.append(f"• **Highest-risk day:** {highest_day}")
    else:
        lines.append("• **Highest-risk day:** None")

    lines.append(f"• **Main event:** {featured_event}")
    lines.append("")
    lines.append(separator())

    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    for day in weekdays:
        day_events = grouped.get(day, [])

        day_date = monday + timedelta(days=weekdays.index(day))

        lines.append("")
        lines.append(
            f"**{day} — {day_date.strftime('%B %-d')}**"
        )
        lines.append("")

        if not day_events:
            lines.append("✓ No high-impact USD events scheduled.")
            lines.append("")
            lines.append(separator())
            continue

        for event in day_events:
            time_text = event["datetime"].strftime("%-I:%M %p ET")

            lines.append(f"🟥 **{time_text}**")
            lines.append(f"**{event['title']}**")
            lines.append(f"Forecast: **{event['forecast']}**")
            lines.append(f"Previous: **{event['previous']}**")
            lines.append("")

        lines.append(separator())

    lines.append("")
    lines.append("⚠️ **Trading Reminder**")
    lines.append(
        "High-impact releases can cause rapid volatility, wider spreads, "
        "slippage, and sudden changes in price action."
    )
    lines.append("")
    lines.append(
        "Know the release times, manage your risk, and avoid being caught by surprise. 📈"
    )

    return "\n".join(lines)


def split_message(message, limit=1900):
    if len(message) <= limit:
        return [message]

    chunks = []
    current = []

    current_length = 0

    for line in message.splitlines():
        additional = len(line) + 1

        if current and current_length + additional > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0

        current.append(line)
        current_length += additional

    if current:
        chunks.append("\n".join(current))

    return chunks


def post_to_discord(message):
    if not WEBHOOK_URL:
        raise RuntimeError(
            "ECONOMIC_CALENDAR_WEBHOOK is not set."
        )

    payload = json.dumps(
        {
            "username": "Main Line Trades Economic Calendar",
            "content": message,
            "allowed_mentions": {
                "parse": []
            },
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MainLineTradesWeeklyCalendar/1.0",
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
    events = high_impact_usd_events(events)

    message = build_message(events)
    messages = split_message(message)

    for chunk in messages:
        post_to_discord(chunk)

    print(
        f"Posted weekly U.S. economic calendar with "
        f"{len(events)} high-impact USD event(s) "
        f"in {len(messages)} Discord message(s)."
    )


if __name__ == "__main__":
    main()
