"""Whether a command's reply is posted in the channel or shown only to the
person who ran it.

Replies are private by default, so a channel is not filled with bot output.
They are public when:
  * the server's managers opened the channel (or the whole server) to
    public replies,
  * the server's managers made the command public by default, or
  * a manager passes ``public: True``.
Anyone can pass ``public: False`` to keep a reply private in an open channel.
Direct messages are unaffected.
"""

from __future__ import annotations

import logging
from typing import NamedTuple, Optional

import discord
from discord import app_commands

from lib.utils import is_manager

_log = logging.getLogger(__name__)

PUBLIC_OPTION_HELP = (
    "True: post in the channel (managers only, outside open channels). False: only you see it."
)

GET_SETTINGS = "SELECT public_everywhere FROM guild_reply_settings WHERE guild_id = ?;"
GET_CHANNELS = "SELECT channel_id FROM guild_public_channels WHERE guild_id = ?;"
GET_COMMANDS = "SELECT command_name FROM guild_public_commands WHERE guild_id = ?;"
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
DELETE_COMMANDS = "DELETE FROM guild_public_commands WHERE guild_id = ?;"
INSERT_COMMAND = "INSERT OR IGNORE INTO guild_public_commands (guild_id, command_name) VALUES (?, ?);"
TOUCH_SETTINGS = """
INSERT INTO guild_reply_settings (guild_id, updated_by, updated_at)
VALUES (?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(guild_id) DO UPDATE SET
    updated_by = excluded.updated_by,
    updated_at = CURRENT_TIMESTAMP;
"""


class GuildSettings(NamedTuple):
    public_everywhere: bool
    channels: frozenset[int]
    commands: frozenset[str]


PRIVATE_DEFAULT = GuildSettings(False, frozenset(), frozenset())

# Every write goes through this module and refreshes the entry, so the cache
# never goes stale.
_cache: dict[int, GuildSettings] = {}


async def guild_settings(bot, guild_id: int) -> GuildSettings:
    cached = _cache.get(guild_id)
    if cached is not None:
        return cached
    try:
        row = await bot.GET_ONE(GET_SETTINGS, (guild_id,))
        channels = await bot.GET(GET_CHANNELS, (guild_id,))
        commands = await bot.GET(GET_COMMANDS, (guild_id,))
    except Exception:  # noqa: BLE001
        # Unreadable settings fall back to the private default.
        _log.warning("reply settings unavailable for guild=%s", guild_id, exc_info=True)
        return PRIVATE_DEFAULT
    settings = GuildSettings(
        bool(row and row[0]),
        frozenset(c for (c,) in channels),
        frozenset(c for (c,) in commands),
    )
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


async def set_public_commands(bot, guild_id: int, names: list[str], by_user: int) -> None:
    statements = [(DELETE_COMMANDS, (guild_id,))]
    statements += [(INSERT_COMMAND, (guild_id, name)) for name in names]
    statements.append((TOUCH_SETTINGS, (guild_id, by_user)))
    await bot.RUN_TRANSACTION(statements)
    _cache.pop(guild_id, None)


def public_capable_commands(bot) -> list[str]:
    """Commands whose reply visibility follows these rules: those taking the
    ``public`` option."""
    tree = getattr(bot, "tree", None)
    if tree is None:
        return []
    return sorted(
        cmd.qualified_name
        for cmd in tree.walk_commands()
        if isinstance(cmd, app_commands.Command)
        and any(p.name == "public" for p in cmd.parameters)
    )


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


async def public_by_default(interaction: discord.Interaction) -> bool:
    settings = await guild_settings(interaction.client, interaction.guild_id)
    command = getattr(getattr(interaction, "command", None), "qualified_name", None)
    return (
        settings.public_everywhere
        or bool(_channel_ids(interaction) & settings.channels)
        or command in settings.commands
    )


async def reply_is_private(interaction: discord.Interaction, public: Optional[bool] = None) -> bool:
    if interaction.guild_id is None:
        return False
    if public is False:
        return True
    if await public_by_default(interaction):
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
