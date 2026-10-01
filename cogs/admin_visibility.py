"""`/manage_visibility`: where command replies are posted publicly.

Replies are private by default (see lib/visibility.py). Managers open
chosen channels, or the whole server, to public replies from a private
panel.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from lib import style
from lib.bot import VNClubBot
from lib.utils import BotError, handle_command_error, send_error, validate_user_permission
from lib import visibility

_log = logging.getLogger(__name__)

PANEL_TIMEOUT = 600
# Discord caps a select at 25 values.
MAX_OPEN_CHANNELS = 25
OPEN_CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.forum,
    discord.ChannelType.media,
    discord.ChannelType.voice,
]
MODE_PRIVATE = "private"
MODE_PUBLIC = "public"


def panel_embed(public_everywhere: bool, channels: frozenset[int]) -> discord.Embed:
    embed = discord.Embed(title=style.title("🛠️", "Reply visibility"), color=style.ACCENT)
    embed.description = (
        "Replies are shown only to the person who ran the command, except in "
        "open channels. Managers can post any reply publicly with `public: True`; "
        "anyone can keep a reply private with `public: False`. "
        "Threads and forum posts follow their parent channel."
    )
    embed.add_field(
        name="Mode",
        value="Public everywhere" if public_everywhere else "Private except open channels (default)",
        inline=False,
    )
    if public_everywhere:
        open_text = "Every channel is open while the mode is Public everywhere."
    elif channels:
        open_text = " ".join(f"<#{cid}>" for cid in sorted(channels))
    else:
        open_text = "None yet. Pick channels below, such as a bot channel."
    embed.add_field(name="Open channels", value=open_text, inline=False)
    return embed


class VisibilityPanel(discord.ui.View):
    def __init__(self, bot: VNClubBot, guild_id: int, owner_id: int,
                 public_everywhere: bool, channels: frozenset[int]):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id

        mode = discord.ui.Select(
            placeholder="Mode",
            options=[
                discord.SelectOption(
                    label="Private except open channels (default)", value=MODE_PRIVATE,
                    default=not public_everywhere,
                ),
                discord.SelectOption(
                    label="Public everywhere", value=MODE_PUBLIC, default=public_everywhere,
                ),
            ],
            row=0,
        )
        mode.callback = self._on_mode
        self.add_item(mode)

        picker = discord.ui.ChannelSelect(
            placeholder="Open channels: replies here are public",
            channel_types=OPEN_CHANNEL_TYPES,
            min_values=0,
            max_values=MAX_OPEN_CHANNELS,
            default_values=[
                discord.SelectDefaultValue(id=cid, type=discord.SelectDefaultValueType.channel)
                for cid in sorted(channels)[:MAX_OPEN_CHANNELS]
            ],
            row=1,
        )
        picker.callback = self._on_channels
        self.add_item(picker)
        self.picker = picker

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await send_error(interaction, style.error("This panel isn't yours. Run `/manage_visibility` to open your own."))
            return False
        try:
            # Manager rights can be revoked while the panel is open.
            await validate_user_permission(interaction)
        except BotError as e:
            await handle_command_error(interaction, e)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        public_everywhere, channels = await visibility.guild_settings(self.bot, self.guild_id)
        panel = VisibilityPanel(self.bot, self.guild_id, self.owner_id, public_everywhere, channels)
        self.stop()
        await interaction.response.edit_message(
            embed=panel_embed(public_everywhere, channels), view=panel,
        )

    async def _on_mode(self, interaction: discord.Interaction) -> None:
        value = interaction.data.get("values", [MODE_PRIVATE])[0]
        await visibility.set_public_everywhere(
            self.bot, self.guild_id, value == MODE_PUBLIC, interaction.user.id,
        )
        _log.info(
            "reply visibility mode: guild=%s manager=%s mode=%s",
            self.guild_id, interaction.user.id, value,
        )
        await self._refresh(interaction)

    async def _on_channels(self, interaction: discord.Interaction) -> None:
        ids = [c.id for c in self.picker.values]
        await visibility.set_public_channels(self.bot, self.guild_id, ids, interaction.user.id)
        _log.info(
            "reply visibility channels: guild=%s manager=%s count=%d",
            self.guild_id, interaction.user.id, len(ids),
        )
        await self._refresh(interaction)


class AdminVisibility(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @app_commands.command(
        name="manage_visibility",
        description="[MANAGER] Choose where command replies are posted publicly.",
    )
    @app_commands.guild_only()
    async def manage_visibility(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await validate_user_permission(interaction)
            public_everywhere, channels = await visibility.guild_settings(self.bot, interaction.guild_id)
            view = VisibilityPanel(
                self.bot, interaction.guild_id, interaction.user.id, public_everywhere, channels,
            )
            await interaction.followup.send(
                embed=panel_embed(public_everywhere, channels), view=view, ephemeral=True,
            )
        except BotError as e:
            await handle_command_error(interaction, e)


async def setup(bot: VNClubBot):
    await bot.add_cog(AdminVisibility(bot))
