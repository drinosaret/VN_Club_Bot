import os
import sys
import time
import discord
import aiosqlite
import logging
from discord.ext import commands, tasks

_log = logging.getLogger(__name__)


class VNClubBot(commands.Bot):
    def __init__(self, command_prefix, cog_folder="cogs", path_to_db="data/db.sqlite3"):
        # AllowedMentions.none() as the bot-wide default means no
        # message the bot sends pings @everyone / @here / a role /
        # a user *unless* the calling code explicitly overrides via
        # `allowed_mentions=` on the specific send. User-controlled
        # text (reasons, comments, display names) flows into many
        # admin announcements; without this default, a manager
        # writing a `reason` that contains `@everyone` would ring
        # the channel. Backtick formatting does NOT suppress pings.
        super().__init__(
            command_prefix=command_prefix,
            intents=discord.Intents.all(),
            allowed_mentions=discord.AllowedMentions.none(),
            # Disable the auto-registered ``.help`` — /help is the user-facing one.
            help_command=None,
        )
        self.cog_folder = cog_folder
        self.path_to_db = path_to_db
        self._last_heartbeat = time.time()

        db_directory = os.path.dirname(self.path_to_db)
        if not os.path.exists(db_directory):
            os.makedirs(db_directory)

    async def on_ready(self):
        self._last_heartbeat = time.time()
        _log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        await self.change_presence(activity=discord.Game(name="装甲悪鬼村正"))

        # Auto-sync commands globally — gated on SYNC_COMMANDS=true so reconnects
        # don't burn the 1/min global-sync rate-limit and silently fail. For a
        # routine deploy, leave this unset and use /sync (cogs/sync.py) on demand.
        if os.getenv("SYNC_COMMANDS", "").lower() == "true":
            try:
                synced = await self.tree.sync()
                _log.info("Synced %d command(s) globally", len(synced))
            except Exception:
                _log.exception("Failed to sync commands")

    async def setup_hook(self):
        self.tree.on_error = self.on_application_command_error
        if not self._connection_watchdog.is_running():
            self._connection_watchdog.start()

    async def on_resumed(self):
        self._last_heartbeat = time.time()
        _log.info("Connection resumed")

    @tasks.loop(seconds=60)
    async def _connection_watchdog(self):
        if not self.is_ready():
            elapsed = time.time() - self._last_heartbeat
            if elapsed > 300:  # 5 minutes
                _log.error(f"Connection lost for {elapsed:.0f}s, exiting for restart")
                await self.close()
                sys.exit(1)

    async def load_cogs(self):
        # Migrations run before any cog so cogs that read schema during
        # cog_load (e.g., the cycle cog re-registering persistent VoteViews)
        # see the new tables. setup_hook fires after cog load, which is too late.
        from lib.migrations import run_migrations
        await run_migrations(self)

        cogs = [cog for cog in os.listdir(self.cog_folder) if cog.endswith(".py")]

        loaded: list[str] = []
        for cog in cogs:
            cog = f"{self.cog_folder}.{cog[:-3]}"
            await self.load_extension(cog)
            _log.info("Loaded %s", cog)
            loaded.append(cog)
        _log.info("Startup ready: migrations applied, %d cog(s) loaded", len(loaded))

    async def RUN(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.path_to_db) as db:
            await db.execute(query, params)
            await db.commit()

    async def RUN_RETURNING_ID(self, query: str, params: tuple = ()) -> int:
        """Execute a query and return the last inserted row id."""
        async with aiosqlite.connect(self.path_to_db) as db:
            cursor = await db.execute(query, params)
            new_id = cursor.lastrowid
            await db.commit()
            return new_id

    async def RUN_TRANSACTION(self, statements: list[tuple[str, tuple]]) -> None:
        """Execute multiple writes atomically in a single transaction.

        Use for multi-step state changes that must succeed or fail together
        (e.g., cycle close: promote winners + close cycle row). Each item is
        ``(query, params)``. On any failure, the whole transaction rolls back
        and the exception propagates.
        """
        async with aiosqlite.connect(self.path_to_db) as db:
            try:
                for query, params in statements:
                    await db.execute(query, params)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def GET(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.path_to_db) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return rows

    async def GET_ONE(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.path_to_db) as db:
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
                return row

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            _log.info(
                f"Command by user {ctx.author.name} not found: {ctx.message.content}"
            )
            return

        raise error

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        # Import here to avoid circular imports
        from lib.utils import BotError

        # Unwrap the original exception if wrapped
        original = getattr(error, 'original', error)

        # Handle known error types
        if isinstance(original, BotError):
            message = original.user_message
        elif isinstance(error, discord.app_commands.MissingAnyRole):
            message = "You do not have the permission to use this command."
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            message = f"This command is currently on cooldown. Try again in {int(error.retry_after)} seconds."
        else:
            message = "An unexpected error occurred. Please try again later."
            # Log unexpected errors for debugging. Include user/guild/command
            # context so a generic Discord report ("I got an error toast") is
            # immediately correlatable in hikaru_bot.log without grepping by
            # timestamp.
            command = interaction.command
            command_name = command.name if command is not None else "<no-command>"
            guild_id = interaction.guild_id  # None in DMs
            user_id = getattr(interaction.user, "id", "?")
            _log.error(
                "Unhandled error in command %r: user=%s guild=%s data=%s: %s",
                command_name, user_id, guild_id, interaction.data, error,
                exc_info=error,
            )

        # Send ephemeral error message
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ {message}", ephemeral=True)
        except Exception as e:
            _log.error(f"Failed to send error message: {e}")

    async def on_error(self, event_method, *args, **kwargs):
        _log.exception("Ignoring exception in %s", event_method)
