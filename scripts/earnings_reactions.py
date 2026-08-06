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
"""

from __future__ import annotations

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

USER_AGENT = "MainLineTrades-EarningsReactions/2.0"
PUBLIC_WEBHOOK_USERNAME = "Main Line Trades Earnings"
PRIVATE_WEBHOOK_USERNAME = "Main Line Trades Research"

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


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

    symbol = candidate["symbol"]
    tradingview_url = (
        "https://www.tradingview.com/chart/"
        f"?symbol={urllib.parse.quote(symbol)}"
    )

    return "\n".join(
        [
            f"# 🔬 Earnings Review #{rank}",
            "",
            public_message,
            "",
            f"**Review score:** {candidate['score']:.1f}",
            "",
            f"📈 Open in TradingView: {tradingview_url}",
        ]
    )


def send_discord_message(
    webhook_url: str,
    message: str,
    username: str,
) -> None:
    payload = json.dumps(
        {
            "username": username,
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
                    f"Discord returned HTTP {response.status}."
                )

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Discord returned HTTP {exc.code}: {body}"
        ) from exc


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "public": {},
            "private": {},
        }

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {
            "public": {},
            "private": {},
        }

    state.setdefault("public", {})
    state.setdefault("private", {})

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
            "Defaults to today Eastern."
        ),
    )

    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview rankings without posting to Discord.",
    )

    parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="Maximum candidates shown in each preview list.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore saved posting state.",
    )

    arguments = parser.parse_args()

    today_eastern = datetime.now(EASTERN).date().isoformat()

    target_date = (
        arguments.date
        or today_eastern
    )

    if target_date != today_eastern:
        print(
            "WARNING: Finnhub quote movement is current, "
            "not historical. Historical date testing validates "
            "filtering and formatting only."
        )

    reports = get_completed_reports(target_date)

    quote_delay = env_float(
        "EARNINGS_QUOTE_DELAY_SECONDS",
        1.1,
    )

    candidates: list[dict[str, Any]] = []

    for index, report in enumerate(reports, start=1):
        symbol = str(report["symbol"])

        print(
            f"Retrieving quote {index}/{len(reports)}: "
            f"{symbol}"
        )

        try:
            quote = get_quote_with_retry(symbol)
        except RuntimeError as exc:
            print(
                f"Could not retrieve quote for {symbol}: {exc}"
            )
            quote = {}

        candidate = calculate_candidate(
            report,
            quote,
        )

        candidates.append(candidate)

        if index < len(reports):
            time.sleep(quote_delay)

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

        print()
        print(
            f"Completed reports: {len(reports)} | "
            f"Private review: {len(private_candidates)} | "
            f"Public reactions: {len(public_candidates)}"
        )

        return

    public_webhook = required_env(
        "EARNINGS_REACTIONS_WEBHOOK"
    )

    private_webhook = os.getenv(
        "EARNINGS_REVIEW_WEBHOOK",
        "",
    ).strip()

    state = load_state()

    private_posted = 0
    public_posted = 0

    if private_webhook:
        for rank, candidate in enumerate(
            private_candidates,
            start=1,
        ):
            key = report_key(candidate["report"])

            if (
                key in state["private"]
                and not arguments.force
            ):
                continue

            send_discord_message(
                private_webhook,
                build_private_message(candidate, rank),
                PRIVATE_WEBHOOK_USERNAME,
            )

            state["private"][key] = {
                "posted_at": datetime.now(EASTERN).isoformat(),
                "symbol": candidate["symbol"],
            }

            save_state(state)
            private_posted += 1
            time.sleep(1)

    for candidate in public_candidates:
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
        time.sleep(1)

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