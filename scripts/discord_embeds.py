from __future__ import annotations


DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
BRAND_ELECTRIC_BLUE = 0x00CFFF
BRAND_NEON_PINK = 0xFF2BD6
DEFAULT_EMBED_COLOR = BRAND_ELECTRIC_BLUE


def bordered_embed(
    description,
    *,
    color=DEFAULT_EMBED_COLOR,
    image_url=None,
):
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Discord embed description must be nonempty.")

    if len(description) > DISCORD_EMBED_DESCRIPTION_LIMIT:
        raise ValueError("Discord embed description exceeds 4,096 characters.")

    if (
        not isinstance(color, int)
        or isinstance(color, bool)
        or not 0 <= color <= 0xFFFFFF
    ):
        raise ValueError("Discord embed color is invalid.")

    embed = {
        "description": description,
        "color": color,
    }
    if image_url is not None:
        if not isinstance(image_url, str) or not image_url.strip():
            raise ValueError("Discord embed image URL must be nonempty.")
        embed["image"] = {"url": image_url}
    return embed


def bordered_webhook_payload(
    username,
    description,
    *,
    color=DEFAULT_EMBED_COLOR,
):
    return {
        "username": username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            bordered_embed(description, color=color)
        ],
    }
