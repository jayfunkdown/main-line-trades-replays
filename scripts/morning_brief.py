#!/usr/bin/env python3

import json
import os
import random
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


COMPANY_NAMES = {
    "AAPL": "Apple",
    "ABBV": "AbbVie",
    "ABNB": "Airbnb",
    "ADBE": "Adobe",
    "AMD": "Advanced Micro Devices",
    "AMGN": "Amgen",
    "AMZN": "Amazon",
    "AVGO": "Broadcom",
    "BA": "Boeing",
    "BAC": "Bank of America",
    "BABA": "Alibaba",
    "BLK": "BlackRock",
    "CAT": "Caterpillar",
    "COIN": "Coinbase",
    "COST": "Costco",
    "CRM": "Salesforce",
    "CVX": "Chevron",
    "DIS": "Disney",
    "F": "Ford",
    "FDX": "FedEx",
    "GE": "GE Aerospace",
    "GM": "General Motors",
    "GOOG": "Alphabet",
    "GOOGL": "Alphabet",
    "GS": "Goldman Sachs",
    "HD": "Home Depot",
    "IBM": "IBM",
    "INTC": "Intel",
    "JNJ": "Johnson & Johnson",
    "JPM": "JPMorgan Chase",
    "KO": "Coca-Cola",
    "LLY": "Eli Lilly",
    "LOW": "Lowe's",
    "MA": "Mastercard",
    "MCD": "McDonald's",
    "META": "Meta Platforms",
    "MS": "Morgan Stanley",
    "MSFT": "Microsoft",
    "MU": "Micron Technology",
    "NFLX": "Netflix",
    "NKE": "Nike",
    "NVDA": "Nvidia",
    "ORCL": "Oracle",
    "PEP": "PepsiCo",
    "PFE": "Pfizer",
    "PLTR": "Palantir",
    "PYPL": "PayPal",
    "QCOM": "Qualcomm",
    "SBUX": "Starbucks",
    "SHOP": "Shopify",
    "SNAP": "Snap",
    "SOFI": "SoFi Technologies",
    "T": "AT&T",
    "TGT": "Target",
    "TSLA": "Tesla",
    "UBER": "Uber",
    "UNH": "UnitedHealth",
    "UPS": "UPS",
    "V": "Visa",
    "VZ": "Verizon",
    "WMT": "Walmart",
    "XOM": "Exxon Mobil",
}

MAJOR_TICKERS = set(COMPANY_NAMES)

MARKET_SYMBOLS = [
    ("SPY", "S&P 500 ETF"),
    ("QQQ", "Nasdaq-100 ETF"),
    ("DIA", "Dow ETF"),
    ("IWM", "Russell 2000 ETF"),
]

KEY_MARKETS = [
    ("BINANCE:BTCUSDT", "₿ Bitcoin", True),
    ("GLD", "🥇 Gold ETF", False),
    ("USO", "🛢️ Oil ETF", False),
    ("UUP", "💵 U.S. Dollar ETF", False),
]

TRADING_QUOTES = [
    "Trade the reaction—not the prediction.",
    "Protecting capital comes before making profit.",
    "Patience is a position.",
    "The best trade may be no trade at all.",
    "Follow the process and let the outcome take care of itself.",
    "Good risk management keeps you in the game.",
    "Wait for confirmation. Do not force the setup.",
    "Consistency beats excitement.",
]


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MainLineTrades-MorningBrief/2.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def finnhub_url(endpoint, parameters):
    values = dict(parameters)
    values["token"] = FINNHUB_API_KEY
    return (
        f"{FINNHUB_BASE_URL}/{endpoint}?"
        f"{urllib.parse.urlencode(values)}"
    )


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


def get_quote(symbol):
    try:
        data = get_json(
            finnhub_url("quote", {"symbol": symbol})
        )

        price = data.get("c")
        percent_change = data.get("dp")

        if not isinstance(price, (int, float)) or price <= 0:
            return None

        return {
            "price": float(price),
            "percent_change": (
                float(percent_change)
                if isinstance(percent_change, (int, float))
                else None
            ),
        }
    except Exception as error:
        print(f"Quote unavailable for {symbol}: {error}")
        return None


def get_market_snapshot():
    results = []

    for symbol, name in MARKET_SYMBOLS:
        results.append(
            {
                "symbol": symbol,
                "name": name,
                "quote": get_quote(symbol),
            }
        )

    return results


def get_key_markets():
    results = []

    for symbol, name, is_crypto in KEY_MARKETS:
        results.append(
            {
                "symbol": symbol,
                "name": name,
                "is_crypto": is_crypto,
                "quote": get_quote(symbol),
            }
        )

    return results


