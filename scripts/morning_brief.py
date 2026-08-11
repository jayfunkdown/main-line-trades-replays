#!/usr/bin/env python3
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import json
import os
import random
import re
import urllib.parse
import urllib.request
import time
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from scripts.discord_embeds import bordered_webhook_payload
except ModuleNotFoundError:
    from discord_embeds import bordered_webhook_payload


# ============================================================
# Configuration
# ============================================================

ECONOMIC_CALENDAR_URL = (
    "https://nfs.faireconomy.media/"
    "ff_calendar_thisweek.json"
)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"].strip()

EASTERN = ZoneInfo("America/New_York")

DISCORD_MESSAGE_LIMIT = 4096
SAFE_MESSAGE_LIMIT = 3900

MORNING_BRIEF_WEBHOOK_USERNAME = "Main Line Trades Morning Brief"
EARNINGS_CALENDAR_WEBHOOK_USERNAME = "Main Line Trades Earnings Calendar"
USER_AGENT = "MainLineTrades-MorningBrief/3.0"


# These names are displayed when available.
# Every other earnings ticker is still included using its symbol.
COMPANY_NAMES = {
    "AAL": "American Airlines",
    "AAPL": "Apple",
    "ABBV": "AbbVie",
    "ABNB": "Airbnb",
    "ABT": "Abbott Laboratories",
    "ACN": "Accenture",
    "ADBE": "Adobe",
    "ADI": "Analog Devices",
    "ADM": "Archer-Daniels-Midland",
    "ADP": "Automatic Data Processing",
    "ADSK": "Autodesk",
    "AEP": "American Electric Power",
    "AFRM": "Affirm",
    "AI": "C3.ai",
    "ALB": "Albemarle",
    "AMAT": "Applied Materials",
    "AMD": "Advanced Micro Devices",
    "AMGN": "Amgen",
    "AMZN": "Amazon",
    "ANET": "Arista Networks",
    "ARM": "Arm Holdings",
    "ASML": "ASML",
    "AVGO": "Broadcom",
    "AXP": "American Express",
    "BA": "Boeing",
    "BABA": "Alibaba",
    "BAC": "Bank of America",
    "BB": "BlackBerry",
    "BIDU": "Baidu",
    "BIIB": "Biogen",
    "BKNG": "Booking Holdings",
    "BLK": "BlackRock",
    "BMY": "Bristol Myers Squibb",
    "BP": "BP",
    "BX": "Blackstone",
    "C": "Citigroup",
    "CAG": "Conagra Brands",
    "CAR": "Avis Budget",
    "CAT": "Caterpillar",
    "CCL": "Carnival",
    "CELH": "Celsius Holdings",
    "CHWY": "Chewy",
    "CL": "Colgate-Palmolive",
    "CMG": "Chipotle",
    "COIN": "Coinbase",
    "COP": "ConocoPhillips",
    "COST": "Costco",
    "CRM": "Salesforce",
    "CRWD": "CrowdStrike",
    "CSCO": "Cisco",
    "CVNA": "Carvana",
    "CVS": "CVS Health",
    "CVX": "Chevron",
    "DAL": "Delta Air Lines",
    "DASH": "DoorDash",
    "DDOG": "Datadog",
    "DE": "Deere",
    "DELL": "Dell Technologies",
    "DG": "Dollar General",
    "DIS": "Disney",
    "DKNG": "DraftKings",
    "DOCU": "DocuSign",
    "DPZ": "Domino's Pizza",
    "EA": "Electronic Arts",
    "EBAY": "eBay",
    "EL": "Estée Lauder",
    "ENPH": "Enphase Energy",
    "ETSY": "Etsy",
    "EXPE": "Expedia",
    "F": "Ford",
    "FAST": "Fastenal",
    "FCX": "Freeport-McMoRan",
    "FDX": "FedEx",
    "FSLR": "First Solar",
    "GE": "GE Aerospace",
    "GILD": "Gilead Sciences",
    "GIS": "General Mills",
    "GM": "General Motors",
    "GME": "GameStop",
    "GOOG": "Alphabet",
    "GOOGL": "Alphabet",
    "GS": "Goldman Sachs",
    "HD": "Home Depot",
    "HIMS": "Hims & Hers",
    "HLT": "Hilton",
    "HOOD": "Robinhood",
    "HPQ": "HP",
    "IBM": "IBM",
    "INTC": "Intel",
    "ISRG": "Intuitive Surgical",
    "JD": "JD.com",
    "JNJ": "Johnson & Johnson",
    "JPM": "JPMorgan Chase",
    "KHC": "Kraft Heinz",
    "KLAC": "KLA",
    "KO": "Coca-Cola",
    "KR": "Kroger",
    "LHX": "L3Harris Technologies",
    "LI": "Li Auto",
    "LLY": "Eli Lilly",
    "LMT": "Lockheed Martin",
    "LOW": "Lowe's",
    "LRCX": "Lam Research",
    "LULU": "Lululemon",
    "LYFT": "Lyft",
    "MA": "Mastercard",
    "MAR": "Marriott",
    "MARA": "MARA Holdings",
    "MCD": "McDonald's",
    "MDB": "MongoDB",
    "MDLZ": "Mondelez",
    "MELI": "MercadoLibre",
    "META": "Meta Platforms",
    "MGM": "MGM Resorts",
    "MHK": "Mohawk Industries",
    "MNDY": "Monday.com",
    "MRK": "Merck",
    "MRNA": "Moderna",
    "MRVL": "Marvell Technology",
    "MS": "Morgan Stanley",
    "MSFT": "Microsoft",
    "MSTR": "Strategy",
    "MU": "Micron Technology",
    "NCLH": "Norwegian Cruise Line",
    "NET": "Cloudflare",
    "NFLX": "Netflix",
    "NIO": "Nio",
    "NKE": "Nike",
    "NOW": "ServiceNow",
    "NTES": "NetEase",
    "NUE": "Nucor",
    "NVDA": "Nvidia",
    "OKTA": "Okta",
    "ON": "ON Semiconductor",
    "ORCL": "Oracle",
    "PANW": "Palo Alto Networks",
    "PATH": "UiPath",
    "PDD": "PDD Holdings",
    "PEP": "PepsiCo",
    "PFE": "Pfizer",
    "PG": "Procter & Gamble",
    "PINS": "Pinterest",
    "PLTR": "Palantir",
    "PM": "Philip Morris",
    "PYPL": "PayPal",
    "QCOM": "Qualcomm",
    "RBLX": "Roblox",
    "RCL": "Royal Caribbean",
    "RIVN": "Rivian",
    "ROKU": "Roku",
    "RTX": "RTX",
    "S": "SentinelOne",
    "SBUX": "Starbucks",
    "SCHW": "Charles Schwab",
    "SHOP": "Shopify",
    "SLB": "SLB",
    "SMCI": "Super Micro Computer",
    "SNAP": "Snap",
    "SNOW": "Snowflake",
    "SOFI": "SoFi Technologies",
    "SPOT": "Spotify",
    "SQ": "Block",
    "T": "AT&T",
    "TGT": "Target",
    "TJX": "TJX Companies",
    "TMO": "Thermo Fisher Scientific",
    "TMUS": "T-Mobile",
    "TSLA": "Tesla",
    "TSM": "Taiwan Semiconductor",
    "TTD": "The Trade Desk",
    "TWLO": "Twilio",
    "UAL": "United Airlines",
    "UBER": "Uber",
    "ULTA": "Ulta Beauty",
    "UNH": "UnitedHealth",
    "UPS": "UPS",
    "V": "Visa",
    "VZ": "Verizon",
    "W": "Wayfair",
    "WBA": "Walgreens Boots Alliance",
    "WBD": "Warner Bros. Discovery",
    "WFC": "Wells Fargo",
    "WMT": "Walmart",
    "WYNN": "Wynn Resorts",
    "XOM": "Exxon Mobil",
    "XP": "XP",
    "XPEV": "XPeng",
    "ZM": "Zoom",
    "ZS": "Zscaler",
}


