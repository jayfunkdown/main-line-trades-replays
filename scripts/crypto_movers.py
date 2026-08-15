#!/usr/bin/env python3
"""
Main Line Trades - Crypto Movers

Grades top-100 crypto coins by market cap and posts the strongest daily
movers to Market Intel using the earnings-movers card style.

Required for --post:
    CRYPTO_MOVERS_WEBHOOK

Optional:
    COINGECKO_API_KEY
    CRYPTO_MOVERS_DAILY_MAX=10
    CRYPTO_MOVERS_MIN_MOVE_PCT=5
    CRYPTO_MOVERS_PRIORITY_MIN_MOVE_PCT=3
    CRYPTO_MOVERS_UNIVERSE_SIZE=100
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.discord_embeds import BRAND_NEON_PINK, bordered_embed
    from scripts.earnings_reactions import (
        cleanup_weekly_chart,
        generate_weekly_chart,
        multipart_body,
        temporary_weekly_chart_path,
        weekly_chart_filename,
    )
except ModuleNotFoundError:
    from discord_embeds import BRAND_NEON_PINK, bordered_embed
    from earnings_reactions import (
        cleanup_weekly_chart,
        generate_weekly_chart,
        multipart_body,
        temporary_weekly_chart_path,
        weekly_chart_filename,
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "crypto_movers_state.json"

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
EASTERN = ZoneInfo("America/New_York")
USER_AGENT = "MainLineTrades-CryptoMovers/1.0"
WEBHOOK_USERNAME = "Main Line Trades Crypto Movers"

DISCORD_POST_DELAY_SECONDS = 2.0
MAX_DISCORD_ATTEMPTS = 4

PRIORITY_SYMBOLS = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "BNB",
        "ADA",
        "DOGE",
        "AVAX",
        "LINK",
        "DOT",
    }
)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()

    if not raw:
        return default

    return float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()

    if not raw:
        return default

    return int(raw)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


def eastern_today_label() -> str:
    return datetime.now(EASTERN).date().isoformat()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"posted": {}}

    payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    if not isinstance(payload, dict):
        raise RuntimeError("Crypto movers state file is invalid.")

    posted = payload.get("posted")

    if not isinstance(posted, dict):
        raise RuntimeError("Crypto movers state file is missing posted records.")

    return payload


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def coingecko_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    api_key = os.getenv("COINGECKO_API_KEY", "").strip()

    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    return headers


def fetch_top_coins(limit: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": max(1, min(limit, 250)),
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "24h",
        }
    )

    request = urllib.request.Request(
        f"{COINGECKO_BASE_URL}/coins/markets?{query}",
        headers=coingecko_headers(),
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, list):
        raise RuntimeError("CoinGecko returned an unexpected payload.")

    return payload


def movement_score(change_percent: float) -> float:
    absolute_move = abs(change_percent)

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


def safe_number(value: Any) -> float | None:
    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if number != number:
        return None

    return number


def format_compact_usd(value: float | None) -> str:
    if value is None:
        return "Not available"

    absolute = abs(value)

    if absolute >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    return f"${value:,.0f}"



def calculate_candidate(coin: dict[str, Any]) -> dict[str, Any]:
    symbol = str(coin.get("symbol", "")).upper()
    coin_id = str(coin.get("id", "")).strip()
    name = str(coin.get("name", symbol)).strip() or symbol

    change_24h = safe_number(coin.get("price_change_percentage_24h"))
    current_price = safe_number(coin.get("current_price"))
    market_cap = safe_number(coin.get("market_cap"))
    volume_24h = safe_number(coin.get("total_volume"))
    market_cap_rank = coin.get("market_cap_rank")

    score = 0.0

    if change_24h is not None:
        score += movement_score(change_24h)

    priority = symbol in PRIORITY_SYMBOLS

    if priority:
        score += 30

    if (
        volume_24h is not None
        and market_cap is not None
        and market_cap > 0
    ):
        volume_ratio = volume_24h / market_cap

        if volume_ratio >= 0.15:
            score += 15
        elif volume_ratio >= 0.08:
            score += 8

    if isinstance(market_cap_rank, int) and market_cap_rank <= 10:
        score += 10

    return {
        "coin_id": coin_id,
        "symbol": symbol,
        "name": name,
        "change_24h": change_24h,
        "current_price": current_price,
        "market_cap": market_cap,
        "volume_24h": volume_24h,
        "market_cap_rank": market_cap_rank,
        "priority": priority,
        "score": score,
    }


def qualifies_for_public(candidate: dict[str, Any]) -> bool:
    change_24h = candidate["change_24h"]

    if change_24h is None:
        return False

    absolute_move = abs(change_24h)
    minimum_move = max(
        env_float("CRYPTO_MOVERS_MIN_MOVE_PCT", 5),
        5,
    )
    priority_minimum = env_float(
        "CRYPTO_MOVERS_PRIORITY_MIN_MOVE_PCT",
        3,
    )

    if candidate["priority"] and absolute_move >= priority_minimum:
        return True

    return absolute_move >= minimum_move


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    qualified = [
        candidate
        for candidate in candidates
        if qualifies_for_public(candidate)
    ]

    return sorted(
        qualified,
        key=lambda item: (
            item["priority"],
            item["score"],
            abs(item["change_24h"] or 0),
        ),
        reverse=True,
    )


def build_public_message(candidate: dict[str, Any]) -> str:
    symbol = candidate["symbol"]
    change_24h = candidate["change_24h"]
    current_price = candidate["current_price"]
    market_cap = candidate["market_cap"]
    volume_24h = candidate["volume_24h"]
    market_cap_rank = candidate["market_cap_rank"]

    move_icon = "🟢" if (change_24h or 0) >= 0 else "🔴"

    rank_text = (
        f"#{market_cap_rank}"
        if isinstance(market_cap_rank, int)
        else "Not ranked"
    )

    price_text = (
        f" at **${current_price:,.2f}**"
        if current_price is not None
        else ""
    )

    return "\n".join(
        [
            "# 🪙 Crypto Mover",
            "",
            f"## {symbol}",
            "",
            (
                f"{move_icon} **24h move: "
                f"{change_24h:+.2f}%**{price_text}"
            ),
            "",
            f"🏆 **Market cap rank:** {rank_text}",
            f"💰 **Market cap:** {format_compact_usd(market_cap)}",
            "",
            f"📊 **24h volume:** {format_compact_usd(volume_24h)}",
            "",
            "*Market data — not a trade signal.*",
        ]
    )


def crypto_chart_symbol(symbol: str) -> str:
    """Map a crypto ticker to the Yahoo Finance chart symbol."""
    return f"{symbol.upper()}-USD"


def build_webhook_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    message = build_public_message(candidate)

    return {
        "username": WEBHOOK_USERNAME,
        "embeds": [
            bordered_embed(
                message,
                color=BRAND_NEON_PINK,
            )
        ],
        "allowed_mentions": {"parse": []},
    }


def already_posted_today(
    state: dict[str, Any],
    *,
    date_label: str,
    coin_id: str,
) -> bool:
    posted_for_day = state["posted"].get(date_label, {})

    return coin_id in posted_for_day


def remaining_daily_slots(
    state: dict[str, Any],
    *,
    date_label: str,
    daily_max: int,
) -> int:
    posted_for_day = state["posted"].get(date_label, {})

    return max(daily_max - len(posted_for_day), 0)


def mark_posted(
    state: dict[str, Any],
    *,
    date_label: str,
    candidate: dict[str, Any],
    message_id: str | None,
) -> dict[str, Any]:
    posted = state.setdefault("posted", {})
    day_records = posted.setdefault(date_label, {})
    day_records[candidate["coin_id"]] = {
        "symbol": candidate["symbol"],
        "score": candidate["score"],
        "change_24h": candidate["change_24h"],
        "discord_message_id": message_id,
    }
    return state


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
            chart_path = temporary_weekly_chart_path(chart_symbol)
            chart_path = generate_weekly_chart(
                crypto_chart_symbol(chart_symbol),
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
                        raise RuntimeError(
                            f"Discord returned HTTP {response.status}"
                        )

                    if response.status != 200:
                        return None

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
    finally:
        if chart_path is not None:
            cleanup_weekly_chart(chart_path)


def configure_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)

    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def print_preview_list(
    title: str,
    candidates: list[dict[str, Any]],
    preview_limit: int | None,
) -> None:
    print(title)
    print("=" * len(title))

    if not candidates:
        print("No qualifying candidates.")
        return

    limit = preview_limit if preview_limit is not None else len(candidates)

    for rank, candidate in enumerate(candidates[:limit], start=1):
        change_24h = candidate["change_24h"] or 0
        print(
            f"{rank:>2}. "
            f"{candidate['symbol']:<8} "
            f"{change_24h:+7.2f}% "
            f"Score {candidate['score']:>7.1f}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Grade top crypto movers and post the strongest candidates "
            "to Market Intel."
        )
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Print rankings and a sample card without posting.",
    )
    mode.add_argument(
        "--post",
        action="store_true",
        help="Post up to the daily maximum of qualifying crypto movers.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional posting cap for this run (defaults to remaining daily slots).",
    )

    parser.add_argument(
        "--preview-limit",
        type=int,
        default=10,
        help="How many ranked candidates to print in preview mode.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    arguments = parse_args(argv)
    date_label = eastern_today_label()
    universe_size = env_int("CRYPTO_MOVERS_UNIVERSE_SIZE", 100)
    daily_max = min(env_int("CRYPTO_MOVERS_DAILY_MAX", 10), 10)

    coins = fetch_top_coins(universe_size)
    candidates = [calculate_candidate(coin) for coin in coins]
    ranked = rank_candidates(candidates)

    if arguments.preview:
        configure_stdout()
        print_preview_list(
            "CRYPTO MOVERS",
            ranked,
            arguments.preview_limit,
        )

        if ranked:
            print()
            print("SAMPLE DISCORD POST")
            print("=" * 18)
            print(build_public_message(ranked[0]))

        print()
        print(
            f"Universe: top {len(candidates)} by market cap | "
            f"Qualified: {len(ranked)} | "
            f"Daily cap: {daily_max}"
        )
        print()
        print("PREVIEW ONLY — nothing was posted; posting state was not changed.")
        return

    webhook_url = required_env("CRYPTO_MOVERS_WEBHOOK")
    state = load_state()
    remaining = remaining_daily_slots(
        state,
        date_label=date_label,
        daily_max=daily_max,
    )

    if remaining <= 0:
        print(
            f"Crypto movers already reached the daily cap for {date_label}."
        )
        return

    post_limit = min(
        remaining,
        arguments.limit if arguments.limit is not None else remaining,
    )

    posted_count = 0

    for candidate in ranked:
        if posted_count >= post_limit:
            break

        if already_posted_today(
            state,
            date_label=date_label,
            coin_id=candidate["coin_id"],
        ):
            continue

        message_id = send_discord_message(
            webhook_url,
            build_public_message(candidate),
            WEBHOOK_USERNAME,
            chart_symbol=candidate["symbol"],
        )
        state = mark_posted(
            state,
            date_label=date_label,
            candidate=candidate,
            message_id=message_id,
        )
        save_state(state)
        posted_count += 1
        time.sleep(DISCORD_POST_DELAY_SECONDS)

    print(
        f"Crypto movers run complete for {date_label}: "
        f"{len(candidates)} scanned, "
        f"{len(ranked)} qualified, "
        f"{posted_count} posted."
    )


if __name__ == "__main__":
    main()