def get_major_earnings():
    today = datetime.now(EASTERN).date().isoformat()

    response = get_json(
        finnhub_url(
            "calendar/earnings",
            {"from": today, "to": today},
        )
    )

    earnings = response.get("earningsCalendar", [])
    results = []
    seen = set()

    for report in earnings:
        symbol = str(report.get("symbol", "")).upper()

        if symbol not in MAJOR_TICKERS:
            continue

        hour = str(report.get("hour", "")).lower()
        key = (symbol, hour)

        if key in seen:
            continue

        seen.add(key)

        results.append(
            {
                "symbol": symbol,
                "name": COMPANY_NAMES.get(symbol, symbol),
                "hour": hour,
            }
        )

    return results


def group_earnings(earnings):
    before_open = []
    after_close = []
    unspecified = []

    for report in earnings:
        if report["hour"] == "bmo":
            before_open.append(report)
        elif report["hour"] == "amc":
            after_close.append(report)
        else:
            unspecified.append(report)

    return before_open, after_close, unspecified


def direction_icon(percent_change):
    if percent_change is None:
        return "•"
    if percent_change > 0:
        return "▲"
    if percent_change < 0:
        return "▼"
    return "—"


def format_price(price, is_crypto=False):
    if is_crypto:
        return f"${price:,.0f}"

    if price >= 100:
        return f"${price:,.2f}"

    return f"${price:.2f}"


def quote_line(item):
    quote = item["quote"]

    if not quote:
        return f"• **{item['name']} ({item['symbol']}):** Unavailable"

    change = quote["percent_change"]
    icon = direction_icon(change)
    change_text = (
        f"{change:+.2f}%"
        if change is not None
        else "Change unavailable"
    )

    price_text = format_price(
        quote["price"],
        item.get("is_crypto", False),
    )

    return (
        f"{icon} **{item['symbol']} — {item['name']}:** "
        f"{price_text} ({change_text})"
    )


def add_earnings_group(lines, title, reports):
    if not reports:
        return

    lines.extend([title, ""])

    # Cap each group to keep the Discord message readable.
    for report in sorted(
        reports,
        key=lambda item: item["symbol"],
    )[:10]:
        lines.append(
            f"• **{report['symbol']}** — {report['name']}"
        )

    if len(reports) > 10:
        lines.append(f"• Plus {len(reports) - 10} additional report(s)")

    lines.append("")


def build_message(events, earnings, market_snapshot, key_markets):
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
            lines.extend(
                [
                    (
                        f"🇺🇸 **{format_eastern_time(event['time'])} ET"
                        f" — {event['title']}**"
                    ),
                    (
                        f"🎯 Forecast: **{event['forecast']}** | "
                        f"📉 Previous: **{event['previous']}**"
                    ),
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

    lines.extend(["---", "", "## 📈 U.S. Market Snapshot", ""])

    for item in market_snapshot:
        lines.append(quote_line(item))

    lines.extend(
        [
            "",
            "*ETF proxies shown; these are not futures contracts.*",
            "",
            "---",
            "",
            "## 💰 Key Markets",
            "",
        ]
    )

    for item in key_markets:
        lines.append(quote_line(item))

    lines.extend(["", "---", "", "## 📅 Major Earnings Today", ""])

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
        add_earnings_group(
            lines,
            "### 🔔 Before Market Open",
            before_open,
        )
        add_earnings_group(
            lines,
            "### 🌙 After Market Close",
            after_close,
        )
        add_earnings_group(
            lines,
            "### 🕒 Time Not Confirmed",
            unspecified,
        )

        lines.extend(
            [
                "⚠️ Earnings schedules can change. Confirm timing "
                "before trading an individual stock.",
                "",
            ]
        )

    quote = random.choice(TRADING_QUOTES)

    lines.extend(
        [
            "---",
            "",
            "## 🧠 Trading Focus",
            "",
            f"*“{quote}”*",
            "",
            "## 🎥 Live Today",
            "",
            "Join Main Line Trades around **8:30 AM Eastern** "
            "for the New York Open session.",
            "",
            "📢 Live links will be posted in **📢︱announcements**.",
            "",
            "Trade smart, manage your risk, and we'll see you live! 📈",
        ]
    )

    return "\n".join(lines)


def post_to_discord(message):
    # Discord messages have a 2,000-character limit.
    if len(message) > 1990:
        message = message[:1950] + "\n\n*Brief shortened to fit Discord.*"

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
            "User-Agent": "MainLineTrades-MorningBrief/2.0",
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
    market_snapshot = get_market_snapshot()
    key_markets = get_key_markets()

    message = build_message(
        events,
        earnings,
        market_snapshot,
        key_markets,
    )

    post_to_discord(message)

    print(
        f"Posted morning brief with {len(events)} event(s), "
        f"{len(earnings)} earnings report(s), "
        f"{len(market_snapshot)} market proxies, and "
        f"{len(key_markets)} key markets."
    )


if __name__ == "__main__":
    main()