# These appear first within each reporting-time group.
# Every other ticker is still included afterward.
PRIORITY_TICKERS = {
    "AAPL",
    "ABBV",
    "ABNB",
    "ABT",
    "ADBE",
    "AFRM",
    "AI",
    "AMAT",
    "AMD",
    "AMZN",
    "ARM",
    "ASML",
    "AVGO",
    "BA",
    "BABA",
    "BAC",
    "BKNG",
    "BLK",
    "C",
    "CAT",
    "CCL",
    "CELH",
    "CMG",
    "COIN",
    "COST",
    "CRM",
    "CRWD",
    "CVNA",
    "CVX",
    "DASH",
    "DDOG",
    "DE",
    "DELL",
    "DIS",
    "DKNG",
    "F",
    "FDX",
    "GE",
    "GM",
    "GME",
    "GOOG",
    "GOOGL",
    "GS",
    "HD",
    "HIMS",
    "HOOD",
    "IBM",
    "INTC",
    "JNJ",
    "JPM",
    "LLY",
    "LULU",
    "MA",
    "MCD",
    "MELI",
    "META",
    "MRNA",
    "MRVL",
    "MS",
    "MSFT",
    "MSTR",
    "MU",
    "NET",
    "NFLX",
    "NIO",
    "NKE",
    "NOW",
    "NVDA",
    "ORCL",
    "PANW",
    "PDD",
    "PFE",
    "PINS",
    "PLTR",
    "PYPL",
    "QCOM",
    "RBLX",
    "RIVN",
    "ROKU",
    "SBUX",
    "SHOP",
    "SMCI",
    "SNAP",
    "SNOW",
    "SOFI",
    "SPOT",
    "SQ",
    "TGT",
    "TSLA",
    "TSM",
    "TTD",
    "UBER",
    "UNH",
    "V",
    "WMT",
    "XOM",
    "ZM",
}


