import asyncio
import discord
import logging
from lib.bot import VNClubBot
from discord.ext import commands

_log = logging.getLogger(__name__)

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    discord_user_id INTEGER PRIMARY KEY,
    user_name TEXT,
    user_tag TEXT
);"""

UPDATE_USERNAME_QUERY = """
UPDATE users
SET user_name = ?
WHERE discord_user_id = ?;"""

INSERT_USER_QUERY = """
INSERT INTO users (discord_user_id, user_name, user_tag)
VALUES (?, ?, ?) ON CONFLICT(discord_user_id) DO UPDATE SET
    user_name = excluded.user_name,
    user_tag = excluded.user_tag;"""

FETCH_USER_QUERY = """
SELECT user_name FROM users WHERE discord_user_id = ?;"""

FETCH_LOCK = asyncio.Lock()


async def get_username_db(bot: VNClubBot, user_id: int) -> str:
    user = bot.get_user(user_id)
    if user:
        await bot.RUN(INSERT_USER_QUERY, (user.id, user.display_name, user.name))
        return user.display_name
    user_name = await bot.GET_ONE(FETCH_USER_QUERY, (user_id,))
    if user_name:
        return user_name[0]
    async with FETCH_LOCK:
        await asyncio.sleep(1)  # Rate limit protection
        try:
            user = await bot.fetch_user(user_id)
            if user:
                await bot.RUN(INSERT_USER_QUERY, (user.id, user.display_name, user.name))
                return user.display_name
            else:
                return "Unknown User"
        except discord.NotFound:
            _log.warning(f"User {user_id} not found on Discord")
            return "Unknown User"
        except discord.HTTPException:
            _log.exception("HTTP error fetching user %s", user_id)
            return "Unknown User"


async def cache_user(bot: VNClubBot, user) -> None:
    """Upsert a user's display_name and unique handle into the local
    `users` table. Call from write-time paths (vote, nominate) so the
    cache populates organically as users interact with the bot. Failures
    are swallowed (logged) so a cache write never disrupts the user's
    primary action.
    """
    if user is None:
        return
    try:
        await bot.RUN(INSERT_USER_QUERY, (user.id, user.display_name, user.name))
    except Exception:  # noqa: BLE001
        _log.exception("cache_user failed for user_id=%s", getattr(user, "id", "?"))


class UsernameFetcher(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.RUN(CREATE_USERS_TABLE)


async def setup(bot: VNClubBot):
    await bot.add_cog(UsernameFetcher(bot))
