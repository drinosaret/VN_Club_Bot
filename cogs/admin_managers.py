"""`/manage_managers` — grant/revoke per-guild VN manager permission.

Restricted to ``AUTHORIZED_USERS`` (bot operators). No bootstrap
exception, no Discord-permission fallback: the host is the only
principal that can edit the manager list. The
``@app_commands.default_permissions(administrator=True)`` decorator
hides the command from non-admin members in Discord's UI, but that's
cosmetic — the real gate is the ``AUTHORIZED_USER_IDS`` check inside
the callback.

The command operates on the current guild by default; the optional
``guild_id`` parameter lets the host target another guild the bot is
in (for grant/revoke without joining each server's interaction
context).
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from lib.autocomplete import bot_guilds_autocomplete
from lib.bot import VNClubBot
from lib.utils import (
    AUTHORIZED_USER_IDS,
    DatabaseQueries,
    ValidationError,
    handle_command_error,
)

_log = logging.getLogger(__name__)


_ACTION_CHOICES = [
    app_commands.Choice(name="Add a manager (user or role)", value="add"),
    app_commands.Choice(name="Remove a manager", value="remove"),
    app_commands.Choice(name="List managers for the target guild", value="list"),
]


def _parse_guild_id(raw: str) -> int:
    """Parse the required guild_id parameter into an int.

    The slash command takes guild_id as a string because Discord has no
    native guild-ID parameter type. The autocomplete dropdown surfaces
    every guild the bot is in by name, but the parameter still accepts
    a hand-typed numeric ID for the rare case where the bot was just
    added to a guild and the dropdown hasn't refreshed.
    """
    try:
        return int((raw or "").strip())
    except ValueError:
        raise ValidationError(
            f"bad guild_id {raw!r}",
            "`guild_id` must be a numeric Discord guild ID.",
        )


# The cross-guild target picker is shared with /manage_pool via
# lib.autocomplete.bot_guilds_autocomplete — same "list every guild
# the bot is in, no default-to-current-guild" semantics. Keep both
# admin commands using the same dropdown so a host doesn't have to
# remember which one offers autocomplete and which doesn't.


def _format_principal_line(
    bot: VNClubBot, guild: Optional[discord.Guild],
    principal_type: str, principal_id: int,
    added_by_user_id: Optional[int], added_at: str,
) -> str:
    """Render one entry for the `list` action.

    Falls back gracefully when the bot can't resolve a user/role —
    common when listing a guild the bot host isn't in, or for stale
    grants whose target left the server.
    """
    if principal_type == "user":
        target = f"<@{principal_id}>"
    elif principal_type == "role":
        role = guild.get_role(principal_id) if guild else None
        target = f"`@{role.name}` (role)" if role else f"`<role {principal_id}>`"
    else:
        target = f"`{principal_type}:{principal_id}`"
    added_by = f"<@{added_by_user_id}>" if added_by_user_id else "unknown"
    return f"- {target} · added by {added_by} · {added_at}"


class AdminManagers(commands.Cog):
    """Single-command cog. Sits next to `/sync` rather than crowding
    `/manage_voting`'s already-busy dashboard.
    """

    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @app_commands.command(
        name="manage_managers",
        description="Grant/revoke per-guild VN-manager permission (bot operator only).",
    )
    @app_commands.choices(action=_ACTION_CHOICES)
    @app_commands.describe(
        action="add, remove, or list managers.",
        guild_id="The server to manage (pick from the dropdown).",
        user="The user to add or remove (omit for role-targeted ops or list).",
        role="The role to add or remove (omit for user-targeted ops or list).",
    )
    @app_commands.autocomplete(guild_id=bot_guilds_autocomplete)
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_managers(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        guild_id: str,
        user: Optional[discord.User] = None,
        role: Optional[discord.Role] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            # AUTHORIZED_USERS only. default_permissions above hides
            # the command from non-admins in Discord's UI but that's
            # cosmetic — this is the actual gate.
            if interaction.user.id not in AUTHORIZED_USER_IDS:
                raise ValidationError(
                    f"User {interaction.user.id} not in AUTHORIZED_USERS",
                    "Only bot operators can manage the manager list.",
                )

            target_guild_id = _parse_guild_id(guild_id)
            target_guild = self.bot.get_guild(target_guild_id)

            if action.value == "list":
                await self._managers_list(interaction, target_guild_id, target_guild)
                return

            # add / remove both need exactly one of user/role.
            if user is None and role is None:
                raise ValidationError(
                    f"action {action.value} requires user or role",
                    f"`{action.value}` needs a `user` or `role` to target.",
                )
            if user is not None and role is not None:
                raise ValidationError(
                    f"action {action.value} got both user and role",
                    "Pick exactly one of `user` or `role`, not both.",
                )

            if action.value == "add":
                await self._managers_add(
                    interaction, target_guild_id, target_guild, user, role,
                )
            elif action.value == "remove":
                await self._managers_remove(
                    interaction, target_guild_id, target_guild, user, role,
                )
            else:
                # Shouldn't reach — Discord constrains action to the
                # three Choices above.
                raise ValidationError(
                    f"unknown action {action.value!r}",
                    f"Unknown action `{action.value}`.",
                )
        except ValidationError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/manage_managers failed")
            await handle_command_error(
                interaction, e,
                "Something went wrong managing the manager list.",
            )

    async def _managers_add(
        self,
        interaction: discord.Interaction,
        target_guild_id: int,
        target_guild: Optional[discord.Guild],
        user: Optional[discord.User],
        role: Optional[discord.Role],
    ) -> None:
        principal_type = "user" if user is not None else "role"
        principal_id = user.id if user is not None else role.id

        # INSERT OR IGNORE — re-adding is a friendly no-op. To tell
        # the host whether the row was actually inserted, check the
        # table first.
        already = await self.bot.GET_ONE(
            "SELECT 1 FROM guild_managers "
            "WHERE guild_id = ? AND principal_type = ? AND principal_id = ? LIMIT 1",
            (target_guild_id, principal_type, principal_id),
        )
        await self.bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER,
            (target_guild_id, principal_type, principal_id, interaction.user.id),
        )

        target_label = user.mention if user is not None else role.mention
        guild_label = target_guild.name if target_guild else f"guild `{target_guild_id}`"
        verb = "already a manager" if already else "added as manager"
        _log.info(
            "manage_managers add: %s %s %s in guild %s by user %s%s",
            principal_type, principal_id, target_label,
            target_guild_id, interaction.user.id,
            " (no-op, already present)" if already else "",
        )
        await interaction.followup.send(
            f"✅ {target_label} {verb} in **{guild_label}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _managers_remove(
        self,
        interaction: discord.Interaction,
        target_guild_id: int,
        target_guild: Optional[discord.Guild],
        user: Optional[discord.User],
        role: Optional[discord.Role],
    ) -> None:
        principal_type = "user" if user is not None else "role"
        principal_id = user.id if user is not None else role.id

        # Check whether the row exists so we can tell the host the
        # operation actually did something. bot.RUN doesn't surface
        # rowcount through aiosqlite's wrapper.
        existed = await self.bot.GET_ONE(
            "SELECT 1 FROM guild_managers "
            "WHERE guild_id = ? AND principal_type = ? AND principal_id = ? LIMIT 1",
            (target_guild_id, principal_type, principal_id),
        )
        await self.bot.RUN(
            DatabaseQueries.DELETE_GUILD_MANAGER,
            (target_guild_id, principal_type, principal_id),
        )

        target_label = user.mention if user is not None else role.mention
        guild_label = target_guild.name if target_guild else f"guild `{target_guild_id}`"
        if existed:
            _log.info(
                "manage_managers remove: %s %s %s in guild %s by user %s",
                principal_type, principal_id, target_label,
                target_guild_id, interaction.user.id,
            )
            await interaction.followup.send(
                f"🗑 Removed {target_label} from managers of **{guild_label}**.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                f"ℹ️ {target_label} wasn't a manager in **{guild_label}** — nothing to do.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _managers_list(
        self,
        interaction: discord.Interaction,
        target_guild_id: int,
        target_guild: Optional[discord.Guild],
    ) -> None:
        rows = await self.bot.GET(
            DatabaseQueries.LIST_GUILD_MANAGERS, (target_guild_id,),
        )
        guild_label = target_guild.name if target_guild else f"guild `{target_guild_id}`"
        if not rows:
            await interaction.followup.send(
                f"No managers configured for **{guild_label}**. "
                f"Use `/manage_managers action:add` to grant the first one.",
                ephemeral=True,
            )
            return

        lines = [
            _format_principal_line(
                self.bot, target_guild, row[0], row[1], row[2], row[3],
            )
            for row in rows
        ]
        body = "\n".join(lines)
        await interaction.followup.send(
            f"**Managers for {guild_label}** ({len(rows)}):\n{body}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: VNClubBot):
    await bot.add_cog(AdminManagers(bot))
