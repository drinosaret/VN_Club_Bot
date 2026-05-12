"""`/manage_backups` — on-demand database backup for the bot operator.

Posts the live SQLite DB to the configured ``DB_BACKUP_CHANNEL`` right
now, using the same code path as the scheduled and startup backups in
``cogs/db_poster.py`` so manual posts look identical to automated ones
in the backup channel. Useful for pre-deploy snapshots, one-off
"capture this state" moments, or anything else that wants a backup
off the 6-hour cadence.

Same permission model as ``/manage_managers``: AUTHORIZED_USERS only,
with ``@app_commands.default_permissions(administrator=True)`` hiding
the command from non-admin members in Discord's UI. The Discord-perm
filter is cosmetic — the real gate is the ``AUTHORIZED_USER_IDS``
check inside the callback.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from lib.bot import VNClubBot
from lib.utils import AUTHORIZED_USER_IDS, ValidationError, handle_command_error

_log = logging.getLogger(__name__)


class AdminBackups(commands.Cog):
    """Single-command cog. Lives next to ``admin_managers.py`` rather
    than crowding ``db_poster.py``'s background-loop concerns. Delegates
    the actual send to ``db_poster`` to keep the file-upload + embed
    shape in one place."""

    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @app_commands.command(
        name="manage_backups",
        description="Post a database backup to the backup channel now (bot operator only).",
    )
    @app_commands.default_permissions(administrator=True)
    async def manage_backups(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            # AUTHORIZED_USERS only. default_permissions above hides
            # the command from non-admins in Discord's UI but that's
            # cosmetic — this is the actual gate.
            if interaction.user.id not in AUTHORIZED_USER_IDS:
                raise ValidationError(
                    f"User {interaction.user.id} not in AUTHORIZED_USERS",
                    "Only bot operators can trigger manual backups.",
                )

            # Reuse DatabasePoster.send_backup so the manual post has
            # the same embed/format as scheduled and startup posts.
            poster = self.bot.get_cog("DatabasePoster")
            if poster is None:
                raise ValidationError(
                    "DatabasePoster cog not loaded",
                    "The backup cog isn't loaded — check the bot logs.",
                )
            if not getattr(poster, "target_channel_id", 0):
                raise ValidationError(
                    "DB_BACKUP_CHANNEL not configured",
                    (
                        "`DB_BACKUP_CHANNEL` is unset (or `0`). Set it to "
                        "a channel ID in the bot's env and restart, then "
                        "scheduled/startup/manual backups will all post "
                        "there."
                    ),
                )

            ok = await poster.send_backup("Manual")
            if ok:
                channel_mention = f"<#{poster.target_channel_id}>"
                _log.info(
                    "manage_backups: manual backup posted to channel %s by user %s",
                    poster.target_channel_id, interaction.user.id,
                )
                await interaction.followup.send(
                    f"✅ Manual backup posted to {channel_mention}.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                # send_backup logs the underlying exception itself; the
                # user-facing message just needs to point at the logs.
                await interaction.followup.send(
                    "❌ Backup failed — check the bot logs for the underlying error.",
                    ephemeral=True,
                )
        except ValidationError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/manage_backups failed")
            await handle_command_error(
                interaction, e,
                "Something went wrong triggering the backup.",
            )


async def setup(bot: VNClubBot):
    await bot.add_cog(AdminBackups(bot))