# Five-star names tend to be the most market-moving earnings reports.
# Three-star names are major sector leaders. Every other ticker in the
# established priority list remains a four-star active-trading watchlist name.
FIVE_STAR_EARNINGS_TICKERS = {
    "AAPL", "AMD", "AMZN", "AVGO", "BA", "BABA", "BAC", "COIN",
    "COST", "CRM", "CVX", "DDOG", "DIS", "GOOG", "GOOGL", "GS",
    "HD", "JPM", "LLY", "META", "MSFT", "MSTR", "NFLX", "NVDA",
    "ORCL", "PLTR", "QCOM", "TSLA", "TSM", "UNH", "WMT", "XOM",
}

THREE_STAR_EARNINGS_TICKERS = {
    "ABBV", "ABT", "BLK", "CAT", "DE", "FDX", "GE", "IBM", "JNJ",
    "MA", "MCD", "MS", "PFE", "V",
}

EARNINGS_PRIORITY_GUIDE = [
    "🔥 **Priority guide:**",
    "⭐⭐⭐⭐⭐ High impact",
    "⭐⭐⭐⭐ Active trading watchlist",
    "⭐⭐⭐ Major sector leader",
]


MARKET_SYMBOLS = [
    ("ES=F", "ES", "S&P 500 Futures"),
    ("NQ=F", "NQ", "Nasdaq-100 Futures"),
    ("YM=F", "YM", "Dow Futures"),
    ("RTY=F", "RTY", "Russell 2000 Futures"),
]


