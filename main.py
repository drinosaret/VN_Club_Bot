import discord
import os
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from lib.bot import VNClubBot

load_dotenv()

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX")
TOKEN = os.getenv("TOKEN")
PATH_TO_DB = os.getenv("PATH_TO_DB")
COG_FOLDER = "cogs"
LOG_FILE = os.getenv("LOG_FILE", "hikaru_bot.log")

my_bot = VNClubBot(
    command_prefix=COMMAND_PREFIX, cog_folder=COG_FOLDER, path_to_db=PATH_TO_DB
)


def setup_logging():
    """Setup logging to both console and file"""
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Console handler (discord's default)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler with rotation (max 5MB, keep 3 backups)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Also capture discord.py logs
    discord_logger = logging.getLogger('discord')
    discord_logger.setLevel(logging.INFO)


async def main():
    setup_logging()
    logging.info("Starting Hikaru bot...")
    await my_bot.load_cogs()
    await my_bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
