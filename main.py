import discord
import os
import asyncio
from dotenv import load_dotenv
from lib.bot import VNClubBot

load_dotenv()

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX")
TOKEN = os.getenv("TOKEN")
PATH_TO_DB = os.getenv("PATH_TO_DB")
COG_FOLDER = "cogs"

my_bot = VNClubBot(
    command_prefix=COMMAND_PREFIX, cog_folder=COG_FOLDER, path_to_db=PATH_TO_DB
)


async def main():
    discord.utils.setup_logging()
    await my_bot.load_cogs()
    await my_bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
