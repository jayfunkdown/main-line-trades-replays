#!/usr/bin/env python3

"""
Main Line Trades - Earnings Reactions

Reads completed U.S. earnings reports from Finnhub and posts
high-signal earnings reactions to Discord.

Required environment variables:
    FINNHUB_API_KEY
    EARNINGS_REACTIONS_WEBHOOK

Optional environment variables:
    EARNINGS_MIN_EPS_SURPRISE_PCT       Default: 10
    EARNINGS_MIN_REVENUE_SURPRISE_PCT   Default: 5
    EARNINGS_MIN_PRICE_MOVE_PCT         Default: 8
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
EASTERN = ZoneInfo("America/New_York")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "earnings_reactions_state.json"

WEBHOOK_USERNAME = "Main Line Trades Earnings"
USER_AGENT = "MainLineTrades-EarningsReactions/1.0"

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# These names can post even when the numerical surprise is modest.
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


def env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()

    if not raw_value:
        return default

    try:
        return float(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a number, not {raw_value!r}."
        ) from exc


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
            raw_body = response.read().decode("utf-8")
            return json.loads(raw_body)

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
    api_key = required_env("FINNHUB_API_KEY")

    query = dict(parameters)
    query["token"] = api_key

    url = (
        f"{FINNHUB_BASE_URL}{endpoint}?"
        f"{urllib.parse.urlencode(query)}"
    )

    return get_json(url)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "posted": {},
        }

    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {
            "posted": {},
        }

    if not isinstance(data, dict):
        return {
            "posted": {},
        }

    data.setdefault("posted", {})
    return data


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


def safe_number(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def result_label(
    actual: Any,
    estimate: Any,
) -> tuple[str, str]:
    actual_number = safe_number(actual)
    estimate_number = safe_number(estimate)

    if actual_number is None or estimate_number is None:
        return "⚪", "Not available"

    if actual_number > estimate_number:
        return "✅", "Beat"

    if actual_number < estimate_number:
        return "❌", "Miss"

    return "➖", "Inline"


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


def get_quote(symbol: str) -> dict[str, Any]:
    return finnhub_get(
        "/quote",
        {
            "symbol": symbol,
        },
    )


def get_completed_reports(
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

    completed_reports = []

    for report in reports:
        if not isinstance(report, dict):
            continue

        symbol = str(
            report.get("symbol") or ""
        ).strip().upper()

        if not symbol:
            continue

        has_eps_result = (
            safe_number(report.get("epsActual"))
            is not None
        )

        has_revenue_result = (
            safe_number(report.get("revenueActual"))
            is not None
        )

        if not has_eps_result and not has_revenue_result:
            continue

        completed_reports.append(report)

    return completed_reports


def should_review_quote(
    report: dict[str, Any],
) -> bool:
    symbol = str(
        report.get("symbol") or ""
    ).upper()

    eps_surprise = surprise_percent(
        report.get("epsActual"),
        report.get("epsEstimate"),
    )

    revenue_surprise = surprise_percent(
        report.get("revenueActual"),
        report.get("revenueEstimate"),
    )

    minimum_eps_surprise = env_float(
        "EARNINGS_MIN_EPS_SURPRISE_PCT",
        10,
    )

    minimum_revenue_surprise = env_float(
        "EARNINGS_MIN_REVENUE_SURPRISE_PCT",
        5,
    )

    return any(
        (
            symbol in PRIORITY_TICKERS,
            (
                eps_surprise is not None
                and abs(eps_surprise)
                >= minimum_eps_surprise
            ),
            (
                revenue_surprise is not None
                and abs(revenue_surprise)
                >= minimum_revenue_surprise
            ),
        )
    )


def should_post(
    report: dict[str, Any],
    quote: dict[str, Any],
) -> bool:
    if should_review_quote(report):
        return True

    price_move = safe_number(
        quote.get("dp")
    )

    minimum_price_move = env_float(
        "EARNINGS_MIN_PRICE_MOVE_PCT",
        8,
    )

    return (
        price_move is not None
        and abs(price_move) >= minimum_price_move
    )


def build_message(
    report: dict[str, Any],
    quote: dict[str, Any],
) -> str:
    symbol = str(
        report.get("symbol") or ""
    ).upper()

    eps_icon, eps_result = result_label(
        report.get("epsActual"),
        report.get("epsEstimate"),
    )

    revenue_icon, revenue_result = result_label(
        report.get("revenueActual"),
        report.get("revenueEstimate"),
    )

    eps_surprise = surprise_percent(
        report.get("epsActual"),
        report.get("epsEstimate"),
    )

    revenue_surprise = surprise_percent(
        report.get("revenueActual"),
        report.get("revenueEstimate"),
    )

    price_move = safe_number(
        quote.get("dp")
    )

    current_price = safe_number(
        quote.get("c")
    )

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

    if price_move is None:
        move_line = "Latest move: **Not available**"
    else:
        move_icon = "🟢" if price_move >= 0 else "🔴"

        if current_price is None:
            move_line = (
                f"{move_icon} Latest move: "
                f"**{price_move:+.2f}%**"
            )
        else:
            move_line = (
                f"{move_icon} Latest move: "
                f"**{price_move:+.2f}%** "
                f"at **${current_price:,.2f}**"
            )

    lines = [
        "# 💰 Earnings Reaction",
        "",
        f"## {symbol}",
        "",
        (
            f"{eps_icon} **EPS: {eps_result}**"
            f"{eps_surprise_text}"
        ),
        (
            f"Actual: **{format_eps(report.get('epsActual'))}** "
            f"| Estimate: "
            f"**{format_eps(report.get('epsEstimate'))}**"
        ),
        "",
        (
            f"{revenue_icon} "
            f"**Revenue: {revenue_result}**"
            f"{revenue_surprise_text}"
        ),
        (
            f"Actual: "
            f"**{format_revenue(report.get('revenueActual'))}** "
            f"| Estimate: "
            f"**{format_revenue(report.get('revenueEstimate'))}**"
        ),
        "",
        move_line,
        "",
        (
            f"🕒 **Session:** "
            f"{reporting_session(report.get('hour'))}"
        ),
        "",
        DIVIDER,
        "",
        "*Earnings figures are reported data, not a trade signal.*",
    ]

    return "\n".join(lines)


def send_discord_message(
    webhook_url: str,
    message: str,
) -> None:
    payload = json.dumps(
        {
            "username": WEBHOOK_USERNAME,
            "content": message,
            "allowed_mentions": {
                "parse": [],
            },
        }
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

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            if response.status not in (200, 204):
                raise RuntimeError(
                    "Discord returned HTTP "
                    f"{response.status}."
                )

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Discord returned HTTP {exc.code}: {body}"
        ) from exc


def report_key(
    report: dict[str, Any],
) -> str:
    return ":".join(
        (
            str(report.get("date") or ""),
            str(report.get("symbol") or "").upper(),
            str(report.get("year") or ""),
            str(report.get("quarter") or ""),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Post completed earnings reactions to Discord."
        )
    )

    parser.add_argument(
        "--date",
        help="Target date in YYYY-MM-DD format. Defaults to today ET.",
    )

    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print messages without posting to Discord.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore saved state and process reports again.",
    )

    arguments = parser.parse_args()

    target_date = (
        arguments.date
        or datetime.now(EASTERN).date().isoformat()
    )

    webhook_url = ""

    if not arguments.preview:
        webhook_url = required_env(
            "EARNINGS_REACTIONS_WEBHOOK"
        )

    state = load_state()
    posted = state["posted"]

    reports = get_completed_reports(
        target_date
    )

    reviewed_count = 0
    posted_count = 0
    skipped_count = 0

    for report in reports:
        key = report_key(report)

        if key in posted and not arguments.force:
            skipped_count += 1
            continue

        # Finnhub quote data is requested only for completed
        # reports to keep API usage controlled.
        symbol = str(
            report.get("symbol") or ""
        ).upper()

        try:
            quote = get_quote(symbol)
        except RuntimeError as exc:
            print(
                f"Could not retrieve quote for {symbol}: {exc}"
            )
            quote = {}

        reviewed_count += 1

        if not should_post(report, quote):
            skipped_count += 1
            continue

        message = build_message(
            report,
            quote,
        )

        if arguments.preview:
            print(message)
            print("\n" + "=" * 70 + "\n")
        else:
            send_discord_message(
                webhook_url,
                message,
            )

            posted[key] = {
                "posted_at": datetime.now(
                    EASTERN
                ).isoformat(),
                "symbol": symbol,
            }

            save_state(state)

            # Prevent rapid webhook/API bursts.
            time.sleep(1)

        posted_count += 1

    mode = "previewed" if arguments.preview else "posted"

    print(
        f"Earnings reactions complete for {target_date}: "
        f"{len(reports)} completed report(s), "
        f"{reviewed_count} reviewed, "
        f"{posted_count} {mode}, "
        f"{skipped_count} skipped."
    )


if __name__ == "__main__":
    main()
