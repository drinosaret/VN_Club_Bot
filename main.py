import discord
import os
import sys
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from lib.bot import VNClubBot

load_dotenv()

TOKEN = os.getenv("TOKEN")
PATH_TO_DB = os.getenv("PATH_TO_DB")
COG_FOLDER = "cogs"
LOG_FILE = os.getenv("LOG_FILE", "hikaru_bot.log")

# Fail fast on missing required config so deploy mistakes surface immediately
# instead of crashing opaquely on the first slash command.
_missing = [name for name, val in (
    ("TOKEN", TOKEN),
    ("PATH_TO_DB", PATH_TO_DB),
) if not val]
if _missing:
    print(f"FATAL: missing required env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

my_bot = VNClubBot(cog_folder=COG_FOLDER, path_to_db=PATH_TO_DB)


class _CollapseNewlinesFilter(logging.Filter):
    """Keep each record on one physical line by escaping newlines in the
    message, so log files stay one-entry-per-line and parse cleanly. Tracebacks
    (exc_info) are left as-is. Added to the handlers so it also covers records
    propagated up from child loggers."""

    def filter(self, record):
        if record.args:
            record.msg = record.getMessage()
            record.args = None
        if isinstance(record.msg, str) and ("\n" in record.msg or "\r" in record.msg):
            record.msg = record.msg.replace("\r", "\\r").replace("\n", "\\n")
        return True


def setup_logging():
    """Setup logging to both console and file"""
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    newline_filter = _CollapseNewlinesFilter()

    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Console handler (discord's default)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(newline_filter)
    root_logger.addHandler(console_handler)

    # File handler with rotation (max 5MB, keep 3 backups)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(newline_filter)
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
