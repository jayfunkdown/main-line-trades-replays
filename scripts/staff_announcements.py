#!/usr/bin/env python3
"""Staff announcement cards for the Earnings Review bot."""

from __future__ import annotations

from pathlib import Path

try:
    from scripts.discord_embeds import BRAND_NEON_PINK, bordered_embed
except ModuleNotFoundError:
    from discord_embeds import BRAND_NEON_PINK, bordered_embed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANNOUNCEMENT_BANNER_PATH = PROJECT_ROOT / "assets" / "announcement_banner.png"
ANNOUNCEMENT_BANNER_FILENAME = "announcement_banner.png"

SIGNALS_ANNOUNCEMENT_HEADLINE = "A new Signals channel is live."
SIGNALS_ANNOUNCEMENT_BODY = """
We've opened a dedicated **Signals** channel for trade setups.

📈 **What you'll see**
--------------------------------
🎯 Trade Signal cards with direction and the reference level
📊 Weekly charts drawn on the weekly, same format as the rest of the server
🧠 A short thesis on each setup

This is the home for those cards going forward. Keep an eye on it when new names hit.
""".strip()


def banner_path() -> Path:
    if not ANNOUNCEMENT_BANNER_PATH.is_file():
        raise RuntimeError(
            f"Announcement banner is missing: {ANNOUNCEMENT_BANNER_PATH}"
        )
    return ANNOUNCEMENT_BANNER_PATH


def build_announcement_description(headline: str, body: str) -> str:
    headline_text = str(headline or "").strip()
    body_text = str(body or "").strip()
    if not headline_text:
        raise ValueError("Announcement headline is required.")
    if not body_text:
        raise ValueError("Announcement body is required.")
    return "\n\n".join(
        [
            f"**{headline_text}**",
            body_text,
        ]
    )


def announcement_embed(description: str) -> dict:
    return bordered_embed(
        description,
        color=BRAND_NEON_PINK,
        image_url=f"attachment://{ANNOUNCEMENT_BANNER_FILENAME}",
    )
