"""Whether a command's reply is posted in the channel or shown only to the
person who ran it.

Replies are private by default, so a channel is not filled with bot output.
They are public when:
  * the server's managers opened the channel (or the whole server) to
    public replies, or
  * a manager passes ``public: True``.
Anyone can pass ``public: False`` to keep a reply private in an open channel.
Direct messages are unaffected.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

from lib.utils import is_manager

_log = logging.getLogger(__name__)

PUBLIC_OPTION_HELP = (
    "True: post in the channel (managers only, outside open channels). False: only you see it."
)

GET_SETTINGS = "SELECT public_everywhere FROM guild_reply_settings WHERE guild_id = ?;"
GET_CHANNELS = "SELECT channel_id FROM guild_public_channels WHERE guild_id = ?;"
UPSERT_MODE = """
INSERT INTO guild_reply_settings (guild_id, public_everywhere, updated_by, updated_at)
VALUES (?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(guild_id) DO UPDATE SET
    public_everywhere = excluded.public_everywhere,
    updated_by = excluded.updated_by,
    updated_at = CURRENT_TIMESTAMP;
"""
DELETE_CHANNELS = "DELETE FROM guild_public_channels WHERE guild_id = ?;"
INSERT_CHANNEL = "INSERT OR IGNORE INTO guild_public_channels (guild_id, channel_id) VALUES (?, ?);"
TOUCH_SETTINGS = """
INSERT INTO guild_reply_settings (guild_id, updated_by, updated_at)
VALUES (?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(guild_id) DO UPDATE SET
    updated_by = excluded.updated_by,
    updated_at = CURRENT_TIMESTAMP;
"""

# guild_id -> (public_everywhere, open channel ids). Every write goes through
# this module and refreshes the entry, so the cache never goes stale.
_cache: dict[int, tuple[bool, frozenset[int]]] = {}


async def guild_settings(bot, guild_id: int) -> tuple[bool, frozenset[int]]:
    cached = _cache.get(guild_id)
    if cached is not None:
        return cached
    try:
        row = await bot.GET_ONE(GET_SETTINGS, (guild_id,))
        channels = await bot.GET(GET_CHANNELS, (guild_id,))
    except Exception:  # noqa: BLE001
        # Unreadable settings fall back to the private default.
        _log.warning("reply settings unavailable for guild=%s", guild_id, exc_info=True)
        return False, frozenset()
    settings = (bool(row and row[0]), frozenset(c for (c,) in channels))
    _cache[guild_id] = settings
    return settings


async def set_public_everywhere(bot, guild_id: int, value: bool, by_user: int) -> None:
    await bot.RUN(UPSERT_MODE, (guild_id, 1 if value else 0, by_user))
    _cache.pop(guild_id, None)


async def set_public_channels(bot, guild_id: int, channel_ids: list[int], by_user: int) -> None:
    statements = [(DELETE_CHANNELS, (guild_id,))]
    statements += [(INSERT_CHANNEL, (guild_id, cid)) for cid in channel_ids]
    statements.append((TOUCH_SETTINGS, (guild_id, by_user)))
    await bot.RUN_TRANSACTION(statements)
    _cache.pop(guild_id, None)


def _channel_ids(interaction: discord.Interaction) -> set[int]:
    """The channel, plus its parent for threads and forum posts."""
    ids = set()
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id:
        ids.add(channel_id)
    parent = getattr(getattr(interaction, "channel", None), "parent_id", None)
    if parent:
        ids.add(parent)
    return ids


async def channel_is_open(interaction: discord.Interaction) -> bool:
    public_everywhere, channels = await guild_settings(interaction.client, interaction.guild_id)
    return public_everywhere or bool(_channel_ids(interaction) & channels)


async def reply_is_private(interaction: discord.Interaction, public: Optional[bool] = None) -> bool:
    if interaction.guild_id is None:
        return False
    if public is False:
        return True
    if await channel_is_open(interaction):
        return False
    if public is True:
        return not await is_manager(interaction)
    return True


async def defer_reply(interaction: discord.Interaction, public: Optional[bool] = None) -> bool:
    """Defer with the visibility the rules above give. Returns True when the
    reply is private. Every public command sends a single follow-up, which
    takes the visibility of this deferral."""
    private = await reply_is_private(interaction, public)
    await interaction.response.defer(ephemeral=private)
    return private
