#!/usr/bin/env python3
"""
Main Line Trades - Weekly Gain/Loss Retest Screener

Step 1 (--scan): batched weekly OHLC after that market's weekly closes.
US: Friday 4:00 PM America/New_York through Sunday. Crypto: Monday 00:00 UTC
once that weekly has printed. Names that gain or lose the weekly are
admitted to a watchlist. No Discord post.

Step 2 (--watch): hourly last-price check. Names within 5% of the line
are quoted every hour. The rest rotate in (~200 per hour) so a 1,500–2,000
name list is fully refreshed about every 8–10 hours. When price is within
1% of the watched weekly level, post a chart card.

Required for --watch:
    WEEKLY_SCREENER_WEBHOOK

Optional:
    WEEKLY_SCREENER_CRYPTO_UNIVERSE_SIZE=750
    WEEKLY_SCREENER_US_MIN_PRICE=2
    WEEKLY_SCREENER_US_MIN_AVG_VOLUME=200000
    WEEKLY_SCREENER_YAHOO_DELAY_SECONDS=0.15
    WEEKLY_SCREENER_SCAN_BATCH_SIZE=400
    WEEKLY_SCREENER_WATCH_EXPIRE_WEEKS=8
    WEEKLY_SCREENER_RETEST_PCT=1
    WEEKLY_SCREENER_WATCH_NEAR_PCT=5
    WEEKLY_SCREENER_WATCH_FAR_PER_RUN=200
    COINGECKO_API_KEY
    FINNHUB_API_KEY
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import argparse
import copy
import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

try:
    from scripts.crypto_movers import (
        COINGECKO_BASE_URL,
        coingecko_headers,
        crypto_chart_symbol,
    )
    from scripts.discord_embeds import BRAND_NEON_PINK, bordered_embed
    from scripts.earnings_reactions import (
        aggregate_weekly_candles,
        cleanup_weekly_chart,
        fetch_daily_candles,
        generate_weekly_chart,
        latest_chart_close,
        multipart_body,
        temporary_weekly_chart_path,
        weekly_chart_filename,
    )
except ModuleNotFoundError:
    from crypto_movers import (
        COINGECKO_BASE_URL,
        coingecko_headers,
        crypto_chart_symbol,
    )
    from discord_embeds import BRAND_NEON_PINK, bordered_embed
    from earnings_reactions import (
        aggregate_weekly_candles,
        cleanup_weekly_chart,
        fetch_daily_candles,
        generate_weekly_chart,
        latest_chart_close,
        multipart_body,
        temporary_weekly_chart_path,
        weekly_chart_filename,
    )


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "weekly_screener_state.json"
STATE_LOCK_PATH = PROJECT_ROOT / "data" / "weekly_screener_state.json.lock"
UNIVERSE_CACHE_PATH = PROJECT_ROOT / "data" / "weekly_screener_universe_cache.json"

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
USER_AGENT = "MainLineTrades-WeeklyScreener/1.0"
WEBHOOK_USERNAME = "Main Line Trades Weekly Screener"
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"

DISCORD_POST_DELAY_SECONDS = 2.0
MAX_DISCORD_ATTEMPTS = 4
DEFAULT_CRYPTO_UNIVERSE = 750
DEFAULT_US_MIN_PRICE = 2.0
DEFAULT_US_MIN_AVG_VOLUME = 200_000
DEFAULT_YAHOO_DELAY = 0.15
DEFAULT_SCAN_BATCH_SIZE = 400
DEFAULT_WATCH_EXPIRE_WEEKS = 8
DEFAULT_RETEST_PCT = 1.0
DEFAULT_WATCH_NEAR_PCT = 5.0
DEFAULT_WATCH_FAR_PER_RUN = 200
VOLUME_LOOKBACK_DAYS = 20

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "master/data/constituents.csv"
)
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
RUSSELL1000_CSV_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)

OTC_MICS = frozenset(
    {
        "OTCM",
        "OTCX",
        "PINX",
        "PSGM",
        "OTCB",
        "OOTC",
        "PINL",
    }
)

CRYPTO_EXCLUDED_SYMBOLS = frozenset(
    {
        "USDT",
        "USDC",
        "DAI",
        "BUSD",
        "TUSD",
        "FDUSD",
        "USDE",
        "PYUSD",
        "FRAX",
        "GUSD",
        "LUSD",
        "WBTC",
        "WETH",
        "STETH",
        "WSTETH",
        "WBETH",
        "WEETH",
        "CBETH",
    }
)

US_SKIP_SUFFIXES = (".W", ".WS", ".U", ".R", ".P", "-W", "-WS", "-U")


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


def eastern_now() -> datetime:
    return datetime.now(EASTERN)


def eastern_today_label() -> str:
    return eastern_now().date().isoformat()


def current_iso_week_id(now: datetime | None = None) -> str:
    day = now or eastern_now()
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def previous_iso_week_id(now: datetime) -> str:
    current = now.astimezone(UTC)
    iso_year, iso_week, iso_weekday = current.isocalendar()
    monday = current.date() - timedelta(days=iso_weekday - 1)
    previous = monday - timedelta(days=1)
    year, week, _ = previous.isocalendar()
    return f"{year}-W{week:02d}"


def us_scan_window(now: datetime | None = None) -> bool:
    """US cash weeklies print at Friday 4:00 PM America/New_York."""
    current = (now or eastern_now()).astimezone(EASTERN)
    if current.weekday() == 4 and current.hour >= 16:
        return True
    return current.weekday() in {5, 6}


def crypto_scan_window(now: datetime | None = None) -> bool:
    """Crypto weeklies print at Monday 00:00 UTC."""
    current = (now or eastern_now()).astimezone(UTC)
    return current.weekday() == 0


def scan_market_allowed(market: str, now: datetime | None = None) -> bool:
    if market == "us":
        return us_scan_window(now)
    if market == "crypto":
        return crypto_scan_window(now)
    return False


def scan_skip_reason(markets: str, now: datetime | None = None) -> str | None:
    us_open = us_scan_window(now)
    crypto_open = crypto_scan_window(now)
    if markets == "us" and not us_open:
        return "US weekly is still open until Friday 4:00 PM America/New_York."
    if markets == "crypto" and not crypto_open:
        return "Crypto weekly is still open until Monday 00:00 UTC."
    if markets == "all" and not us_open and not crypto_open:
        return (
            "No weekly is closed yet (US: Friday 4:00 PM America/New_York; "
            "crypto: Monday 00:00 UTC)."
        )
    return None


def scan_week_id_for_market(market: str, now: datetime | None = None) -> str:
    current = now or eastern_now()
    if market == "crypto":
        return previous_iso_week_id(current)
    return current_iso_week_id(current.astimezone(EASTERN))


def closed_weekly_candles(
    weekly: list[dict[str, Any]],
    *,
    market: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Crypto scans use the last closed UTC week, not a forming Monday bar."""
    if market != "crypto":
        return weekly
    current = now or eastern_now()
    forming_week_id = current_iso_week_id(current.astimezone(UTC))
    closed = [
        candle
        for candle in weekly
        if week_id_from_candle(candle) != forming_week_id
    ]
    return closed if len(closed) >= 8 else weekly


