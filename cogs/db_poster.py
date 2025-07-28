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

    @tasks.loop(hours=24)
    async def post_database(self):
        """Post the database file to the target channel once daily."""
        await asyncio.sleep(60)
        try:
            channel = self.bot.get_channel(self.target_channel_id)
            if not channel:
                _log.error(f"Could not find channel with ID {self.target_channel_id}")
                return

            # Create a Discord file from the database
            db_file = discord.File(
                self.bot.path_to_db, filename="database_backup.sqlite3"
            )

            # Send the file with a timestamp
            embed = discord.Embed(
                title="Daily Database Backup",
                description=f"Database backup for {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
                color=discord.Color.blue(),
            )

            await channel.send(embed=embed, file=db_file)
            _log.info(
                f"Successfully posted database backup to channel {self.target_channel_id}"
            )

        except Exception as e:
            _log.error(f"Failed to post database backup: {e}")


async def setup(bot: VNClubBot):
    await bot.add_cog(DatabasePoster(bot))
