#!/usr/bin/env python3

"""
Main Line Trades - Earnings Reactions

Creates two ranked earnings feeds:

1. Public earnings reactions
   - High-importance names only
   - Default maximum: 15

2. Private earnings review
   - Broader chart-review queue
   - Default maximum: 50

Price movement is the primary ranking factor.

Required environment variables:
    FINNHUB_API_KEY

Required for public posting:
    EARNINGS_REACTIONS_WEBHOOK

Optional for private posting:
    EARNINGS_REVIEW_WEBHOOK

Optional configuration:
    EARNINGS_PUBLIC_MAX=15
    EARNINGS_PRIVATE_MAX=50
    EARNINGS_PRIVATE_MOVE_PCT=5
    EARNINGS_PUBLIC_MOVE_PCT=15
    EARNINGS_PRIORITY_PRIVATE_MOVE_PCT=3
    EARNINGS_QUOTE_DELAY_SECONDS=1.1
    EARNINGS_QUOTE_CACHE_MINUTES=20
    EARNINGS_MAX_QUOTE_CALLS_PER_RUN=120
    EARNINGS_EARLY_MORNING_CUTOFF_HOUR=6

Required for private review chart/button workflow:
    DISCORD_BOT_TOKEN
    SIGNALS_CHANNEL_ID

Optional for workflow logging:
    BOT_LOG_CHANNEL_ID
    (If omitted, the bot searches for a channel containing "bot-log".)

The private review channel is resolved automatically from
EARNINGS_REVIEW_WEBHOOK.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


import argparse
import asyncio
import calendar
import copy
import hashlib
import io
import math
import tempfile
import json
from numbers import Real
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable
from zoneinfo import ZoneInfo

try:
    from .earnings_state import EarningsStateError, EarningsStateStore
except ImportError:
    from earnings_state import EarningsStateError, EarningsStateStore

try:
    from .discord_embeds import BRAND_NEON_PINK, bordered_embed
except ImportError:
    from discord_embeds import BRAND_NEON_PINK, bordered_embed


FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
EASTERN = ZoneInfo("America/New_York")

STATE_FILE = PROJECT_ROOT / "data" / "earnings_reactions_state.json"
CALENDAR_CACHE_FILE = PROJECT_ROOT / "data" / "earnings_calendar_cache.json"

USER_AGENT = "MainLineTrades-EarningsReactions/8.1"
PUBLIC_WEBHOOK_USERNAME = "Main Line Trades Earnings"
PRIVATE_WEBHOOK_USERNAME = "Main Line Trades Research"

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

MAX_DISCORD_ATTEMPTS = 4
DISCORD_POST_DELAY_SECONDS = 1.5
DISCORD_BULK_DELETE_LIMIT = 100
DISCORD_BULK_DELETE_SAFE_AGE = timedelta(days=13, hours=23)
DISCORD_API_BASE = "https://discord.com/api/v10"
YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
WEEKLY_CHART_WEEKS = 52

SIGNAL_DELIVERY_READY = "ready"
SIGNAL_DELIVERY_SENDING = "sending"
SIGNAL_DELIVERY_SENT = "sent"
SIGNAL_DELIVERY_UNKNOWN = "unknown"
SIGNAL_DELIVERY_STATUSES = {
    SIGNAL_DELIVERY_READY,
    SIGNAL_DELIVERY_SENDING,
    SIGNAL_DELIVERY_SENT,
    SIGNAL_DELIVERY_UNKNOWN,
}

MANUAL_SIGNAL_READY = "ready"
MANUAL_SIGNAL_SENDING = "sending"
MANUAL_SIGNAL_SENT = "sent"
MANUAL_SIGNAL_UNKNOWN = "unknown"
MANUAL_SIGNAL_STATUSES = {
    MANUAL_SIGNAL_READY,
    MANUAL_SIGNAL_SENDING,
    MANUAL_SIGNAL_SENT,
    MANUAL_SIGNAL_UNKNOWN,
}
MANUAL_SIGNAL_MAX_CONTENT_LENGTH = 2000
TRADE_DIRECTION_LONG = "long"
TRADE_DIRECTION_SHORT = "short"
TRADE_DIRECTIONS = {
    TRADE_DIRECTION_LONG,
    TRADE_DIRECTION_SHORT,
}

POST_SIGNAL_REVIEW_SCHEDULED = "scheduled"
POST_SIGNAL_REVIEW_DRAFTING = "drafting"
POST_SIGNAL_REVIEW_DRAFT_READY = "draft_ready"
POST_SIGNAL_REVIEW_PUBLISHING = "publishing"
POST_SIGNAL_REVIEW_PUBLISHED = "published"
POST_SIGNAL_REVIEW_DISMISSED = "dismissed"
POST_SIGNAL_REVIEW_UNKNOWN = "unknown"
POST_SIGNAL_REVIEW_STATUSES = {
    POST_SIGNAL_REVIEW_SCHEDULED,
    POST_SIGNAL_REVIEW_DRAFTING,
    POST_SIGNAL_REVIEW_DRAFT_READY,
    POST_SIGNAL_REVIEW_PUBLISHING,
    POST_SIGNAL_REVIEW_PUBLISHED,
    POST_SIGNAL_REVIEW_DISMISSED,
    POST_SIGNAL_REVIEW_UNKNOWN,
}
POST_SIGNAL_OUTCOMES = {
    "still_active": "Still Active",
    "worked": "Worked",
    "invalidated": "Invalidated",
    "no_clear_follow_through": "No Clear Follow-Through",
}
POST_SIGNAL_REVIEW_SOURCE_EARNINGS = "earnings"
POST_SIGNAL_REVIEW_SOURCE_MANUAL = "manual"
POST_SIGNAL_REVIEW_SOURCES = {
    POST_SIGNAL_REVIEW_SOURCE_EARNINGS,
    POST_SIGNAL_REVIEW_SOURCE_MANUAL,
}
MANUAL_SIGNAL_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
MANUAL_SIGNAL_DISCORD_CDN_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
}
MANUAL_SIGNAL_MAX_CHART_BYTES = 25 * 1024 * 1024

FEED_DELIVERY_RESERVED = "reserved"
FEED_DELIVERY_CONFIRMED = "confirmed"
FEED_DELIVERY_FAILED = "failed"
FEED_DELIVERY_UNKNOWN = "unknown"
FEED_DELIVERY_INVALID = "invalid"
FEED_DELIVERY_STATUSES = {
    FEED_DELIVERY_RESERVED,
    FEED_DELIVERY_CONFIRMED,
    FEED_DELIVERY_FAILED,
    FEED_DELIVERY_UNKNOWN,
}


class DefiniteDeliveryError(RuntimeError):
    """Discord definitely did not accept the delivery."""


class AmbiguousDeliveryError(RuntimeError):
    """Discord may have accepted the delivery; automatic retry is unsafe."""


class PublicChartPreparationError(DefiniteDeliveryError):
    """Public chart preparation failed before Discord was contacted."""


class PublicChartPreparationCancelled(asyncio.CancelledError):
    """Public chart preparation was canceled before Discord was contacted."""


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


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()

    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be an integer."
        ) from exc


def env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()

    if not raw_value:
        return default

    try:
        return float(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a number."
        ) from exc


def safe_number(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_json(
    url: str,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"HTTP {exc.code} returned by {url}: {body}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach {url}: {exc.reason}"
        ) from exc


def finnhub_get(
    endpoint: str,
    parameters: dict[str, str],
) -> dict[str, Any]:
    query = dict(parameters)
    query["token"] = required_env("FINNHUB_API_KEY")

    url = (
        f"{FINNHUB_BASE_URL}{endpoint}?"
        f"{urllib.parse.urlencode(query)}"
    )

    return get_json(url)


def get_quote_with_retry(
    symbol: str,
    *,
    maximum_attempts: int = 4,
) -> dict[str, Any]:
    for attempt in range(1, maximum_attempts + 1):
        try:
            return finnhub_get(
                "/quote",
                {
                    "symbol": symbol,
                },
            )

        except RuntimeError as exc:
            error_text = str(exc)

            if "HTTP 429" not in error_text:
                raise

            if attempt == maximum_attempts:
                raise

            wait_seconds = 10 * attempt

            print(
                f"Finnhub rate limit for {symbol}. "
                f"Waiting {wait_seconds} seconds..."
            )

            time.sleep(wait_seconds)

    return {}


def surprise_percent(
    actual: Any,
    estimate: Any,
) -> float | None:
    actual_number = safe_number(actual)
    estimate_number = safe_number(estimate)

    if actual_number is None or estimate_number is None:
        return None

    denominator = abs(estimate_number)

    if denominator < 0.0000001:
        return None

    return (
        (actual_number - estimate_number)
        / denominator
        * 100
    )


def result_direction(
    actual: Any,
    estimate: Any,
) -> str:
    actual_number = safe_number(actual)
    estimate_number = safe_number(estimate)

    if actual_number is None or estimate_number is None:
        return "unknown"

    if actual_number > estimate_number:
        return "beat"

    if actual_number < estimate_number:
        return "miss"

    return "inline"


def format_eps(value: Any) -> str:
    number = safe_number(value)

    if number is None:
        return "N/A"

    return f"${number:,.2f}"


def format_revenue(value: Any) -> str:
    number = safe_number(value)

    if number is None:
        return "N/A"

    absolute_number = abs(number)

    if absolute_number >= 1_000_000_000:
        return f"${number / 1_000_000_000:,.2f}B"

    if absolute_number >= 1_000_000:
        return f"${number / 1_000_000:,.1f}M"

    if absolute_number >= 1_000:
        return f"${number / 1_000:,.1f}K"

    return f"${number:,.0f}"


def reporting_session(hour: Any) -> str:
    normalized = str(hour or "").strip().lower()

    labels = {
        "bmo": "Before Market Open",
        "amc": "After Market Close",
        "dmh": "During Market Hours",
    }

    return labels.get(
        normalized,
        "Reporting Time Not Confirmed",
    )



def previous_us_weekday(current_date):
    candidate = current_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def resolve_automatic_target_date(
    now_eastern: datetime | None = None,
) -> tuple[str, str]:
    now_eastern = now_eastern or datetime.now(EASTERN)

    cutoff_hour = env_int(
        "EARNINGS_EARLY_MORNING_CUTOFF_HOUR",
        6,
    )

    if not 0 <= cutoff_hour <= 23:
        raise RuntimeError(
            "EARNINGS_EARLY_MORNING_CUTOFF_HOUR "
            "must be between 0 and 23."
        )

    today = now_eastern.date()

    if today.weekday() >= 5:
        target = previous_us_weekday(today)
        return target.isoformat(), "weekend rollover"

    if now_eastern.hour < cutoff_hour:
        target = previous_us_weekday(today)
        return (
            target.isoformat(),
            f"early-morning rollover (before {cutoff_hour:02d}:00 ET)",
        )

    return today.isoformat(), "current Eastern date"



def fetch_completed_reports_from_finnhub(
    target_date: str,
) -> list[dict[str, Any]]:
    response = finnhub_get(
        "/calendar/earnings",
        {
            "from": target_date,
            "to": target_date,
            "international": "false",
        },
    )

    reports = response.get(
        "earningsCalendar",
        [],
    )

    if not isinstance(reports, list):
        return []

    completed_reports: list[dict[str, Any]] = []

    for report in reports:
        if not isinstance(report, dict):
            continue

        symbol = str(
            report.get("symbol") or ""
        ).strip().upper()

        if not symbol:
            continue

        has_eps = (
            safe_number(report.get("epsActual"))
            is not None
        )

        has_revenue = (
            safe_number(report.get("revenueActual"))
            is not None
        )

        if not has_eps and not has_revenue:
            continue

        report["symbol"] = symbol
        completed_reports.append(report)

    return completed_reports



def load_calendar_cache() -> dict[str, Any]:
    if not CALENDAR_CACHE_FILE.exists():
        return {}

    try:
        value = json.loads(
            CALENDAR_CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return {}

    return value if isinstance(value, dict) else {}


def save_calendar_cache(
    cache: dict[str, Any],
) -> None:
    CALENDAR_CACHE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file = (
        CALENDAR_CACHE_FILE.with_suffix(".tmp")
    )

    temporary_file.write_text(
        json.dumps(
            cache,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(
        CALENDAR_CACHE_FILE
    )


def get_cached_calendar_reports(
    target_date: str,
) -> list[dict[str, Any]]:
    cache = load_calendar_cache()
    entry = cache.get(target_date)

    if not isinstance(entry, dict):
        return []

    reports = entry.get("reports")

    if not isinstance(reports, list):
        return []

    return [
        report
        for report in reports
        if isinstance(report, dict)
    ]


def store_calendar_reports(
    target_date: str,
    reports: list[dict[str, Any]],
) -> None:
    cache = load_calendar_cache()

    cache[target_date] = {
        "fetched_at": datetime.now(EASTERN).isoformat(),
        "count": len(reports),
        "reports": reports,
    }

    # Keep only the most recent 7 dates.
    ordered_dates = sorted(
        cache.keys(),
        reverse=True,
    )

    for old_date in ordered_dates[7:]:
        cache.pop(old_date, None)

    save_calendar_cache(cache)


def get_completed_reports(
    target_date: str,
) -> list[dict[str, Any]]:
    """
    Fetch completed earnings reports from Finnhub safely.

    A valid non-empty calendar is cached by date. If Finnhub temporarily
    returns an empty calendar, retry before falling back to cache.

    Critically, if Finnhub returns zero and no cache exists, fail loudly
    instead of pretending that the earnings day contains zero reports.
    """
    cached_reports = get_cached_calendar_reports(
        target_date
    )

    maximum_attempts = 4
    last_error: RuntimeError | None = None

    for attempt in range(
        1,
        maximum_attempts + 1,
    ):
        try:
            reports = fetch_completed_reports_from_finnhub(
                target_date
            )
        except RuntimeError as exc:
            last_error = exc
            reports = []

        if reports:
            store_calendar_reports(
                target_date,
                reports,
            )

            if attempt > 1:
                print(
                    f"Finnhub earnings calendar recovered "
                    f"on attempt {attempt}: "
                    f"{len(reports)} completed report(s)."
                )

            return reports

        if attempt < maximum_attempts:
            wait_seconds = 10 * attempt

            print(
                f"Finnhub earnings calendar returned no usable "
                f"reports for {target_date} "
                f"(attempt {attempt}/{maximum_attempts}). "
                f"Waiting {wait_seconds} seconds before retry..."
            )

            time.sleep(wait_seconds)

    if cached_reports:
        print(
            "Finnhub still returned no usable earnings reports; "
            f"using {len(cached_reports)} cached report(s) "
            f"for {target_date} instead."
        )
        return cached_reports

    if last_error is not None:
        raise RuntimeError(
            "Finnhub earnings calendar failed and no cached "
            f"calendar exists for {target_date}: {last_error}"
        ) from last_error

    raise RuntimeError(
        "Finnhub returned 0 usable completed earnings reports "
        f"for {target_date} after {maximum_attempts} attempts, "
        "and no cached calendar exists yet. "
        "Nothing will be posted."
    )


def movement_score(move_percent: float) -> float:
    absolute_move = abs(move_percent)

    score = absolute_move * 5

    if absolute_move >= 20:
        score += 35
    elif absolute_move >= 15:
        score += 25
    elif absolute_move >= 10:
        score += 15
    elif absolute_move >= 7:
        score += 8

    return score


def calculate_candidate(
    report: dict[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    symbol = str(report["symbol"]).upper()

    eps_surprise = surprise_percent(
        report.get("epsActual"),
        report.get("epsEstimate"),
    )

    revenue_surprise = surprise_percent(
        report.get("revenueActual"),
        report.get("revenueEstimate"),
    )

    eps_direction = result_direction(
        report.get("epsActual"),
        report.get("epsEstimate"),
    )

    revenue_direction = result_direction(
        report.get("revenueActual"),
        report.get("revenueEstimate"),
    )

    move_percent = safe_number(
        quote.get("dp")
    )

    current_price = safe_number(
        quote.get("c")
    )

    score = 0.0

    if move_percent is not None:
        score += movement_score(move_percent)

    if symbol in PRIORITY_TICKERS:
        score += 30

    if eps_direction == revenue_direction:
        if eps_direction in {"beat", "miss"}:
            score += 8

    if (
        eps_surprise is not None
        and abs(eps_surprise) >= 50
    ):
        score += 5

    if (
        revenue_surprise is not None
        and abs(revenue_surprise) >= 15
    ):
        score += 10

    return {
        "report": report,
        "quote": quote,
        "symbol": symbol,
        "move_percent": move_percent,
        "current_price": current_price,
        "eps_surprise": eps_surprise,
        "revenue_surprise": revenue_surprise,
        "eps_direction": eps_direction,
        "revenue_direction": revenue_direction,
        "priority": symbol in PRIORITY_TICKERS,
        "score": score,
    }


def qualifies_for_private(
    candidate: dict[str, Any],
) -> bool:
    move_percent = candidate["move_percent"]

    if move_percent is None:
        return False

    absolute_move = abs(move_percent)

    private_move = env_float(
        "EARNINGS_PRIVATE_MOVE_PCT",
        5,
    )

    priority_private_move = env_float(
        "EARNINGS_PRIORITY_PRIVATE_MOVE_PCT",
        3,
    )

    if absolute_move >= private_move:
        return True

    if (
        candidate["priority"]
        and absolute_move >= priority_private_move
    ):
        return True

    extreme_eps = (
        candidate["eps_surprise"] is not None
        and abs(candidate["eps_surprise"]) >= 75
    )

    extreme_revenue = (
        candidate["revenue_surprise"] is not None
        and abs(candidate["revenue_surprise"]) >= 20
    )

    return (
        absolute_move >= 2
        and (extreme_eps or extreme_revenue)
    )


def qualifies_for_public(
    candidate: dict[str, Any],
) -> bool:
    move_percent = candidate["move_percent"]

    if move_percent is None:
        return False

    absolute_move = abs(move_percent)

    public_move = max(
        env_float("EARNINGS_PUBLIC_MOVE_PCT", 15),
        15,
    )

    return is_priority_candidate(candidate) or absolute_move >= public_move


def is_priority_candidate(candidate: dict[str, Any]) -> bool:
    priority = candidate.get("priority")

    if isinstance(priority, bool):
        return priority

    symbol = candidate.get("symbol")
    return (
        isinstance(symbol, str)
        and symbol.upper() in PRIORITY_TICKERS
    )


def result_icon(direction: str) -> str:
    icons = {
        "beat": "✅",
        "miss": "❌",
        "inline": "➖",
        "unknown": "⚪",
    }

    return icons.get(direction, "⚪")


def result_label(direction: str) -> str:
    labels = {
        "beat": "Beat",
        "miss": "Miss",
        "inline": "Inline",
        "unknown": "Not available",
    }

    return labels.get(direction, "Not available")


def build_public_message(
    candidate: dict[str, Any],
    *,
    leading_divider: bool = False,
) -> str:
    report = candidate["report"]
    symbol = candidate["symbol"]
    move_percent = candidate["move_percent"]
    current_price = candidate["current_price"]

    eps_surprise = candidate["eps_surprise"]
    revenue_surprise = candidate["revenue_surprise"]

    eps_surprise_text = (
        f" ({eps_surprise:+.1f}%)"
        if eps_surprise is not None
        else ""
    )

    revenue_surprise_text = (
        f" ({revenue_surprise:+.1f}%)"
        if revenue_surprise is not None
        else ""
    )

    move_icon = "🟢" if move_percent >= 0 else "🔴"

    price_text = (
        f" at **${current_price:,.2f}**"
        if current_price is not None
        else ""
    )

    return "\n".join(
        [
            *([DIVIDER, ""] if leading_divider else []),
            "# 💰 Earnings Reaction",
            "",
            f"## {symbol}",
            "",
            (
                f"{move_icon} **Price reaction: "
                f"{move_percent:+.2f}%**{price_text}"
            ),
            "",
            (
                f"{result_icon(candidate['eps_direction'])} "
                f"**EPS: "
                f"{result_label(candidate['eps_direction'])}**"
                f"{eps_surprise_text}"
            ),
            (
                f"Actual: **{format_eps(report.get('epsActual'))}** "
                f"| Estimate: "
                f"**{format_eps(report.get('epsEstimate'))}**"
            ),
            "",
            (
                f"{result_icon(candidate['revenue_direction'])} "
                f"**Revenue: "
                f"{result_label(candidate['revenue_direction'])}**"
                f"{revenue_surprise_text}"
            ),
            (
                f"Actual: "
                f"**{format_revenue(report.get('revenueActual'))}** "
                f"| Estimate: "
                f"**{format_revenue(report.get('revenueEstimate'))}**"
            ),
            "",
            (
                f"🕒 **Session:** "
                f"{reporting_session(report.get('hour'))}"
            ),
            "",
            "*Reported earnings data — not a trade signal.*",
        ]
    )


def build_private_message(
    candidate: dict[str, Any],
    rank: int,
) -> str:
    public_message = build_public_message(
        candidate,
        leading_divider=False,
    )

    return "\n".join(
        [
            f"# 🔬 Earnings Review #{rank}",
            "",
            public_message,
            "",
            f"**Review score:** {candidate['score']:.1f}",
            "",
            "📊 **Weekly chart is attached below.**",
        ]
    )



def yahoo_chart_url(
    symbol: str,
) -> str:
    encoded_symbol = urllib.parse.quote(
        symbol.upper()
    )

    query = urllib.parse.urlencode(
        {
            "range": "5y",
            "interval": "1d",
            "includeAdjustedClose": "true",
            "events": "div,splits",
        }
    )

    return (
        f"{YAHOO_CHART_BASE}/"
        f"{encoded_symbol}?{query}"
    )


def fetch_daily_candles(
    symbol: str,
) -> list[dict[str, float]]:
    request = urllib.request.Request(
        yahoo_chart_url(symbol),
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
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
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(
            f"Could not load chart data for "
            f"{symbol}: {exc}"
        ) from exc

    try:
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]

        opens = quote["open"]
        highs = quote["high"]
        lows = quote["low"]
        closes = quote["close"]
        volumes = quote.get(
            "volume",
            [0] * len(timestamps),
        )
    except (
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        raise RuntimeError(
            f"Yahoo returned incomplete chart "
            f"data for {symbol}."
        ) from exc

    candles: list[
        dict[str, float]
    ] = []

    for (
        timestamp,
        open_price,
        high_price,
        low_price,
        close_price,
        volume,
    ) in zip(
        timestamps,
        opens,
        highs,
        lows,
        closes,
        volumes,
    ):
        if any(
            value is None
            for value in (
                timestamp,
                open_price,
                high_price,
                low_price,
                close_price,
            )
        ):
            continue

        candles.append(
            {
                "timestamp": float(timestamp),
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(
                    volume or 0
                ),
            }
        )

    if not candles:
        raise RuntimeError(
            f"No usable chart candles returned "
            f"for {symbol}."
        )

    return candles


def latest_chart_close(symbol: str) -> float:
    candles = fetch_daily_candles(symbol)
    close = normalized_reference_level(candles[-1].get("close"))
    if close is None:
        raise RuntimeError(f"No current chart price returned for {symbol}.")
    return close


def aggregate_weekly_candles(
    daily_candles: list[
        dict[str, float]
    ],
    *,
    max_weeks: int = WEEKLY_CHART_WEEKS,
) -> list[dict[str, Any]]:
    weeks: dict[
        tuple[int, int],
        list[dict[str, float]],
    ] = {}

    for candle in daily_candles:
        day = datetime.fromtimestamp(
            candle["timestamp"],
            tz=timezone.utc,
        )

        iso_year, iso_week, _ = (
            day.isocalendar()
        )

        weeks.setdefault(
            (iso_year, iso_week),
            [],
        ).append(candle)

    weekly: list[
        dict[str, Any]
    ] = []

    for week_key in sorted(
        weeks.keys()
    ):
        entries = weeks[week_key]

        first = entries[0]
        last = entries[-1]

        weekly.append(
            {
                "date": datetime.fromtimestamp(
                    last["timestamp"],
                    tz=timezone.utc,
                ),
                "open": first["open"],
                "high": max(
                    item["high"]
                    for item in entries
                ),
                "low": min(
                    item["low"]
                    for item in entries
                ),
                "close": last["close"],
                "volume": sum(
                    item["volume"]
                    for item in entries
                ),
            }
        )

    return weekly[-max_weeks:]


def generate_weekly_chart(
    symbol: str,
    *,
    output_path: Path | None = None,
    weeks: int = WEEKLY_CHART_WEEKS,
    level_segments: list[dict[str, Any]] | None = None,
) -> Path:
    """
    Generate the earnings weekly candlestick chart.

    This chart is used for public Earnings Movers and private review posts.
    The final Signals post uses the user's pasted TradingView chart.
    """
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError as exc:
        raise RuntimeError(
            "Weekly charts require matplotlib. "
            "Install it with: python -m pip install matplotlib"
        ) from exc

    daily = fetch_daily_candles(symbol)
    weekly = aggregate_weekly_candles(daily, max_weeks=weeks)
    if weeks < 4:
        raise ValueError("Weekly chart history must include at least four weeks.")

    if len(weekly) < 4:
        raise RuntimeError(
            f"Not enough weekly chart history for {symbol}."
        )

    chart_dir = PROJECT_ROOT / "data" / "earnings_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        output_path = chart_dir / f"{symbol.upper()}_weekly.png"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    background = "#05070B"
    grid_color = "#202938"
    text_color = "#D7DEE9"
    axis_color = "#596579"
    bullish_color = "#00E5FF"
    bearish_color = "#A855F7"

    fig, ax = plt.subplots(
        figsize=(12, 7),
        dpi=140,
        facecolor=background,
    )
    ax.set_facecolor(background)

    for index, candle in enumerate(weekly):
        open_price = candle["open"]
        high_price = candle["high"]
        low_price = candle["low"]
        close_price = candle["close"]

        candle_color = (
            bullish_color
            if close_price >= open_price
            else bearish_color
        )

        ax.vlines(
            index,
            low_price,
            high_price,
            color=candle_color,
            linewidth=1.25,
            zorder=2,
        )

        body_low = min(open_price, close_price)
        body_height = abs(close_price - open_price)

        if body_height == 0:
            body_height = max(close_price * 0.001, 0.01)

        ax.add_patch(
            Rectangle(
                (index - 0.33, body_low),
                0.66,
                body_height,
                facecolor=candle_color,
                edgecolor=candle_color,
                linewidth=1.0,
                zorder=3,
            )
        )

    for segment in level_segments or []:
        price = segment.get("price")
        start_date = parse_iso_datetime(segment.get("start_date"))
        if isinstance(price, bool) or not isinstance(price, Real) or start_date is None:
            raise ValueError("Chart levels require numeric prices and ISO start dates.")
        start_index = next(
            (
                index
                for index, candle in enumerate(weekly)
                if candle["date"].date() >= start_date.date()
            ),
            len(weekly) - 1,
        )
        ax.hlines(
            float(price),
            start_index,
            len(weekly) - 0.35,
            color="#FF9800",
            linewidth=1.8,
            zorder=4,
        )
        ax.annotate(
            f"{float(price):.4f}",
            xy=(len(weekly) - 0.35, float(price)),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            color="#05070B",
            fontsize=8,
            fontweight="bold",
            bbox={"boxstyle": "round,pad=0.16", "fc": "#FF9800", "ec": "#FF9800"},
            zorder=5,
        )

    label_indexes = list(
        range(
            0,
            len(weekly),
            max(1, len(weekly) // 8),
        )
    )

    ax.set_xticks(label_indexes)
    ax.set_xticklabels(
        [
            weekly[index]["date"].strftime("%b %y")
            for index in label_indexes
        ],
        color=text_color,
    )

    latest = weekly[-1]

    ax.set_title(
        f"{symbol.upper()} • Weekly Chart • Close ${latest['close']:.2f}",
        fontsize=16,
        fontweight="bold",
        pad=14,
        color=text_color,
    )

    ax.set_ylabel("Price ($)", color=text_color)

    ax.grid(
        True,
        color=grid_color,
        alpha=0.45,
        linewidth=0.7,
    )

    for spine in ax.spines.values():
        spine.set_color(axis_color)
        spine.set_linewidth(0.8)

    ax.tick_params(axis="both", colors=text_color)
    ax.margins(x=0.015)

    fig.tight_layout()
    fig.savefig(
        output_path,
        bbox_inches="tight",
        facecolor=background,
    )
    plt.close(fig)

    return output_path


def temporary_weekly_chart_path(symbol: str) -> Path:
    """Return a unique public-chart path without creating the file."""
    chart_dir = PROJECT_ROOT / "data" / "earnings_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = re.sub(r"[^A-Za-z0-9._-]", "_", symbol.upper())
    return chart_dir / f".{safe_symbol}_weekly_{uuid.uuid4().hex}.tmp.png"


def combine_post_signal_review_charts(
    original_chart: bytes | Path,
    updated_chart: bytes | Path,
    *,
    output_path: Path,
) -> Path:
    """Build one wide Before/After image that Discord renders card-width."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Signal review chart composition requires Pillow.") from exc

    def open_image(source: bytes | Path) -> Any:
        if isinstance(source, bytes):
            return Image.open(io.BytesIO(source)).convert("RGB")
        return Image.open(source).convert("RGB")

    original = open_image(original_chart)
    updated = open_image(updated_chart)
    panel_width = max(original.width, updated.width)

    def fit_width(image: Any) -> Any:
        if image.width == panel_width:
            return image
        height = max(1, round(image.height * panel_width / image.width))
        return image.resize((panel_width, height), Image.Resampling.LANCZOS)

    original = fit_width(original)
    updated = fit_width(updated)
    panel_height = max(original.height, updated.height)

    def fit_panel_height(image: Any) -> Any:
        if image.height == panel_height:
            return image
        panel = Image.new("RGB", (panel_width, panel_height), "#05070B")
        panel.paste(image, (0, (panel_height - image.height) // 2))
        return panel

    original = fit_panel_height(original)
    updated = fit_panel_height(updated)
    width = panel_width * 2
    band_height = max(72, round(panel_width * 0.045))
    canvas = Image.new(
        "RGB",
        (width, band_height + panel_height),
        "#05070B",
    )
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arialbd.ttf", max(24, round(panel_width * 0.022)))
    except OSError:
        font = ImageFont.load_default()

    def title_band(x: int, title: str) -> None:
        draw.rectangle((x, 0, x + panel_width, band_height), fill="#0D1119")
        draw.line(
            (x, band_height - 3, x + panel_width, band_height - 3),
            fill="#FF2BD6",
            width=3,
        )
        box = draw.textbbox((0, 0), title, font=font)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        draw.text(
            (x + (panel_width - text_width) / 2, (band_height - text_height) / 2 - box[1]),
            title,
            fill="#F4F7FF",
            font=font,
        )

    title_band(0, "BEFORE — ORIGINAL SIGNAL CHART")
    title_band(panel_width, "AFTER — UPDATED WEEKLY CHART")
    canvas.paste(original, (0, band_height))
    canvas.paste(updated, (panel_width, band_height))
    draw.line((panel_width, 0, panel_width, canvas.height), fill="#FF2BD6", width=3)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path


def weekly_chart_filename(symbol: str) -> str:
    """Return the clean member-facing weekly chart filename."""
    safe_symbol = re.sub(r"[^A-Za-z0-9._-]", "_", symbol.upper())
    return f"{safe_symbol}_weekly.png"


def cleanup_weekly_chart(path: Path) -> None:
    """Best-effort cleanup that cannot change a delivery outcome."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(
            "Could not remove temporary earnings chart:",
            repr(exc),
            flush=True,
        )

def resolve_webhook_channel_id(
    webhook_url: str,
) -> str:
    request = urllib.request.Request(
        webhook_url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            payload = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(
            "Could not resolve the private "
            "earnings-review channel from "
            "EARNINGS_REVIEW_WEBHOOK."
        ) from exc

    channel_id = str(
        payload.get("channel_id")
        or ""
    ).strip()

    if not channel_id:
        raise RuntimeError(
            "EARNINGS_REVIEW_WEBHOOK did not "
            "return a channel_id."
        )

    return channel_id


def candidate_button_token(
    candidate: dict[str, Any],
) -> str:
    key = report_key(
        candidate["report"]
    )

    return hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()[:20]


def serializable_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return json.loads(
        json.dumps(candidate)
    )


def multipart_body(
    *,
    payload: dict[str, Any],
    file_path: Path,
    file_name: str | None = None,
) -> tuple[bytes, str]:
    """
    Build Discord multipart/form-data correctly.

    Discord requires real CRLF separators between multipart headers,
    payload_json, and file data. Literal backslash-r/backslash-n text
    causes Discord to ignore the payload and report an empty message.
    """
    boundary = (
        "----MainLineTrades"
        + hashlib.sha256(
            str(time.time_ns()).encode()
        ).hexdigest()[:24]
    )

    crlf = b"\r\n"
    file_bytes = file_path.read_bytes()
    attachment_name = file_name or file_path.name

    parts: list[bytes] = []

    parts.append(
        f"--{boundary}".encode("utf-8")
        + crlf
    )

    parts.append(
        b'Content-Disposition: form-data; name="payload_json"'
        + crlf
    )

    parts.append(
        b"Content-Type: application/json"
        + crlf
        + crlf
    )

    parts.append(
        json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
    )

    parts.append(crlf)

    parts.append(
        f"--{boundary}".encode("utf-8")
        + crlf
    )

    parts.append(
        (
            'Content-Disposition: form-data; '
            'name="files[0]"; '
            f'filename="{attachment_name}"'
        ).encode("utf-8")
        + crlf
    )

    parts.append(
        b"Content-Type: image/png"
        + crlf
        + crlf
    )

    parts.append(file_bytes)
    parts.append(crlf)

    parts.append(
        f"--{boundary}--".encode("utf-8")
        + crlf
    )

    return (
        b"".join(parts),
        boundary,
    )

def send_private_review_with_chart(
    candidate: dict[str, Any],
    rank: int,
    state: dict[str, Any],
) -> str:
    bot_token = required_env(
        "DISCORD_BOT_TOKEN"
    )

    review_webhook = required_env(
        "EARNINGS_REVIEW_WEBHOOK"
    )

    review_channel_id = (
        resolve_webhook_channel_id(
            review_webhook
        )
    )

    chart_path = (
        generate_weekly_chart(
            candidate["symbol"]
        )
    )

    token = candidate_button_token(
        candidate
    )

    attachment_name = chart_path.name
    payload = {
        "embeds": [
            bordered_embed(
                build_private_message(candidate, rank),
                color=BRAND_NEON_PINK,
                image_url=f"attachment://{attachment_name}",
            )
        ],
        "attachments": [
            {
                "id": 0,
                "filename": attachment_name,
                "description": (
                    f"{candidate['symbol']} "
                    "weekly chart"
                ),
            }
        ],
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": (
                            "Send to Signals"
                        ),
                        "emoji": {
                            "name": "📣",
                        },
                        "custom_id": "earnings_send_to_signals",
                    }
                ],
            },
        ],
        "allowed_mentions": {
            "parse": [],
        },
    }

    body, boundary = multipart_body(
        payload=payload,
        file_path=chart_path,
    )

    url = (
        f"{DISCORD_API_BASE}/channels/"
        f"{review_channel_id}/messages"
    )

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": (
                f"Bot {bot_token}"
            ),
            "Content-Type": (
                "multipart/form-data; "
                f"boundary={boundary}"
            ),
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=45,
        ) as response:
            response_payload = json.loads(
                response.read().decode(
                    "utf-8"
                )
            )
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise DefiniteDeliveryError(
            "Discord bot could not post the "
            "private earnings review: "
            f"HTTP {exc.code}: {error_body}"
        ) from exc

    if not isinstance(response_payload, dict):
        raise AmbiguousDeliveryError(
            "Discord accepted the private earnings review, "
            "but returned an unexpected response."
        )

    message_id = str(
        response_payload.get("id")
        or ""
    )

    if not message_id:
        raise AmbiguousDeliveryError(
            "Discord accepted the private earnings review, "
            "but did not return a message ID."
        )

    queue_item = {
        "candidate": (
            serializable_candidate(
                candidate
            )
        ),
        "review_message_id": (
            message_id
        ),
        "review_channel_id": (
            review_channel_id
        ),
        "created_at": (
            datetime.now(
                EASTERN
            ).isoformat()
        ),
        "sent_to_signals": False,
        "delivery_status": SIGNAL_DELIVERY_READY,
    }

    try:
        latest_state = set_state_record(
            "signal_queue",
            token,
            queue_item,
        )
    except Exception as exc:
        raise AmbiguousDeliveryError(
            "Discord accepted the private earnings review, "
            "but its review state could not be confirmed."
        ) from exc
    state.clear()
    state.update(latest_state)

    return message_id



def private_test_candidate() -> dict[str, Any]:
    """
    Small deterministic candidate used only to test the private
    chart/button Discord workflow without scanning the earnings calendar.
    """
    return {
        "report": {
            "date": datetime.now(EASTERN).date().isoformat(),
            "symbol": "IOVA",
            "year": datetime.now(EASTERN).year,
            "quarter": 3,
            "hour": "bmo",
            "epsActual": -0.11,
            "epsEstimate": -0.13,
            "revenueActual": 99_300_000,
            "revenueEstimate": 89_400_000,
        },
        "quote": {
            "c": 6.21,
            "dp": 43.09,
        },
        "symbol": "IOVA",
        "move_percent": 43.09,
        "current_price": 6.21,
        "eps_surprise": 15.3846153846,
        "revenue_surprise": 11.0738255034,
        "eps_direction": "beat",
        "revenue_direction": "beat",
        "priority": False,
        "score": 258.4,
    }


def run_private_test() -> None:
    candidate = private_test_candidate()
    key = "private-test:" + report_key(candidate["report"])
    state, reservation_status, attempt_id = reserve_feed_delivery(
        "private",
        key,
        candidate["symbol"],
        force=True,
    )
    if (
        reservation_status != FEED_DELIVERY_RESERVED
        or attempt_id is None
    ):
        raise RuntimeError(
            "The private test delivery is already reserved or has an "
            "unknown outcome; reconcile its state before retrying."
        )

    try:
        message_id = send_private_review_with_chart(
            candidate,
            1,
            state,
        )
    except asyncio.CancelledError as exc:
        transition_feed_delivery(
            "private",
            key,
            attempt_id,
            FEED_DELIVERY_UNKNOWN,
            error=concise_delivery_error(exc),
        )
        raise
    except Exception as exc:
        transition_feed_delivery(
            "private",
            key,
            attempt_id,
            failed_feed_delivery_status(exc),
            error=concise_delivery_error(exc),
        )
        raise

    _state, confirmed = transition_feed_delivery(
        "private",
        key,
        attempt_id,
        FEED_DELIVERY_CONFIRMED,
        discord_message_id=message_id,
    )
    if not confirmed:
        raise EarningsStateError(
            "Private test delivery confirmation no longer matches "
            "its reservation."
        )

    print(
        "Private earnings review test posted "
        f"successfully. Message ID: {message_id}"
    )


def build_signal_message(
    candidate: dict[str, Any],
    trade_thesis: str,
    trade_direction: str,
    reference_level: Any = None,
) -> str:
    direction = normalized_trade_direction(trade_direction)
    if direction is None:
        raise ValueError("Trade direction must be Long or Short.")
    move_percent = candidate.get("move_percent")
    current_price = candidate.get("current_price")

    move_text = (
        f"{move_percent:+.2f}%"
        if move_percent is not None
        else "N/A"
    )

    price_text = (
        f"${current_price:,.2f}"
        if current_price is not None
        else "N/A"
    )

    eps_surprise = candidate.get("eps_surprise")
    revenue_surprise = candidate.get("revenue_surprise")

    eps_surprise_text = (
        f" ({eps_surprise:+.1f}%)"
        if eps_surprise is not None
        else ""
    )

    revenue_surprise_text = (
        f" ({revenue_surprise:+.1f}%)"
        if revenue_surprise is not None
        else ""
    )

    lines = [
            "# 📈 Trade Signal",
            "",
            f"## {candidate['symbol']}",
            "",
            trade_direction_line(direction),
            "",
    ]
    normalized_level = normalized_reference_level(reference_level)
    if normalized_level is not None:
        lines.extend([f"🎯 **Reference level:** ${normalized_level:,.4f}", ""])
    lines.extend(
        [
            f"🟢 **Earnings Reaction:** {move_text}",
            f"💰 **Price:** {price_text}",
            "",
            (
                f"{result_icon(candidate['eps_direction'])} "
                f"**EPS: {result_label(candidate['eps_direction'])}**"
                f"{eps_surprise_text}"
            ),
            (
                f"{result_icon(candidate['revenue_direction'])} "
                f"**Revenue: {result_label(candidate['revenue_direction'])}**"
                f"{revenue_surprise_text}"
            ),
            "",
            "## 🧠 Trade Thesis",
            "",
            trade_thesis.strip(),
            "",
            "📊 **Trade Chart**",
            "",
            "*Chart and thesis provided by Main Line Trades.*",
            "",
            "⚠️ **Manage risk. This is not financial advice.**",
        ]
    )
    return "\n".join(lines)


def build_manual_signal_message(
    instrument: str,
    trade_thesis: str,
    *,
    trade_direction: str | None = None,
    timeframe: str = "",
    setup_name: str = "",
    reference_level: Any = None,
) -> str:
    """Build the exact member-facing manual Signals message."""
    lines = [
        "# 📈 Trade Signal",
        "",
        f"## {instrument.strip()}",
        "",
        trade_direction_line(trade_direction),
        "",
    ]
    normalized_level = normalized_reference_level(reference_level)
    if normalized_level is not None:
        lines.append(f"🎯 **Reference level:** ${normalized_level:,.4f}")
    if timeframe.strip():
        lines.append(f"🕒 **Timeframe:** {timeframe.strip()}")
    if setup_name.strip():
        lines.append(f"🎯 **Setup:** {setup_name.strip()}")
    if timeframe.strip() or setup_name.strip():
        lines.append("")
    lines.extend(
        [
            "## 🧠 Trade Thesis",
            "",
            trade_thesis.strip(),
            "",
            "## 📊 Trade Chart",
            "",
            "*Chart and thesis provided by Main Line Trades.*",
            "",
            "⚠️ **Manage risk. This is not financial advice.**",
        ]
    )
    return "\n".join(lines)


def normalized_trade_direction(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    direction = value.strip().lower()
    return direction if direction in TRADE_DIRECTIONS else None


def normalized_reference_level(value: Any) -> float | None:
    """Return one positive finite chart reference level."""
    if isinstance(value, bool):
        return None
    try:
        level = float(value)
    except (TypeError, ValueError):
        return None
    return level if math.isfinite(level) and level > 0 else None


def direction_adjusted_performance(
    reference_level: Any,
    current_price: Any,
    trade_direction: Any,
) -> float | None:
    reference = normalized_reference_level(reference_level)
    current = normalized_reference_level(current_price)
    direction = normalized_trade_direction(trade_direction)
    if reference is None or current is None or direction is None:
        return None
    raw_change = ((current - reference) / reference) * 100.0
    return raw_change if direction == TRADE_DIRECTION_LONG else -raw_change


def trade_direction_line(value: Any) -> str:
    direction = normalized_trade_direction(value)
    if direction == TRADE_DIRECTION_LONG:
        return "🟢 **Direction:** Long"
    if direction == TRADE_DIRECTION_SHORT:
        return "🔴 **Direction:** Short"
    return "⚪ **Direction:** Select Long or Short"


def one_calendar_month_after(value: Any) -> str:
    sent_at = parse_iso_datetime(value)
    if sent_at is None:
        raise ValueError("Signal sent_at must be a valid ISO datetime.")

    if sent_at.month == 12:
        target_year = sent_at.year + 1
        target_month = 1
    else:
        target_year = sent_at.year
        target_month = sent_at.month + 1

    target_day = min(
        sent_at.day,
        calendar.monthrange(target_year, target_month)[1],
    )
    return sent_at.replace(
        year=target_year,
        month=target_month,
        day=target_day,
    ).isoformat()


def normalized_post_signal_outcome(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    outcome = value.strip().lower()
    return outcome if outcome in POST_SIGNAL_OUTCOMES else None


def is_due_post_signal_review(value: Any, now: datetime) -> bool:
    if not is_valid_post_signal_review_record(value):
        return False
    due_at = parse_iso_datetime(value.get("review_due_at"))
    if due_at is None:
        return False
    if now.tzinfo is None:
        return False
    return (
        value.get("review_status") == POST_SIGNAL_REVIEW_SCHEDULED
        and due_at <= now
    )


def post_signal_review_elapsed_days(value: Any, now: datetime) -> int:
    sent_at = parse_iso_datetime(value)
    if sent_at is None or now.tzinfo is None:
        raise ValueError("Review dates must be timezone-aware ISO datetimes.")
    return max(0, (now - sent_at).days)


def build_post_signal_review_record(
    *,
    source: str,
    source_record_id: str,
    signals_channel_id: Any,
    signals_message_id: Any,
    symbol: str,
    trade_direction: Any,
    trade_thesis: str,
    original_chart_filename: str,
    sent_at: str,
    reference_level: Any = None,
) -> dict[str, Any]:
    normalized_source = str(source or "").strip().lower()
    normalized_direction = normalized_trade_direction(trade_direction)
    channel_id = discord_id_text(signals_channel_id)
    message_id = discord_id_text(signals_message_id)
    sent_datetime = parse_iso_datetime(sent_at)
    required_text = {
        "source_record_id": source_record_id,
        "symbol": symbol,
        "trade_thesis": trade_thesis,
        "original_chart_filename": original_chart_filename,
    }
    if (
        normalized_source not in POST_SIGNAL_REVIEW_SOURCES
        or normalized_direction is None
        or channel_id is None
        or message_id is None
        or sent_datetime is None
        or any(
            not isinstance(item, str) or not item.strip()
            for item in required_text.values()
        )
    ):
        raise ValueError("Signal review metadata is incomplete or invalid.")

    normalized_sent_at = sent_datetime.isoformat()
    normalized_level = normalized_reference_level(reference_level)
    return {
        "review_id": message_id,
        "source": normalized_source,
        "source_record_id": source_record_id.strip(),
        "signals_channel_id": channel_id,
        "signals_message_id": message_id,
        "symbol": symbol.strip(),
        "trade_direction": normalized_direction,
        "trade_thesis": trade_thesis.strip(),
        "original_chart_filename": Path(original_chart_filename).name,
        **({"reference_level": normalized_level} if normalized_level is not None else {}),
        "sent_at": normalized_sent_at,
        "review_due_at": one_calendar_month_after(normalized_sent_at),
        "review_status": POST_SIGNAL_REVIEW_SCHEDULED,
        "review_cycle": 1,
        "deferral_count": 0,
        "review_history": [],
        "proposed_outcome": "still_active",
        "review_summary": "",
        "comparison_chart_verified": False,
        "created_at": normalized_sent_at,
        "updated_at": normalized_sent_at,
    }


def is_valid_post_signal_review_record(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("source") not in POST_SIGNAL_REVIEW_SOURCES:
        return False
    if normalized_trade_direction(value.get("trade_direction")) is None:
        return False
    if value.get("review_status") not in POST_SIGNAL_REVIEW_STATUSES:
        return False
    if not isinstance(value.get("review_cycle"), int) or value["review_cycle"] < 1:
        return False
    if discord_id_text(value.get("review_id")) is None:
        return False
    if value.get("review_id") != value.get("signals_message_id"):
        return False
    if discord_id_text(value.get("signals_channel_id")) is None:
        return False
    for field in (
        "source_record_id",
        "symbol",
        "trade_thesis",
        "original_chart_filename",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            return False
    for field in ("sent_at", "review_due_at", "created_at", "updated_at"):
        if parse_iso_datetime(value.get(field)) is None:
            return False
    if not isinstance(value.get("deferral_count", 0), int):
        return False
    if value.get("deferral_count", 0) < 0:
        return False
    if not isinstance(value.get("review_history", []), list):
        return False
    if normalized_post_signal_outcome(
        value.get("proposed_outcome", "still_active")
    ) is None:
        return False
    if not isinstance(value.get("review_summary", ""), str):
        return False
    if not isinstance(value.get("comparison_chart_verified", False), bool):
        return False
    if "reference_level" in value and normalized_reference_level(
        value.get("reference_level")
    ) is None:
        return False
    if "current_price" in value and normalized_reference_level(
        value.get("current_price")
    ) is None:
        return False
    for field in (
        "draft_channel_id",
        "draft_message_id",
        "public_channel_id",
        "public_message_id",
    ):
        if field in value and value[field] is not None:
            if discord_id_text(value[field]) is None:
                return False
    return True


def claim_due_post_signal_review(
    review_id: str,
    attempt_id: str,
    now: str,
) -> tuple[dict[str, Any], str]:
    def mutation(state: dict[str, Any]) -> str:
        reviews = state.get("post_signal_reviews")
        if not isinstance(reviews, dict):
            return "invalid"
        record = reviews.get(str(review_id))
        now_datetime = parse_iso_datetime(now)
        if not isinstance(record, dict) or now_datetime is None:
            return "invalid"
        if not is_due_post_signal_review(record, now_datetime):
            return "not_due"
        record["review_status"] = POST_SIGNAL_REVIEW_DRAFTING
        record["review_attempt_id"] = attempt_id
        record["updated_at"] = now_datetime.isoformat()
        record.pop("last_error", None)
        return "claimed"

    return update_state(mutation)


def transition_post_signal_review(
    review_id: str,
    attempt_id: str,
    target_status: str,
    now: str,
    *,
    updates: dict[str, Any] | None = None,
    error: str | None = None,
) -> tuple[dict[str, Any], str]:
    if target_status not in POST_SIGNAL_REVIEW_STATUSES:
        raise ValueError("Invalid post-signal review status.")

    def mutation(state: dict[str, Any]) -> str:
        reviews = state.get("post_signal_reviews")
        record = reviews.get(str(review_id)) if isinstance(reviews, dict) else None
        now_datetime = parse_iso_datetime(now)
        if not isinstance(record, dict) or now_datetime is None:
            return "invalid"
        if target_status == POST_SIGNAL_REVIEW_PUBLISHED:
            public_channel_id = (
                updates.get("public_channel_id") if updates else None
            )
            public_message_id = (
                updates.get("public_message_id") if updates else None
            )
            if (
                discord_id_text(public_channel_id) is None
                or discord_id_text(public_message_id) is None
            ):
                return "invalid"
        if record.get("review_attempt_id") != attempt_id:
            return "stale"
        candidate = copy.deepcopy(record)
        candidate["review_status"] = target_status
        candidate["updated_at"] = now_datetime.isoformat()
        if updates:
            candidate.update(copy.deepcopy(updates))
        if error:
            candidate["last_error"] = str(error)[:240]
        else:
            candidate.pop("last_error", None)
        history = copy.deepcopy(candidate.get("review_history", []))
        history.append(
            {
                "action": target_status,
                "at": now_datetime.isoformat(),
                "review_cycle": candidate["review_cycle"],
                **({"error": str(error)[:240]} if error else {}),
            }
        )
        candidate["review_history"] = history
        if not is_valid_post_signal_review_record(candidate):
            return "invalid"
        reviews[str(review_id)] = candidate
        return "transitioned"

    return update_state(mutation)


def defer_post_signal_review(
    review_id: str,
    expected_draft_message_id: str,
    now: str,
) -> tuple[dict[str, Any], str]:
    def mutation(state: dict[str, Any]) -> str:
        reviews = state.get("post_signal_reviews")
        record = reviews.get(str(review_id)) if isinstance(reviews, dict) else None
        now_datetime = parse_iso_datetime(now)
        if not isinstance(record, dict) or now_datetime is None:
            return "invalid"
        if (
            record.get("review_status") != POST_SIGNAL_REVIEW_DRAFT_READY
            or str(record.get("draft_message_id"))
            != str(expected_draft_message_id)
        ):
            return "unavailable"
        history = copy.deepcopy(record.get("review_history", []))
        history.append(
            {
                "action": "deferred",
                "at": now_datetime.isoformat(),
                "review_cycle": record["review_cycle"],
                "previous_due_at": record["review_due_at"],
            }
        )
        record.update(
            {
                "review_status": POST_SIGNAL_REVIEW_SCHEDULED,
                "review_due_at": one_calendar_month_after(now_datetime.isoformat()),
                "review_cycle": record["review_cycle"] + 1,
                "deferral_count": record.get("deferral_count", 0) + 1,
                "review_history": history,
                "comparison_chart_verified": False,
                "review_summary": "",
                "proposed_outcome": "still_active",
                "updated_at": now_datetime.isoformat(),
            }
        )
        for field in (
            "draft_channel_id",
            "draft_message_id",
            "review_attempt_id",
            "comparison_chart_filename",
            "last_error",
        ):
            record.pop(field, None)
        return "deferred" if is_valid_post_signal_review_record(record) else "invalid"

    return update_state(mutation)


def claim_post_signal_review_action(
    review_id: str,
    draft_message_id: str,
    attempt_id: str,
    target_status: str,
    now: str,
) -> tuple[dict[str, Any], str]:
    if target_status not in {
        POST_SIGNAL_REVIEW_PUBLISHING,
        POST_SIGNAL_REVIEW_DISMISSED,
    }:
        raise ValueError("Invalid post-signal review action.")

    def mutation(state: dict[str, Any]) -> str:
        reviews = state.get("post_signal_reviews")
        record = reviews.get(str(review_id)) if isinstance(reviews, dict) else None
        now_datetime = parse_iso_datetime(now)
        if not isinstance(record, dict) or now_datetime is None:
            return "invalid"
        if (
            record.get("review_status") != POST_SIGNAL_REVIEW_DRAFT_READY
            or str(record.get("draft_message_id")) != str(draft_message_id)
        ):
            return "unavailable"
        if (
            target_status == POST_SIGNAL_REVIEW_PUBLISHING
            and not record.get("comparison_chart_verified")
        ):
            return "verification_required"
        history = copy.deepcopy(record.get("review_history", []))
        history.append(
            {
                "action": (
                    "publish_started"
                    if target_status == POST_SIGNAL_REVIEW_PUBLISHING
                    else "dismissed"
                ),
                "at": now_datetime.isoformat(),
                "review_cycle": record["review_cycle"],
            }
        )
        record["review_status"] = target_status
        record["review_attempt_id"] = attempt_id
        record["review_history"] = history
        record["updated_at"] = now_datetime.isoformat()
        return "claimed"

    return update_state(mutation)


def edit_post_signal_review(
    review_id: str,
    draft_message_id: str,
    now: str,
    *,
    outcome: str,
    summary: str,
    comparison_chart_verified: bool,
) -> tuple[dict[str, Any], str]:
    normalized_outcome = normalized_post_signal_outcome(outcome)
    clean_summary = str(summary or "").strip()
    if normalized_outcome is None or not clean_summary or len(clean_summary) > 1200:
        return load_state(), "invalid"

    def mutation(state: dict[str, Any]) -> str:
        reviews = state.get("post_signal_reviews")
        record = reviews.get(str(review_id)) if isinstance(reviews, dict) else None
        now_datetime = parse_iso_datetime(now)
        if not isinstance(record, dict) or now_datetime is None:
            return "invalid"
        if (
            record.get("review_status") != POST_SIGNAL_REVIEW_DRAFT_READY
            or str(record.get("draft_message_id")) != str(draft_message_id)
        ):
            return "unavailable"
        record["proposed_outcome"] = normalized_outcome
        record["review_summary"] = clean_summary
        record["comparison_chart_verified"] = bool(comparison_chart_verified)
        history = copy.deepcopy(record.get("review_history", []))
        history.append(
            {
                "action": "edited",
                "at": now_datetime.isoformat(),
                "review_cycle": record["review_cycle"],
                "outcome": normalized_outcome,
                "comparison_chart_verified": bool(comparison_chart_verified),
            }
        )
        record["review_history"] = history
        record["updated_at"] = now_datetime.isoformat()
        return "updated" if is_valid_post_signal_review_record(record) else "invalid"

    return update_state(mutation)


def find_post_signal_review_by_draft(
    state: dict[str, Any],
    draft_channel_id: Any,
    draft_message_id: Any,
) -> tuple[str, dict[str, Any]] | None:
    channel_id = discord_id_text(draft_channel_id)
    message_id = discord_id_text(draft_message_id)
    reviews = state.get("post_signal_reviews")
    if channel_id is None or message_id is None or not isinstance(reviews, dict):
        return None
    matches = [
        (str(review_id), record)
        for review_id, record in reviews.items()
        if isinstance(record, dict)
        and record.get("review_status") == POST_SIGNAL_REVIEW_DRAFT_READY
        and str(record.get("draft_channel_id")) == channel_id
        and str(record.get("draft_message_id")) == message_id
        and is_valid_post_signal_review_record(record)
    ]
    return matches[0] if len(matches) == 1 else None


def build_post_signal_review_message(
    record: dict[str, Any],
    now: datetime,
    *,
    private: bool,
) -> str:
    if not is_valid_post_signal_review_record(record):
        raise ValueError("Post-signal review record is invalid.")
    outcome = normalized_post_signal_outcome(
        record.get("proposed_outcome", "still_active")
    )
    if outcome is None:
        raise ValueError("Post-signal review outcome is invalid.")
    sent_at = parse_iso_datetime(record["sent_at"])
    if sent_at is None or now.tzinfo is None:
        raise ValueError("Post-signal review dates are invalid.")
    direction = normalized_trade_direction(record["trade_direction"])
    direction_label = "Long" if direction == TRADE_DIRECTION_LONG else "Short"
    elapsed_days = post_signal_review_elapsed_days(record["sent_at"], now)
    summary = str(record.get("review_summary") or "").strip()
    if not summary:
        summary = (
            "Pending staff review of the updated chart and original marked levels."
            if private
            else "The updated chart was reviewed against the original setup."
        )
    verification = (
        "✅ Updated chart verified by staff."
        if record.get("comparison_chart_verified")
        else "⚠️ Updated chart requires staff verification before publishing."
    )
    reference_level = normalized_reference_level(record.get("reference_level"))
    current_price = normalized_reference_level(record.get("current_price"))
    performance = direction_adjusted_performance(
        reference_level,
        current_price,
        direction,
    )
    if performance is None:
        performance_lines = [
            "## 📈 Performance Since Signal",
            "",
            "Performance is unavailable because this older signal did not store a reference level.",
        ]
    else:
        result_word = "Gain" if performance >= 0 else "Loss"
        performance_lines = [
            "## 📈 Performance Since Signal",
            "",
            f"🎯 **Original level:** ${reference_level:,.4f}",
            f"💵 **Current price:** ${current_price:,.4f}",
            f"{'🟢' if performance >= 0 else '🔴'} **{result_word}:** {performance:+.2f}%",
        ]
    lines = [
        "# 📊 Signal Review",
        "",
        f"## {record['symbol']} — {direction_label}",
        "",
        f"📅 **Original signal:** {sent_at.strftime('%B %d, %Y')}",
        f"🔎 **Review date:** {now.strftime('%B %d, %Y')}",
        f"⏳ **Time elapsed:** {elapsed_days} days",
        f"📌 **Status:** {POST_SIGNAL_OUTCOMES[outcome]}",
        "",
        *performance_lines,
        "",
        "## 🧠 Original Thesis",
        "",
        record["trade_thesis"],
        "",
        "## 📝 Review Summary",
        "",
        summary,
        "",
        verification,
        "",
        "## 🖼️ Before & After",
        "",
        "The complete original Signals chart appears first, followed by the updated weekly chart.",
        "",
        "*Market movement is informational and is not a claim of realized profit.*",
        "",
        "⚠️ **Manage risk. This is not financial advice.**",
    ]
    return "\n".join(lines)


def build_post_signal_chart_embed(
    discord_module: Any,
    title: str,
    image_url: str,
) -> Any:
    embed = discord_module.Embed(
        title=title,
        color=BRAND_NEON_PINK,
    )
    embed.set_image(url=image_url)
    return embed


def store_post_signal_review(
    state: dict[str, Any],
    review_record: Any,
) -> bool:
    if not is_valid_post_signal_review_record(review_record):
        return False
    reviews = state.get("post_signal_reviews")
    if reviews is None and "post_signal_reviews" not in state:
        reviews = {}
        state["post_signal_reviews"] = reviews
    if not isinstance(reviews, dict):
        return False
    review_id = review_record["review_id"]
    existing = reviews.get(review_id)
    if existing is not None and existing != review_record:
        return False
    reviews[review_id] = copy.deepcopy(review_record)
    return True


def build_bordered_discord_embed(
    discord_module: Any,
    description: str,
    attachment_name: str,
) -> Any:
    """Build the branded Signals card used by drafts and publications."""
    embed_data = bordered_embed(
        description,
        color=BRAND_NEON_PINK,
        image_url=f"attachment://{attachment_name}",
    )
    return discord_module.Embed.from_dict(embed_data)


def safe_manual_signal_log_value(value: Any, max_length: int = 80) -> str:
    """Compact user-controlled log labels without exposing message content."""
    text = " ".join(str(value or "Unknown").split())
    text = text.replace("@", "@\u200b")
    if not text:
        text = "Unknown"
    return text[:max_length]


def is_valid_manual_signal_fields(
    instrument: Any,
    trade_thesis: Any,
    timeframe: Any = "",
    setup_name: Any = "",
) -> bool:
    if not isinstance(instrument, str) or not instrument.strip():
        return False
    if not isinstance(trade_thesis, str) or not trade_thesis.strip():
        return False
    if not isinstance(timeframe, str) or not isinstance(setup_name, str):
        return False
    content = build_manual_signal_message(
        instrument,
        trade_thesis,
        timeframe=timeframe,
        setup_name=setup_name,
    )
    return len(content) <= MANUAL_SIGNAL_MAX_CONTENT_LENGTH


def is_valid_manual_chart_attachment(attachment: Any) -> bool:
    filename = getattr(attachment, "filename", None)
    content_type = getattr(attachment, "content_type", None)
    if not isinstance(filename, str) or not filename.strip():
        return False
    suffix = Path(filename).suffix.lower()
    expected_type = MANUAL_SIGNAL_IMAGE_TYPES.get(suffix)
    if expected_type is None:
        return False
    return content_type in {None, "", expected_type}


def manual_chart_metadata(attachment: Any) -> dict[str, str]:
    return {
        "filename": str(getattr(attachment, "filename", "")),
        "content_type": str(getattr(attachment, "content_type", "") or ""),
        "attachment_id": str(getattr(attachment, "id", "") or ""),
    }


def is_valid_manual_chart_metadata(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    filename = value.get("filename")
    content_type = value.get("content_type")
    if not isinstance(filename, str) or not filename.strip():
        return False
    if not isinstance(content_type, str):
        return False
    suffix = Path(filename).suffix.lower()
    expected_type = MANUAL_SIGNAL_IMAGE_TYPES.get(suffix)
    if expected_type is None or content_type not in {"", expected_type}:
        return False
    attachment_id = value.get("attachment_id", "")
    return isinstance(attachment_id, str)


def manual_chart_matches_record(attachment: Any, metadata: Any) -> bool:
    if (
        not is_valid_manual_chart_attachment(attachment)
        or not is_valid_manual_chart_metadata(metadata)
    ):
        return False
    return (
        Path(attachment.filename).name == Path(metadata["filename"]).name
        and str(getattr(attachment, "content_type", "") or "")
        == metadata["content_type"]
    )


def manual_chart_embed_url(message: Any, metadata: Any) -> str | None:
    if not is_valid_manual_chart_metadata(metadata):
        return None
    expected_filename = Path(metadata["filename"]).name
    for embed in list(getattr(message, "embeds", []) or []):
        image = getattr(embed, "image", None)
        url = getattr(image, "url", None)
        if not isinstance(url, str) or not url:
            continue
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in MANUAL_SIGNAL_DISCORD_CDN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not parsed.path.startswith("/attachments/")
            or Path(urllib.parse.unquote(parsed.path)).name
            != expected_filename
        ):
            continue
        return url
    return None


def download_manual_chart_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "MainLineTrades/1.0 "
                "(+https://github.com/jayfunkdown/main-line-trades-replays)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_length = response.headers.get("Content-Length")
        if (
            isinstance(content_length, str)
            and content_length.isdigit()
            and int(content_length) > MANUAL_SIGNAL_MAX_CHART_BYTES
        ):
            raise ValueError("Manual signal chart is too large")
        data = response.read(MANUAL_SIGNAL_MAX_CHART_BYTES + 1)
    if not data or len(data) > MANUAL_SIGNAL_MAX_CHART_BYTES:
        raise ValueError("Manual signal chart is empty or too large")
    return data


def manual_signal_delivery_status(record: Any) -> str | None:
    """Validate a complete manual draft and return its delivery state."""
    if not isinstance(record, dict):
        return None
    required_ids = (
        "draft_message_id",
        "draft_channel_id",
        "creator_user_id",
    )
    if any(discord_id_text(record.get(field)) is None for field in required_ids):
        return None
    if not isinstance(record.get("draft_id"), str) or not record["draft_id"]:
        return None
    if not is_valid_manual_signal_fields(
        record.get("instrument"),
        record.get("trade_thesis"),
        record.get("timeframe"),
        record.get("setup_name"),
    ):
        return None
    if not is_valid_manual_chart_metadata(record.get("chart")):
        return None
    direction = record.get("trade_direction")
    if direction is not None and normalized_trade_direction(direction) is None:
        return None
    if "reference_level" in record and normalized_reference_level(
        record.get("reference_level")
    ) is None:
        return None
    if not isinstance(record.get("canceled"), bool):
        return None
    for field in ("created_at", "updated_at"):
        if not isinstance(record.get(field), str) or parse_iso_datetime(
            record[field]
        ) is None:
            return None
    status = record.get("delivery_status")
    if status not in MANUAL_SIGNAL_STATUSES:
        return None
    if status == MANUAL_SIGNAL_SENT and discord_id_text(
        record.get("signals_message_id")
    ) is None:
        return None
    return status


def validated_manual_signal_draft(
    state: dict[str, Any],
    draft_id: Any,
    message_id: Any,
    channel_id: Any,
    configured_channel_id: Any,
) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not isinstance(draft_id, str):
        return None
    drafts = state.get("manual_signal_drafts")
    if not isinstance(drafts, dict):
        return None
    record = drafts.get(draft_id)
    if manual_signal_delivery_status(record) is None:
        return None
    expected_message_id = discord_id_text(message_id)
    expected_channel_id = discord_id_text(channel_id)
    if (
        expected_message_id is None
        or expected_channel_id is None
        or expected_channel_id != discord_id_text(configured_channel_id)
        or record["draft_message_id"] != expected_message_id
        or record["draft_channel_id"] != expected_channel_id
        or record["draft_id"] != draft_id
    ):
        return None
    return record


def find_manual_signal_draft_by_message(
    state: dict[str, Any],
    message_id: Any,
) -> tuple[str, dict[str, Any]] | None:
    expected = discord_id_text(message_id)
    drafts = state.get("manual_signal_drafts") if isinstance(state, dict) else None
    if expected is None or not isinstance(drafts, dict):
        return None
    matches = [
        (draft_id, record)
        for draft_id, record in drafts.items()
        if isinstance(draft_id, str)
        and isinstance(record, dict)
        and record.get("draft_message_id") == expected
    ]
    return matches[0] if len(matches) == 1 else None


def claim_manual_signal_delivery(
    draft_id: str,
    message_id: str,
    channel_id: str,
    attempt_id: str,
    started_at: str,
) -> tuple[dict[str, Any], str]:
    outcome = "missing"

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        record = validated_manual_signal_draft(
            state,
            draft_id,
            message_id,
            channel_id,
            channel_id,
        )
        if record is None:
            outcome = "invalid"
            return
        status = manual_signal_delivery_status(record)
        if record["canceled"]:
            outcome = "canceled"
            return
        if status != MANUAL_SIGNAL_READY:
            outcome = str(status or "invalid")
            return
        if normalized_trade_direction(record.get("trade_direction")) is None:
            outcome = "direction_required"
            return
        record["delivery_status"] = MANUAL_SIGNAL_SENDING
        record["delivery_attempt_id"] = attempt_id
        record["delivery_started_at"] = started_at
        record["updated_at"] = started_at
        record.pop("delivery_error", None)
        record.pop("delivery_finished_at", None)
        record.pop("signals_message_id", None)
        outcome = "claimed"

    return update_state(mutation), outcome


def transition_manual_signal_delivery(
    draft_id: str,
    attempt_id: str,
    status: str,
    finished_at: str,
    *,
    error: str | None = None,
    signals_message_id: str | None = None,
    post_signal_review: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    if status not in {
        MANUAL_SIGNAL_READY,
        MANUAL_SIGNAL_SENT,
        MANUAL_SIGNAL_UNKNOWN,
    }:
        raise ValueError(f"Unsupported manual delivery transition: {status}")
    outcome = "missing"
    stored_review = copy.deepcopy(post_signal_review)

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        record = state["manual_signal_drafts"].get(draft_id)
        if not isinstance(record, dict):
            return
        if (
            manual_signal_delivery_status(record) != MANUAL_SIGNAL_SENDING
            or record.get("delivery_attempt_id") != attempt_id
        ):
            outcome = "mismatch"
            return
        if status == MANUAL_SIGNAL_SENT and discord_id_text(
            signals_message_id
        ) is None:
            outcome = "invalid"
            return
        if (
            status == MANUAL_SIGNAL_SENT
            and stored_review is not None
            and (
                stored_review.get("source")
                != POST_SIGNAL_REVIEW_SOURCE_MANUAL
                or stored_review.get("source_record_id") != draft_id
                or stored_review.get("signals_message_id")
                != discord_id_text(signals_message_id)
                or not store_post_signal_review(state, stored_review)
            )
        ):
            outcome = "invalid"
            return
        record["delivery_status"] = status
        record["delivery_finished_at"] = finished_at
        record["updated_at"] = finished_at
        if error is None:
            record.pop("delivery_error", None)
        else:
            record["delivery_error"] = error
        if status == MANUAL_SIGNAL_SENT:
            record["signals_message_id"] = str(signals_message_id)
        outcome = "transitioned"

    return update_state(mutation), outcome


def update_manual_signal_draft(
    draft_id: str,
    message_id: str,
    channel_id: str,
    *,
    instrument: str,
    trade_thesis: str,
    trade_direction: str,
    timeframe: str,
    setup_name: str,
    reference_level: Any,
    chart: dict[str, str] | None,
    updated_at: str,
) -> tuple[dict[str, Any], str]:
    outcome = "missing"

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        record = validated_manual_signal_draft(
            state,
            draft_id,
            message_id,
            channel_id,
            channel_id,
        )
        if record is None:
            outcome = "invalid"
            return
        if record["canceled"] or manual_signal_delivery_status(
            record
        ) != MANUAL_SIGNAL_READY:
            outcome = "unavailable"
            return
        normalized_direction = normalized_trade_direction(trade_direction)
        normalized_level = normalized_reference_level(reference_level)
        if not is_valid_manual_signal_fields(
            instrument,
            trade_thesis,
            timeframe,
            setup_name,
        ) or normalized_direction is None or normalized_level is None:
            outcome = "invalid"
            return
        if chart is not None and not is_valid_manual_chart_metadata(chart):
            outcome = "invalid"
            return
        record.update(
            {
                "instrument": instrument.strip(),
                "trade_thesis": trade_thesis.strip(),
                "trade_direction": normalized_direction,
                "timeframe": timeframe.strip(),
                "setup_name": setup_name.strip(),
                "reference_level": normalized_level,
                "updated_at": updated_at,
            }
        )
        if chart is not None:
            record["chart"] = copy.deepcopy(chart)
        outcome = "updated"

    return update_state(mutation), outcome


def set_manual_signal_direction(
    draft_id: str,
    message_id: str,
    channel_id: str,
    direction: str,
    updated_at: str,
) -> tuple[dict[str, Any], str]:
    normalized = normalized_trade_direction(direction)
    if normalized is None:
        return {}, "invalid"
    outcome = "missing"

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        record = validated_manual_signal_draft(
            state,
            draft_id,
            message_id,
            channel_id,
            channel_id,
        )
        if record is None:
            outcome = "invalid"
            return
        if record["canceled"] or manual_signal_delivery_status(
            record
        ) != MANUAL_SIGNAL_READY:
            outcome = "unavailable"
            return
        record["trade_direction"] = normalized
        record["updated_at"] = updated_at
        outcome = "updated"

    return update_state(mutation), outcome


def cancel_manual_signal_draft(
    draft_id: str,
    message_id: str,
    channel_id: str,
    canceled_at: str,
) -> tuple[dict[str, Any], str]:
    outcome = "missing"

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        record = validated_manual_signal_draft(
            state,
            draft_id,
            message_id,
            channel_id,
            channel_id,
        )
        if record is None:
            outcome = "invalid"
            return
        if record["canceled"] or manual_signal_delivery_status(
            record
        ) != MANUAL_SIGNAL_READY:
            outcome = "unavailable"
            return
        record["canceled"] = True
        record["canceled_at"] = canceled_at
        record["updated_at"] = canceled_at
        outcome = "canceled"

    return update_state(mutation), outcome


def find_signal_item_by_review_message(
    state: dict[str, Any],
    message_id: str,
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(state, dict):
        return None

    signal_queue = state.get("signal_queue")
    if not isinstance(signal_queue, dict):
        return None

    for token, item in signal_queue.items():
        if not isinstance(item, dict):
            continue

        if str(item.get("review_message_id") or "") == str(message_id):
            return token, item

    return None


def discord_id_text(value: Any) -> str | None:
    """Normalize a Discord snowflake while rejecting missing/bad values."""
    if isinstance(value, bool):
        return None

    text = str(value or "")
    if not text.isdecimal() or int(text) <= 0:
        return None

    return text


def is_valid_signal_candidate(candidate: Any) -> bool:
    """Validate every candidate field consumed by Signals delivery."""
    if not isinstance(candidate, dict):
        return False

    symbol = candidate.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return False

    for field in ("eps_direction", "revenue_direction"):
        if not isinstance(candidate.get(field), str):
            return False

    for field in (
        "move_percent",
        "current_price",
        "eps_surprise",
        "revenue_surprise",
    ):
        value = candidate.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, Real)
        ):
            return False

    return True


def signal_delivery_status(item: Any) -> str | None:
    """Resolve additive delivery status, including legacy queue records."""
    if not isinstance(item, dict):
        return None

    sent_to_signals = item.get("sent_to_signals")
    if not isinstance(sent_to_signals, bool):
        return None

    status = item.get("delivery_status")
    if status is None:
        return (
            SIGNAL_DELIVERY_SENT
            if sent_to_signals
            else SIGNAL_DELIVERY_READY
        )

    if status not in SIGNAL_DELIVERY_STATUSES:
        return None

    if sent_to_signals != (status == SIGNAL_DELIVERY_SENT):
        return None

    return status


def signal_delivery_rejection_message(status: str | None) -> str:
    # Sending and unknown are deliberately fail-closed. Staff must reconcile
    # them against Discord before changing state or allowing another attempt.
    return {
        "direction_required": (
            "Select Long or Short before sending this signal."
        ),
        SIGNAL_DELIVERY_SENDING: (
            "This setup is already being sent to Signals."
        ),
        SIGNAL_DELIVERY_SENT: (
            "This setup was already sent to Signals."
        ),
        SIGNAL_DELIVERY_UNKNOWN: (
            "This setup needs staff reconciliation before retrying."
        ),
    }.get(
        status,
        "This earnings review is no longer available.",
    )


def claim_signal_delivery(
    token: str,
    review_message_id: str,
    review_channel_id: str,
    attempt_id: str,
    started_at: str,
    trade_direction: str | None = None,
    reference_level: Any = None,
) -> tuple[dict[str, Any], str]:
    """Atomically move one ready queue record to sending."""
    outcome = "missing"

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        found = validated_review_state_item(
            state,
            review_message_id,
            review_channel_id,
            review_channel_id,
        )
        if found is None or found[0] != token:
            outcome = "invalid"
            return

        _found_token, item = found
        status = signal_delivery_status(item)

        if status is None:
            outcome = "invalid"
            return

        if status != SIGNAL_DELIVERY_READY:
            outcome = status
            return
        direction = normalized_trade_direction(
            trade_direction
        ) or normalized_trade_direction(item.get("trade_direction"))
        if direction is None:
            outcome = "direction_required"
            return
        level = normalized_reference_level(reference_level)
        if level is None:
            outcome = "reference_level_required"
            return

        item["trade_direction"] = direction
        item["reference_level"] = level
        item["delivery_status"] = SIGNAL_DELIVERY_SENDING
        item["delivery_attempt_id"] = attempt_id
        item["delivery_started_at"] = started_at
        item["sent_to_signals"] = False
        item.pop("delivery_error", None)
        item.pop("delivery_finished_at", None)
        item.pop("signals_message_id", None)
        outcome = "claimed"

    state = update_state(mutation)
    return state, outcome


def transition_signal_delivery(
    token: str,
    attempt_id: str,
    status: str,
    finished_at: str,
    *,
    error: str | None = None,
    updates: dict[str, Any] | None = None,
    post_signal_review: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Atomically finish the matching sending attempt."""
    if status not in {
        SIGNAL_DELIVERY_READY,
        SIGNAL_DELIVERY_SENT,
        SIGNAL_DELIVERY_UNKNOWN,
    }:
        raise ValueError(f"Unsupported signal delivery transition: {status}")

    outcome = "missing"
    stored_updates = copy.deepcopy(updates or {})
    stored_review = copy.deepcopy(post_signal_review)

    def mutation(state: dict[str, Any]) -> None:
        nonlocal outcome
        item = state["signal_queue"].get(token)

        if not isinstance(item, dict):
            outcome = "missing"
            return

        if (
            signal_delivery_status(item) != SIGNAL_DELIVERY_SENDING
            or str(item.get("delivery_attempt_id") or "") != attempt_id
        ):
            outcome = "mismatch"
            return

        if (
            status == SIGNAL_DELIVERY_SENT
            and stored_review is not None
            and (
                stored_review.get("source")
                != POST_SIGNAL_REVIEW_SOURCE_EARNINGS
                or stored_review.get("source_record_id") != token
                or stored_review.get("signals_message_id")
                != discord_id_text(stored_updates.get("signals_message_id"))
                or not store_post_signal_review(state, stored_review)
            )
        ):
            outcome = "invalid"
            return

        item["delivery_status"] = status
        item["delivery_finished_at"] = finished_at
        item["sent_to_signals"] = status == SIGNAL_DELIVERY_SENT

        if error is None:
            item.pop("delivery_error", None)
        else:
            item["delivery_error"] = error

        item.update(stored_updates)
        outcome = "transitioned"

    state = update_state(mutation)
    return state, outcome


class ReviewMessageAsyncLocks:
    """Serialize same-process submissions while allowing different reviews."""

    def __init__(self) -> None:
        self._entries: dict[str, list[Any]] = {}

    @property
    def active_key_count(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def hold(self, message_id: str) -> AsyncIterator[None]:
        key = str(message_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._entries[key] = entry

        entry[1] += 1
        lock = entry[0]
        acquired = False

        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()

            entry[1] -= 1
            if entry[1] == 0 and self._entries.get(key) is entry:
                self._entries.pop(key, None)


def can_clear_earnings_review(
    user: Any,
    guild: Any,
) -> bool:
    """Authorize the guild owner or a member with moderation rights."""
    if user is None or guild is None:
        return False

    user_id = discord_id_text(getattr(user, "id", None))
    owner_id = getattr(guild, "owner_id", None)

    if user_id is None:
        return False

    if discord_id_text(owner_id) == user_id:
        return True

    permissions = getattr(user, "guild_permissions", None)

    if permissions is None:
        return False

    return (
        getattr(permissions, "administrator", False) is True
        or getattr(permissions, "manage_messages", False) is True
    )


def validated_review_state_item(
    state: dict[str, Any],
    message_id: Any,
    channel_id: Any,
    configured_channel_id: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Return a complete state record matching the Discord provenance."""
    expected_message_id = discord_id_text(message_id)
    expected_channel_id = discord_id_text(channel_id)

    if expected_message_id is None or expected_channel_id is None:
        return None

    if expected_channel_id != discord_id_text(configured_channel_id):
        return None

    found = find_signal_item_by_review_message(
        state,
        expected_message_id,
    )
    if found is None:
        return None

    token, item = found
    candidate = item.get("candidate")

    if not str(token):
        return None

    if str(item.get("review_message_id") or "") != expected_message_id:
        return None

    if str(item.get("review_channel_id") or "") != expected_channel_id:
        return None

    if not is_valid_signal_candidate(candidate):
        return None

    if not isinstance(item.get("sent_to_signals"), bool):
        return None

    if signal_delivery_status(item) is None:
        return None

    return token, item


def is_valid_review_message_provenance(
    message: Any,
    *,
    message_id: Any,
    channel_id: Any,
    bot_user_id: Any,
    normal_message_type: Any,
) -> bool:
    """Validate the original review message against Discord metadata."""
    expected_message_id = discord_id_text(message_id)
    expected_channel_id = discord_id_text(channel_id)
    expected_bot_id = discord_id_text(bot_user_id)

    if (
        message is None
        or expected_message_id is None
        or expected_channel_id is None
        or expected_bot_id is None
    ):
        return False

    author = getattr(message, "author", None)
    channel = getattr(message, "channel", None)

    return bool(
        discord_id_text(getattr(message, "id", None))
        == expected_message_id
        and author is not None
        and discord_id_text(getattr(author, "id", None))
        == expected_bot_id
        and channel is not None
        and discord_id_text(getattr(channel, "id", None))
        == expected_channel_id
        and getattr(message, "type", None) == normal_message_type
    )


def is_configured_review_channel(
    channel_id: Any,
    review_channel_id: int,
) -> bool:
    return str(channel_id or "") == str(review_channel_id)


def is_safe_review_message(
    message: Any,
    bot_user_id: int,
    normal_message_type: Any,
) -> bool:
    """Apply Discord-message safeguards after state provenance checks."""
    author = getattr(message, "author", None)

    return bool(
        author is not None
        and str(getattr(author, "id", "")) == str(bot_user_id)
        and not getattr(message, "pinned", False)
        and getattr(message, "type", None) == normal_message_type
    )


async def collect_safe_review_messages(
    channel: Any,
    bot_user_id: int,
    normal_message_type: Any,
) -> tuple[list[Any], int]:
    """Collect bot-authored, normal, unpinned messages in the channel."""
    candidates: list[Any] = []
    skipped = 0

    async for message in channel.history(limit=None):
        if is_safe_review_message(
            message,
            bot_user_id,
            normal_message_type,
        ):
            candidates.append(message)
        else:
            skipped += 1

    return candidates, skipped


def partition_review_messages_for_deletion(
    messages: list[Any],
    now_utc: datetime,
) -> tuple[list[Any], list[Any]]:
    """Separate safely bulk-deletable messages from older messages."""
    bulk_cutoff = now_utc - DISCORD_BULK_DELETE_SAFE_AGE
    recent: list[Any] = []
    individual: list[Any] = []

    for message in messages:
        created_at = getattr(message, "created_at", None)

        if created_at is not None and created_at > bulk_cutoff:
            recent.append(message)
        else:
            individual.append(message)

    return recent, individual


async def delete_review_messages_safely(
    channel: Any,
    messages: list[Any],
    *,
    now_utc: datetime,
    http_error_types: tuple[type[BaseException], ...],
    not_found_type: type[BaseException],
) -> dict[str, int]:
    """Bulk-delete recent messages and individually delete older ones."""
    recent, individual = partition_review_messages_for_deletion(
        messages,
        now_utc,
    )

    deleted = 0
    missing = 0
    failed = 0

    for start in range(0, len(recent), DISCORD_BULK_DELETE_LIMIT):
        batch = recent[
            start:start + DISCORD_BULK_DELETE_LIMIT
        ]

        if len(batch) < 2:
            individual.extend(batch)
            continue

        try:
            await channel.delete_messages(batch)
        except http_error_types:
            individual.extend(batch)
        else:
            deleted += len(batch)

    for message in individual:
        try:
            await message.delete()
        except not_found_type:
            missing += 1
        except http_error_types:
            failed += 1
        else:
            deleted += 1

    return {
        "deleted": deleted,
        "missing": missing,
        "failed": failed,
    }


def pending_signal_for_user(
    state: dict[str, Any],
    user_id: str,
    channel_id: str,
) -> tuple[str, dict[str, Any]] | None:
    matches: list[tuple[str, dict[str, Any]]] = []

    for token, item in state.setdefault("signal_queue", {}).items():
        if not isinstance(item, dict):
            continue

        if item.get("sent_to_signals"):
            continue

        if not item.get("awaiting_chart"):
            continue

        if str(item.get("pending_user_id") or "") != str(user_id):
            continue

        if str(item.get("review_channel_id") or "") != str(channel_id):
            continue

        if not str(item.get("trade_thesis") or "").strip():
            continue

        matches.append((token, item))

    if not matches:
        return None

    matches.sort(
        key=lambda pair: str(
            pair[1].get("thesis_submitted_at") or ""
        ),
        reverse=True,
    )

    return matches[0]


async def run_review_button_bot() -> None:
    """
    Persistent Earnings Review approval workflow.

    Click review button -> enter Trade Thesis -> paste TradingView chart ->
    bot posts the final member-facing signal with the user's chart.
    """
    try:
        import discord
    except ImportError as exc:
        raise RuntimeError(
            "The review workflow requires discord.py. "
            "Install it with: python -m pip install discord.py"
        ) from exc

    # Validate duplicate-protection and interaction state before resolving
    # Discord configuration or starting any network activity.
    load_state()

    bot_token = required_env("DISCORD_BOT_TOKEN")
    signals_channel_id = int(required_env("SIGNALS_CHANNEL_ID"))

    review_channel_id = int(
        resolve_webhook_channel_id(
            required_env("EARNINGS_REVIEW_WEBHOOK")
        )
    )
    raw_drafts_channel_id = os.getenv(
        "SIGNAL_DRAFTS_CHANNEL_ID",
        "",
    ).strip()
    try:
        drafts_channel_id = (
            int(raw_drafts_channel_id)
            if raw_drafts_channel_id
            else None
        )
    except ValueError as exc:
        raise RuntimeError(
            "SIGNAL_DRAFTS_CHANNEL_ID must be a Discord channel ID."
        ) from exc

    def optional_channel_id(name: str) -> int | None:
        raw_value = os.getenv(name, "").strip()
        if not raw_value:
            return None
        try:
            return int(raw_value)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a Discord channel ID.") from exc

    signal_review_drafts_channel_id = optional_channel_id(
        "SIGNAL_REVIEW_DRAFTS_CHANNEL_ID"
    )
    signal_reviews_channel_id = optional_channel_id(
        "SIGNAL_REVIEWS_CHANNEL_ID"
    )

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    command_tree = discord.app_commands.CommandTree(client)
    command_sync_attempted = False
    post_signal_review_task_started = False

    async def get_bot_log_channel():
        """
        Resolve the bot-log channel.

        BOT_LOG_CHANNEL_ID in .env is preferred if present.
        Otherwise, search accessible guild text channels for a name
        containing "bot-log".
        """
        configured_id = os.getenv(
            "BOT_LOG_CHANNEL_ID",
            "",
        ).strip()

        if configured_id:
            try:
                channel_id = int(
                    configured_id
                )
            except ValueError:
                print(
                    "BOT_LOG_CHANNEL_ID is not a valid integer.",
                    flush=True,
                )
            else:
                channel = client.get_channel(
                    channel_id
                )

                if channel is None:
                    try:
                        channel = await client.fetch_channel(
                            channel_id
                        )
                    except Exception:
                        channel = None

                if channel is not None:
                    return channel

        for guild in client.guilds:
            for channel in guild.text_channels:
                if "bot-log" in channel.name.lower():
                    return channel

        return None

    async def write_bot_log(
        message: str,
    ) -> None:
        channel = await get_bot_log_channel()

        if channel is None:
            print(
                "BOT LOG:",
                message,
                flush=True,
            )
            return

        try:
            await channel.send(
                message,
                allowed_mentions=(
                    discord.AllowedMentions.none()
                ),
            )
        except Exception as exc:
            print(
                "Could not write to bot-log:",
                repr(exc),
                flush=True,
            )

    async def write_manual_signal_log(
        instrument: Any,
        user: Any,
        outcome: str,
    ) -> None:
        """Write a concise best-effort lifecycle event without draft content."""
        instrument_label = discord.utils.escape_markdown(
            safe_manual_signal_log_value(instrument)
        )
        staff_label = discord.utils.escape_markdown(
            safe_manual_signal_log_value(user)
        )
        try:
            await write_bot_log(
                f"Manual Signal **{instrument_label}** {outcome} "
                f"by **{staff_label}**."
            )
        except Exception as exc:
            print(
                "Could not write Manual Signal lifecycle log:",
                repr(exc),
                flush=True,
            )

    def interaction_response_is_done(
        interaction: Any,
    ) -> bool:
        response = getattr(interaction, "response", None)
        is_done = getattr(response, "is_done", None)

        if not callable(is_done):
            return False

        try:
            return bool(is_done())
        except Exception:
            return True

    async def send_ephemeral_rejection(
        interaction: Any,
        message: str,
    ) -> None:
        response = getattr(interaction, "response", None)
        response_sender = getattr(response, "send_message", None)

        if (
            callable(response_sender)
            and not interaction_response_is_done(interaction)
        ):
            try:
                await response_sender(
                    message,
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                pass
            else:
                return

        followup = getattr(interaction, "followup", None)
        followup_sender = getattr(followup, "send", None)

        if callable(followup_sender):
            await followup_sender(
                message,
                ephemeral=True,
            )

    async def defer_ephemeral_response(
        interaction: Any,
    ) -> None:
        if interaction_response_is_done(interaction):
            return

        response = getattr(interaction, "response", None)
        defer = getattr(response, "defer", None)

        if not callable(defer):
            return

        try:
            await defer(
                ephemeral=True,
                thinking=True,
            )
        except discord.InteractionResponded:
            pass

    class PostSignalReviewEditModal(
        discord.ui.Modal,
        title="Edit Signal Review",
    ):
        outcome = discord.ui.TextInput(
            label="Outcome",
            placeholder="still_active, worked, invalidated, or no_clear_follow_through",
            required=True,
            max_length=40,
        )
        summary = discord.ui.TextInput(
            label="Review summary",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1200,
        )
        chart_verified = discord.ui.TextInput(
            label="Updated chart verified?",
            placeholder="Type YES only after checking levels and line anchors",
            required=True,
            max_length=3,
        )
        comparison_chart = discord.ui.FileUpload(
            custom_id="post_signal_review_comparison_chart",
            required=False,
            min_values=0,
            max_values=1,
        )

        def __init__(self, review_id: str, draft_message_id: str, record: dict[str, Any]):
            super().__init__()
            self.review_id = review_id
            self.draft_message_id = draft_message_id
            self.symbol = str(record.get("symbol") or "Signal")
            self.outcome.default = str(record.get("proposed_outcome") or "still_active")
            self.summary.default = str(record.get("review_summary") or "")
            self.chart_verified.default = (
                "YES" if record.get("comparison_chart_verified") else "NO"
            )
            self.add_item(
                discord.ui.Label(
                    text="Corrected comparison chart (optional)",
                    description=(
                        "Upload PNG, JPG, JPEG, or WEBP; blank keeps the current chart."
                    ),
                    component=self.comparison_chart,
                )
            )

        async def on_submit(self, interaction: Any) -> None:
            if not can_clear_earnings_review(interaction.user, interaction.guild):
                await send_ephemeral_rejection(interaction, "You cannot edit this review.")
                return
            uploads = list(self.comparison_chart.values)
            if uploads and (
                len(uploads) != 1
                or not is_valid_manual_chart_attachment(uploads[0])
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "Upload one PNG, JPG, JPEG, or WEBP comparison chart.",
                )
                return
            replacement_file = None
            replacement_filename = None
            requested_chart_verification = (
                str(self.chart_verified).strip().upper() == "YES"
            )
            if uploads:
                suffix = Path(uploads[0].filename).suffix.lower()
                replacement_filename = f"{self.symbol}_review{suffix}"
                try:
                    replacement_file = await uploads[0].to_file(
                        filename=replacement_filename
                    )
                except Exception:
                    await send_ephemeral_rejection(
                        interaction,
                        "The corrected chart could not be prepared; no review changes were saved.",
                    )
                    return
            try:
                latest, outcome = edit_post_signal_review(
                    self.review_id,
                    self.draft_message_id,
                    datetime.now(EASTERN).isoformat(),
                    outcome=str(self.outcome),
                    summary=str(self.summary),
                    # A replacement chart is not publishable until Discord has
                    # accepted that exact attachment below.
                    comparison_chart_verified=(
                        requested_chart_verification
                        and replacement_file is None
                    ),
                )
            except EarningsStateError:
                outcome = "invalid"
                latest = {}
            if outcome != "updated":
                await send_ephemeral_rejection(interaction, "This review could not be updated.")
                return
            record = latest["post_signal_reviews"][self.review_id]
            existing_embeds = list(
                getattr(getattr(interaction, "message", None), "embeds", []) or []
            )
            content_embed = discord.Embed.from_dict(
                bordered_embed(
                    build_post_signal_review_message(
                        record,
                        datetime.now(EASTERN),
                        private=True,
                    ),
                    color=BRAND_NEON_PINK,
                )
            )
            if replacement_file is None:
                await interaction.response.edit_message(
                    embeds=[content_embed, *existing_embeds[1:]],
                    view=PostSignalReviewView(),
                )
            else:
                record["comparison_chart_verified"] = requested_chart_verification
                original_embed = existing_embeds[1] if len(existing_embeds) > 1 else None
                comparison_embed = build_post_signal_chart_embed(
                    discord,
                    "Updated Weekly Chart — Staff Verified",
                    f"attachment://{replacement_filename}",
                )
                await interaction.response.edit_message(
                    embeds=[
                        content_embed,
                        *([original_embed] if original_embed is not None else []),
                        comparison_embed,
                    ],
                    attachments=[replacement_file],
                    view=PostSignalReviewView(),
                )
                try:
                    _, persisted_outcome = edit_post_signal_review(
                        self.review_id,
                        self.draft_message_id,
                        datetime.now(EASTERN).isoformat(),
                        outcome=str(self.outcome),
                        summary=str(self.summary),
                        comparison_chart_verified=requested_chart_verification,
                    )
                except EarningsStateError:
                    persisted_outcome = "invalid"
                if persisted_outcome != "updated":
                    await send_ephemeral_rejection(
                        interaction,
                        "The corrected chart was attached, but verification was not saved. "
                        "Publishing remains blocked; please edit the review again.",
                    )

    class PostSignalReviewView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        async def resolve(self, interaction: Any):
            if (
                signal_review_drafts_channel_id is None
                or not can_clear_earnings_review(interaction.user, interaction.guild)
                or discord_id_text(interaction.channel_id)
                != str(signal_review_drafts_channel_id)
            ):
                return None
            message = getattr(interaction, "message", None)
            if message is None:
                return None
            try:
                state = load_state()
            except EarningsStateError:
                return None
            found = find_post_signal_review_by_draft(
                state,
                interaction.channel_id,
                getattr(message, "id", None),
            )
            return (*found, message) if found is not None else None

        @discord.ui.button(
            label="Publish Review",
            style=discord.ButtonStyle.success,
            custom_id="post_signal_review_publish",
            row=0,
        )
        async def publish(self, interaction: Any, button: Any) -> None:
            message_id = str(getattr(getattr(interaction, "message", None), "id", ""))
            async with post_signal_review_locks.hold(message_id):
                resolved = await self.resolve(interaction)
                if resolved is None or signal_reviews_channel_id is None:
                    await send_ephemeral_rejection(interaction, "This review is unavailable.")
                    return
                review_id, record, draft_message = resolved
                # Acknowledge Discord before any transactional state change.
                # If acknowledgement itself fails, the review remains safely
                # draft_ready and another action can still be chosen.
                try:
                    await defer_ephemeral_response(interaction)
                except Exception:
                    await write_bot_log(
                        f"Signal Review {review_id} could not acknowledge Publish; "
                        "no publication was claimed."
                    )
                    return
                attempt_id = uuid.uuid4().hex
                now = datetime.now(EASTERN).isoformat()
                try:
                    _state, outcome = claim_post_signal_review_action(
                        review_id,
                        message_id,
                        attempt_id,
                        POST_SIGNAL_REVIEW_PUBLISHING,
                        now,
                    )
                except EarningsStateError:
                    outcome = "invalid"
                if outcome == "verification_required":
                    await send_ephemeral_rejection(
                        interaction,
                        "Verify the updated chart in Edit before publishing.",
                    )
                    return
                if outcome != "claimed":
                    await send_ephemeral_rejection(interaction, "This review is already being handled.")
                    return
                try:
                    public_channel = client.get_channel(signal_reviews_channel_id)
                    if public_channel is None:
                        public_channel = await client.fetch_channel(signal_reviews_channel_id)
                    attachments = list(getattr(draft_message, "attachments", []) or [])
                    if len(attachments) != 2:
                        raise DefiniteDeliveryError("review charts unavailable")
                    original_filename = f"{record['symbol']}_original.png"
                    updated_filename = f"{record['symbol']}_updated.png"
                    try:
                        original_file, updated_file = await asyncio.gather(
                            asyncio.wait_for(
                                attachments[0].to_file(
                                    filename=original_filename,
                                    use_cached=True,
                                ),
                                timeout=20,
                            ),
                            asyncio.wait_for(
                                attachments[1].to_file(
                                    filename=updated_filename,
                                    use_cached=True,
                                ),
                                timeout=20,
                            ),
                        )
                    except asyncio.TimeoutError as exc:
                        raise DefiniteDeliveryError(
                            "review chart retrieval timed out"
                        ) from exc
                    content_embed = discord.Embed.from_dict(
                        bordered_embed(
                            build_post_signal_review_message(
                                record,
                                datetime.now(EASTERN),
                                private=False,
                            ),
                            color=BRAND_NEON_PINK,
                        )
                    )
                    original_embed = build_post_signal_chart_embed(
                        discord,
                        "Original Signal Chart",
                        f"attachment://{original_filename}",
                    )
                    updated_embed = build_post_signal_chart_embed(
                        discord,
                        "Updated Weekly Chart",
                        f"attachment://{updated_filename}",
                    )
                    public_message = await public_channel.send(
                        embeds=[content_embed, original_embed, updated_embed],
                        files=[original_file, updated_file],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.Forbidden, DefiniteDeliveryError):
                    transition_post_signal_review(
                        review_id,
                        attempt_id,
                        POST_SIGNAL_REVIEW_DRAFT_READY,
                        datetime.now(EASTERN).isoformat(),
                        error="public_delivery_failed",
                    )
                    await send_ephemeral_rejection(interaction, "The review was not published.")
                    return
                except Exception:
                    transition_post_signal_review(
                        review_id,
                        attempt_id,
                        POST_SIGNAL_REVIEW_UNKNOWN,
                        datetime.now(EASTERN).isoformat(),
                        error="public_delivery_ambiguous",
                    )
                    await send_ephemeral_rejection(
                        interaction,
                        "Publication is ambiguous. Do not retry until staff reconcile it.",
                    )
                    return
                public_message_id = discord_id_text(getattr(public_message, "id", None))
                try:
                    _state, confirmed = transition_post_signal_review(
                        review_id,
                        attempt_id,
                        POST_SIGNAL_REVIEW_PUBLISHED,
                        datetime.now(EASTERN).isoformat(),
                        updates={
                            "public_channel_id": str(signal_reviews_channel_id),
                            "public_message_id": public_message_id,
                            "published_at": datetime.now(EASTERN).isoformat(),
                        },
                    )
                except EarningsStateError:
                    confirmed = "invalid"
                if confirmed != "transitioned":
                    await send_ephemeral_rejection(
                        interaction,
                        "Discord accepted the review but confirmation failed. Do not retry.",
                    )
                    return
                try:
                    await draft_message.delete()
                except discord.NotFound:
                    pass
                except Exception:
                    await send_ephemeral_rejection(
                        interaction,
                        "The review was published; its private draft needs manual cleanup.",
                    )

        @discord.ui.button(
            label="Review in 1 Month",
            style=discord.ButtonStyle.secondary,
            custom_id="post_signal_review_defer",
            row=0,
        )
        async def defer_review(self, interaction: Any, button: Any) -> None:
            resolved = await self.resolve(interaction)
            if resolved is None:
                await send_ephemeral_rejection(interaction, "This review is unavailable.")
                return
            review_id, _record, message = resolved
            try:
                await defer_ephemeral_response(interaction)
            except Exception:
                return
            try:
                _state, outcome = defer_post_signal_review(
                    review_id,
                    str(message.id),
                    datetime.now(EASTERN).isoformat(),
                )
            except EarningsStateError:
                outcome = "invalid"
            if outcome != "deferred":
                await send_ephemeral_rejection(interaction, "The review was not rescheduled.")
                return
            await send_ephemeral_rejection(
                interaction,
                "Review rescheduled for one calendar month from today.",
            )
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except Exception:
                await write_bot_log(
                    f"Signal Review {review_id} was deferred but its draft needs manual cleanup."
                )

        @discord.ui.button(
            label="Dismiss",
            style=discord.ButtonStyle.danger,
            custom_id="post_signal_review_dismiss",
            row=0,
        )
        async def dismiss(self, interaction: Any, button: Any) -> None:
            resolved = await self.resolve(interaction)
            if resolved is None:
                await send_ephemeral_rejection(interaction, "This review is unavailable.")
                return
            review_id, _record, message = resolved
            try:
                await defer_ephemeral_response(interaction)
            except Exception:
                return
            attempt_id = uuid.uuid4().hex
            try:
                _state, outcome = claim_post_signal_review_action(
                    review_id,
                    str(message.id),
                    attempt_id,
                    POST_SIGNAL_REVIEW_DISMISSED,
                    datetime.now(EASTERN).isoformat(),
                )
            except EarningsStateError:
                outcome = "invalid"
            if outcome != "claimed":
                await send_ephemeral_rejection(interaction, "This review is unavailable.")
                return
            await send_ephemeral_rejection(interaction, "Review dismissed.")
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except Exception:
                await write_bot_log(
                    f"Signal Review {review_id} was dismissed but its draft needs manual cleanup."
                )

    async def resolve_original_signal_chart_url(record: dict[str, Any]) -> str:
        signal_channel = client.get_channel(int(record["signals_channel_id"]))
        if signal_channel is None:
            signal_channel = await client.fetch_channel(
                int(record["signals_channel_id"])
            )
        signal_message = await signal_channel.fetch_message(
            int(record["signals_message_id"])
        )
        attachments = list(getattr(signal_message, "attachments", []) or [])
        if len(attachments) == 1:
            url = str(getattr(attachments[0], "url", "") or "")
            if url:
                return url
        for embed in list(getattr(signal_message, "embeds", []) or []):
            image = getattr(embed, "image", None)
            url = str(getattr(image, "url", "") or "")
            if url:
                return url
        raise DefiniteDeliveryError("Original Signals chart is unavailable.")

    async def create_due_post_signal_review_draft(
        review_id: str,
        record: dict[str, Any],
    ) -> None:
        if signal_review_drafts_channel_id is None:
            return
        attempt_id = uuid.uuid4().hex
        now = datetime.now(EASTERN)
        try:
            _state, outcome = claim_due_post_signal_review(
                review_id,
                attempt_id,
                now.isoformat(),
            )
        except EarningsStateError:
            return
        if outcome != "claimed":
            return
        chart_path: Path | None = None
        discord_delivery_started = False
        try:
            original_chart_url = await resolve_original_signal_chart_url(record)
            original_chart_bytes = await asyncio.to_thread(
                download_manual_chart_bytes,
                original_chart_url,
            )
            current_price = await asyncio.to_thread(
                latest_chart_close,
                record["symbol"],
            )
            record["current_price"] = current_price
            chart_path = temporary_weekly_chart_path(record["symbol"])
            chart_path = await asyncio.to_thread(
                generate_weekly_chart,
                record["symbol"],
                output_path=chart_path,
                weeks=130,
                level_segments=(
                    [
                        {
                            "price": record["reference_level"],
                            "start_date": parse_iso_datetime(record["sent_at"]).date().isoformat(),
                        }
                    ]
                    if normalized_reference_level(record.get("reference_level")) is not None
                    else None
                ),
            )
            draft_channel = client.get_channel(signal_review_drafts_channel_id)
            if draft_channel is None:
                draft_channel = await client.fetch_channel(
                    signal_review_drafts_channel_id
                )
            original_filename = f"{record['symbol']}_original.png"
            updated_filename = f"{record['symbol']}_updated.png"
            original_file = discord.File(
                io.BytesIO(original_chart_bytes),
                filename=original_filename,
            )
            updated_file = discord.File(
                chart_path,
                filename=updated_filename,
            )
            content_embed = discord.Embed.from_dict(
                bordered_embed(
                    build_post_signal_review_message(record, now, private=True),
                    color=BRAND_NEON_PINK,
                )
            )
            original_embed = build_post_signal_chart_embed(
                discord,
                "Original Signal Chart",
                f"attachment://{original_filename}",
            )
            updated_embed = build_post_signal_chart_embed(
                discord,
                "Updated Weekly Chart",
                f"attachment://{updated_filename}",
            )
            discord_delivery_started = True
            draft_message = await draft_channel.send(
                embeds=[content_embed, original_embed, updated_embed],
                files=[original_file, updated_file],
                view=PostSignalReviewView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            draft_message_id = discord_id_text(getattr(draft_message, "id", None))
            if draft_message_id is None:
                raise AmbiguousDeliveryError("Draft message ID is unavailable.")
            _latest, confirmation = transition_post_signal_review(
                review_id,
                attempt_id,
                POST_SIGNAL_REVIEW_DRAFT_READY,
                datetime.now(EASTERN).isoformat(),
                updates={
                    "draft_channel_id": str(signal_review_drafts_channel_id),
                    "draft_message_id": draft_message_id,
                    "original_review_chart_filename": original_filename,
                    "updated_review_chart_filename": updated_filename,
                    "draft_created_at": datetime.now(EASTERN).isoformat(),
                    "current_price": current_price,
                },
            )
            if confirmation != "transitioned":
                raise AmbiguousDeliveryError("Draft confirmation failed.")
            await write_bot_log(
                f"Signal Review **{safe_manual_signal_log_value(record['symbol'])}** "
                "is ready for staff review."
            )
        except asyncio.CancelledError:
            try:
                transition_post_signal_review(
                    review_id,
                    attempt_id,
                    (
                        POST_SIGNAL_REVIEW_UNKNOWN
                        if discord_delivery_started
                        else POST_SIGNAL_REVIEW_SCHEDULED
                    ),
                    datetime.now(EASTERN).isoformat(),
                    error=(
                        "draft_delivery_cancelled"
                        if discord_delivery_started
                        else "draft_preparation_cancelled"
                    ),
                )
            except EarningsStateError:
                pass
            raise
        except AmbiguousDeliveryError:
            try:
                transition_post_signal_review(
                    review_id,
                    attempt_id,
                    POST_SIGNAL_REVIEW_UNKNOWN,
                    datetime.now(EASTERN).isoformat(),
                    error="draft_delivery_ambiguous",
                )
            except EarningsStateError:
                pass
        except Exception as exc:
            try:
                transition_post_signal_review(
                    review_id,
                    attempt_id,
                    (
                        POST_SIGNAL_REVIEW_UNKNOWN
                        if discord_delivery_started
                        else POST_SIGNAL_REVIEW_SCHEDULED
                    ),
                    datetime.now(EASTERN).isoformat(),
                    error=(
                        f"draft_delivery_ambiguous:{type(exc).__name__}"
                        if discord_delivery_started
                        else f"draft_preparation_failed:{type(exc).__name__}"
                    ),
                )
            except EarningsStateError:
                pass
            print("Could not create Signal Review draft:", repr(exc), flush=True)
        finally:
            if chart_path is not None:
                cleanup_weekly_chart(chart_path)

    async def post_signal_review_scheduler() -> None:
        await client.wait_until_ready()
        while not client.is_closed():
            try:
                now = datetime.now(EASTERN)
                state = load_state()
                reviews = state.get("post_signal_reviews", {})
                due = [
                    (str(review_id), copy.deepcopy(record))
                    for review_id, record in reviews.items()
                    if is_due_post_signal_review(record, now)
                ] if isinstance(reviews, dict) else []
                due.sort(key=lambda pair: pair[1]["review_due_at"])
                for review_id, record in due:
                    await create_due_post_signal_review_draft(review_id, record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("Signal Review scheduler error:", repr(exc), flush=True)
            await asyncio.sleep(3600)

    class SentEarningsReviewView(
        discord.ui.View,
    ):
        def __init__(self):
            super().__init__(
                timeout=None
            )

            sent_button = discord.ui.Button(
                label="Sent",
                emoji="✅",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                custom_id="earnings_signal_sent",
            )

            self.add_item(
                sent_button
            )

    async def mark_review_as_sent(
        message_id: str,
    ) -> None:
        try:
            review_channel = client.get_channel(
                review_channel_id
            )

            if review_channel is None:
                review_channel = await client.fetch_channel(
                    review_channel_id
                )

            review_message = await review_channel.fetch_message(
                int(message_id)
            )

            await review_message.edit(
                view=SentEarningsReviewView()
            )

        except Exception as exc:
            print(
                "Could not gray out review button:",
                repr(exc),
                flush=True,
            )

    review_submission_locks = ReviewMessageAsyncLocks()
    manual_draft_locks = ReviewMessageAsyncLocks()
    post_signal_review_locks = ReviewMessageAsyncLocks()

    class ManualSignalClosedView(discord.ui.View):
        def __init__(self, label: str):
            super().__init__(timeout=None)
            self.add_item(
                discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    custom_id=f"manual_signal_{label.lower()}",
                )
            )

    async def resolve_manual_draft(
        interaction: Any,
        *,
        require_creator: bool,
    ) -> tuple[str, dict[str, Any], Any] | None:
        user = getattr(interaction, "user", None)
        guild = getattr(interaction, "guild", None)
        message = getattr(interaction, "message", None)
        message_id = getattr(message, "id", None)
        channel_id = getattr(interaction, "channel_id", None)
        if (
            drafts_channel_id is None
            or not can_clear_earnings_review(user, guild)
            or discord_id_text(channel_id) != str(drafts_channel_id)
        ):
            return None
        try:
            state = load_state()
        except EarningsStateError:
            return None
        found = find_manual_signal_draft_by_message(state, message_id)
        if found is None:
            return None
        draft_id, _record = found
        record = validated_manual_signal_draft(
            state,
            draft_id,
            message_id,
            channel_id,
            drafts_channel_id,
        )
        if record is None:
            return None
        if require_creator and discord_id_text(
            getattr(user, "id", None)
        ) != record["creator_user_id"]:
            return None
        if not is_valid_review_message_provenance(
            message,
            message_id=message_id,
            channel_id=drafts_channel_id,
            bot_user_id=getattr(client.user, "id", None),
            normal_message_type=discord.MessageType.default,
        ):
            return None
        try:
            draft_channel = client.get_channel(drafts_channel_id)
            if draft_channel is None:
                draft_channel = await client.fetch_channel(drafts_channel_id)
            current_message = await draft_channel.fetch_message(
                int(record["draft_message_id"])
            )
        except Exception:
            return None
        if not is_valid_review_message_provenance(
            current_message,
            message_id=record["draft_message_id"],
            channel_id=drafts_channel_id,
            bot_user_id=getattr(client.user, "id", None),
            normal_message_type=discord.MessageType.default,
        ):
            return None
        return draft_id, record, current_message

    class ManualSignalModal(discord.ui.Modal):
        def __init__(
            self,
            opener_user_id: str,
            *,
            draft_id: str | None = None,
            record: dict[str, Any] | None = None,
        ):
            super().__init__(title="New Trade Signal" if record is None else "Edit Trade Signal")
            self.opener_user_id = opener_user_id
            self.draft_id = draft_id
            self.is_edit = record is not None
            record = record or {}
            self.draft_message_id = str(
                record.get("draft_message_id", "")
            )
            self.instrument = discord.ui.TextInput(
                required=True,
                max_length=100,
                default=record.get("instrument"),
                custom_id="manual_signal_instrument",
            )
            self.timeframe = discord.ui.TextInput(
                required=False,
                max_length=100,
                default=record.get("timeframe") or "Weekly",
                custom_id="manual_signal_timeframe",
            )
            self.reference_level = discord.ui.TextInput(
                required=True,
                max_length=30,
                default=(
                    str(record["reference_level"])
                    if normalized_reference_level(record.get("reference_level")) is not None
                    else None
                ),
                placeholder="Price of the single line on your chart",
                custom_id="manual_signal_reference_level",
            )
            current_direction = normalized_trade_direction(
                record.get("trade_direction")
            )
            self.initial_direction = current_direction
            self.trade_direction = discord.ui.Select(
                custom_id="manual_signal_create_direction",
                placeholder="Select Long or Short",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label="Long",
                        value=TRADE_DIRECTION_LONG,
                        emoji="🟢",
                        default=current_direction == TRADE_DIRECTION_LONG,
                    ),
                    discord.SelectOption(
                        label="Short",
                        value=TRADE_DIRECTION_SHORT,
                        emoji="🔴",
                        default=current_direction == TRADE_DIRECTION_SHORT,
                    ),
                ],
            )
            self.setup_name = None
            self.trade_thesis = discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1800,
                default=record.get("trade_thesis"),
                custom_id="manual_signal_thesis",
            )
            self.trade_chart = discord.ui.FileUpload(
                custom_id="manual_signal_chart",
                required=not self.is_edit,
                min_values=0 if self.is_edit else 1,
                max_values=1,
            )
            fields = [("Instrument or symbol", self.instrument)]
            fields.append(("Trade direction", self.trade_direction))
            fields.append(("Reference level", self.reference_level))
            if self.setup_name is not None:
                fields.append(("Setup name (optional)", self.setup_name))
            fields.append(("Trade thesis", self.trade_thesis))
            for label, item in fields:
                self.add_item(discord.ui.Label(text=label, component=item))
            self.add_item(
                discord.ui.Label(
                    text="Trade Chart",
                    description=(
                        "Upload PNG, JPG, JPEG, or WEBP. Leave blank while "
                        "editing to retain the current chart."
                    ),
                    component=self.trade_chart,
                )
            )

        async def on_submit(self, interaction: Any) -> None:
            if str(getattr(getattr(interaction, "user", None), "id", "")) != self.opener_user_id:
                await send_ephemeral_rejection(
                    interaction,
                    "This signal form is no longer available.",
                )
                return
            if self.is_edit:
                async with manual_draft_locks.hold(self.draft_message_id):
                    await self._submit_edit(interaction)
            else:
                await self._submit_new(interaction)

        def fields(self) -> tuple[str, str, str, str, float | None]:
            return (
                str(self.instrument.value).strip(),
                str(self.trade_thesis.value).strip(),
                str(self.timeframe.value or "Weekly").strip(),
                (
                    str(self.setup_name.value or "").strip()
                    if self.setup_name is not None
                    else ""
                ),
                normalized_reference_level(str(self.reference_level.value).strip()),
            )

        def selected_direction(self) -> str | None:
            values = list(self.trade_direction.values)
            if len(values) == 1:
                return normalized_trade_direction(values[0])
            if self.is_edit:
                return self.initial_direction
            return None

        async def _submit_new(self, interaction: Any) -> None:
            if (
                drafts_channel_id is None
                or not can_clear_earnings_review(
                    getattr(interaction, "user", None),
                    getattr(interaction, "guild", None),
                )
                or discord_id_text(getattr(interaction, "channel_id", None))
                != str(drafts_channel_id)
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "This signal form is no longer available.",
                )
                return
            instrument, thesis, timeframe, setup_name, reference_level = self.fields()
            trade_direction = self.selected_direction()
            if trade_direction is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Select Long or Short before creating this signal.",
                )
                return
            if reference_level is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Enter the price of the single reference line on the chart.",
                )
                return
            if not is_valid_manual_signal_fields(
                instrument, thesis, timeframe, setup_name
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "The signal is incomplete or too long.",
                )
                return
            uploads = list(self.trade_chart.values)
            if len(uploads) != 1 or not is_valid_manual_chart_attachment(uploads[0]):
                await send_ephemeral_rejection(
                    interaction,
                    "The chart must be a PNG, JPG, JPEG, or WEBP image.",
                )
                return
            await defer_ephemeral_response(interaction)
            attachment = uploads[0]
            try:
                draft_channel = client.get_channel(drafts_channel_id)
                if draft_channel is None:
                    draft_channel = await client.fetch_channel(drafts_channel_id)
                draft_filename = Path(attachment.filename).name
                draft_file = await attachment.to_file(filename=draft_filename)
                content = build_manual_signal_message(
                    instrument,
                    thesis,
                    trade_direction=trade_direction,
                    timeframe=timeframe,
                    setup_name=setup_name,
                    reference_level=reference_level,
                )
                draft_message = await draft_channel.send(
                    embed=build_bordered_discord_embed(
                        discord,
                        content,
                        draft_filename,
                    ),
                    file=draft_file,
                    view=ManualSignalDraftView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as exc:
                print("Manual signal draft creation failed:", repr(exc), flush=True)
                await send_ephemeral_rejection(
                    interaction,
                    "The signal draft could not be created.",
                )
                return
            message_id = discord_id_text(getattr(draft_message, "id", None))
            if message_id is None:
                await send_ephemeral_rejection(
                    interaction,
                    "The signal draft could not be confirmed.",
                )
                return
            draft_id = uuid.uuid4().hex
            now = datetime.now(EASTERN).isoformat()
            posted_attachments = list(
                getattr(draft_message, "attachments", []) or []
            )
            stored_attachment = (
                posted_attachments[0]
                if len(posted_attachments) == 1
                and is_valid_manual_chart_attachment(posted_attachments[0])
                else attachment
            )
            record = {
                "draft_id": draft_id,
                "draft_message_id": message_id,
                "draft_channel_id": str(drafts_channel_id),
                "creator_user_id": str(interaction.user.id),
                "instrument": instrument,
                "trade_thesis": thesis,
                "trade_direction": trade_direction,
                "timeframe": timeframe,
                "setup_name": setup_name,
                "reference_level": reference_level,
                "chart": manual_chart_metadata(stored_attachment),
                "created_at": now,
                "updated_at": now,
                "delivery_status": MANUAL_SIGNAL_READY,
                "canceled": False,
            }
            try:
                set_state_record("manual_signal_drafts", draft_id, record)
            except (EarningsStateError, OSError):
                await write_manual_signal_log(
                    instrument,
                    interaction.user,
                    "state persistence failed while creating a draft",
                )
                try:
                    await draft_message.edit(view=ManualSignalClosedView("Unavailable"))
                except Exception:
                    pass
                await send_ephemeral_rejection(
                    interaction,
                    "The signal draft could not be saved.",
                )
                return
            try:
                await interaction.delete_original_response()
            except Exception:
                pass

        async def _submit_edit(self, interaction: Any) -> None:
            resolved = await resolve_manual_draft(
                interaction,
                require_creator=True,
            )
            if resolved is None or resolved[0] != self.draft_id:
                await send_ephemeral_rejection(
                    interaction,
                    "This signal draft is no longer available.",
                )
                return
            draft_id, record, draft_message = resolved
            if record["canceled"] or manual_signal_delivery_status(record) != MANUAL_SIGNAL_READY:
                await send_ephemeral_rejection(
                    interaction,
                    "This signal draft is no longer available.",
                )
                return
            instrument, thesis, timeframe, setup_name, reference_level = self.fields()
            trade_direction = self.selected_direction()
            if trade_direction is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Select Long or Short before updating this signal.",
                )
                return
            if reference_level is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Enter the price of the single reference line on the chart.",
                )
                return
            setup_name = record["setup_name"]
            if not is_valid_manual_signal_fields(
                instrument, thesis, timeframe, setup_name
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "The signal is incomplete or too long.",
                )
                return
            uploads = list(self.trade_chart.values)
            if len(uploads) > 1 or (
                uploads and not is_valid_manual_chart_attachment(uploads[0])
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "The chart must be a PNG, JPG, JPEG, or WEBP image.",
                )
                return
            await defer_ephemeral_response(interaction)
            content = build_manual_signal_message(
                instrument,
                thesis,
                trade_direction=trade_direction,
                timeframe=timeframe,
                setup_name=setup_name,
                reference_level=reference_level,
            )
            chart = None
            try:
                if uploads:
                    replacement = uploads[0]
                    replacement_filename = Path(replacement.filename).name
                    replacement_file = await replacement.to_file(
                        filename=replacement_filename
                    )
                    updated_message = await draft_message.edit(
                        content=None,
                        embed=build_bordered_discord_embed(
                            discord,
                            content,
                            replacement_filename,
                        ),
                        attachments=[replacement_file],
                        view=ManualSignalDraftView(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    updated_attachments = list(
                        getattr(updated_message, "attachments", []) or []
                    )
                    stored_replacement = (
                        updated_attachments[0]
                        if len(updated_attachments) == 1
                        and is_valid_manual_chart_attachment(updated_attachments[0])
                        else replacement
                    )
                    chart = manual_chart_metadata(stored_replacement)
                else:
                    await draft_message.edit(
                        content=None,
                        embed=build_bordered_discord_embed(
                            discord,
                            content,
                            Path(record["chart"]["filename"]).name,
                        ),
                        view=ManualSignalDraftView(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception:
                await send_ephemeral_rejection(
                    interaction,
                    "The signal draft could not be updated.",
                )
                return
            try:
                _state, outcome = update_manual_signal_draft(
                    draft_id,
                    record["draft_message_id"],
                    record["draft_channel_id"],
                    instrument=instrument,
                    trade_thesis=thesis,
                    trade_direction=trade_direction,
                    timeframe=timeframe,
                    setup_name=setup_name,
                    reference_level=reference_level,
                    chart=chart,
                    updated_at=datetime.now(EASTERN).isoformat(),
                )
            except (EarningsStateError, OSError):
                outcome = "persistence_failed"
            if outcome != "updated":
                if outcome == "persistence_failed":
                    await write_manual_signal_log(
                        instrument,
                        interaction.user,
                        "state persistence failed while updating a draft",
                    )
                await send_ephemeral_rejection(
                    interaction,
                    "The draft changed, but its state needs staff review.",
                )
                return
            try:
                await interaction.delete_original_response()
            except Exception:
                pass

    class ManualSignalDraftView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(
            label="Publish",
            style=discord.ButtonStyle.success,
            custom_id="manual_signal_publish",
            row=0,
        )
        async def publish(self, interaction, button):
            message_id = str(getattr(getattr(interaction, "message", None), "id", ""))
            async with manual_draft_locks.hold(message_id):
                resolved = await resolve_manual_draft(
                    interaction,
                    require_creator=False,
                )
                if resolved is None:
                    await send_ephemeral_rejection(
                        interaction,
                        "This signal draft is no longer available.",
                    )
                    return
                draft_id, record, draft_message = resolved
                if record["canceled"]:
                    await send_ephemeral_rejection(
                        interaction,
                        "This signal draft is no longer available.",
                    )
                    return
                attempt_id = uuid.uuid4().hex
                now = datetime.now(EASTERN).isoformat()
                try:
                    _state, outcome = claim_manual_signal_delivery(
                        draft_id,
                        record["draft_message_id"],
                        record["draft_channel_id"],
                        attempt_id,
                        now,
                    )
                except EarningsStateError:
                    outcome = "invalid"
                    await write_manual_signal_log(
                        record["instrument"],
                        interaction.user,
                        "state persistence failed before publication",
                    )
                if outcome != "claimed":
                    messages = {
                        "direction_required": "Select Long or Short before publishing.",
                        "reference_level_required": "Enter the chart reference level before publishing.",
                        MANUAL_SIGNAL_SENDING: "This signal is already being published.",
                        MANUAL_SIGNAL_SENT: "This signal was already published.",
                        MANUAL_SIGNAL_UNKNOWN: "This signal needs staff reconciliation before retrying.",
                    }
                    await send_ephemeral_rejection(
                        interaction,
                        messages.get(outcome, "This signal draft is no longer available."),
                    )
                    return
                await defer_ephemeral_response(interaction)

                async def finish(status: str, error: str, user_message: str) -> None:
                    try:
                        _latest, transition = transition_manual_signal_delivery(
                            draft_id,
                            attempt_id,
                            status,
                            datetime.now(EASTERN).isoformat(),
                            error=error,
                        )
                    except (EarningsStateError, OSError):
                        transition = "persistence_failed"
                    if transition != "transitioned":
                        await write_manual_signal_log(
                            record["instrument"],
                            interaction.user,
                            "state persistence failed; staff reconciliation is required",
                        )
                        user_message = (
                            "The delivery state needs staff reconciliation. "
                            "Do not retry this signal yet."
                        )
                    elif status == MANUAL_SIGNAL_UNKNOWN:
                        await write_manual_signal_log(
                            record["instrument"],
                            interaction.user,
                            "has an ambiguous delivery requiring staff reconciliation",
                        )
                    await send_ephemeral_rejection(interaction, user_message)

                attachments = list(getattr(draft_message, "attachments", []) or [])
                attachment = (
                    attachments[0]
                    if len(attachments) == 1
                    and manual_chart_matches_record(
                        attachments[0], record["chart"]
                    )
                    else None
                )
                embed_chart_url = (
                    manual_chart_embed_url(draft_message, record["chart"])
                    if not attachments
                    else None
                )
                if attachment is None and embed_chart_url is None:
                    await finish(
                        MANUAL_SIGNAL_READY,
                        "draft_chart_unavailable",
                        "The draft chart is unavailable. Please edit the draft.",
                    )
                    return
                content = build_manual_signal_message(
                    record["instrument"],
                    record["trade_thesis"],
                    trade_direction=record.get("trade_direction"),
                    timeframe=record["timeframe"],
                    setup_name=record["setup_name"],
                    reference_level=record.get("reference_level"),
                )
                if len(content) > MANUAL_SIGNAL_MAX_CONTENT_LENGTH:
                    await finish(
                        MANUAL_SIGNAL_READY,
                        "signal_content_too_long",
                        "The signal is too long. Please edit the draft.",
                    )
                    return
                try:
                    signals_channel = client.get_channel(signals_channel_id)
                    if signals_channel is None:
                        signals_channel = await client.fetch_channel(signals_channel_id)
                    if signals_channel is None:
                        raise DefiniteDeliveryError("Signals channel unavailable")
                    chart_filename = Path(record["chart"]["filename"]).name
                    if attachment is not None:
                        chart_file = await attachment.to_file(
                            filename=chart_filename
                        )
                    else:
                        chart_bytes = await asyncio.to_thread(
                            download_manual_chart_bytes,
                            str(embed_chart_url),
                        )
                        chart_file = discord.File(
                            io.BytesIO(chart_bytes),
                            filename=chart_filename,
                        )
                except asyncio.CancelledError:
                    state_persistence_failed = False
                    try:
                        transition_manual_signal_delivery(
                            draft_id,
                            attempt_id,
                            MANUAL_SIGNAL_READY,
                            datetime.now(EASTERN).isoformat(),
                            error="pre_delivery_cancelled",
                        )
                    except (EarningsStateError, OSError):
                        state_persistence_failed = True
                    if state_persistence_failed:
                        await write_manual_signal_log(
                            record["instrument"],
                            interaction.user,
                            "state persistence failed before publication",
                        )
                    raise
                except Exception:
                    await finish(
                        MANUAL_SIGNAL_READY,
                        "pre_delivery_failed",
                        "The signal could not be prepared. Please try again.",
                    )
                    return
                try:
                    signals_message = await signals_channel.send(
                        embed=build_bordered_discord_embed(
                            discord,
                            content,
                            chart_filename,
                        ),
                        file=chart_file,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except asyncio.CancelledError:
                    state_persistence_failed = False
                    try:
                        transition_manual_signal_delivery(
                            draft_id,
                            attempt_id,
                            MANUAL_SIGNAL_UNKNOWN,
                            datetime.now(EASTERN).isoformat(),
                            error="discord_delivery_ambiguous",
                        )
                    except (EarningsStateError, OSError):
                        state_persistence_failed = True
                    await write_manual_signal_log(
                        record["instrument"],
                        interaction.user,
                        (
                            "state persistence failed during an ambiguous delivery"
                            if state_persistence_failed
                            else "has an ambiguous delivery requiring staff reconciliation"
                        ),
                    )
                    raise
                except (discord.Forbidden, discord.HTTPException):
                    await finish(
                        MANUAL_SIGNAL_READY,
                        "discord_rejected",
                        "Discord rejected the Signals post. Please try again.",
                    )
                    return
                except Exception:
                    await finish(
                        MANUAL_SIGNAL_UNKNOWN,
                        "discord_delivery_ambiguous",
                        "Delivery could not be confirmed. Do not retry until staff reconcile it.",
                    )
                    return
                signals_message_id = discord_id_text(
                    getattr(signals_message, "id", None)
                )
                if signals_message_id is None:
                    await finish(
                        MANUAL_SIGNAL_UNKNOWN,
                        "discord_message_id_missing",
                        "Delivery could not be confirmed. Do not retry until staff reconcile it.",
                    )
                    return
                sent_at = datetime.now(EASTERN).isoformat()
                try:
                    review_record = build_post_signal_review_record(
                        source=POST_SIGNAL_REVIEW_SOURCE_MANUAL,
                        source_record_id=draft_id,
                        signals_channel_id=signals_channel_id,
                        signals_message_id=signals_message_id,
                        symbol=record["instrument"],
                        trade_direction=record.get("trade_direction"),
                        trade_thesis=record["trade_thesis"],
                        original_chart_filename=chart_filename,
                        sent_at=sent_at,
                        reference_level=record.get("reference_level"),
                    )
                    _state, confirmation = transition_manual_signal_delivery(
                        draft_id,
                        attempt_id,
                        MANUAL_SIGNAL_SENT,
                        sent_at,
                        signals_message_id=signals_message_id,
                        post_signal_review=review_record,
                    )
                except (EarningsStateError, OSError, ValueError):
                    confirmation = "persistence_failed"
                if confirmation != "transitioned":
                    await write_manual_signal_log(
                        record["instrument"],
                        interaction.user,
                        (
                            "state persistence failed after Signals accepted publication"
                            if confirmation == "persistence_failed"
                            else "was accepted but is unconfirmed; staff reconciliation is required"
                        ),
                    )
                    await send_ephemeral_rejection(
                        interaction,
                        "Signals accepted the post, but confirmation failed. Do not retry.",
                    )
                    return
                await write_manual_signal_log(
                    record["instrument"],
                    interaction.user,
                    "was published successfully to Signals",
                )
                try:
                    await draft_message.delete()
                except discord.NotFound:
                    pass
                except Exception as exc:
                    await write_manual_signal_log(
                        record["instrument"],
                        interaction.user,
                        "draft deletion failed after successful publication",
                    )
                    try:
                        await draft_message.edit(
                            view=ManualSignalClosedView("Published")
                        )
                    except Exception as fallback_exc:
                        print(
                            "Could not disable published Manual Signal controls:",
                            repr(fallback_exc),
                            flush=True,
                        )
                    await send_ephemeral_rejection(
                        interaction,
                        (
                            "The signal was published, but its draft message "
                            "requires manual cleanup."
                        ),
                    )
                    return
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass

        @discord.ui.button(
            label="Edit",
            style=discord.ButtonStyle.primary,
            custom_id="manual_signal_edit",
            row=0,
        )
        async def edit(self, interaction, button):
            resolved = await resolve_manual_draft(
                interaction,
                require_creator=True,
            )
            if resolved is None:
                await send_ephemeral_rejection(
                    interaction,
                    "This signal draft is no longer available.",
                )
                return
            draft_id, record, _message = resolved
            if record["canceled"] or manual_signal_delivery_status(record) != MANUAL_SIGNAL_READY:
                await send_ephemeral_rejection(
                    interaction,
                    "This signal draft is no longer available.",
                )
                return
            if interaction_response_is_done(interaction):
                await send_ephemeral_rejection(
                    interaction,
                    "This signal draft is no longer available.",
                )
                return
            await interaction.response.send_modal(
                ManualSignalModal(
                    str(interaction.user.id),
                    draft_id=draft_id,
                    record=record,
                )
            )

        @discord.ui.button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id="manual_signal_cancel",
            row=0,
        )
        async def cancel(self, interaction, button):
            message_id = str(getattr(getattr(interaction, "message", None), "id", ""))
            async with manual_draft_locks.hold(message_id):
                resolved = await resolve_manual_draft(
                    interaction,
                    require_creator=True,
                )
                if resolved is None:
                    await send_ephemeral_rejection(
                        interaction,
                        "This signal draft is no longer available.",
                    )
                    return
                draft_id, record, draft_message = resolved
                try:
                    _state, outcome = cancel_manual_signal_draft(
                        draft_id,
                        record["draft_message_id"],
                        record["draft_channel_id"],
                        datetime.now(EASTERN).isoformat(),
                    )
                except EarningsStateError:
                    outcome = "invalid"
                    await write_manual_signal_log(
                        record["instrument"],
                        interaction.user,
                        "state persistence failed during cancellation",
                    )
                if outcome != "canceled":
                    await send_ephemeral_rejection(
                        interaction,
                        "This signal draft is no longer available.",
                    )
                    return
                try:
                    await draft_message.delete()
                except discord.NotFound:
                    pass
                except Exception:
                    await write_manual_signal_log(
                        record["instrument"],
                        interaction.user,
                        "draft deletion failed after cancellation",
                    )
                    try:
                        await draft_message.edit(
                            view=ManualSignalClosedView("Canceled")
                        )
                    except Exception as exc:
                        print(
                            "Could not disable canceled Manual Signal controls:",
                            repr(exc),
                            flush=True,
                        )
                    await send_ephemeral_rejection(
                        interaction,
                        "The draft was canceled, but its message could not be removed.",
                    )
                    return
                await write_manual_signal_log(
                    record["instrument"],
                    interaction.user,
                    "was canceled and its draft message was deleted",
                )
                await send_ephemeral_rejection(interaction, "Signal draft canceled.")

    class TradeThesisModal(discord.ui.Modal):
        def __init__(
            self,
            review_message_id: str,
            symbol: str,
            opener_user_id: str,
        ):
            super().__init__(
                title=f"{symbol} Trade Signal"
            )

            self.review_message_id = str(
                review_message_id
            )
            self.symbol = symbol
            self.opener_user_id = str(
                opener_user_id
            )
            self._submission_locks = review_submission_locks

            # Components V2 modal: thesis and chart live in the SAME form.
            self.trade_direction = discord.ui.Select(
                custom_id="trade_direction",
                placeholder="Select Long or Short",
                min_values=1,
                max_values=1,
                required=True,
                options=[
                    discord.SelectOption(
                        label="Long",
                        value=TRADE_DIRECTION_LONG,
                        emoji="🟢",
                    ),
                    discord.SelectOption(
                        label="Short",
                        value=TRADE_DIRECTION_SHORT,
                        emoji="🔴",
                    ),
                ],
            )
            self.trade_thesis = discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                placeholder=(
                    "Add your setup, key levels, "
                    "confirmation, and invalidation."
                ),
                required=True,
                max_length=1800,
                custom_id="trade_thesis",
            )
            self.reference_level = discord.ui.TextInput(
                placeholder="Price of the single line on your chart",
                required=True,
                max_length=30,
                custom_id="trade_reference_level",
            )

            self.trade_chart = discord.ui.FileUpload(
                custom_id="trade_chart",
                required=True,
                min_values=1,
                max_values=1,
            )

            self.add_item(
                discord.ui.Label(
                    text="Trade Direction",
                    description="Required. Choose the intended trade outcome.",
                    component=self.trade_direction,
                )
            )

            self.add_item(
                discord.ui.Label(
                    text="Reference Level",
                    description="Required. Enter the price of the single chart line.",
                    component=self.reference_level,
                )
            )

            self.add_item(
                discord.ui.Label(
                    text="Trade Thesis",
                    description=(
                        "Your notes will appear in the "
                        "member-facing Signals post."
                    ),
                    component=self.trade_thesis,
                )
            )

            self.add_item(
                discord.ui.Label(
                    text="TradingView Chart",
                    description=(
                        "Upload or paste one chart image "
                        "for the final Signals post."
                    ),
                    component=self.trade_chart,
                )
            )

        async def on_submit(
            self,
            interaction,
        ):
            user = getattr(interaction, "user", None)
            guild = getattr(interaction, "guild", None)

            if not can_clear_earnings_review(user, guild):
                await send_ephemeral_rejection(
                    interaction,
                    "You cannot use this earnings review action.",
                )
                return

            if str(getattr(user, "id", None) or "") != self.opener_user_id:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review form is no longer available.",
                )
                return

            async with review_submission_locks.hold(
                self.review_message_id
            ):
                await self._on_submit_locked(interaction)

        async def _on_submit_locked(
            self,
            interaction,
        ):
            user = getattr(interaction, "user", None)
            guild = getattr(interaction, "guild", None)
            channel_id = getattr(interaction, "channel_id", None)
            user_id = getattr(user, "id", None)

            if not can_clear_earnings_review(user, guild):
                await send_ephemeral_rejection(
                    interaction,
                    "You cannot use this earnings review action.",
                )
                return

            if str(user_id or "") != self.opener_user_id:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review form is no longer available.",
                )
                return

            try:
                state = load_state()
            except EarningsStateError:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is currently unavailable.",
                )
                return

            found = validated_review_state_item(
                state,
                self.review_message_id,
                channel_id,
                review_channel_id,
            )

            if found is None:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is no longer available.",
                )
                return

            # Acknowledge before the Discord fetch and attachment conversion.
            await defer_ephemeral_response(interaction)

            try:
                review_channel = client.get_channel(
                    review_channel_id
                )
                if review_channel is None:
                    review_channel = await client.fetch_channel(
                        review_channel_id
                    )

                review_message = await review_channel.fetch_message(
                    int(self.review_message_id)
                )
            except Exception:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is no longer available.",
                )
                return

            token, item = found

            if not is_valid_review_message_provenance(
                review_message,
                message_id=self.review_message_id,
                channel_id=review_channel_id,
                bot_user_id=getattr(client.user, "id", None),
                normal_message_type=discord.MessageType.default,
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is no longer available.",
                )
                return

            current_status = signal_delivery_status(item)
            if current_status != SIGNAL_DELIVERY_READY:
                await send_ephemeral_rejection(
                    interaction,
                    signal_delivery_rejection_message(current_status),
                )
                return

            candidate = item["candidate"]
            trade_direction = normalized_trade_direction(
                self.trade_direction.values[0]
                if len(self.trade_direction.values) == 1
                else None
            )
            if trade_direction is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Select Long or Short before sending this signal.",
                )
                return

            thesis = str(
                self.trade_thesis.value
            ).strip()
            reference_level = normalized_reference_level(
                str(self.reference_level.value).strip()
            )
            if reference_level is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Enter the price of the single reference line on the chart.",
                )
                return

            uploads = list(
                self.trade_chart.values
            )

            if not uploads:
                await interaction.followup.send(
                    "⚠️ Please include a chart image.",
                    ephemeral=True,
                )
                return

            attachment = uploads[0]

            filename_lower = (
                attachment.filename.lower()
            )

            valid_image = (
                (
                    attachment.content_type
                    or ""
                ).startswith("image/")
                or filename_lower.endswith(
                    (
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".webp",
                    )
                )
            )

            if not valid_image:
                await interaction.followup.send(
                    (
                        "⚠️ The chart must be a PNG, "
                        "JPG, JPEG, or WEBP image."
                    ),
                    ephemeral=True,
                )
                return

            attempt_id = uuid.uuid4().hex
            attempt_started_at = datetime.now(
                EASTERN
            ).isoformat()

            try:
                state, claim_outcome = claim_signal_delivery(
                    token,
                    self.review_message_id,
                    str(review_channel_id),
                    attempt_id,
                    attempt_started_at,
                    trade_direction=trade_direction,
                    reference_level=reference_level,
                )
            except EarningsStateError:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is currently unavailable.",
                )
                return

            if claim_outcome != "claimed":
                await send_ephemeral_rejection(
                    interaction,
                    signal_delivery_rejection_message(claim_outcome),
                )
                return

            item = state["signal_queue"][token]
            candidate = item["candidate"]
            trade_direction = normalized_trade_direction(
                item.get("trade_direction")
            )
            if trade_direction is None:
                await send_ephemeral_rejection(
                    interaction,
                    "Select Long or Short before sending this signal.",
                )
                return

            async def finish_failed_attempt(
                status: str,
                error: str,
                user_message: str,
            ) -> bool:
                try:
                    _latest_state, outcome = transition_signal_delivery(
                        token,
                        attempt_id,
                        status,
                        datetime.now(EASTERN).isoformat(),
                        error=error,
                    )
                except (EarningsStateError, OSError):
                    outcome = "persistence_failed"

                if outcome != "transitioned":
                    await send_ephemeral_rejection(
                        interaction,
                        (
                            "The delivery state needs staff reconciliation. "
                            "Do not retry this setup yet."
                        ),
                    )
                    return False

                await send_ephemeral_rejection(
                    interaction,
                    user_message,
                )
                return True

            signals_channel = client.get_channel(
                signals_channel_id
            )

            if signals_channel is None:
                try:
                    signals_channel = await client.fetch_channel(
                        signals_channel_id
                    )
                except asyncio.CancelledError:
                    try:
                        transition_signal_delivery(
                            token,
                            attempt_id,
                            SIGNAL_DELIVERY_READY,
                            datetime.now(EASTERN).isoformat(),
                            error="signals_channel_resolution_cancelled",
                        )
                    except (EarningsStateError, OSError):
                        pass
                    raise
                except Exception:
                    await finish_failed_attempt(
                        SIGNAL_DELIVERY_READY,
                        "signals_channel_unavailable",
                        "The Signals channel is currently unavailable.",
                    )
                    return

            if signals_channel is None:
                await finish_failed_attempt(
                    SIGNAL_DELIVERY_READY,
                    "signals_channel_unavailable",
                    "The Signals channel is currently unavailable.",
                )
                return

            try:
                signal_filename = (
                    f"{candidate['symbol']}"
                    "_trade_chart"
                    f"{Path(attachment.filename).suffix or '.png'}"
                )
                signal_file = await attachment.to_file(
                    filename=signal_filename
                )
                signal_content = build_signal_message(
                    candidate,
                    thesis,
                    trade_direction,
                    reference_level,
                )
            except asyncio.CancelledError:
                try:
                    transition_signal_delivery(
                        token,
                        attempt_id,
                        SIGNAL_DELIVERY_READY,
                        datetime.now(EASTERN).isoformat(),
                        error="attachment_conversion_cancelled",
                    )
                except (EarningsStateError, OSError):
                    pass
                raise
            except Exception:
                await finish_failed_attempt(
                    SIGNAL_DELIVERY_READY,
                    "attachment_conversion_failed",
                    "The chart could not be prepared. Please try again.",
                )
                return

            try:
                signals_message = await signals_channel.send(
                    embed=build_bordered_discord_embed(
                        discord,
                        signal_content,
                        signal_filename,
                    ),
                    file=signal_file,
                    allowed_mentions=(
                        discord.AllowedMentions.none()
                    ),
                )
            except asyncio.CancelledError:
                try:
                    transition_signal_delivery(
                        token,
                        attempt_id,
                        SIGNAL_DELIVERY_UNKNOWN,
                        datetime.now(EASTERN).isoformat(),
                        error="discord_delivery_ambiguous",
                    )
                except (EarningsStateError, OSError):
                    pass
                raise
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(
                    "Discord signal post failed:",
                    repr(exc),
                    flush=True,
                )
                await finish_failed_attempt(
                    SIGNAL_DELIVERY_READY,
                    "discord_rejected",
                    "Discord rejected the Signals post. Please try again.",
                )
                return
            except Exception as exc:
                print(
                    "Discord signal delivery is ambiguous:",
                    repr(exc),
                    flush=True,
                )
                await finish_failed_attempt(
                    SIGNAL_DELIVERY_UNKNOWN,
                    "discord_delivery_ambiguous",
                    (
                        "Discord delivery could not be confirmed. "
                        "Do not retry until staff reconcile it."
                    ),
                )
                return

            signals_message_id = discord_id_text(
                getattr(signals_message, "id", None)
            )
            if signals_message_id is None:
                await finish_failed_attempt(
                    SIGNAL_DELIVERY_UNKNOWN,
                    "discord_message_id_missing",
                    (
                        "Discord delivery could not be confirmed. "
                        "Do not retry until staff reconcile it."
                    ),
                )
                return

            sent_at = datetime.now(EASTERN).isoformat()
            signal_updates = {
                "trade_thesis": thesis,
                "trade_direction": trade_direction,
                "reference_level": reference_level,
                "sent_at": sent_at,
                "sent_by": str(interaction.user),
                "signal_chart_filename": attachment.filename,
                "signals_message_id": signals_message_id,
                "awaiting_chart": False,
            }

            try:
                review_record = build_post_signal_review_record(
                    source=POST_SIGNAL_REVIEW_SOURCE_EARNINGS,
                    source_record_id=token,
                    signals_channel_id=signals_channel_id,
                    signals_message_id=signals_message_id,
                    symbol=candidate["symbol"],
                    trade_direction=trade_direction,
                    trade_thesis=thesis,
                    original_chart_filename=attachment.filename,
                    sent_at=sent_at,
                    reference_level=reference_level,
                )
                latest_state, confirmation_outcome = (
                    transition_signal_delivery(
                        token,
                        attempt_id,
                        SIGNAL_DELIVERY_SENT,
                        sent_at,
                        updates=signal_updates,
                        post_signal_review=review_record,
                    )
                )
            except (EarningsStateError, OSError, ValueError):
                confirmation_outcome = "persistence_failed"

            if confirmation_outcome != "transitioned":
                await send_ephemeral_rejection(
                    interaction,
                    (
                        "Signals accepted the post, but confirmation failed. "
                        "Do not retry; staff reconciliation is required."
                    ),
                )
                return

            state.clear()
            state.update(latest_state)

            # Change the review button from green "Send to Signals"
            # to a gray disabled "Sent" button.
            await mark_review_as_sent(
                self.review_message_id
            )

            log_message = (
                f"✅ **{candidate['symbol']}** sent to Signals "
                f"by **{interaction.user}** with Trade Thesis "
                "and uploaded chart."
            )

            await write_bot_log(
                log_message
            )

            # Remove the ephemeral "thinking" response so earnings-review
            # stays visually clean. Operational confirmation lives in bot-log.
            try:
                await interaction.delete_original_response()
            except Exception:
                pass

            print(
                f"{candidate['symbol']} sent to "
                "Signals from the modal with the "
                "user's thesis and uploaded chart.",
                flush=True,
            )

    class EarningsReviewView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(
            label="Send to Signals",
            emoji="📣",
            style=discord.ButtonStyle.success,
            custom_id="earnings_send_to_signals",
            row=0,
        )
        async def send_to_signals(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ):
            user = getattr(interaction, "user", None)
            guild = getattr(interaction, "guild", None)
            channel_id = getattr(interaction, "channel_id", None)
            message = getattr(interaction, "message", None)
            message_id = getattr(message, "id", None)

            if not can_clear_earnings_review(user, guild):
                await send_ephemeral_rejection(
                    interaction,
                    "You cannot use this earnings review action.",
                )
                return

            try:
                state = load_state()
            except EarningsStateError:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is currently unavailable.",
                )
                return

            found = validated_review_state_item(
                state,
                message_id,
                channel_id,
                review_channel_id,
            )

            if found is None or not is_valid_review_message_provenance(
                message,
                message_id=message_id,
                channel_id=review_channel_id,
                bot_user_id=getattr(client.user, "id", None),
                normal_message_type=discord.MessageType.default,
            ):
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is no longer available.",
                )
                return

            _token, item = found
            current_status = signal_delivery_status(item)
            if current_status != SIGNAL_DELIVERY_READY:
                await send_ephemeral_rejection(
                    interaction,
                    signal_delivery_rejection_message(current_status),
                )
                return
            print(
                "Send to Signals clicked:",
                message_id,
                user,
                flush=True,
            )

            message_text = str(getattr(message, "content", "") or "")
            symbol = "Trade"
            for raw_line in message_text.splitlines():
                line = raw_line.strip().replace("**", "")
                if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", line):
                    symbol = line
                    break

            candidate_symbol = str(
                item["candidate"].get("symbol") or ""
            ).strip()
            if candidate_symbol:
                symbol = candidate_symbol

            if interaction_response_is_done(interaction):
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is no longer available.",
                )
                return

            try:
                await interaction.response.send_modal(
                    TradeThesisModal(
                        str(message_id),
                        symbol,
                        str(getattr(user, "id", "")),
                    )
                )
                print(
                    "Trade Thesis modal opened for:",
                    message_id,
                    symbol,
                    flush=True,
                )
            except discord.InteractionResponded:
                await send_ephemeral_rejection(
                    interaction,
                    "This earnings review is no longer available.",
                )
                return
            except Exception as exc:
                print(
                    "ERROR opening Trade Thesis modal:",
                    repr(exc),
                    flush=True,
                )
                raise

    @command_tree.command(
        name="new-signal",
        description="Create a staff-reviewed Signals draft.",
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(manage_messages=True)
    async def new_signal(interaction: discord.Interaction) -> None:
        if (
            drafts_channel_id is None
            or not can_clear_earnings_review(
                getattr(interaction, "user", None),
                getattr(interaction, "guild", None),
            )
            or discord_id_text(getattr(interaction, "channel_id", None))
            != str(drafts_channel_id)
        ):
            await send_ephemeral_rejection(
                interaction,
                "This command is not available here.",
            )
            return
        if interaction_response_is_done(interaction):
            await send_ephemeral_rejection(
                interaction,
                "This command is no longer available.",
            )
            return
        await interaction.response.send_modal(
            ManualSignalModal(str(interaction.user.id))
        )

    @command_tree.command(
        name="clear-earnings-review",
        description="Clear bot posts from earnings review.",
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(
        manage_messages=True,
    )
    async def clear_earnings_review(
        interaction: discord.Interaction,
    ) -> None:
        if not can_clear_earnings_review(
            interaction.user,
            interaction.guild,
        ):
            await interaction.response.send_message(
                (
                    "You need server owner, Administrator, or "
                    "Manage Messages permission to use this command."
                ),
                ephemeral=True,
            )
            return

        if not is_configured_review_channel(
            interaction.channel_id,
            review_channel_id,
        ):
            await interaction.response.send_message(
                "This command only works in the earnings-review channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )

        review_channel = client.get_channel(
            review_channel_id
        )

        if review_channel is None:
            try:
                review_channel = await client.fetch_channel(
                    review_channel_id
                )
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "I could not access the earnings-review channel.",
                    ephemeral=True,
                )
                return

        try:
            candidates, skipped = await collect_safe_review_messages(
                review_channel,
                client.user.id,
                discord.MessageType.default,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not read the earnings-review channel history.",
                ephemeral=True,
            )
            return

        results = await delete_review_messages_safely(
            review_channel,
            candidates,
            now_utc=datetime.now(timezone.utc),
            http_error_types=(
                discord.Forbidden,
                discord.HTTPException,
            ),
            not_found_type=discord.NotFound,
        )

        total_skipped = skipped + results["missing"]

        await interaction.followup.send(
            (
                f"Deleted {results['deleted']} earnings "
                f"review message(s). Skipped {total_skipped}; "
                f"failed {results['failed']}."
            ),
            ephemeral=True,
        )

    @client.event
    async def on_ready():
        nonlocal command_sync_attempted, post_signal_review_task_started

        if not command_sync_attempted:
            command_sync_attempted = True

            try:
                synced_commands = await command_tree.sync()
            except Exception as exc:
                print(
                    "Could not sync Earnings Review slash commands:",
                    repr(exc),
                    flush=True,
                )
            else:
                print(
                    f"Synced {len(synced_commands)} Earnings Review "
                    "slash command(s).",
                    flush=True,
                )

        if (
            signal_review_drafts_channel_id is not None
            and signal_reviews_channel_id is not None
            and not post_signal_review_task_started
        ):
            post_signal_review_task_started = True
            asyncio.create_task(post_signal_review_scheduler())
            print("Post-Signal Review scheduler started.", flush=True)

        print(
            "Earnings Review workflow bot connected as "
            f"{client.user}."
        )
        print(
            "Persistent Send to Signals button registered."
        )
        print(
            "Waiting for Trade Signal form submissions."
        )

        bot_log = await get_bot_log_channel()

        if bot_log is not None:
            print(
                f"Bot logs will be sent to #{bot_log.name}.",
                flush=True,
            )
        else:
            print(
                "No bot-log channel found; operational logs "
                "will remain in the terminal.",
                flush=True,
            )

    # Register once, globally, so buttons continue to work even though
    # review messages are posted by a separate process through REST.
    client.add_view(ManualSignalDraftView())
    client.add_view(EarningsReviewView())
    if (
        signal_review_drafts_channel_id is not None
        and signal_reviews_channel_id is not None
    ):
        client.add_view(PostSignalReviewView())

    await client.start(bot_token)

def discord_retry_seconds(
    exc: urllib.error.HTTPError,
    attempt: int,
) -> float:
    retry_after = exc.headers.get("Retry-After", "")

    try:
        if retry_after:
            return max(float(retry_after), 1.0)
    except ValueError:
        pass

    try:
        body = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
        seconds = float(payload.get("retry_after", 0))
        if seconds > 0:
            return seconds
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return float(2 ** attempt)


def send_discord_message(
    webhook_url: str,
    message: str,
    username: str,
    *,
    chart_symbol: str | None = None,
) -> str | None:
    payload_data = {
        "username": username,
        "embeds": [
            bordered_embed(
                message,
                color=BRAND_NEON_PINK,
            )
        ],
        "allowed_mentions": {"parse": []},
    }
    chart_path: Path | None = None

    try:
        if chart_symbol is None:
            payload = json.dumps(payload_data).encode("utf-8")
            content_type = "application/json"
        else:
            try:
                chart_path = temporary_weekly_chart_path(chart_symbol)
                chart_path = generate_weekly_chart(
                    chart_symbol,
                    output_path=chart_path,
                )
                attachment_name = weekly_chart_filename(chart_symbol)
                payload_data["embeds"][0]["image"] = {
                    "url": f"attachment://{attachment_name}",
                }
                payload_data["attachments"] = [
                    {
                        "id": 0,
                        "filename": attachment_name,
                        "description": f"{chart_symbol} weekly chart",
                    }
                ]
                payload, boundary = multipart_body(
                    payload=payload_data,
                    file_path=chart_path,
                    file_name=attachment_name,
                )
                content_type = f"multipart/form-data; boundary={boundary}"
            except asyncio.CancelledError as exc:
                raise PublicChartPreparationCancelled(
                    "Public chart preparation was canceled."
                ) from exc
            except Exception as exc:
                raise PublicChartPreparationError(
                    "Public chart preparation failed."
                ) from exc

        for attempt in range(1, MAX_DISCORD_ATTEMPTS + 1):
            request = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={
                    "Content-Type": content_type,
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    if response.status not in (200, 204):
                        raise AmbiguousDeliveryError(
                            f"Discord returned HTTP {response.status}."
                        )
                    if response.status == 200:
                        try:
                            response_payload = json.loads(
                                response.read().decode("utf-8")
                            )
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            return None
                        if isinstance(response_payload, dict):
                            message_id = response_payload.get("id")
                            if message_id:
                                return str(message_id)
                    return None

            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt >= MAX_DISCORD_ATTEMPTS:
                    body = exc.read().decode("utf-8", errors="replace")
                    raise DefiniteDeliveryError(
                        f"Discord returned HTTP {exc.code}: {body}"
                    ) from exc

                wait_seconds = discord_retry_seconds(exc, attempt)
                print(
                    "Discord rate limit reached. "
                    f"Waiting {wait_seconds:.1f} seconds..."
                )
                time.sleep(wait_seconds)

        raise DefiniteDeliveryError("Discord post failed after retries.")
    finally:
        if chart_path is not None:
            cleanup_weekly_chart(chart_path)


def earnings_state_store() -> EarningsStateStore:
    return EarningsStateStore(STATE_FILE)


def load_state() -> dict[str, Any]:
    return earnings_state_store().load()


def update_state(
    mutation: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    state, _result = earnings_state_store().transaction(
        mutation
    )
    return state


def set_state_record(
    section: str,
    key: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    stored_value = copy.deepcopy(value)

    def mutation(state: dict[str, Any]) -> None:
        state[section][key] = stored_value

    return update_state(mutation)


def feed_delivery_status(
    record: Any,
    *,
    feed: str | None = None,
    report_key_value: str | None = None,
) -> str | None:
    """Resolve feed delivery state while treating legacy records as sent."""
    if record is None:
        return None
    if not isinstance(record, dict):
        return FEED_DELIVERY_INVALID

    if "delivery_status" not in record:
        symbol = record.get("symbol")
        posted_at = record.get("posted_at")
        if (
            not isinstance(symbol, str)
            or not symbol.strip()
            or not isinstance(posted_at, str)
            or not posted_at.strip()
            or parse_iso_datetime(posted_at) is None
        ):
            return FEED_DELIVERY_INVALID
        return FEED_DELIVERY_CONFIRMED

    status = record["delivery_status"]
    if status not in FEED_DELIVERY_STATUSES:
        return FEED_DELIVERY_INVALID

    required_strings = (
        "feed",
        "report_key",
        "symbol",
        "delivery_attempt_id",
        "reserved_at",
    )
    if any(
        not isinstance(record.get(field), str)
        or not record[field].strip()
        for field in required_strings
    ):
        return FEED_DELIVERY_INVALID
    if parse_iso_datetime(record["reserved_at"]) is None:
        return FEED_DELIVERY_INVALID
    if feed is not None and record["feed"] != feed:
        return FEED_DELIVERY_INVALID
    if (
        report_key_value is not None
        and record["report_key"] != report_key_value
    ):
        return FEED_DELIVERY_INVALID

    return status


def reserve_feed_delivery(
    feed: str,
    key: str,
    symbol: str,
    *,
    force: bool = False,
    attempt_id: str | None = None,
    reserved_at: str | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Atomically reserve one public/private delivery before external work."""
    if feed not in {"public", "private"}:
        raise ValueError(f"Unsupported earnings feed: {feed}")

    new_attempt_id = attempt_id or uuid.uuid4().hex
    reservation_time = reserved_at or datetime.now(EASTERN).isoformat()
    outcome: str | None = None
    claimed = False

    def mutation(state: dict[str, Any]) -> None:
        nonlocal claimed, outcome
        existing = state[feed].get(key)
        status = feed_delivery_status(
            existing,
            feed=feed,
            report_key_value=key,
        )

        claimable = status in (None, FEED_DELIVERY_FAILED)
        if status == FEED_DELIVERY_CONFIRMED and force:
            claimable = True

        if not claimable:
            outcome = status or FEED_DELIVERY_INVALID
            return

        record = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        record.update(
            {
                "delivery_status": FEED_DELIVERY_RESERVED,
                "delivery_attempt_id": new_attempt_id,
                "reserved_at": reservation_time,
                "feed": feed,
                "report_key": key,
                "symbol": symbol,
            }
        )
        for field in (
            "confirmed_at",
            "failed_at",
            "unknown_at",
            "delivery_error",
            "discord_message_id",
        ):
            record.pop(field, None)
        state[feed][key] = record
        claimed = True
        outcome = FEED_DELIVERY_RESERVED

    state, _result = earnings_state_store().transaction(mutation)
    return (
        state,
        outcome or FEED_DELIVERY_INVALID,
        new_attempt_id if claimed else None,
    )


def transition_feed_delivery(
    feed: str,
    key: str,
    attempt_id: str,
    status: str,
    *,
    discord_message_id: str | None = None,
    error: str | None = None,
    finished_at: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Finish only the reservation owned by the matching delivery attempt."""
    if status not in {
        FEED_DELIVERY_CONFIRMED,
        FEED_DELIVERY_FAILED,
        FEED_DELIVERY_UNKNOWN,
    }:
        raise ValueError(f"Unsupported feed delivery transition: {status}")

    transition_time = finished_at or datetime.now(EASTERN).isoformat()
    transitioned = False

    def mutation(state: dict[str, Any]) -> None:
        nonlocal transitioned
        record = state[feed].get(key)
        if (
            feed_delivery_status(
                record,
                feed=feed,
                report_key_value=key,
            )
            != FEED_DELIVERY_RESERVED
            or record.get("delivery_attempt_id") != attempt_id
        ):
            return

        record["delivery_status"] = status
        timestamp_field = {
            FEED_DELIVERY_CONFIRMED: "confirmed_at",
            FEED_DELIVERY_FAILED: "failed_at",
            FEED_DELIVERY_UNKNOWN: "unknown_at",
        }[status]
        record[timestamp_field] = transition_time

        if status == FEED_DELIVERY_CONFIRMED:
            record["posted_at"] = transition_time
            record.pop("delivery_error", None)
            if discord_message_id:
                record["discord_message_id"] = str(discord_message_id)
        elif error:
            record["delivery_error"] = error

        transitioned = True

    state, _result = earnings_state_store().transaction(mutation)
    return state, transitioned


def failed_feed_delivery_status(exc: BaseException) -> str:
    """Classify whether Discord acceptance is known or ambiguous."""
    if isinstance(
        exc,
        (
            AmbiguousDeliveryError,
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            asyncio.CancelledError,
            json.JSONDecodeError,
            EarningsStateError,
        ),
    ):
        return FEED_DELIVERY_UNKNOWN
    return FEED_DELIVERY_FAILED


def concise_delivery_error(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "delivery_cancelled"
    if failed_feed_delivery_status(exc) == FEED_DELIVERY_UNKNOWN:
        return "delivery_outcome_ambiguous"
    return "delivery_rejected"


def persist_quote_cache_changes(
    original_quotes: dict[str, Any],
    updated_quotes: dict[str, Any],
) -> dict[str, Any]:
    changed_quotes = {
        key: copy.deepcopy(value)
        for key, value in updated_quotes.items()
        if original_quotes.get(key) != value
    }

    def mutation(state: dict[str, Any]) -> None:
        prune_quote_cache(state)
        state["quotes"].update(changed_quotes)

    return update_state(mutation)



def quote_cache_key(
    target_date: str,
    symbol: str,
) -> str:
    return f"{target_date}:{symbol.upper()}"


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)

    return parsed


def cached_quote(
    state: dict[str, Any],
    target_date: str,
    symbol: str,
    *,
    max_age_minutes: float,
) -> dict[str, Any] | None:
    cache = state.setdefault("quotes", {})
    item = cache.get(quote_cache_key(target_date, symbol))

    if not isinstance(item, dict):
        return None

    quote = item.get("quote")
    fetched_at = parse_iso_datetime(item.get("fetched_at"))

    if not isinstance(quote, dict) or fetched_at is None:
        return None

    age = datetime.now(EASTERN) - fetched_at

    if age > timedelta(minutes=max_age_minutes):
        return None

    return quote


def store_cached_quote(
    state: dict[str, Any],
    target_date: str,
    symbol: str,
    quote: dict[str, Any],
) -> None:
    state.setdefault("quotes", {})[
        quote_cache_key(target_date, symbol)
    ] = {
        "fetched_at": datetime.now(EASTERN).isoformat(),
        "quote": quote,
    }


def prune_quote_cache(
    state: dict[str, Any],
    *,
    keep_days: int = 3,
) -> None:
    cache = state.setdefault("quotes", {})
    cutoff = datetime.now(EASTERN) - timedelta(days=keep_days)

    stale_keys: list[str] = []

    for key, item in cache.items():
        if not isinstance(item, dict):
            stale_keys.append(key)
            continue

        fetched_at = parse_iso_datetime(item.get("fetched_at"))

        if fetched_at is None or fetched_at < cutoff:
            stale_keys.append(key)

    for key in stale_keys:
        cache.pop(key, None)


def prequote_priority_score(
    report: dict[str, Any],
) -> float:
    """
    Decide which earnings reports deserve a live quote first.

    This does NOT decide whether a stock qualifies for Discord.
    It only orders the quote work so high-value names and large
    earnings surprises are checked before low-information reports.
    """
    symbol = str(report.get("symbol") or "").upper()

    eps_surprise = surprise_percent(
        report.get("epsActual"),
        report.get("epsEstimate"),
    )
    revenue_surprise = surprise_percent(
        report.get("revenueActual"),
        report.get("revenueEstimate"),
    )

    score = 0.0

    if symbol in PRIORITY_TICKERS:
        score += 1000.0

    if eps_surprise is not None:
        score += min(abs(eps_surprise), 250.0)

    if revenue_surprise is not None:
        score += min(abs(revenue_surprise) * 3.0, 250.0)

    # Reports with both actual and estimate data are more useful.
    if safe_number(report.get("epsActual")) is not None and safe_number(
        report.get("epsEstimate")
    ) is not None:
        score += 20.0

    if safe_number(report.get("revenueActual")) is not None and safe_number(
        report.get("revenueEstimate")
    ) is not None:
        score += 20.0

    return score


def build_candidates_optimized(
    reports: list[dict[str, Any]],
    target_date: str,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, int]:
    """
    Build candidates without hammering Finnhub on every run.

    Fresh cached quotes are reused. Only a bounded number of stale/missing
    quotes are requested from Finnhub, with priority names and the largest
    earnings surprises checked first.
    """
    quote_delay = env_float(
        "EARNINGS_QUOTE_DELAY_SECONDS",
        1.1,
    )
    cache_minutes = env_float(
        "EARNINGS_QUOTE_CACHE_MINUTES",
        20,
    )
    max_quote_calls = env_int(
        "EARNINGS_MAX_QUOTE_CALLS_PER_RUN",
        120,
    )

    if cache_minutes < 0:
        raise RuntimeError(
            "EARNINGS_QUOTE_CACHE_MINUTES cannot be negative."
        )

    if max_quote_calls < 1:
        raise RuntimeError(
            "EARNINGS_MAX_QUOTE_CALLS_PER_RUN must be at least 1."
        )

    prune_quote_cache(state)

    ordered_reports = sorted(
        reports,
        key=prequote_priority_score,
        reverse=True,
    )

    candidates: list[dict[str, Any]] = []
    quote_calls = 0
    cache_hits = 0
    skipped_for_budget = 0

    for index, report in enumerate(ordered_reports, start=1):
        symbol = str(report["symbol"])

        quote = cached_quote(
            state,
            target_date,
            symbol,
            max_age_minutes=cache_minutes,
        )

        if quote is not None:
            cache_hits += 1
            print(
                f"Quote {index}/{len(ordered_reports)}: "
                f"{symbol} [cache]"
            )
        else:
            if quote_calls >= max_quote_calls:
                skipped_for_budget += 1
                continue

            quote_calls += 1
            print(
                f"Quote {index}/{len(ordered_reports)}: "
                f"{symbol} [Finnhub {quote_calls}/{max_quote_calls}]"
            )

            try:
                quote = get_quote_with_retry(symbol)
            except RuntimeError as exc:
                print(
                    f"Could not retrieve quote for {symbol}: {exc}"
                )
                quote = {}

            store_cached_quote(
                state,
                target_date,
                symbol,
                quote,
            )

            # Delay only after a real Finnhub request, never for cache hits.
            if quote_calls < max_quote_calls:
                time.sleep(quote_delay)

        candidates.append(
            calculate_candidate(
                report,
                quote,
            )
        )

    if skipped_for_budget:
        print(
            f"Quote budget reached: {skipped_for_budget} lower-priority "
            "report(s) deferred to a later run."
        )

    return candidates, quote_calls, cache_hits


def report_key(
    report: dict[str, Any],
) -> str:
    return ":".join(
        [
            str(report.get("date") or ""),
            str(report.get("symbol") or "").upper(),
            str(report.get("year") or ""),
            str(report.get("quarter") or ""),
        ]
    )


def print_preview_list(
    title: str,
    candidates: list[dict[str, Any]],
    limit: int,
) -> None:
    print()
    print(title)
    print("=" * len(title))

    if not candidates:
        print("No qualifying candidates.")
        return

    for rank, candidate in enumerate(
        candidates[:limit],
        start=1,
    ):
        move = candidate["move_percent"]

        print(
            f"{rank:>2}. "
            f"{candidate['symbol']:<7} "
            f"{move:+7.2f}%  "
            f"Score {candidate['score']:>7.1f}"
        )

    if len(candidates) > limit:
        print(
            f"... plus {len(candidates) - limit} more."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create ranked public and private earnings feeds."
        )
    )

    parser.add_argument(
        "--date",
        help=(
            "Target date in YYYY-MM-DD format. "
            "If omitted, automatically uses the previous U.S. "
            "weekday before the early-morning Eastern cutoff."
        ),
    )

    mode = parser.add_mutually_exclusive_group()

    mode.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Preview rankings and sample Discord messages "
            "without posting or changing state."
        ),
    )

    mode.add_argument(
        "--post",
        action="store_true",
        help="Post qualifying earnings messages to Discord.",
    )

    mode.add_argument(
        "--review-bot",
        action="store_true",
        help=(
            "Run the persistent Discord listener "
            "for Send to Signals buttons."
        ),
    )

    mode.add_argument(
        "--private-test",
        action="store_true",
        help=(
            "Post one private earnings-review test with "
            "weekly chart and Send to Signals button. "
            "Does not scan Finnhub earnings."
        ),
    )

    parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="Maximum candidates shown in each preview list.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Maximum number of posts per feed for this run. "
            "Example: --limit 1 posts at most one private review "
            "and one public reaction."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore saved posting state.",
    )

    arguments = parser.parse_args()

    if not (
        arguments.preview
        or arguments.post
        or arguments.review_bot
        or arguments.private_test
    ):
        parser.print_help()
        return

    if arguments.review_bot:
        asyncio.run(
            run_review_button_bot()
        )
        return

    if arguments.private_test:
        run_private_test()
        return

    if arguments.preview_limit < 1:
        parser.error("--preview-limit must be at least 1")

    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be at least 1")

    if arguments.force and not arguments.post:
        parser.error("--force can only be used with --post")

    now_eastern = datetime.now(EASTERN)
    today_eastern = now_eastern.date().isoformat()

    if arguments.date:
        target_date = arguments.date
        date_reason = "explicit --date override"
    else:
        target_date, date_reason = resolve_automatic_target_date(
            now_eastern
        )

    print(
        f"Earnings target date: {target_date} "
        f"[{date_reason}]"
    )

    if arguments.date and target_date != today_eastern:
        print(
            "WARNING: Finnhub quote movement is current, "
            "not historical. Historical date testing validates "
            "filtering and formatting only."
        )

    state = load_state()
    original_quotes = copy.deepcopy(
        state["quotes"]
    )

    reports = get_completed_reports(target_date)

    candidates, quote_calls, cache_hits = build_candidates_optimized(
        reports,
        target_date,
        state,
    )

    # Save quote cache even in preview mode. This is operational cache only;
    # preview still does not change Discord posting state.
    state = persist_quote_cache_changes(
        original_quotes,
        state["quotes"],
    )

    private_candidates = sorted(
        [
            candidate
            for candidate in candidates
            if qualifies_for_private(candidate)
        ],
        key=lambda item: (
            item["score"],
            abs(item["move_percent"] or 0),
        ),
        reverse=True,
    )

    public_candidates = sorted(
        [
            candidate
            for candidate in candidates
            if qualifies_for_public(candidate)
        ],
        key=lambda item: (
            is_priority_candidate(item),
            item["score"],
            abs(item["move_percent"] or 0),
        ),
        reverse=True,
    )

    private_max = env_int(
        "EARNINGS_PRIVATE_MAX",
        50,
    )

    public_max = min(
        env_int("EARNINGS_PUBLIC_MAX", 15),
        15,
    )

    private_candidates = private_candidates[:private_max]
    public_candidates = public_candidates[:public_max]

    if arguments.preview:
        print_preview_list(
            "PRIVATE EARNINGS REVIEW",
            private_candidates,
            arguments.preview_limit,
        )

        print_preview_list(
            "PUBLIC EARNINGS REACTIONS",
            public_candidates,
            arguments.preview_limit,
        )

        if public_candidates:
            print()
            print("SAMPLE PUBLIC DISCORD POST")
            print("=" * 26)
            print(build_public_message(public_candidates[0]))

        if private_candidates:
            print()
            print("SAMPLE PRIVATE REVIEW POST")
            print("=" * 26)
            print(build_private_message(private_candidates[0], 1))

        print()
        print(
            f"Completed reports: {len(reports)} | "
            f"Finnhub quote calls: {quote_calls} | "
            f"Quote cache hits: {cache_hits} | "
            f"Private review: {len(private_candidates)} | "
            f"Public reactions: {len(public_candidates)}"
        )
        print()
        print("PREVIEW ONLY — nothing was posted; posting state was not changed.")

        return

    public_webhook = required_env(
        "EARNINGS_REACTIONS_WEBHOOK"
    )

    private_webhook = os.getenv(
        "EARNINGS_REVIEW_WEBHOOK",
        "",
    ).strip()

    private_posted = 0
    public_posted = 0

    if private_webhook:
        private_to_post = (
            private_candidates[:arguments.limit]
            if arguments.limit is not None
            else private_candidates
        )

        for rank, candidate in enumerate(
            private_to_post,
            start=1,
        ):
            key = report_key(candidate["report"])

            state, reservation_status, attempt_id = reserve_feed_delivery(
                "private",
                key,
                candidate["symbol"],
                force=arguments.force,
            )
            if (
                reservation_status != FEED_DELIVERY_RESERVED
                or attempt_id is None
            ):
                continue

            try:
                message_id = send_private_review_with_chart(
                    candidate,
                    rank,
                    state,
                )
            except asyncio.CancelledError as exc:
                transition_feed_delivery(
                    "private",
                    key,
                    attempt_id,
                    FEED_DELIVERY_UNKNOWN,
                    error=concise_delivery_error(exc),
                )
                raise
            except Exception as exc:
                transition_feed_delivery(
                    "private",
                    key,
                    attempt_id,
                    failed_feed_delivery_status(exc),
                    error=concise_delivery_error(exc),
                )
                raise

            state, _confirmed = transition_feed_delivery(
                "private",
                key,
                attempt_id,
                FEED_DELIVERY_CONFIRMED,
                discord_message_id=message_id,
            )
            if not _confirmed:
                raise EarningsStateError(
                    "Private delivery confirmation no longer matches "
                    "its reservation."
                )
            private_posted += 1
            time.sleep(DISCORD_POST_DELAY_SECONDS)

    public_to_post = (
        public_candidates[:arguments.limit]
        if arguments.limit is not None
        else public_candidates
    )

    for candidate in public_to_post:
        key = report_key(candidate["report"])

        state, reservation_status, attempt_id = reserve_feed_delivery(
            "public",
            key,
            candidate["symbol"],
            force=arguments.force,
        )
        if (
            reservation_status != FEED_DELIVERY_RESERVED
            or attempt_id is None
        ):
            continue

        try:
            message_id = send_discord_message(
                public_webhook,
                build_public_message(candidate),
                PUBLIC_WEBHOOK_USERNAME,
                chart_symbol=candidate["symbol"],
            )
        except asyncio.CancelledError as exc:
            transition_feed_delivery(
                "public",
                key,
                attempt_id,
                (
                    FEED_DELIVERY_FAILED
                    if isinstance(exc, PublicChartPreparationCancelled)
                    else FEED_DELIVERY_UNKNOWN
                ),
                error=concise_delivery_error(exc),
            )
            raise
        except Exception as exc:
            transition_feed_delivery(
                "public",
                key,
                attempt_id,
                failed_feed_delivery_status(exc),
                error=concise_delivery_error(exc),
            )
            raise

        state, _confirmed = transition_feed_delivery(
            "public",
            key,
            attempt_id,
            FEED_DELIVERY_CONFIRMED,
            discord_message_id=message_id,
        )
        if not _confirmed:
            raise EarningsStateError(
                "Public delivery confirmation no longer matches "
                "its reservation."
            )
        public_posted += 1
        time.sleep(DISCORD_POST_DELAY_SECONDS)

    print(
        f"Earnings run complete for {target_date}: "
        f"{len(reports)} completed, "
        f"{len(private_candidates)} private candidates, "
        f"{len(public_candidates)} public candidates, "
        f"{private_posted} private posted, "
        f"{public_posted} public posted."
    )


if __name__ == "__main__":
    main()
