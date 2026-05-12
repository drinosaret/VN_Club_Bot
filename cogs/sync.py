import logging

from lib.bot import VNClubBot
import discord
from discord.ext import commands

from lib.utils import AUTHORIZED_USER_IDS


_log = logging.getLogger(__name__)


def is_authorized():
    async def predicate(ctx: commands.Context):
        return ctx.author.id in AUTHORIZED_USER_IDS

    return commands.check(predicate)


class Sync(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        pass

    @commands.command()
    @is_authorized()
    async def sync_guild(self, ctx: discord.ext.commands.Context):
        """Sync commands to current guild."""
        guild_id = ctx.guild.id
        _log.info(
            "sync_guild: invoked user=%s guild=%s", ctx.author.id, guild_id,
        )
        try:
            self.bot.tree.copy_global_to(guild=discord.Object(id=guild_id))
            self.bot.tree.clear_commands(guild=None)
            synced = await self.bot.tree.sync(guild=discord.Object(id=guild_id))
        except Exception:
            _log.exception(
                "sync_guild: failed user=%s guild=%s", ctx.author.id, guild_id,
            )
            await ctx.send(f"❌ Failed to sync to guild {guild_id} — see log.")
            return
        _log.info(
            "sync_guild: synced %d command(s) to guild=%s",
            len(synced), guild_id,
        )
        await ctx.send(
            f"Synced {len(synced)} command(s) to guild with id {guild_id}."
        )

    @commands.command()
    @is_authorized()
    async def sync_global(self, ctx: discord.ext.commands.Context):
        """Sync commands to global."""
        _log.info("sync_global: invoked user=%s", ctx.author.id)
        try:
            synced = await self.bot.tree.sync()
        except Exception:
            _log.exception("sync_global: failed user=%s", ctx.author.id)
            await ctx.send("❌ Failed to sync globally — see log.")
            return
        _log.info("sync_global: synced %d command(s) globally", len(synced))
        await ctx.send(f"Synced {len(synced)} command(s) to global.")

    @commands.command()
    @is_authorized()
    async def clear_global_commands(self, ctx):
        """Clear all global commands."""
        _log.info("clear_global_commands: invoked user=%s", ctx.author.id)
        try:
            self.bot.tree.clear_commands(guild=None)
            await self.bot.tree.sync()
        except Exception:
            _log.exception(
                "clear_global_commands: failed user=%s", ctx.author.id,
            )
            await ctx.send("❌ Failed to clear global commands — see log.")
            return
        _log.info("clear_global_commands: cleared")
        await ctx.send("Cleared global commands.")

    @commands.command()
    @is_authorized()
    async def clear_guild_commands(self, ctx):
        """Clear all guild commands."""
        guild_id = ctx.guild.id
        _log.info(
            "clear_guild_commands: invoked user=%s guild=%s",
            ctx.author.id, guild_id,
        )
        try:
            self.bot.tree.clear_commands(guild=discord.Object(id=guild_id))
            await self.bot.tree.sync(guild=discord.Object(id=guild_id))
        except Exception:
            _log.exception(
                "clear_guild_commands: failed user=%s guild=%s",
                ctx.author.id, guild_id,
            )
            await ctx.send(
                f"❌ Failed to clear guild commands for {guild_id} — see log."
            )
            return
        _log.info("clear_guild_commands: cleared for guild=%s", guild_id)
        await ctx.send(f"Cleared guild commands for guild with id {guild_id}.")


async def setup(bot: VNClubBot):
    await bot.add_cog(Sync(bot))
