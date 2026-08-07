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
    EARNINGS_PUBLIC_MOVE_PCT=8
    EARNINGS_PRIORITY_PRIVATE_MOVE_PCT=3
    EARNINGS_PRIORITY_PUBLIC_MOVE_PCT=5
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
import hashlib
import tempfile
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


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


PRIORITY_TICKERS = {
    "AAPL",
    "ABNB",
    "ADBE",
    "AFRM",
    "AMD",
    "AMZN",
    "ARM",
    "AVGO",
    "BA",
    "BAC",
    "C",
    "CAT",
    "COIN",
    "COST",
    "CRM",
    "CRWD",
    "CVNA",
    "DASH",
    "DIS",
    "DKNG",
    "GOOG",
    "GOOGL",
    "GS",
    "HD",
    "HIMS",
    "HOOD",
    "INTC",
    "JPM",
    "LLY",
    "LULU",
    "MA",
    "MARA",
    "META",
    "MS",
    "MSFT",
    "MU",
    "NFLX",
    "NKE",
    "NVDA",
    "ORCL",
    "PANW",
    "PINS",
    "PLTR",
    "PYPL",
    "QCOM",
    "RBLX",
    "RIOT",
    "RIVN",
    "ROKU",
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
    "WFC",
    "WMT",
    "XOM",
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

    public_move = env_float(
        "EARNINGS_PUBLIC_MOVE_PCT",
        8,
    )

    priority_public_move = env_float(
        "EARNINGS_PRIORITY_PUBLIC_MOVE_PCT",
        5,
    )

    if absolute_move >= public_move:
        return True

    return (
        candidate["priority"]
        and absolute_move >= priority_public_move
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
            DIVIDER,
            "",
            "*Reported earnings data — not a trade signal.*",
        ]
    )


def build_private_message(
    candidate: dict[str, Any],
    rank: int,
) -> str:
    public_message = build_public_message(candidate)

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
            "range": "2y",
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


