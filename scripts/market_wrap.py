#!/usr/bin/env python3
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from scripts import morning_brief
except ModuleNotFoundError:
    import morning_brief

try:
    from scripts.discord_embeds import BRAND_ELECTRIC_BLUE
except ModuleNotFoundError:
    from discord_embeds import BRAND_ELECTRIC_BLUE


MARKET_WRAP_WEBHOOK_USERNAME = "Main Line Trades Market Wrap"
USER_AGENT = "MainLineTrades-MarketWrap/1.0"

DISCORD_EMBED_TOTAL_LIMIT = 6000
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_COUNT_LIMIT = 25
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBED_FOOTER_LIMIT = 2048

CRYPTO_SYMBOLS = [
    ("BINANCE:BTCUSDT", "BTC", "Bitcoin"),
    ("BINANCE:ETHUSDT", "ETH", "Ethereum"),
]

CROSS_MARKET_SYMBOLS = [
    ("GLD", "Gold ETF"),
    ("USO", "Oil ETF"),
    ("UUP", "U.S. Dollar ETF"),
]

GLOBAL_MARKET_SYMBOLS = [
    ("^N225", "Nikkei 225"),
    ("^HSI", "Hang Seng"),
    ("000001.SS", "Shanghai Composite"),
    ("^NSEI", "Nifty 50"),
    ("^FTSE", "FTSE 100"),
    ("^GDAXI", "DAX"),
    ("^FCHI", "CAC 40"),
]

YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


def get_named_quotes(symbols, *, is_crypto=False):
    return [
        {
            "symbol": display_symbol,
            "name": name,
            "is_crypto": is_crypto,
            "quote": morning_brief.get_quote(finnhub_symbol),
        }
        for finnhub_symbol, display_symbol, name in symbols
    ]


def get_crypto_snapshot():
    return get_named_quotes(
        CRYPTO_SYMBOLS,
        is_crypto=True,
    )


def get_cross_market_snapshot():
    symbols = [
        (symbol, symbol, name)
        for symbol, name in CROSS_MARKET_SYMBOLS
    ]
    return get_named_quotes(symbols)


def yahoo_index_chart_url(symbol):
    encoded_symbol = urllib.parse.quote(symbol)
    query = urllib.parse.urlencode(
        {
            "range": "5d",
            "interval": "1d",
            "includeAdjustedClose": "true",
        }
    )
    return f"{YAHOO_CHART_BASE}/{encoded_symbol}?{query}"


