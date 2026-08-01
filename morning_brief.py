#!/usr/bin/env python3

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


ECONOMIC_CALENDAR_URL = (
    "https://nfs.faireconomy.media/"
    "ff_calendar_thisweek.json"
)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

WEBHOOK_URL = os.environ["MORNING_BRIEF_WEBHOOK"].strip()
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"].strip()

EASTERN = ZoneInfo("America/New_York")


# Only widely followed, market-relevant companies will appear.
MAJOR_TICKERS = {
    "AAPL", "ABBV", "ABNB", "ADBE", "AMD", "AMGN", "AMZN",
    "AVGO", "BA", "BAC", "BABA", "BLK", "CAT", "COIN", "COST",
    "CRM", "CVX", "DIS", "F", "FDX", "GE", "GM", "GOOGL", "GS",
    "HD", "IBM", "INTC", "JNJ", "JPM", "KO", "LLY", "LOW", "MA",
    "MCD", "META", "MS", "MSFT", "MU", "NFLX", "NKE", "NVDA",
    "ORCL", "PEP", "PFE", "PLTR", "PYPL", "QCOM", "SBUX", "SHOP",
    "SNAP", "SOFI", "T", "TGT", "TSLA", "UBER", "UNH", "UPS",
    "V", "VZ", "WMT", "XOM"
}


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MainLineTrades-MorningBrief/1.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def format_eastern_time(event_time):
    return event_time.strftime("%I:%M %p").lstrip("0")


def get_high_impact_usd_events():
    events = get_json(ECONOMIC_CALENDAR_URL)
    today = datetime.now(EASTERN).date()
    results = []

    for event in events:
        if event.get("impact") != "High":
            continue

        if event.get("country") != "USD":
            continue

        event_time = datetime.fromisoformat(event["date"])
        event_time = event_time.astimezone(EASTERN)

        if event_time.date() != today:
            continue

        results.append(
            {
                "title": event.get("title", "Unknown Event"),
                "time": event_time,
                "forecast": event.get("forecast") or "Not listed",
                "previous": event.get("previous") or "Not listed",
            }
        )

    return sorted(results, key=lambda item: item["time"])


def get_major_earnings():
    today = datetime.now(EASTERN).date().isoformat()

    query = urllib.parse.urlencode(
        {
            "from": today,
            "to": today,
            "token": FINNHUB_API_KEY,
        }
    )

    url = f"{FINNHUB_BASE_URL}/calendar/earnings?{query}"
    response = get_json(url)

    earnings = response.get("earningsCalendar", [])
    results = []

    for report in earnings:
        symbol = str(report.get("symbol", "")).upper()

        if symbol not in MAJOR_TICKERS:
            continue

        results.append(
            {
                "symbol": symbol,
                "hour": str(report.get("hour", "")).lower(),
            }
        )

    # Remove duplicates while preserving order.
    unique = []
    seen = set()

    for report in results:
        key = (report["symbol"], report["hour"])

        if key not in seen:
            seen.add(key)
            unique.append(report)

    return unique


def group_earnings(earnings):
    before_open = []
    after_close = []
    unspecified = []

    for report in earnings:
        hour = report["hour"]
        symbol = report["symbol"]

        if hour == "bmo":
            before_open.append(symbol)
        elif hour == "amc":
            after_close.append(symbol)
        else:
            unspecified.append(symbol)

    return before_open, after_close, unspecified


def build_message(events, earnings):
    today = datetime.now(EASTERN)

    lines = [
        "# 🌅 Main Line Trades Morning Brief",
        "",
        f"📅 **{today.strftime('%A, %B %d, %Y')}**",
        "",
        "## 🚨 High-Impact USD Events",
        "",
    ]

    if events:
        for event in events:
            time_text = format_eastern_time(event["time"])

            lines.extend(
                [
                    f"### 🇺🇸 {time_text} ET — {event['title']}",
                    f"🎯 Forecast: **{event['forecast']}**",
                    f"📉 Previous: **{event['previous']}**",
                    "",
                ]
            )

        lines.extend(
            [
                "⚠️ Be prepared for increased volatility around "
                "these scheduled release times.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "✅ No high-impact U.S. economic releases "
                "are scheduled today.",
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "## 📅 Major Earnings Today",
            "",
        ]
    )

    before_open, after_close, unspecified = group_earnings(earnings)

    if not earnings:
        lines.extend(
            [
                "✅ No major companies from our watchlist "
                "are scheduled to report today.",
                "",
            ]
        )
    else:
        if before_open:
            lines.append("### 🔔 Before Market Open")
            lines.append("")
            for symbol in sorted(before_open):
                lines.append(f"• **{symbol}**")
            lines.append("")

        if after_close:
            lines.append("### 🌙 After Market Close")
            lines.append("")
            for symbol in sorted(after_close):
                lines.append(f"• **{symbol}**")
            lines.append("")

        if unspecified:
            lines.append("### 🕒 Time Not Confirmed")
            lines.append("")
            for symbol in sorted(unspecified):
                lines.append(f"• **{symbol}**")
            lines.append("")

        lines.extend(
            [
                "⚠️ Earnings schedules can change. Confirm the "
                "reporting time before trading an individual stock.",
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "## 🎯 Trading Reminder",
            "",
            "Know when volatility is scheduled, protect your capital, "
            "and trade the reaction—not the prediction.",
            "",
            "See you at the New York Open! 📈",
        ]
    )

    return "\n".join(lines)


def post_to_discord(message):
    payload = json.dumps(
        {
            "username": "Main Line Trades Morning Brief",
            "content": message,
            "allowed_mentions": {"parse": []},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MainLineTrades-MorningBrief/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord returned HTTP {response.status}"
            )


def main():
    events = get_high_impact_usd_events()
    earnings = get_major_earnings()

    message = build_message(events, earnings)
    post_to_discord(message)

    print(
        f"Posted morning brief with {len(events)} economic event(s) "
        f"and {len(earnings)} major earnings report(s)."
    )


if __name__ == "__main__":
    main()