KEY_MARKETS = [
    ("BINANCE:BTCUSDT", "BTC", "₿ Bitcoin", "finnhub", True, "$"),
    ("GC=F", "GC", "🥇 Gold Futures", "yahoo", False, "$"),
    ("CL=F", "CL", "🛢️ Crude Oil Futures", "yahoo", False, "$"),
    ("DX-Y.NYB", "DXY", "💵 U.S. Dollar Index", "yahoo", False, ""),
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


# ============================================================
# HTTP helpers
# ============================================================

def get_json(url):
    request = urllib.request.Request(
        url,
        headers={
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


def finnhub_url(endpoint, parameters):
    values = dict(parameters)
    values["token"] = FINNHUB_API_KEY

    return (
        f"{FINNHUB_BASE_URL}/{endpoint}?"
        f"{urllib.parse.urlencode(values)}"
    )


# ============================================================
# Economic calendar
# ============================================================

def format_eastern_time(event_time):
    return event_time.strftime(
        "%I:%M %p"
    ).lstrip("0")


def get_high_impact_usd_events():
    try:
        events = get_json(
            ECONOMIC_CALENDAR_URL
        )
    except Exception as error:
        print(
            f"Economic calendar unavailable: {error}"
        )
        return []

    today = datetime.now(EASTERN).date()
    results = []

    for event in events:
        if event.get("impact") != "High":
            continue

        if event.get("country") != "USD":
            continue

        raw_date = event.get("date")

        if not raw_date:
            continue

        try:
            event_time = datetime.fromisoformat(
                raw_date
            ).astimezone(EASTERN)
        except (TypeError, ValueError):
            continue

        if event_time.date() != today:
            continue

        results.append(
            {
                "title": (
                    event.get("title")
                    or "Unknown Event"
                ),
                "time": event_time,
                "forecast": (
                    event.get("forecast")
                    or "Not listed"
                ),
                "actual": (
                    event.get("actual")
                    or "Not released"
                ),
                "previous": (
                    event.get("previous")
                    or "Not listed"
                ),
            }
        )

    return sorted(
        results,
        key=lambda item: item["time"],
    )


# ============================================================
# Market quotes
# ============================================================

def get_quote(symbol):
    try:
        data = get_json(
            finnhub_url(
                "quote",
                {"symbol": symbol},
            )
        )

        price = data.get("c")
        percent_change = data.get("dp")

        if (
            not isinstance(price, (int, float))
            or price <= 0
        ):
            return None

        return {
            "price": float(price),
            "percent_change": (
                float(percent_change)
                if isinstance(
                    percent_change,
                    (int, float),
                )
                else None
            ),
        }

    except Exception as error:
        print(
            f"Quote unavailable for "
            f"{symbol}: {error}"
        )
        return None


def get_market_snapshot():
    return [
        {
            "symbol": display_symbol,
            "name": name,
            "quote": get_yahoo_index_quote(source_symbol),
            "price_prefix": "",
        }
        for source_symbol, display_symbol, name in MARKET_SYMBOLS
    ]


def get_key_markets():
    results = []

    for (
        source_symbol,
        display_symbol,
        name,
        source,
        is_crypto,
        price_prefix,
    ) in KEY_MARKETS:
        results.append(
            {
                "symbol": display_symbol,
                "name": name,
                "is_crypto": is_crypto,
                "price_prefix": price_prefix,
                "quote": (
                    get_quote(source_symbol)
                    if source == "finnhub"
                    else get_yahoo_index_quote(source_symbol)
                ),
            }
        )

    return results


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
    try:
        payload = get_json(yahoo_index_chart_url(symbol))
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
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as error:
        print(f"Global index unavailable for {symbol}: {error}")
        return None
    except Exception as error:
        print(f"Global index unavailable for {symbol}: {error}")
        return None


def get_global_market_snapshot():
    return [
        {
            "symbol": symbol,
            "name": name,
            "quote": get_yahoo_index_quote(symbol),
        }
        for symbol, name in GLOBAL_MARKET_SYMBOLS
    ]


def direction_icon(percent_change):
    if percent_change is None:
        return "•"

    if percent_change > 0:
        return "▲"

    if percent_change < 0:
        return "▼"

    return "—"


def format_price(price, is_crypto=False, price_prefix="$"):
    if is_crypto:
        return f"{price_prefix}{price:,.0f}"

    return f"{price_prefix}{price:,.2f}"


def quote_line(item):
    quote = item["quote"]

    if not quote:
        return (
            f"• **{item['name']} "
            f"({item['symbol']}):** Unavailable"
        )

    change = quote["percent_change"]
    icon = direction_icon(change)

    if change is None:
        change_text = "Change unavailable"
    else:
        change_text = f"{change:+.2f}%"

    price_text = format_price(
        quote["price"],
        item.get("is_crypto", False),
        item.get("price_prefix", "$"),
    )

    return (
        f"{icon} **{item['symbol']} — "
        f"{item['name']}:** "
        f"{price_text} ({change_text})"
    )


def global_quote_line(item):
    quote = item["quote"]

    if not quote:
        return f"• **{item['name']}:** Unavailable"

    change = quote["percent_change"]
    icon = direction_icon(change)
    change_text = (
        f"{change:+.2f}%"
        if change is not None
        else "Change unavailable"
    )

    return (
        f"{icon} **{item['name']}:** "
        f"{quote['price']:,.2f} ({change_text})"
    )


# ============================================================
# Earnings
# ============================================================

def normalize_symbol(value):
    symbol = str(
        value or ""
    ).upper().strip()

    # Avoid malformed or clearly unusable symbols.
    if not symbol:
        return ""

    if len(symbol) > 15:
        return ""

    if not re.fullmatch(
        r"[A-Z0-9.\-]+",
        symbol,
    ):
        return ""

    return symbol


def get_all_earnings():
    today = datetime.now(
        EASTERN
    ).date().isoformat()

    try:
        response = get_json(
            finnhub_url(
                "calendar/earnings",
                {
                    "from": today,
                    "to": today,
                    "international": "false",
                },
            )
        )
    except Exception as error:
        print(
            f"Earnings calendar unavailable: {error}"
        )
        return []

    calendar = response.get(
        "earningsCalendar",
        [],
    )

    results = []
    seen = set()

    for report in calendar:
        symbol = normalize_symbol(
            report.get("symbol")
        )

        if not symbol:
            continue

        hour = str(
            report.get("hour") or ""
        ).lower().strip()

        if hour not in {
            "bmo",
            "amc",
            "dmh",
        }:
            hour = "unknown"

        key = (symbol, hour)

        if key in seen:
            continue

        seen.add(key)

        results.append(
            {
                "symbol": symbol,
                "name": COMPANY_NAMES.get(
                    symbol,
                    "",
                ),
                "hour": hour,
                "priority": (
                    symbol in PRIORITY_TICKERS
                ),
                "priority_stars": earnings_priority_stars(symbol),
            }
        )

    return results


def group_earnings(earnings):
    groups = {
        "bmo": [],
        "dmh": [],
        "amc": [],
        "unknown": [],
    }

    for report in earnings:
        groups[
            report.get("hour", "unknown")
        ].append(report)

    for key in groups:
        groups[key].sort(
            key=lambda item: (
                not item["priority"],
                -item.get(
                    "priority_stars",
                    earnings_priority_stars(item["symbol"]),
                ),
                item["symbol"],
            )
        )

    return groups


def earnings_priority_stars(symbol):
    if symbol in FIVE_STAR_EARNINGS_TICKERS:
        return 5
    if symbol in THREE_STAR_EARNINGS_TICKERS:
        return 3
    if symbol in PRIORITY_TICKERS:
        return 4
    return 0


def format_featured_earning(report):
    symbol = report["symbol"]
    name = report["name"]
    stars = "⭐" * report.get(
        "priority_stars",
        earnings_priority_stars(symbol),
    )

    if name:
        return f"{stars} **{symbol}** — {name}"

    return f"{stars} **{symbol}**"


def compact_ticker_lines(
    reports,
    max_line_length=110,
):
    if not reports:
        return []

    lines = []
    current = ""

    for report in reports:
        token = f"`{report['symbol']}`"

        candidate = (
            token
            if not current
            else f"{current}  {token}"
        )

        if len(candidate) <= max_line_length:
            current = candidate
        else:
            lines.append(current)
            current = token

    if current:
        lines.append(current)

    return lines


def build_earnings_group(
    title,
    reports,
):
    if not reports:
        return []

    featured = [
        report
        for report in reports
        if report["priority"]
    ]

    other = [
        report
        for report in reports
        if not report["priority"]
    ]

    lines = [
        title,
        "",
    ]

    if featured:
        for report in featured:
            lines.append(
                format_featured_earning(report)
            )

        lines.append("")

    if other:
        lines.append(
            f"*Plus {len(other)} additional scheduled reports.*"
        )
        lines.append("")

    return lines


# ============================================================
# Message construction
# ============================================================

def market_breadth(items):
    changes = [
        item["quote"]["percent_change"]
        for item in items
        if item.get("quote")
        and item["quote"].get("percent_change") is not None
    ]
    if not changes:
        return "unavailable"

    positive = sum(change > 0 for change in changes)
    negative = sum(change < 0 for change in changes)
    threshold = max(1, (len(changes) * 3 + 3) // 4)
    if positive >= threshold:
        return "broadly higher"
    if negative >= threshold:
        return "broadly lower"
    return "mixed"


def build_morning_read(market_snapshot, global_markets):
    futures_read = market_breadth(market_snapshot)
    global_read = market_breadth(global_markets)
    first_sentence = (
        f"U.S. equity futures are {futures_read}, while global markets "
        f"are {global_read}."
    )

    available = [
        (item, item["symbol"])
        for item in market_snapshot
        if item.get("quote")
        and item["quote"].get("percent_change") is not None
    ] + [
        (item, item["name"])
        for item in global_markets
        if item.get("quote")
        and item["quote"].get("percent_change") is not None
    ]
    if not available:
        return first_sentence

    leader = max(
        available,
        key=lambda entry: entry[0]["quote"]["percent_change"],
    )
    laggard = min(
        available,
        key=lambda entry: entry[0]["quote"]["percent_change"],
    )
    leader_item, leader_name = leader
    laggard_item, laggard_name = laggard
    return (
        f"{first_sentence} {leader_name} is leading at "
        f"{leader_item['quote']['percent_change']:+.2f}%, while "
        f"{laggard_name} is lagging at "
        f"{laggard_item['quote']['percent_change']:+.2f}%."
    )


def eastern_capture_time(value):
    return value.strftime("%I:%M %p").lstrip("0")

def build_market_message(
    events,
    market_snapshot,
    global_markets,
    key_markets,
):
    today = datetime.now(EASTERN)

    lines = [
        "# 🌅 Main Line Trades Morning Brief",
        "",
        f"📅 **{today.strftime('%A, %B %d, %Y')}**",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "## 🌅 Morning Read",
        "",
        build_morning_read(market_snapshot, global_markets),
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "## 🚨 High-Impact USD Events",
        "",
    ]

    if events:
        for event in events:
            time_text = format_eastern_time(
                event["time"]
            )

            lines.extend(
                [
                    (
                        f"🇺🇸 **{time_text} ET — "
                        f"{event['title']}**"
                    ),
                    (
                        f"🎯 Forecast: "
                        f"**{event['forecast']}** | "
                        f"📉 Previous: "
                        f"**{event['previous']}**"
                    ),
                    "",
                ]
            )

        lines.extend(
            [
                (
                    "⚠️ Be prepared for increased "
                    "volatility around these "
                    "scheduled release times."
                ),
                "",
            ]
        )
    else:
        lines.extend(
            [
                (
                    "✅ No high-impact U.S. "
                    "economic releases are "
                    "scheduled today."
                ),
                "",
            ]
        )

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "## 📈 U.S. Futures Snapshot",
            "",
        ]
    )

    for item in market_snapshot:
        lines.append(
            quote_line(item)
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "## 🌍 Global Markets",
            "",
        ]
    )

    if global_markets:
        for item in global_markets:
            lines.append(global_quote_line(item))
    else:
        lines.append("• Global index data unavailable.")

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "## 💰 Key Markets",
            "",
        ]
    )

    for item in key_markets:
        lines.append(
            quote_line(item)
        )

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
    )

    quote = random.choice(
        TRADING_QUOTES
    )

    lines.extend(
        [
            "",
            "## 🧠 Trading Focus",
            "",
            f"*“{quote}”*",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "## 🎥 Live Today",
            "",
            (
                "Join Main Line Trades around "
                "**8:30 AM Eastern** for the "
                "New York Open session."
            ),
            "",
            (
                "📢 Live links will be posted in "
                "**📢︱announcements**."
            ),
            "",
            (
                "Trade smart, manage your risk, "
                "and we'll see you live! 📈"
            ),
            "",
            (
                f"*🕒 Market data captured at approximately "
                f"{eastern_capture_time(today)} Eastern. Futures and "
                "global-market prices may be delayed.*"
            ),
        ]
    )

    return "\n".join(lines).strip()


