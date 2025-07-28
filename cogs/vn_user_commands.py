import discord.app_commands as app_commands
import asyncio
import logging
import discord
from discord.ext import commands
from discord.ext import tasks
from cogs.vn_title_management import GET_ALL_VN_QUERY
from lib.bot import VNClubBot
from .username_fetcher import get_username_db

_log = logging.getLogger(__name__)

CREATE_READING_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS reading_logs (
    user_id INTEGER NOT NULL,
    vndb_id TEXT NOT NULL,
    read_month TEXT NOT NULL,
    read_in_month BOOLEAN DEFAULT FALSE,
    comment TEXT,
    logged_in_guild INTEGER,
    PRIMARY KEY (user_id, vndb_id)
);"""

ADD_READING_LOG_QUERY = """
INSERT INTO reading_logs (user_id, vndb_id, read_month, read_in_month, comment, logged_in_guild)
VALUES (?, ?, ?, ?, ?, ?)
"""

GET_ONE_READING_LOG_QUERY = """
SELECT * FROM reading_logs WHERE user_id = ? AND vndb_id = ?;
"""

GET_ALL_USER_LOGS = """SELECT * FROM reading_logs ORDER BY read_month DESC;
"""

CURRENT_VN_DATABASE = None


async def fill_vn_database(bot: VNClubBot):
    global CURRENT_VN_DATABASE
    if CURRENT_VN_DATABASE is None:
        CURRENT_VN_DATABASE = await bot.GET(GET_ALL_VN_QUERY)
    return CURRENT_VN_DATABASE


async def vns_autocomplete(interaction: discord.Interaction, current: str):
    if not CURRENT_VN_DATABASE:
        await fill_vn_database(interaction.client)
    if not current:
        return [
            discord.app_commands.Choice(name=name, value=value)
            for (value, name, _, _, _, _) in CURRENT_VN_DATABASE
        ][:25]
    else:
        return [
            discord.app_commands.Choice(name=name, value=value)
            for (value, name, _, _, _, _) in CURRENT_VN_DATABASE
            if current.lower() in name.lower() or current.lower() in value.lower()
        ][:25]


async def verify_vn_exists(interaction: discord.Interaction, vndb_id: str) -> bool:
    vndb_id = vndb_id.strip()
    if not CURRENT_VN_DATABASE:
        await fill_vn_database(interaction.client)
    for row in CURRENT_VN_DATABASE:
        if row[0] == vndb_id:
            return True
    await interaction.followup.send(
        f"VN with ID `{vndb_id}` does not exist in the database."
    )
    return False


async def determine_if_current_vn_month(
    interaction: discord.Interaction, vndb_id: str
) -> bool:
    current_month = interaction.created_at.strftime("%Y-%m")
    if not CURRENT_VN_DATABASE:
        await fill_vn_database(interaction.client)
    for row in CURRENT_VN_DATABASE:
        if row[0] == vndb_id and row[2] == current_month:
            return True
    return False


async def get_vn_name(interaction: discord.Interaction, vndb_id: str) -> str:
    if not CURRENT_VN_DATABASE:
        await fill_vn_database(interaction.client)
    for row in CURRENT_VN_DATABASE:
        if row[0] == vndb_id:
            return row[1]
    return "Unknown VN"


async def log_already_exists(
    interaction: discord.Interaction, user_id: int, vndb_id: str
) -> bool:
    result = await interaction.client.GET_ONE(
        GET_ONE_READING_LOG_QUERY, (user_id, vndb_id)
    )
    if result:
        await interaction.followup.send(
            f"You have already logged this VN with ID `{vndb_id}`."
        )
        return True
    return False


class VNUserCommands(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.RUN(CREATE_READING_LOGS_TABLE)
        if not self.task_refresh_vn_database.is_running():
            self.task_refresh_vn_database.start()

    @app_commands.command(name="finish_vn", description="Mark a VN as finished.")
    @app_commands.describe(
        vndb_id="The VNDB ID of the title you finished.",
        comment="Short comment about your experience with the VN.",
    )
    @app_commands.autocomplete(vndb_id=vns_autocomplete)
    @app_commands.guild_only()
    async def finish_vn(
        self, interaction: discord.Interaction, vndb_id: str, comment: str
    ):
        await interaction.response.defer()
        await fill_vn_database(self.bot)
        if not await verify_vn_exists(interaction, vndb_id):
            return

        user_id = interaction.user.id
        is_current_month = await determine_if_current_vn_month(interaction, vndb_id)

        if await log_already_exists(interaction, user_id, vndb_id):
            return

        current_month = interaction.created_at.strftime("%Y-%m")

        await self.bot.RUN(
            ADD_READING_LOG_QUERY,
            (
                user_id,
                vndb_id,
                current_month,
                is_current_month,
                comment,
                interaction.guild.id,
            ),
        )

        _log.info(
            f"User {interaction.user.name} marked VN with ID {vndb_id} as finished."
        )

        vn_name = await get_vn_name(interaction, vndb_id)

        if is_current_month:
            await interaction.followup.send(
                f"You finished this months VN giving you 3 points: {vn_name} - {comment}"
            )
        else:
            await interaction.followup.send(
                f"You finished a VN giving you 1 point: {vn_name} - {comment}"
            )

    @app_commands.command(name="vn_leaderboard", description="Print the leaderboard.")
    async def vn_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await fill_vn_database(self.bot)

        results = await self.bot.GET(GET_ALL_USER_LOGS)

        if not results:
            await interaction.followup.send("No reading logs found.")
            return

        leaderboard = {}
        for row in results:
            user_id, vndb_id, read_month, read_in_month, comment, logged_in_guild = row
            username = await get_username_db(self.bot, user_id)
            points = 3 if read_in_month else 1
            if username not in leaderboard:
                leaderboard[username] = 0
            leaderboard[username] += points

        embed = discord.Embed(
            title="📚 Visual Novel Reading Logs Leaderboard", color=discord.Color.blue()
        )

        description_strings = []
        for i, (username, points) in enumerate(
            sorted(leaderboard.items(), key=lambda x: x[1], reverse=True), start=1
        ):
            description_strings.append(f"{i}. **{username}**: {points}点")

        embed.description = "\n".join(description_strings)

        await interaction.followup.send(embed=embed)

    @tasks.loop(minutes=1)
    async def task_refresh_vn_database(self):
        await asyncio.sleep(30)
        await fill_vn_database(self.bot)


async def setup(bot: VNClubBot):
    await bot.add_cog(VNUserCommands(bot))