def aggregate_weekly_candles(
    daily_candles: list[
        dict[str, float]
    ],
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

    return weekly[
        -WEEKLY_CHART_WEEKS:
    ]


def generate_weekly_chart(
    symbol: str,
) -> Path:
    """
    Generate the private review-only weekly candlestick chart.

    This chart is only for scanning inside earnings-review.
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
    weekly = aggregate_weekly_candles(daily)

    if len(weekly) < 4:
        raise RuntimeError(
            f"Not enough weekly chart history for {symbol}."
        )

    chart_dir = PROJECT_ROOT / "data" / "earnings_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)

    output_path = chart_dir / f"{symbol.upper()}_weekly.png"

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
            f'filename="{file_path.name}"'
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

    payload = {
        "content": build_private_message(
            candidate,
            rank,
        ),
        "attachments": [
            {
                "id": 0,
                "filename": (
                    chart_path.name
                ),
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
            }
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

        raise RuntimeError(
            "Discord bot could not post the "
            "private earnings review: "
            f"HTTP {exc.code}: {error_body}"
        ) from exc

    message_id = str(
        response_payload.get("id")
        or ""
    )

    state.setdefault(
        "signal_queue",
        {},
    )[token] = {
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
    }

    save_state(
        state
    )

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
    state = load_state()
    candidate = private_test_candidate()

    message_id = send_private_review_with_chart(
        candidate,
        1,
        state,
    )

    print(
        "Private earnings review test posted "
        f"successfully. Message ID: {message_id}"
    )


def build_signal_message(
    candidate: dict[str, Any],
    trade_thesis: str,
) -> str:
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

    return "\n".join(
        [
            "# 📈 Trade Signal",
            "",
            f"## {candidate['symbol']}",
            "",
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
            DIVIDER,
            "",
            "📊 **Trade Chart**",
            "",
            "*Chart and thesis provided by Main Line Trades.*",
            "",
            "⚠️ **Manage risk. This is not financial advice.**",
        ]
    )


def find_signal_item_by_review_message(
    state: dict[str, Any],
    message_id: str,
) -> tuple[str, dict[str, Any]] | None:
    for token, item in state.setdefault("signal_queue", {}).items():
        if not isinstance(item, dict):
            continue

        if str(item.get("review_message_id") or "") == str(message_id):
            return token, item

    return None


def handled_review_message_ids(
    state: dict[str, Any],
    review_channel_id: int,
) -> list[int]:
    """Return unique handled review IDs for the configured channel."""
    signal_queue = state.get("signal_queue")

    if not isinstance(signal_queue, dict):
        return []

    message_ids: list[int] = []
    seen: set[int] = set()

    for item in signal_queue.values():
        if not isinstance(item, dict):
            continue

        if item.get("sent_to_signals") is not True:
            continue

        if str(item.get("review_channel_id") or "") != str(
            review_channel_id
        ):
            continue

        raw_message_id = str(
            item.get("review_message_id") or ""
        ).strip()

        if not raw_message_id.isdigit():
            continue

        message_id = int(raw_message_id)

        if message_id not in seen:
            seen.add(message_id)
            message_ids.append(message_id)

    return message_ids


def can_clear_earnings_review(
    user: Any,
    guild: Any,
) -> bool:
    """Authorize the guild owner or a member with moderation rights."""
    if user is None or guild is None:
        return False

    if str(getattr(guild, "owner_id", "")) == str(
        getattr(user, "id", "")
    ):
        return True

    permissions = getattr(user, "guild_permissions", None)

    if permissions is None:
        return False

    return bool(
        getattr(permissions, "administrator", False)
        or getattr(permissions, "manage_messages", False)
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

    bot_token = required_env("DISCORD_BOT_TOKEN")
    signals_channel_id = int(required_env("SIGNALS_CHANNEL_ID"))

    review_channel_id = int(
        resolve_webhook_channel_id(
            required_env("EARNINGS_REVIEW_WEBHOOK")
        )
    )

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    command_tree = discord.app_commands.CommandTree(client)
    command_sync_attempted = False

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

    class TradeThesisModal(discord.ui.Modal):
        def __init__(
            self,
            review_message_id: str,
            symbol: str,
        ):
            super().__init__(
                title=f"{symbol} Trade Signal"
            )

            self.review_message_id = str(
                review_message_id
            )
            self.symbol = symbol

            # Components V2 modal: thesis and chart live in the SAME form.
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

            self.trade_chart = discord.ui.FileUpload(
                custom_id="trade_chart",
                required=True,
                min_values=1,
                max_values=1,
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
            # Acknowledge immediately because converting an attachment and
            # posting it can take longer than Discord's interaction window.
            await interaction.response.defer(
                ephemeral=True,
                thinking=True,
            )

            state = load_state()

            found = find_signal_item_by_review_message(
                state,
                self.review_message_id,
            )

            if found is None:
                await interaction.followup.send(
                    (
                        "⚠️ I could not find the saved "
                        "data for this earnings review. "
                        "Please create a fresh review test."
                    ),
                    ephemeral=True,
                )
                return

            token, item = found

            if item.get(
                "sent_to_signals"
            ):
                await interaction.followup.send(
                    (
                        "✅ This setup was already "
                        "sent to Signals."
                    ),
                    ephemeral=True,
                )
                return

            candidate = item.get(
                "candidate"
            )

            if not isinstance(
                candidate,
                dict,
            ):
                await interaction.followup.send(
                    (
                        "⚠️ The saved earnings review "
                        "data is invalid."
                    ),
                    ephemeral=True,
                )
                return

            thesis = str(
                self.trade_thesis.value
            ).strip()

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

            signals_channel = (
                client.get_channel(
                    signals_channel_id
                )
            )

            if signals_channel is None:
                try:
                    signals_channel = (
                        await client.fetch_channel(
                            signals_channel_id
                        )
                    )
                except discord.Forbidden:
                    await interaction.followup.send(
                        (
                            "⚠️ The bot does not have "
                            "access to the Signals channel. "
                            "Give Main Line Trades Feed Filter "
                            "View Channel, Send Messages, "
                            "Embed Links, and Attach Files "
                            "permissions in Signals."
                        ),
                        ephemeral=True,
                    )
                    return

            try:
                signal_file = (
                    await attachment.to_file(
                        filename=(
                            f"{candidate['symbol']}"
                            "_trade_chart"
                            f"{Path(attachment.filename).suffix or '.png'}"
                        )
                    )
                )

                await signals_channel.send(
                    content=build_signal_message(
                        candidate,
                        thesis,
                    ),
                    file=signal_file,
                    allowed_mentions=(
                        discord.AllowedMentions.none()
                    ),
                )

            except discord.Forbidden:
                await interaction.followup.send(
                    (
                        "⚠️ The bot can see Signals but "
                        "cannot post there. Give Main Line "
                        "Trades Feed Filter Send Messages "
                        "and Attach Files permissions in "
                        "the Signals channel."
                    ),
                    ephemeral=True,
                )
                return

            except discord.HTTPException as exc:
                print(
                    "Discord signal post failed:",
                    repr(exc),
                    flush=True,
                )

                await interaction.followup.send(
                    (
                        "⚠️ Discord rejected the Signals "
                        "post. Check the listener terminal "
                        "for the exact error."
                    ),
                    ephemeral=True,
                )
                return

            item[
                "trade_thesis"
            ] = thesis

            item[
                "sent_to_signals"
            ] = True

            item[
                "sent_at"
            ] = datetime.now(
                EASTERN
            ).isoformat()

            item[
                "sent_by"
            ] = str(
                interaction.user
            )

            item[
                "signal_chart_filename"
            ] = attachment.filename

            item[
                "awaiting_chart"
            ] = False

            save_state(
                state
            )

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
        )
        async def send_to_signals(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button,
        ):
            # IMPORTANT: modal response happens immediately. No state-file I/O
            # is allowed before send_modal(), avoiding Discord's 3-second timeout.
            print(
                "Send to Signals clicked:",
                interaction.message.id,
                interaction.user,
                flush=True,
            )

            message_text = str(interaction.message.content or "")
            symbol = "Trade"
            for raw_line in message_text.splitlines():
                line = raw_line.strip().replace("**", "")
                if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", line):
                    symbol = line
                    break

            try:
                await interaction.response.send_modal(
                    TradeThesisModal(str(interaction.message.id), symbol)
                )
                print(
                    "Trade Thesis modal opened for:",
                    interaction.message.id,
                    symbol,
                    flush=True,
                )
            except Exception as exc:
                print(
                    "ERROR opening Trade Thesis modal:",
                    repr(exc),
                    flush=True,
                )
                raise

    @command_tree.command(
        name="clear-earnings-review",
        description="Clear handled bot posts from earnings review.",
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

        state = load_state()
        message_ids = handled_review_message_ids(
            state,
            review_channel_id,
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

        candidates: list[Any] = []
        skipped = 0
        fetch_failed = 0

        for message_id in message_ids:
            try:
                message = await review_channel.fetch_message(
                    message_id
                )
            except discord.NotFound:
                skipped += 1
                continue
            except (discord.Forbidden, discord.HTTPException):
                fetch_failed += 1
                continue

            if not is_safe_review_message(
                message,
                client.user.id,
                discord.MessageType.default,
            ):
                skipped += 1
                continue

            candidates.append(message)

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
        total_failed = fetch_failed + results["failed"]

        await interaction.followup.send(
            (
                f"Deleted {results['deleted']} handled earnings "
                f"review message(s). Skipped {total_skipped}; "
                f"failed {total_failed}."
            ),
            ephemeral=True,
        )

    @client.event
    async def on_ready():
        nonlocal command_sync_attempted

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
    client.add_view(EarningsReviewView())

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
) -> None:
    payload = json.dumps(
        {
            "username": username,
            "content": message,
            "allowed_mentions": {"parse": []},
        }
    ).encode("utf-8")

    for attempt in range(1, MAX_DISCORD_ATTEMPTS + 1):
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in (200, 204):
                    raise RuntimeError(
                        f"Discord returned HTTP {response.status}."
                    )
                return

        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= MAX_DISCORD_ATTEMPTS:
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Discord returned HTTP {exc.code}: {body}"
                ) from exc

            wait_seconds = discord_retry_seconds(exc, attempt)
            print(
                "Discord rate limit reached. "
                f"Waiting {wait_seconds:.1f} seconds..."
            )
            time.sleep(wait_seconds)

    raise RuntimeError("Discord post failed after retries.")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {
            "public": {},
            "private": {},
            "quotes": {},
            "signal_queue": {},
        }

    state.setdefault("public", {})
    state.setdefault("private", {})
    state.setdefault("quotes", {})
    state.setdefault("signal_queue", {})

    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file = STATE_FILE.with_suffix(".tmp")

    temporary_file.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(STATE_FILE)



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
        import asyncio

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

    reports = get_completed_reports(target_date)

    state = load_state()

    candidates, quote_calls, cache_hits = build_candidates_optimized(
        reports,
        target_date,
        state,
    )

    # Save quote cache even in preview mode. This is operational cache only;
    # preview still does not change Discord posting state.
    save_state(state)

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
            for candidate in private_candidates
            if qualifies_for_public(candidate)
        ],
        key=lambda item: (
            item["score"],
            abs(item["move_percent"] or 0),
        ),
        reverse=True,
    )

    private_max = env_int(
        "EARNINGS_PRIVATE_MAX",
        50,
    )

    public_max = env_int(
        "EARNINGS_PUBLIC_MAX",
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

            if (
                key in state["private"]
                and not arguments.force
            ):
                continue

            send_private_review_with_chart(
                candidate,
                rank,
                state,
            )

            state["private"][key] = {
                "posted_at": datetime.now(EASTERN).isoformat(),
                "symbol": candidate["symbol"],
            }

            save_state(state)
            private_posted += 1
            time.sleep(DISCORD_POST_DELAY_SECONDS)

    public_to_post = (
        public_candidates[:arguments.limit]
        if arguments.limit is not None
        else public_candidates
    )

    for candidate in public_to_post:
        key = report_key(candidate["report"])

        if (
            key in state["public"]
            and not arguments.force
        ):
            continue

        send_discord_message(
            public_webhook,
            build_public_message(candidate),
            PUBLIC_WEBHOOK_USERNAME,
        )

        state["public"][key] = {
            "posted_at": datetime.now(EASTERN).isoformat(),
            "symbol": candidate["symbol"],
        }

        save_state(state)
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