def build_earnings_message(earnings):
    groups = group_earnings(
        earnings
    )

    lines = [
        "# 📅 Today's Earnings Calendar",
        "",
        (
            f"**{len(earnings)} scheduled "
            "U.S. earnings report"
            f"{'' if len(earnings) == 1 else 's'} today.**"
        ),
        "",
        *EARNINGS_PRIORITY_GUIDE,
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if not earnings:
        lines.extend(
            [
                (
                    "✅ No U.S. companies are "
                    "currently listed on today's "
                    "earnings calendar."
                ),
                "",
            ]
        )
    else:
        lines.extend(
            build_earnings_group(
                "## 🔔 Before Market Open",
                groups["bmo"],
            )
        )

        lines.extend(
            build_earnings_group(
                "## 🏛️ During Market Hours",
                groups["dmh"],
            )
        )

        lines.extend(
            build_earnings_group(
                "## 🌙 After Market Close",
                groups["amc"],
            )
        )

        lines.extend(
            build_earnings_group(
                "## 🕒 Time Not Confirmed",
                groups["unknown"],
            )
        )

        lines.extend(
            [
                (
                    "⚠️ Earnings dates and reporting "
                    "times can change. Confirm timing "
                    "before trading an individual stock."
                ),
                "",
            ]
        )

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
    )

    return "\n".join(lines).strip()


