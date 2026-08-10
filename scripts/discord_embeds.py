from __future__ import annotations


DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DEFAULT_EMBED_COLOR = 0x5865F2


def bordered_webhook_payload(
    username,
    description,
    *,
    color=DEFAULT_EMBED_COLOR,
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

    return {
        "username": username,
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "description": description,
                "color": color,
            }
        ],
    }