def get_yahoo_index_quote(symbol):
    request = urllib.request.Request(
        yahoo_index_chart_url(symbol),
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        closes = [
            float(value)
            for value in result["indicators"]["quote"][0]["close"]
            if value is not None
        ]
        price = meta.get("regularMarketPrice")
        previous_close = (
            meta.get("chartPreviousClose")
            or meta.get("previousClose")
        )

        if price is None and closes:
            price = closes[-1]
        if previous_close is None and len(closes) >= 2:
            previous_close = closes[-2]

        price = float(price)
        previous_close = float(previous_close)
        if previous_close == 0:
            raise ValueError("previous close is zero")

        return {
            "price": price,
            "percent_change": ((price - previous_close) / previous_close) * 100,
        }
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        return None


def get_global_market_snapshot():
    return [
        {
            "symbol": display_name,
            "name": display_name,
            "quote": get_yahoo_index_quote(yahoo_symbol),
        }
        for yahoo_symbol, display_name in GLOBAL_MARKET_SYMBOLS
    ]


def available_changes(items):
    changes = []

    for item in items:
        quote = item.get("quote")
        if not quote:
            continue

        change = quote.get("percent_change")
        if isinstance(change, (int, float)) and not isinstance(change, bool):
            changes.append(float(change))

    return changes


def session_read(market_snapshot):
    changes = available_changes(market_snapshot)

    if len(changes) < 3:
        return "Market breadth unavailable; confirm conditions before acting."

    positive = sum(change > 0 for change in changes)
    negative = sum(change < 0 for change in changes)

    if positive >= 3:
        return "Broadly positive U.S. close across the major index ETFs."

    if negative >= 3:
        return "Broadly negative U.S. close across the major index ETFs."

    return "Mixed U.S. close with leadership divided across the major index ETFs."


def quote_block(items):
    return "\n".join(
        morning_brief.quote_line(item)
        for item in items
    ) or "• Data unavailable"


def global_market_line(item):
    quote = item.get("quote")
    name = str(item.get("name") or item.get("symbol") or "Unknown index")

    if not quote:
        return f"• **{name}:** Unavailable"

    price = quote.get("price")
    change = quote.get("percent_change")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return f"• **{name}:** Unavailable"
    if not isinstance(change, (int, float)) or isinstance(change, bool):
        return f"• **{name}:** {price:,.2f}"

    direction = "▲" if change > 0 else "▼" if change < 0 else "•"
    return f"{direction} **{name}:** {price:,.2f} ({change:+.2f}%)"


def global_market_block(items):
    return "\n".join(global_market_line(item) for item in items) or "• Data unavailable"


def market_wrap_description(date_label, sections):
    parts = [
        f"**{date_label}**",
        (
            "The closing snapshot for U.S. and global markets, crypto, "
            "and key cross-market signals."
        ),
    ]

    parts.extend(
        f"## {section['name']}\n{section['value']}"
        for section in sections
    )
    return "\n\n".join(parts)


def economic_results_block(events, limit=DISCORD_EMBED_FIELD_VALUE_LIMIT):
    entries = []

    for event in events:
        event_time = event.get("time")
        time_label = (
            morning_brief.format_eastern_time(event_time)
            if isinstance(event_time, datetime)
            else "Time unavailable"
        )
        title = str(event.get("title") or "Unknown Event")
        actual = str(event.get("actual") or "Not released")
        forecast = str(event.get("forecast") or "Not listed")
        previous = str(event.get("previous") or "Not listed")
        entries.append(
            f"**{time_label} — {title}**\n"
            f"Actual: **{actual}** | Forecast: **{forecast}** | "
            f"Previous: **{previous}**"
        )

    included = []

    for index, entry in enumerate(entries):
        omitted_count = len(entries) - index - 1
        candidate_entries = [*included, entry]
        candidate = "\n\n".join(candidate_entries)

        if omitted_count:
            candidate += f"\n\n*Plus {omitted_count} additional high-impact event(s).*"

        if len(candidate) > limit:
            break

        included.append(entry)

    omitted_count = len(entries) - len(included)
    result = "\n\n".join(included)

    if omitted_count:
        omitted_line = f"*Plus {omitted_count} additional high-impact event(s).*"
        result = f"{result}\n\n{omitted_line}" if result else omitted_line

    if len(result) > limit:
        return result[: limit - 1].rstrip() + "…"

    return result


def market_wrap_color(market_snapshot):
    return BRAND_ELECTRIC_BLUE


def build_market_wrap_payload(
    market_snapshot,
    crypto_snapshot,
    cross_market_snapshot,
    economic_events=None,
    global_market_snapshot=None,
    *,
    now=None,
):
    current_time = now or datetime.now(morning_brief.EASTERN)
    date_label = current_time.strftime("%A, %B %d, %Y")

    sections = [
        {
            "name": "📈 U.S. Market Close",
            "value": quote_block(market_snapshot),
            "inline": False,
        },
        {
            "name": "🌍 Global Markets",
            "value": global_market_block(global_market_snapshot or []),
            "inline": False,
        },
    ]

    if economic_events:
        sections.append(
            {
                "name": "📅 High-Impact Economic Results",
                "value": economic_results_block(economic_events),
                "inline": False,
            }
        )

    sections.extend(
        [
            {
                "name": "₿ Crypto Snapshot",
                "value": quote_block(crypto_snapshot),
                "inline": False,
            },
            {
                "name": "🌐 Key Markets",
                "value": quote_block(cross_market_snapshot),
                "inline": False,
            },
            {
                "name": "🧠 Session Read",
                "value": session_read(market_snapshot),
                "inline": False,
            },
            {
                "name": "🔭 Next Session",
                "value": (
                    "Review tomorrow’s Morning Brief and calendars before the next session."
                ),
                "inline": False,
            },
        ]
    )
    description = market_wrap_description(date_label, sections)

    payload = {
        "username": MARKET_WRAP_WEBHOOK_USERNAME,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "🌆 Main Line Trades Market Wrap",
                "description": description,
                "color": market_wrap_color(market_snapshot),
                "footer": {
                    "text": "Market data is informational and may be delayed. Not financial advice."
                },
            }
        ],
    }

    validate_market_wrap_payload(payload)
    return payload