# ============================================================
# Discord message splitting
# ============================================================

def split_long_line(line, limit):
    if len(line) <= limit:
        return [line]

    parts = []
    remaining = line

    while len(remaining) > limit:
        split_at = remaining.rfind(
            " ",
            0,
            limit,
        )

        if split_at <= 0:
            split_at = limit

        parts.append(
            remaining[:split_at].rstrip()
        )

        remaining = remaining[
            split_at:
        ].lstrip()

    if remaining:
        parts.append(remaining)

    return parts


def split_discord_message(
    message,
    limit=SAFE_MESSAGE_LIMIT,
):
    if len(message) <= limit:
        return [message]

    chunks = []
    current_lines = []
    current_length = 0

    for original_line in message.splitlines():
        line_parts = split_long_line(
            original_line,
            limit,
        )

        for line in line_parts:
            added_length = (
                len(line)
                + (1 if current_lines else 0)
            )

            if (
                current_lines
                and current_length + added_length
                > limit
            ):
                chunks.append(
                    "\n".join(
                        current_lines
                    ).strip()
                )

                current_lines = []
                current_length = 0

            current_lines.append(line)

            current_length += (
                len(line)
                + (1 if len(current_lines) > 1 else 0)
            )

    if current_lines:
        chunks.append(
            "\n".join(
                current_lines
            ).strip()
        )

    return [
        chunk
        for chunk in chunks
        if chunk
    ]


