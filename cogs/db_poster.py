import os
import discord
import asyncio
import logging
from discord.ext import commands, tasks
from lib.bot import VNClubBot

_log = logging.getLogger(__name__)


class DatabasePoster(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot
        self.target_channel_id = int(os.getenv("DB_BACKUP_CHANNEL"))
        self.post_database.start()

    def cog_unload(self):
        self.post_database.cancel()

    async def send_backup(self, backup_type: str = "Daily") -> bool:
        """Send a database backup to the target channel.

        Args:
            backup_type: The type of backup (e.g., "Daily", "Startup")

        Returns:
            True if successful, False otherwise
        """
        try:
            channel = self.bot.get_channel(self.target_channel_id)
            if not channel:
                _log.error(f"Could not find channel with ID {self.target_channel_id}")
                return False

            # Create a Discord file from the database
            db_file = discord.File(
                self.bot.path_to_db, filename="database_backup.sqlite3"
            )

            # Send the file with a timestamp
            embed = discord.Embed(
                title=f"{backup_type} Database Backup",
                description=f"Database backup for {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                color=discord.Color.green() if backup_type == "Startup" else discord.Color.blue(),
            )

            await channel.send(embed=embed, file=db_file)
            _log.info(
                f"Successfully posted {backup_type.lower()} database backup to channel {self.target_channel_id}"
            )
            return True

        except Exception as e:
            _log.error(f"Failed to post {backup_type.lower()} database backup: {e}")
            return False

    @commands.Cog.listener()
    async def on_ready(self):
        """Send a backup immediately when the bot starts/restarts."""
        await self.send_backup("Startup")

    @tasks.loop(hours=6)
    async def post_database(self):
        """Post the database file to the target channel every 6 hours."""
        await asyncio.sleep(1200)
        await self.send_backup("Scheduled")


async def setup(bot: VNClubBot):
    await bot.add_cog(DatabasePoster(bot))