def embed_character_count(embed):
    total = len(embed.get("title", ""))
    total += len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))

    for field in embed.get("fields", []):
        total += len(field.get("name", ""))
        total += len(field.get("value", ""))

    return total


def validate_market_wrap_payload(payload):
    embeds = payload.get("embeds")

    if not isinstance(embeds, list) or len(embeds) != 1:
        raise ValueError("Market Wrap must contain exactly one embed.")

    embed = embeds[0]
    title = embed.get("title", "")
    description = embed.get("description", "")
    footer_text = embed.get("footer", {}).get("text", "")
    fields = embed.get("fields", [])

    if len(title) > DISCORD_EMBED_TITLE_LIMIT:
        raise ValueError("Market Wrap embed title exceeds Discord's limit.")

    if len(description) > DISCORD_EMBED_DESCRIPTION_LIMIT:
        raise ValueError("Market Wrap embed description exceeds Discord's limit.")

    if not isinstance(fields, list) or len(fields) > DISCORD_EMBED_FIELD_COUNT_LIMIT:
        raise ValueError("Market Wrap embed field count exceeds Discord's limit.")

    for field in fields:
        if len(field.get("name", "")) > DISCORD_EMBED_FIELD_NAME_LIMIT:
            raise ValueError("Market Wrap embed field name exceeds Discord's limit.")

        if len(field.get("value", "")) > DISCORD_EMBED_FIELD_VALUE_LIMIT:
            raise ValueError("Market Wrap embed field value exceeds Discord's limit.")

    if len(footer_text) > DISCORD_EMBED_FOOTER_LIMIT:
        raise ValueError("Market Wrap embed footer exceeds Discord's limit.")

    if sum(embed_character_count(item) for item in embeds) > DISCORD_EMBED_TOTAL_LIMIT:
        raise ValueError("Market Wrap embeds exceed Discord's combined limit.")


def required_webhook():
    value = os.getenv("MARKET_WRAP_WEBHOOK", "").strip()

    if not value:
        raise RuntimeError("MARKET_WRAP_WEBHOOK is required for --post.")

    return value


def send_market_wrap(webhook_url, payload):
    validate_market_wrap_payload(payload)
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord returned HTTP {response.status}"
            )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build or post the Main Line Trades Market Wrap."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Print the Discord payload without posting it.",
    )
    mode.add_argument(
        "--post",
        action="store_true",
        help="Post one Market Wrap to the configured webhook.",
    )
    return parser.parse_args(argv)


def print_preview(payload):
    reconfigure = getattr(sys.stdout, "reconfigure", None)

    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv=None):
    args = parse_args(argv)
    webhook_url = required_webhook() if args.post else None

    market_snapshot = morning_brief.get_market_snapshot()
    economic_events = morning_brief.get_high_impact_usd_events()
    crypto_snapshot = get_crypto_snapshot()
    cross_market_snapshot = get_cross_market_snapshot()
    global_market_snapshot = get_global_market_snapshot()
    payload = build_market_wrap_payload(
        market_snapshot,
        crypto_snapshot,
        cross_market_snapshot,
        economic_events,
        global_market_snapshot,
    )

    if args.preview:
        print_preview(payload)
        return

    send_market_wrap(webhook_url, payload)
    print("Posted one Market Wrap Discord message.")


if __name__ == "__main__":
    main()