# ============================================================
# Discord posting
# ============================================================

def required_webhook(name):
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"{name} is required."
        )

    return value


def send_webhook_message(
    webhook_url,
    username,
    message,
):
    if len(message) > DISCORD_MESSAGE_LIMIT:
        raise ValueError(
            "Discord message still exceeds "
            "2,000 characters after splitting."
        )

    payload = json.dumps(
        bordered_webhook_payload(
            username,
            message,
        )
    ).encode("utf-8")

    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        if response.status not in (
            200,
            204,
        ):
            raise RuntimeError(
                f"Discord returned HTTP "
                f"{response.status}"
            )


def post_messages(deliveries):
    message_number = 0

    for index, delivery in enumerate(deliveries):
        if index > 0:
            time.sleep(10)

        webhook_url, username, message = delivery
        chunks = split_discord_message(message)

        for chunk in chunks:
            message_number += 1
            send_webhook_message(
                webhook_url,
                username,
                chunk,
            )

    return message_number


# ============================================================
# Main
# ============================================================

def main():
    morning_brief_webhook = required_webhook(
        "MORNING_BRIEF_WEBHOOK"
    )
    earnings_calendar_webhook = required_webhook(
        "EARNINGS_CALENDAR_WEBHOOK"
    )

    events = get_high_impact_usd_events()
    earnings = get_all_earnings()
    market_snapshot = get_market_snapshot()
    global_markets = get_global_market_snapshot()
    key_markets = get_key_markets()

    market_message = build_market_message(
        events,
        market_snapshot,
        global_markets,
        key_markets,
    )

    earnings_message = build_earnings_message(
        earnings
    )

    posted_message_count = post_messages(
        [
            (
                morning_brief_webhook,
                MORNING_BRIEF_WEBHOOK_USERNAME,
                market_message,
            ),
            (
                earnings_calendar_webhook,
                EARNINGS_CALENDAR_WEBHOOK_USERNAME,
                earnings_message,
            ),
        ]
    )

    groups = group_earnings(
        earnings
    )

    print(
        f"Posted {posted_message_count} Discord "
        f"message(s) with {len(events)} economic "
        f"event(s), {len(earnings)} total earnings "
        f"report(s), {len(groups['bmo'])} before open, "
        f"{len(groups['dmh'])} during market hours, "
        f"{len(groups['amc'])} after close, and "
        f"{len(groups['unknown'])} unconfirmed."
    )


if __name__ == "__main__":
    main()
