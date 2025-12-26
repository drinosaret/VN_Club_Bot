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
        super().__init__(command_prefix=command_prefix, intents=discord.Intents.all())
        self.cog_folder = cog_folder
        self.path_to_db = path_to_db
        self._last_heartbeat = time.time()

        db_directory = os.path.dirname(self.path_to_db)
        if not os.path.exists(db_directory):
            os.makedirs(db_directory)

    async def on_ready(self):
        self._last_heartbeat = time.time()
        print(f"Logged in as {self.user}")
        await self.change_presence(activity=discord.Game(name="装甲悪鬼村正"))

        # Auto-sync commands globally
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} command(s) globally")
        except Exception as e:
            _log.error(f"Failed to sync commands: {e}")

    async def setup_hook(self):
        self.tree.on_error = self.on_application_command_error
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
        cogs = [cog for cog in os.listdir(self.cog_folder) if cog.endswith(".py")]

        for cog in cogs:
            cog = f"{self.cog_folder}.{cog[:-3]}"
            await self.load_extension(cog)
            print(f"Loaded {cog}")

    async def RUN(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.path_to_db) as db:
            await db.execute(query, params)
            await db.commit()

    async def RUN_RETURNING_ID(self, query: str, params: tuple = ()) -> int:
        """Execute a query and return the last inserted row id."""
        async with aiosqlite.connect(self.path_to_db) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid

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
            # Log unexpected errors for debugging
            command = interaction.command
            if command is not None:
                _log.error("Unhandled error in command %r: %s", command.name, error, exc_info=error)
            else:
                _log.error("Unhandled error in command tree: %s", error, exc_info=error)

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