def empty_state() -> dict[str, Any]:
    return {
        "seeded": False,
        "watchlist": {},
        "posted": {},
        "daily": {},
        "scan": {},
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return empty_state()
    payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Weekly screener state file is invalid.")
    payload.setdefault("seeded", False)
    payload.setdefault("watchlist", {})
    payload.setdefault("posted", {})
    payload.setdefault("daily", {})
    payload.setdefault("scan", {})
    if not isinstance(payload["watchlist"], dict):
        raise RuntimeError("Weekly screener state file has invalid watchlist.")
    if not isinstance(payload["posted"], dict):
        raise RuntimeError("Weekly screener state file is missing posted records.")
    if not isinstance(payload["daily"], dict):
        raise RuntimeError("Weekly screener state file has invalid daily records.")
    if not isinstance(payload["scan"], dict):
        raise RuntimeError("Weekly screener state file has invalid scan cursor.")
    return payload


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


@contextmanager
def exclusive_state() -> Iterator[None]:
    STATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = STATE_LOCK_PATH.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def merge_persistent_fields(disk: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    """Union posted/watchlist so a long scan cannot wipe a concurrent watch."""
    merged = copy.deepcopy(memory)
    disk_posted = disk.get("posted") if isinstance(disk.get("posted"), dict) else {}
    mem_posted = memory.get("posted") if isinstance(memory.get("posted"), dict) else {}
    posted = dict(disk_posted)
    posted.update(mem_posted)
    merged["posted"] = posted
    merged["seeded"] = bool(disk.get("seeded") or memory.get("seeded"))
    disk_watch = disk.get("watchlist") if isinstance(disk.get("watchlist"), dict) else {}
    mem_watch = memory.get("watchlist") if isinstance(memory.get("watchlist"), dict) else {}
    watchlist = dict(disk_watch)
    watchlist.update(mem_watch)
    merged["watchlist"] = watchlist
    disk_daily = disk.get("daily") if isinstance(disk.get("daily"), dict) else {}
    mem_daily = memory.get("daily") if isinstance(memory.get("daily"), dict) else {}
    daily: dict[str, Any] = dict(disk_daily)
    for day, keys in mem_daily.items():
        existing = list(daily.get(day) or [])
        if not isinstance(existing, list):
            existing = []
        for key in keys if isinstance(keys, list) else []:
            if key not in existing:
                existing.append(key)
        daily[day] = existing
    merged["daily"] = daily
    return merged


def save_state_locked(memory: dict[str, Any]) -> dict[str, Any]:
    with exclusive_state():
        disk = load_state() if STATE_PATH.exists() else empty_state()
        merged = merge_persistent_fields(disk, memory)
        save_state(merged)
        return merged


def http_get_text(url: str, *, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/json,text/csv,text/plain;q=0.9,*/*;q=0.8",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def http_get_json(
    url: str,
    *,
    timeout: int = 30,
    headers: dict[str, str] | None = None,
) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_csv_symbols(text: str, *, ticker_headers: tuple[str, ...]) -> list[str]:
    lines = text.splitlines()
    header_index = 0
    normalized_headers = {name.lower() for name in ticker_headers}
    for index, line in enumerate(lines):
        cells = [cell.strip().strip('"').lower() for cell in line.split(",")]
        if cells and cells[0] in normalized_headers:
            header_index = index
            break
        if any(cell in normalized_headers for cell in cells):
            header_index = index
            break

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    if reader.fieldnames is None:
        return []

    ticker_field = None
    for field in reader.fieldnames:
        if field is None:
            continue
        if field.strip().lower() in normalized_headers:
            ticker_field = field
            break
    if ticker_field is None:
        ticker_field = reader.fieldnames[0]

    symbols: list[str] = []
    seen: set[str] = set()
    for row in reader:
        raw = str(row.get(ticker_field) or "").strip().upper()
        symbol = normalize_us_symbol(raw)
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def normalize_us_symbol(raw: str) -> str | None:
    symbol = raw.strip().upper().replace(" ", "")
    if not symbol:
        return None
    if symbol in {"TICKER", "SYMBOL", "HOLDINGS", "NAME"}:
        return None
    if any(symbol.endswith(suffix) for suffix in US_SKIP_SUFFIXES):
        return None
    if re.search(r"[^A-Z0-9.\-]", symbol):
        return None
    if len(symbol) > 6:
        return None
    if symbol.endswith("-") or symbol.startswith("-"):
        return None
    return symbol


def yahoo_us_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


def parse_nasdaq100_wiki_symbols(html_text: str) -> list[str]:
    unescaped = html.unescape(html_text)
    matches = re.findall(
        r">([A-Z]{1,5}(?:\.[A-Z])?)</a>\s*</td>",
        unescaped,
    )
    if not matches:
        matches = re.findall(
            r"NASDAQ[:\s]+(?:</a>)?(?:<[^>]+>)*([A-Z]{1,5})\b",
            unescaped,
        )
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in matches:
        symbol = normalize_us_symbol(raw)
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def fetch_sp500_symbols() -> list[str]:
    return parse_csv_symbols(
        http_get_text(SP500_CSV_URL),
        ticker_headers=("symbol", "ticker"),
    )


def fetch_nasdaq100_symbols() -> list[str]:
    return parse_nasdaq100_wiki_symbols(http_get_text(NASDAQ100_WIKI_URL))


def fetch_russell1000_symbols() -> list[str]:
    return parse_csv_symbols(
        http_get_text(RUSSELL1000_CSV_URL, timeout=45),
        ticker_headers=("ticker", "symbol"),
    )


def fetch_finnhub_us_symbols() -> list[str]:
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY is not set.")
    query = urllib.parse.urlencode({"exchange": "US", "token": api_key})
    payload = http_get_json(f"{FINNHUB_BASE_URL}/stock/symbol?{query}", timeout=60)
    if not isinstance(payload, list):
        raise RuntimeError("Finnhub US symbol list is invalid.")
    symbols: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        mic = str(item.get("mic") or "").upper()
        if mic in OTC_MICS:
            continue
        listing = str(item.get("type") or "").strip().lower()
        if listing and listing not in {
            "common stock",
            "etp",
            "etf",
            "adr",
            "eqs",
        }:
            continue
        symbol = normalize_us_symbol(str(item.get("symbol") or ""))
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    if not symbols:
        raise RuntimeError("Finnhub returned no usable US listings.")
    return symbols


def load_universe_cache() -> dict[str, Any] | None:
    if not UNIVERSE_CACHE_PATH.exists():
        return None
    try:
        payload = json.loads(UNIVERSE_CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def save_universe_cache(us_symbols: list[str], source: str) -> None:
    UNIVERSE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_CACHE_PATH.write_text(
        json.dumps(
            {
                "saved_at": eastern_now().isoformat(),
                "source": source,
                "us_symbols": us_symbols,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def build_index_fallback_universe() -> list[str]:
    collected: list[str] = []
    errors: list[str] = []
    fetchers = (
        ("S&P 500", fetch_sp500_symbols),
        ("Nasdaq-100", fetch_nasdaq100_symbols),
        ("Russell 1000", fetch_russell1000_symbols),
    )
    for label, fetcher in fetchers:
        try:
            symbols = fetcher()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        collected.extend(symbols)
    seen: set[str] = set()
    unique: list[str] = []
    for symbol in collected:
        if symbol in seen:
            continue
        seen.add(symbol)
        unique.append(symbol)
    if unique:
        return unique
    detail = "; ".join(errors) if errors else "no symbols returned"
    raise RuntimeError(f"Could not build the US weekly-screener universe ({detail}).")


def build_us_universe(*, use_cache: bool = True) -> list[str]:
    if use_cache:
        cached = load_universe_cache()
        if cached and isinstance(cached.get("us_symbols"), list) and cached["us_symbols"]:
            saved_at = cached.get("saved_at")
            try:
                cached_time = datetime.fromisoformat(str(saved_at))
                age = eastern_now() - cached_time.astimezone(EASTERN)
                if age <= timedelta(hours=18):
                    return [str(symbol) for symbol in cached["us_symbols"]]
            except (TypeError, ValueError):
                pass
    source = "finnhub"
    try:
        symbols = fetch_finnhub_us_symbols()
    except Exception as exc:
        print(f"Finnhub US listings unavailable ({exc}); using index fallback.", flush=True)
        source = "index-fallback"
        symbols = build_index_fallback_universe()
    save_universe_cache(symbols, source)
    return symbols


def excluded_crypto_symbol(symbol: str) -> bool:
    return symbol.upper() in CRYPTO_EXCLUDED_SYMBOLS


def fetch_crypto_markets(limit: int) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    remaining = max(1, limit)
    page = 1
    while remaining > 0:
        per_page = min(250, remaining)
        query = urllib.parse.urlencode(
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": per_page,
                "page": page,
                "sparkline": "false",
            }
        )
        payload = http_get_json(
            f"{COINGECKO_BASE_URL}/coins/markets?{query}",
            timeout=45,
            headers=coingecko_headers(),
        )
        if not isinstance(payload, list) or not payload:
            break
        collected.extend(item for item in payload if isinstance(item, dict))
        remaining -= len(payload)
        page += 1
        if len(payload) < per_page:
            break
    return collected[:limit]


def build_crypto_universe(limit: int) -> list[dict[str, Any]]:
    coins = fetch_crypto_markets(limit)
    universe: list[dict[str, Any]] = []
    seen: set[str] = set()
    for coin in coins:
        symbol = str(coin.get("symbol", "")).upper()
        if not symbol or excluded_crypto_symbol(symbol) or symbol in seen:
            continue
        seen.add(symbol)
        universe.append(
            {
                "symbol": symbol,
                "name": str(coin.get("name") or symbol).strip() or symbol,
                "market": "crypto",
                "chart_symbol": crypto_chart_symbol(symbol),
                "coin_id": str(coin.get("id") or symbol).strip() or symbol,
            }
        )
    return universe


def candle_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=EASTERN)
        return value
    text = str(value)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EASTERN)
    return parsed


def week_id_from_candle(candle: dict[str, Any]) -> str:
    day = candle_datetime(candle["date"])
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def candle_body_high(candle: dict[str, Any]) -> float:
    return max(float(candle["open"]), float(candle["close"]))


def candle_body_low(candle: dict[str, Any]) -> float:
    return min(float(candle["open"]), float(candle["close"]))


EXTREME_CLUSTER_PCT = 0.01


def candle_true_range(candle: dict[str, Any]) -> float:
    return abs(float(candle["high"]) - float(candle["low"]))


def close_location(candle: dict[str, Any]) -> float:
    high = float(candle["high"])
    low = float(candle["low"])
    span = high - low
    if span <= 0:
        return 0.5
    return (float(candle["close"]) - low) / span


def week_made_extreme(candle: dict[str, Any], *, kind: str) -> bool:
    """A dump that only tags a high, or a rally that only tags a low, is not the impulse."""
    close = float(candle["close"])
    open_ = float(candle["open"])
    location = close_location(candle)
    if kind == "high":
        return close >= open_ or location >= 0.5
    return close <= open_ or location <= 0.5


def _impulse_in_cluster(
    weekly: list[dict[str, Any]],
    *,
    end: int,
    kind: str,
    in_cluster,
) -> int | None:
    confirmed: list[tuple[float, int]] = []
    fallback: list[tuple[float, int]] = []
    for index in range(3, end):
        if not in_cluster(index):
            continue
        span = candle_true_range(weekly[index])
        fallback.append((span, index))
        if week_made_extreme(weekly[index], kind=kind):
            confirmed.append((span, index))
    pool = confirmed or fallback
    if not pool:
        return None
    chosen = max(pool, key=lambda item: (item[0], item[1]))[1]
    if week_made_extreme(weekly[chosen], kind=kind):
        return chosen
    for index in range(chosen - 1, 2, -1):
        if week_made_extreme(weekly[index], kind=kind):
            return index
    return chosen


def swing_low_index(weekly: list[dict[str, Any]], *, right_pad: int = 1) -> int | None:
    """Impulse week that printed the swing low, not a later nibble at the same low."""
    if len(weekly) < 8:
        return None
    end = len(weekly) - right_pad
    best: int | None = None
    best_low: float | None = None
    for index in range(3, end):
        low = float(weekly[index]["low"])
        window = [float(weekly[j]["low"]) for j in range(index - 2, min(index + 3, end))]
        if low != min(window):
            continue
        if best is None or low <= best_low:
            best = index
            best_low = low
    if best is None or best_low is None:
        return None
    band = abs(best_low) * (1.0 + EXTREME_CLUSTER_PCT)
    return _impulse_in_cluster(
        weekly,
        end=end,
        kind="low",
        in_cluster=lambda index: float(weekly[index]["low"]) <= band,
    )


SWING_FRACTION = 0.5


def last_pause_before(
    weekly: list[dict[str, Any]],
    extreme_index: int,
    *,
    kind: str,
) -> int | None:
    """Most recent counter-trend week before the extreme. That is the local base."""
    for index in range(extreme_index - 1, 1, -1):
        close = float(weekly[index]["close"])
        open_ = float(weekly[index]["open"])
        if kind == "high" and close < open_:
            return index
        if kind == "low" and close > open_:
            return index
    return None


def origin_of_swing(
    weekly: list[dict[str, Any]],
    extreme_index: int,
    *,
    kind: str,
) -> int | None:
    """
    Origin of the weekly swing that made this extreme.

    One-week swing (IMXI, SAFT): the impulse week's open.
    Multi-week swing (EW): the first week of that run, not a later candle
    sitting at the high or low.
    """
    if extreme_index < 0 or extreme_index >= len(weekly):
        return None
    pause = last_pause_before(weekly, extreme_index, kind=kind)
    first_run = extreme_index if pause is None else min(pause + 1, extreme_index)
    start = pause if pause is not None else first_run
    if kind == "high":
        extreme_price = float(weekly[extreme_index]["high"])
        base = min(float(weekly[index]["low"]) for index in range(start, extreme_index + 1))
        move = abs(extreme_price - base)
    else:
        extreme_price = float(weekly[extreme_index]["low"])
        base = max(float(weekly[index]["high"]) for index in range(start, extreme_index + 1))
        move = abs(base - extreme_price)
    if move <= 0:
        return extreme_index
    if candle_true_range(weekly[extreme_index]) / move >= SWING_FRACTION:
        return extreme_index
    return first_run


def origin_of_swing_low(weekly: list[dict[str, Any]], low_index: int) -> int | None:
    return origin_of_swing(weekly, low_index, kind="low")


def origin_of_swing_high(weekly: list[dict[str, Any]], high_index: int) -> int | None:
    return origin_of_swing(weekly, high_index, kind="high")


def swing_high_index(weekly: list[dict[str, Any]], *, right_pad: int = 1) -> int | None:
    """Impulse week that printed the swing high, not a later nibble at the same high."""
    if len(weekly) < 8:
        return None
    end = len(weekly) - right_pad
    best: int | None = None
    best_high: float | None = None
    for index in range(3, end):
        high = float(weekly[index]["high"])
        window = [float(weekly[j]["high"]) for j in range(index - 2, min(index + 3, end))]
        if high != max(window):
            continue
        if best is None or high >= best_high:
            best = index
            best_high = high
    if best is None or best_high is None:
        return None
    band = abs(best_high) * (1.0 - EXTREME_CLUSTER_PCT)
    return _impulse_in_cluster(
        weekly,
        end=end,
        kind="high",
        in_cluster=lambda index: float(weekly[index]["high"]) >= band,
    )


def origin_level(candle: dict[str, Any]) -> float:
    """Start of the weekly move. Never the wick, never a later sit-on-the-extreme close."""
    return float(candle["open"])


def _structure_hit(
    *,
    side: str,
    close: float,
    week_id: str,
    level: float,
    origin_index: int,
    weekly: list[dict[str, Any]],
    extra: dict[str, Any],
) -> dict[str, Any]:
    taken = close > level if side == "gain" else close < level
    if not taken:
        return {
            "side": "none",
            "reason": "origin_body_not_taken",
            "week_id": week_id,
            "close": close,
            "level": level,
            "level_date": candle_datetime(weekly[origin_index]["date"]).isoformat(),
            **extra,
        }
    return {
        "side": side,
        "reason": "close_through_origin_body",
        "week_id": week_id,
        "close": close,
        "level": level,
        "level_date": candle_datetime(weekly[origin_index]["date"]).isoformat(),
        "origin_index": origin_index,
        **extra,
    }


def classify_weekly_structure(weekly: list[dict[str, Any]]) -> dict[str, Any]:
    if len(weekly) < 8:
        return {
            "side": "none",
            "reason": "not_enough_weeks",
        }

    current = weekly[-1]
    close = float(current["close"])
    week_id = week_id_from_candle(current)
    candidates: list[dict[str, Any]] = []

    low_index = swing_low_index(weekly)
    if low_index is not None:
        origin_index = origin_of_swing_low(weekly, low_index)
        if origin_index is not None:
            candidates.append(
                _structure_hit(
                    side="gain",
                    close=close,
                    week_id=week_id,
                    level=origin_level(weekly[origin_index]),
                    origin_index=origin_index,
                    weekly=weekly,
                    extra={"swing_low": float(weekly[low_index]["low"])},
                )
            )

    high_index = swing_high_index(weekly)
    if high_index is not None:
        origin_index = origin_of_swing_high(weekly, high_index)
        if origin_index is not None:
            candidates.append(
                _structure_hit(
                    side="loss",
                    close=close,
                    week_id=week_id,
                    level=origin_level(weekly[origin_index]),
                    origin_index=origin_index,
                    weekly=weekly,
                    extra={"swing_high": float(weekly[high_index]["high"])},
                )
            )

    taken = [item for item in candidates if item.get("side") in {"gain", "loss"}]
    if not taken:
        if candidates:
            return candidates[0]
        return {
            "side": "none",
            "reason": "no_origin_body",
            "week_id": week_id,
            "close": close,
        }
    taken.sort(key=lambda item: int(item.get("origin_index") or 0), reverse=True)
    return dict(taken[0])


def retest_distance(last_price: float, level: float) -> float:
    if level == 0:
        return float("inf")
    return abs(float(last_price) - float(level)) / abs(float(level))


def is_retest(
    last_price: float,
    level: float,
    *,
    proximity: float = DEFAULT_RETEST_PCT / 100.0,
) -> bool:
    return retest_distance(last_price, level) <= proximity


def candle_range_hits_band(
    candle: dict[str, Any],
    level: float,
    *,
    proximity: float,
) -> bool:
    if level == 0:
        return False
    band_low = abs(float(level)) * (1.0 - proximity)
    band_high = abs(float(level)) * (1.0 + proximity)
    return float(candle["high"]) >= band_low and float(candle["low"]) <= band_high


def first_take_index(
    weekly: list[dict[str, Any]],
    *,
    side: str,
    level: float,
    origin_index: int,
) -> int | None:
    for index in range(origin_index + 1, len(weekly)):
        close = float(weekly[index]["close"])
        if side == "gain" and close > float(level):
            return index
        if side == "loss" and close < float(level):
            return index
    return None


def first_visit_index(
    weekly: list[dict[str, Any]],
    *,
    side: str,
    level: float,
    origin_index: int,
    proximity: float,
) -> int | None:
    take_index = first_take_index(
        weekly,
        side=side,
        level=level,
        origin_index=origin_index,
    )
    if take_index is None:
        return None
    if is_retest(float(weekly[take_index]["close"]), level, proximity=proximity):
        return take_index
    for index in range(take_index + 1, len(weekly)):
        if candle_range_hits_band(weekly[index], level, proximity=proximity):
            return index
    return None


def first_visit_already_used(
    weekly: list[dict[str, Any]],
    *,
    side: str,
    level: float,
    proximity: float,
) -> bool:
    """True when the first 1% test already happened on an earlier week."""
    classification = classify_weekly_structure(weekly)
    origin_index = classification.get("origin_index")
    if (
        classification.get("side") not in {"gain", "loss"}
        or origin_index is None
    ):
        return True
    first = first_visit_index(
        weekly,
        side=str(classification["side"]),
        level=float(level),
        origin_index=int(origin_index),
        proximity=proximity,
    )
    return first is not None and first < len(weekly) - 1


def average_volume(daily: list[dict[str, float]], lookback: int = VOLUME_LOOKBACK_DAYS) -> float | None:
    if not daily:
        return None
    window = daily[-lookback:]
    volumes = [float(item.get("volume") or 0) for item in window]
    if not volumes:
        return None
    return sum(volumes) / len(volumes)


def passes_us_liquidity(
    daily: list[dict[str, float]],
    *,
    min_price: float,
    min_avg_volume: float,
) -> bool:
    if not daily:
        return False
    last_close = float(daily[-1]["close"])
    if last_close < min_price:
        return False
    avg_volume = average_volume(daily)
    if avg_volume is None or avg_volume < min_avg_volume:
        return False
    return True


def fetch_weekly_from_yahoo(chart_symbol: str) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    daily = fetch_daily_candles(chart_symbol)
    weekly = aggregate_weekly_candles(daily)
    return weekly, daily


def level_key(level: float) -> str:
    return f"{float(level):.6f}".rstrip("0").rstrip(".")


def watch_key(symbol: str, side: str, level: float) -> str:
    return f"{symbol}:{side}:{level_key(level)}"


def posted_key(symbol: str, side: str, level: float) -> str:
    return watch_key(symbol, side, level)


def format_price(value: float) -> str:
    if value >= 100:
        return f"${value:,.2f}"
    text = f"${value:.4f}".rstrip("0").rstrip(".")
    return text or "$0"


def build_screener_message(hit: dict[str, Any]) -> str:
    side = hit["side"]
    is_gain = side == "gain"
    direction_line = (
        "🟢 **Direction:** Long" if is_gain else "🔴 **Direction:** Short"
    )
    thesis = "Weekly gained" if is_gain else "Weekly lost"
    last_price = hit.get("last_price")
    price_line = (
        f"💰 **Price:** {format_price(float(last_price))}"
        if last_price is not None
        else ""
    )
    lines = [
        "# 📈 Trade Signal",
        "",
        f"## {hit['symbol']}",
        "",
        direction_line,
        "",
        f"🎯 **Reference level:** {format_price(hit['level'])}",
    ]
    if price_line:
        lines.extend(["", price_line])
    lines.extend(
        [
            "",
            "## 🧠 Trade Thesis",
            "",
            thesis,
            "",
            "📊 **Trade Chart**",
            "",
            "*Chart and thesis provided by Main Line Trades.*",
            "",
            "⚠️ **Manage risk. This is not financial advice.**",
        ]
    )
    return "\n".join(lines)


def resolve_webhook() -> str:
    return required_env("WEEKLY_SCREENER_WEBHOOK")


def already_posted(state: dict[str, Any], key: str) -> bool:
    posted = state.get("posted")
    return isinstance(posted, dict) and key in posted


def already_posted_name(state: dict[str, Any], symbol: str, side: str) -> bool:
    posted = state.get("posted")
    if not isinstance(posted, dict):
        return False
    prefix = f"{symbol}:{side}:"
    return any(str(key).startswith(prefix) for key in posted)


def mark_posted(
    state: dict[str, Any],
    *,
    key: str,
    hit: dict[str, Any],
    date_label: str,
    message_id: str | None,
    seeded: bool = False,
) -> dict[str, Any]:
    posted = state.setdefault("posted", {})
    posted[key] = {
        "symbol": hit["symbol"],
        "side": hit["side"],
        "week_id": hit["week_id"],
        "level": hit["level"],
        "market": hit["market"],
        "discord_message_id": message_id,
        "seeded": seeded,
        "posted_at": eastern_now().isoformat(),
    }
    if not seeded:
        daily = state.setdefault("daily", {})
        day_keys = daily.setdefault(date_label, [])
        if key not in day_keys:
            day_keys.append(key)
    return state


def iso_week_ordinal(week_id: str) -> int:
    year_text, week_text = week_id.split("-W")
    return int(year_text) * 53 + int(week_text)


def expire_stale_watches(
    state: dict[str, Any],
    *,
    now_week_id: str,
    expire_weeks: int,
) -> int:
    watchlist = state.setdefault("watchlist", {})
    expired = [
        key
        for key, record in watchlist.items()
        if isinstance(record, dict)
        and now_week_id
        and iso_week_ordinal(now_week_id)
        - iso_week_ordinal(str(record.get("week_id") or now_week_id))
        >= expire_weeks
    ]
    for key in expired:
        watchlist.pop(key, None)
    return len(expired)


def admit_watch(state: dict[str, Any], hit: dict[str, Any]) -> bool:
    key = watch_key(hit["symbol"], hit["side"], float(hit["level"]))
    watchlist = state.setdefault("watchlist", {})
    if key in watchlist:
        return False
    watchlist[key] = {
        "symbol": hit["symbol"],
        "name": hit.get("name") or hit["symbol"],
        "market": hit["market"],
        "chart_symbol": hit["chart_symbol"],
        "side": hit["side"],
        "week_id": hit["week_id"],
        "level": hit["level"],
        "close": hit.get("close"),
        "last_price": hit.get("close"),
        "last_checked_at": eastern_now().isoformat(),
        "level_date": hit.get("level_date"),
        "admitted_at": eastern_now().isoformat(),
    }
    return True


def discord_retry_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
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
    *,
    chart_symbol: str,
    level: float,
    level_date: str,
) -> str | None:
    payload_data = {
        "username": WEBHOOK_USERNAME,
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
        chart_path = temporary_weekly_chart_path(chart_symbol)
        chart_path = generate_weekly_chart(
            chart_symbol,
            output_path=chart_path,
            weeks=80,
            full_width_levels=True,
            level_segments=[
                {
                    "price": level,
                    "start_date": level_date,
                }
            ],
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


def scan_instrument(
    instrument: dict[str, Any],
    *,
    min_price: float,
    min_avg_volume: float,
) -> dict[str, Any] | None:
    weekly, daily = fetch_weekly_from_yahoo(instrument["chart_symbol"])
    if instrument["market"] == "us" and not passes_us_liquidity(
        daily,
        min_price=min_price,
        min_avg_volume=min_avg_volume,
    ):
        return None
    weekly = closed_weekly_candles(weekly, market=str(instrument["market"]))
    classification = classify_weekly_structure(weekly)
    if classification.get("side") not in {"gain", "loss"}:
        return None
    if first_visit_already_used(
        weekly,
        side=str(classification["side"]),
        level=float(classification["level"]),
        proximity=DEFAULT_RETEST_PCT / 100.0,
    ):
        return None
    return {
        **instrument,
        **classification,
    }


def scan_universe_batch(
    instruments: list[dict[str, Any]],
    *,
    min_price: float,
    min_avg_volume: float,
    yahoo_delay: float,
) -> tuple[list[dict[str, Any]], int, int]:
    hits: list[dict[str, Any]] = []
    scanned = 0
    failures = 0
    for index, instrument in enumerate(instruments):
        try:
            hit = scan_instrument(
                instrument,
                min_price=min_price,
                min_avg_volume=min_avg_volume,
            )
            scanned += 1
            if hit is not None:
                hits.append(hit)
        except Exception as exc:
            failures += 1
            print(
                f"Skipping {instrument.get('symbol')}: {exc}",
                flush=True,
            )
        if index + 1 < len(instruments) and yahoo_delay > 0:
            time.sleep(yahoo_delay)
    return hits, scanned, failures


def build_scan_instruments(
    *,
    markets: str,
    crypto_limit: int,
) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []
    if markets in {"all", "us"}:
        for symbol in build_us_universe():
            instruments.append(
                {
                    "symbol": symbol,
                    "name": symbol,
                    "market": "us",
                    "chart_symbol": yahoo_us_symbol(symbol),
                }
            )
    if markets in {"all", "crypto"}:
        instruments.extend(build_crypto_universe(crypto_limit))
    return instruments


def us_scan_complete_for_week(now: datetime | None = None) -> bool:
    return us_scan_window(now)


def market_scan_bucket(
    state: dict[str, Any],
    market: str,
    week_id: str,
) -> dict[str, Any]:
    scan = state.setdefault("scan", {})
    markets = scan.setdefault("markets", {})
    bucket = markets.setdefault(market, {})
    if bucket.get("week_id") != week_id:
        bucket.clear()
        bucket["week_id"] = week_id
        bucket["processed"] = []
    if not isinstance(bucket.get("processed"), list):
        bucket["processed"] = []
    return bucket


def next_scan_batch(
    instruments: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    batch_size: int,
    week_id: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = now or eastern_now()
    remaining: list[dict[str, Any]] = []
    for instrument in instruments:
        market = str(instrument["market"])
        if not scan_market_allowed(market, current):
            continue
        market_week_id = scan_week_id_for_market(market, current)
        bucket = market_scan_bucket(state, market, market_week_id)
        identity = f"{market}:{instrument['symbol']}"
        if identity in set(bucket.get("processed") or []):
            continue
        if (
            market == "us"
            and bucket.get("completed_week_id") == market_week_id
            and us_scan_complete_for_week(current)
        ):
            continue
        remaining.append(instrument)
    scan = state.setdefault("scan", {})
    scan["week_id"] = week_id
    return remaining[:batch_size]


def mark_batch_processed(
    state: dict[str, Any],
    batch: list[dict[str, Any]],
    *,
    week_id: str,
    instruments: list[dict[str, Any]],
    now: datetime | None = None,
) -> None:
    current = now or eastern_now()
    scan = state.setdefault("scan", {})
    scan["week_id"] = week_id
    for market in {str(item["market"]) for item in batch}:
        market_week_id = scan_week_id_for_market(market, current)
        bucket = market_scan_bucket(state, market, market_week_id)
        processed = list(bucket.get("processed") or [])
        for instrument in batch:
            if str(instrument["market"]) != market:
                continue
            identity = f"{market}:{instrument['symbol']}"
            if identity not in processed:
                processed.append(identity)
        bucket["processed"] = processed
        if market == "us":
            remaining_us = [
                item
                for item in instruments
                if item["market"] == "us"
                and f"us:{item['symbol']}" not in set(processed)
            ]
            if not remaining_us:
                bucket["completed_week_id"] = market_week_id
                scan["us_completed_week_id"] = market_week_id


def watch_stored_price(record: dict[str, Any]) -> float | None:
    for field in ("last_price", "close"):
        value = record.get(field)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def watch_is_near(record: dict[str, Any], near_pct: float) -> bool:
    price = watch_stored_price(record)
    if price is None:
        return False
    return retest_distance(price, float(record["level"])) <= near_pct


def select_watch_fetches(
    records: list[dict[str, Any]],
    *,
    near_pct: float,
    far_per_run: int,
    cursor: int,
) -> tuple[list[dict[str, Any]], int]:
    """Quote names already near the line every hour; rotate the rest."""
    near: list[dict[str, Any]] = []
    far: list[dict[str, Any]] = []
    for record in records:
        if watch_is_near(record, near_pct):
            near.append(record)
        else:
            far.append(record)
    far.sort(key=lambda item: str(item.get("symbol") or ""))
    if not far or far_per_run <= 0:
        return near, cursor
    start = cursor % len(far)
    rotated = far[start:] + far[:start]
    chosen_far = rotated[:far_per_run]
    new_cursor = (start + len(chosen_far)) % len(far)
    return near + chosen_far, new_cursor


def evaluate_watchlist(
    state: dict[str, Any],
    *,
    proximity: float,
    yahoo_delay: float,
    near_pct: float = DEFAULT_WATCH_NEAR_PCT / 100.0,
    far_per_run: int = DEFAULT_WATCH_FAR_PER_RUN,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    hits: list[dict[str, Any]] = []
    failures = 0
    checked = 0
    current = now or eastern_now()
    watchlist = state.get("watchlist") or {}
    active: list[dict[str, Any]] = []
    for record in watchlist.values():
        if not isinstance(record, dict):
            continue
        key = watch_key(record["symbol"], record["side"], float(record["level"]))
        if already_posted(state, key) or already_posted_name(
            state, str(record["symbol"]), str(record["side"])
        ):
            continue
        active.append(record)
    cursor = int(state.get("watch_far_cursor") or 0)
    to_fetch, new_cursor = select_watch_fetches(
        active,
        near_pct=near_pct,
        far_per_run=far_per_run,
        cursor=cursor,
    )
    state["watch_far_cursor"] = new_cursor
    for index, record in enumerate(to_fetch):
        try:
            last_price = latest_chart_close(record["chart_symbol"])
        except Exception as exc:
            failures += 1
            print(f"Skipping watch {record.get('symbol')}: {exc}", flush=True)
            continue
        checked += 1
        record["last_price"] = last_price
        record["last_checked_at"] = current.isoformat()
        if not is_retest(last_price, float(record["level"]), proximity=proximity):
            if index + 1 < len(to_fetch) and yahoo_delay > 0:
                time.sleep(yahoo_delay)
            continue
        try:
            weekly, _daily = fetch_weekly_from_yahoo(str(record["chart_symbol"]))
            weekly = closed_weekly_candles(
                weekly,
                market=str(record.get("market") or "us"),
            )
        except Exception as exc:
            failures += 1
            print(f"Skipping watch {record.get('symbol')}: {exc}", flush=True)
            continue
        if first_visit_already_used(
            weekly,
            side=str(record["side"]),
            level=float(record["level"]),
            proximity=proximity,
        ):
            if index + 1 < len(to_fetch) and yahoo_delay > 0:
                time.sleep(yahoo_delay)
            continue
        hits.append(
            {
                **record,
                "last_price": last_price,
                "distance": retest_distance(last_price, float(record["level"])),
            }
        )
        if index + 1 < len(to_fetch) and yahoo_delay > 0:
            time.sleep(yahoo_delay)
    return hits, failures, checked


def seed_watch_hits(state: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
    for hit in hits:
        key = posted_key(hit["symbol"], hit["side"], float(hit["level"]))
        if already_posted(state, key):
            continue
        mark_posted(
            state,
            key=key,
            hit=hit,
            date_label=eastern_today_label(),
            message_id=None,
            seeded=True,
        )
    state["seeded"] = True
    return state


def print_preview_list(
    title: str,
    hits: list[dict[str, Any]],
    preview_limit: int | None,
) -> None:
    print(title)
    print("=" * len(title))
    if not hits:
        print("None.")
        return
    limit = preview_limit if preview_limit is not None else len(hits)
    for rank, hit in enumerate(hits[:limit], start=1):
        extra = ""
        if hit.get("last_price") is not None:
            extra = f" last {format_price(float(hit['last_price']))}"
        print(
            f"{rank:>2}. "
            f"{hit['symbol']:<8} "
            f"{hit['market']:<6} "
            f"{hit['side']:<4} "
            f"{hit['week_id']} "
            f"level {format_price(hit['level'])}"
            f"{extra}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan for weekly gains/losses into a watchlist, then post "
            "when last price retests that weekly level within 1%."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Print the next scan batch and current retest hits without posting.",
    )
    mode.add_argument(
        "--scan",
        action="store_true",
        help="Process the next universe batch into the watchlist. No Discord.",
    )
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Check watchlist last prices and post 1% retests.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional posting cap for this watch run.",
    )
    parser.add_argument(
        "--preview-limit",
        type=int,
        default=20,
        help="How many hits to print in preview mode.",
    )
    parser.add_argument(
        "--markets",
        choices=("all", "us", "crypto"),
        default="all",
        help="Which universe to scan.",
    )
    return parser.parse_args(argv)


def configure_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def runtime_settings() -> dict[str, Any]:
    return {
        "crypto_limit": env_int(
            "WEEKLY_SCREENER_CRYPTO_UNIVERSE_SIZE",
            DEFAULT_CRYPTO_UNIVERSE,
        ),
        "min_price": env_float("WEEKLY_SCREENER_US_MIN_PRICE", DEFAULT_US_MIN_PRICE),
        "min_avg_volume": env_float(
            "WEEKLY_SCREENER_US_MIN_AVG_VOLUME",
            DEFAULT_US_MIN_AVG_VOLUME,
        ),
        "yahoo_delay": env_float(
            "WEEKLY_SCREENER_YAHOO_DELAY_SECONDS",
            DEFAULT_YAHOO_DELAY,
        ),
        "batch_size": env_int(
            "WEEKLY_SCREENER_SCAN_BATCH_SIZE",
            DEFAULT_SCAN_BATCH_SIZE,
        ),
        "expire_weeks": env_int(
            "WEEKLY_SCREENER_WATCH_EXPIRE_WEEKS",
            DEFAULT_WATCH_EXPIRE_WEEKS,
        ),
        "proximity": env_float(
            "WEEKLY_SCREENER_RETEST_PCT",
            DEFAULT_RETEST_PCT,
        )
        / 100.0,
        "watch_near_pct": env_float(
            "WEEKLY_SCREENER_WATCH_NEAR_PCT",
            DEFAULT_WATCH_NEAR_PCT,
        )
        / 100.0,
        "watch_far_per_run": env_int(
            "WEEKLY_SCREENER_WATCH_FAR_PER_RUN",
            DEFAULT_WATCH_FAR_PER_RUN,
        ),
    }


def run_scan_batch(
    *,
    markets: str,
    settings: dict[str, Any],
    state: dict[str, Any] | None,
    persist: bool,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    current = now or eastern_now()
    skip_reason = scan_skip_reason(markets, current)
    if skip_reason:
        print(skip_reason)
        return [], 0, 0, 0
    instruments = build_scan_instruments(
        markets=markets,
        crypto_limit=settings["crypto_limit"],
    )
    working = state if state is not None else empty_state()
    week_id = current_iso_week_id(current)
    batch = next_scan_batch(
        instruments,
        working,
        batch_size=settings["batch_size"],
        week_id=week_id,
        now=current,
    )
    if not batch:
        reason = scan_skip_reason(markets, current)
        if reason:
            print(reason)
        return [], 0, 0, 0
    hits, scanned, failures = scan_universe_batch(
        batch,
        min_price=settings["min_price"],
        min_avg_volume=settings["min_avg_volume"],
        yahoo_delay=settings["yahoo_delay"],
    )
    admitted = 0
    if persist:
        for hit in hits:
            if admit_watch(working, hit):
                admitted += 1
        mark_batch_processed(
            working,
            batch,
            week_id=week_id,
            instruments=instruments,
            now=current,
        )
        save_state_locked(working)
    return hits, scanned, failures, admitted


def main(argv: list[str] | None = None) -> None:
    arguments = parse_args(argv)
    settings = runtime_settings()
    date_label = eastern_today_label()
    week_id = current_iso_week_id()

    if arguments.preview:
        configure_stdout()
        state = load_state() if STATE_PATH.exists() else empty_state()
        hits, scanned, failures, _admitted = run_scan_batch(
            markets=arguments.markets,
            settings=settings,
            state=copy.deepcopy(state),
            persist=False,
        )
        print_preview_list(
            "WATCHLIST ADMISSIONS (NO POST)",
            hits,
            arguments.preview_limit,
        )
        watch_hits, watch_failures, _checked = evaluate_watchlist(
            state,
            proximity=settings["proximity"],
            yahoo_delay=settings["yahoo_delay"],
            near_pct=settings["watch_near_pct"],
            far_per_run=settings["watch_far_per_run"],
        )
        print()
        print_preview_list(
            "RETEST HITS (NO POST)",
            watch_hits,
            arguments.preview_limit,
        )
        if watch_hits:
            print()
            print("SAMPLE DISCORD POST")
            print("=" * 18)
            print(build_screener_message(watch_hits[0]))
        print()
        print(
            f"Scan batch scanned: {scanned} | "
            f"Watch admissions: {len(hits)} | "
            f"Retest hits: {len(watch_hits)} | "
            f"Failures: {failures + watch_failures}"
        )
        print()
        print("PREVIEW ONLY — nothing was posted; posting state was not changed.")
        return

    if arguments.scan:
        state = load_state()
        expired = expire_stale_watches(
            state,
            now_week_id=week_id,
            expire_weeks=settings["expire_weeks"],
        )
        hits, scanned, failures, admitted = run_scan_batch(
            markets=arguments.markets,
            settings=settings,
            state=state,
            persist=True,
        )
        print(
            f"Weekly screener scan complete for {week_id}: "
            f"{scanned} scanned, "
            f"{len(hits)} gained/lost, "
            f"{admitted} new watches, "
            f"{expired} expired, "
            f"{failures} skipped. No Discord post."
        )
        return

    webhook_url = resolve_webhook()
    state = load_state()
    expire_stale_watches(
        state,
        now_week_id=week_id,
        expire_weeks=settings["expire_weeks"],
    )
    hits, failures, checked = evaluate_watchlist(
        state,
        proximity=settings["proximity"],
        yahoo_delay=settings["yahoo_delay"],
        near_pct=settings["watch_near_pct"],
        far_per_run=settings["watch_far_per_run"],
    )
    save_state_locked(state)

    if not state.get("seeded"):
        state = seed_watch_hits(state, hits)
        save_state_locked(state)
        print(
            f"Seeded weekly screener state with {len(state['posted'])} "
            "current retest hit(s) and posted nothing."
        )
        return

    post_limit = arguments.limit
    posted_count = 0
    for hit in hits:
        if post_limit is not None and posted_count >= post_limit:
            break
        key = posted_key(hit["symbol"], hit["side"], float(hit["level"]))
        reserved = False
        with exclusive_state():
            state = load_state()
            if already_posted(state, key) or already_posted_name(
                state, str(hit["symbol"]), str(hit["side"])
            ):
                continue
            state = mark_posted(
                state,
                key=key,
                hit=hit,
                date_label=date_label,
                message_id=None,
            )
            save_state(state)
            reserved = True
        if not reserved:
            continue
        message_id = send_discord_message(
            webhook_url,
            build_screener_message(hit),
            chart_symbol=hit["chart_symbol"],
            level=float(hit["level"]),
            level_date=str(hit.get("level_date") or date_label),
        )
        with exclusive_state():
            state = load_state()
            posted = state.setdefault("posted", {})
            record = posted.get(key)
            if isinstance(record, dict):
                record["discord_message_id"] = message_id
                save_state(state)
        posted_count += 1
        time.sleep(DISCORD_POST_DELAY_SECONDS)

    print(
        f"Weekly screener watch complete for {date_label}: "
        f"{checked} priced, "
        f"{len(hits)} retest hits, "
        f"{posted_count} posted, "
        f"{failures} skipped."
    )


if __name__ == "__main__":
    main()
