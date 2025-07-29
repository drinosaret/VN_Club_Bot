import discord.app_commands as app_commands
import asyncio
import logging
import discord
from discord.ext import commands
from discord.ext import tasks
from cogs.vn_title_management import vn_exists, get_single_monthly_vn
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry
from .username_fetcher import get_username_db

_log = logging.getLogger(__name__)

CREATE_READING_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS reading_logs (
    user_id INTEGER NOT NULL,
    vndb_id TEXT,
    reward_reason TEXT,
    reward_month TEXT NOT NULL,
    points INTEGER NOT NULL,
    comment TEXT,
    logged_in_guild INTEGER
);"""

ADD_READING_LOG_QUERY = """
INSERT INTO reading_logs (user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""

GET_ONE_READING_LOG_QUERY = """
SELECT * FROM reading_logs WHERE user_id = ? AND vndb_id = ?;
"""

GET_ALL_USER_LOGS = """SELECT * FROM reading_logs ORDER BY reward_month DESC;
"""

VN_AUTOCOMPLETE_QUERY = """
SELECT vn.vndb_id, vndb.title_ja FROM vn_titles vn 
INNER JOIN vndb_cache vndb ON vndb.vndb_id = vn.vndb_id
"""


async def is_current_month(
    current_month: str, start_month: str, end_month: str
) -> bool:
    return start_month <= current_month <= end_month


async def log_already_exists(
    interaction: discord.Interaction, user_id: int, vndb_id: str
) -> bool:
    result = await interaction.client.GET_ONE(
        GET_ONE_READING_LOG_QUERY, (user_id, vndb_id)
    )
    if result:
        await interaction.followup.send("You have already logged reading this VN!")
        return True
    return False


async def create_finished_vn_embed(
    interaction: discord.Interaction, vn_info: VN_Entry, comment: str
) -> discord.Embed:
    username = interaction.user.name
    link = await vn_info.get_vndb_link()

    embed = discord.Embed(
        title=f"Finished reading **{vn_info.title_ja}**",
        color=discord.Color.green(),
    )
    embed.set_author(name=username, icon_url=interaction.user.display_avatar.url)
    embed.set_thumbnail(url=vn_info.thumbnail_url)
    embed.add_field(
        name="VNDB Link", value=f"[{vn_info.title_ja}]({link})", inline=False
    )
    embed.add_field(name="Comment", value=comment, inline=False)
    return embed


async def vns_autocomplete(interaction: discord.Interaction, current: str):
    current_vns = await interaction.client.GET(VN_AUTOCOMPLETE_QUERY)
    if not current:
        return [
            discord.app_commands.Choice(name=f"{name} ({value})", value=value)
            for (value, name) in current_vns
        ][:25]
    else:
        return [
            discord.app_commands.Choice(name=f"{name} ({value})", value=value)
            for (value, name) in current_vns
            if current.lower() in name.lower() or current.lower() in value.lower()
        ][:25]


class VNUserCommands(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.RUN(CREATE_READING_LOGS_TABLE)

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

        result = await get_single_monthly_vn(interaction.client, vndb_id)
        if not result:
            await interaction.followup.send(
                f"VN with ID `{vndb_id}` does not exist in the database."
            )
            return

        vndb_id, start_month, end_month, is_monthly_points = result
        vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)

        current_month = discord.utils.utcnow().strftime("%Y-%m")
        read_in_monthly = await is_current_month(current_month, start_month, end_month)

        if await log_already_exists(interaction, interaction.user.id, vndb_id):
            return

        if read_in_monthly:
            reward_points = is_monthly_points
            reward_reason = "As Monthly VN"
        else:
            reward_points = await vn_info.get_points_not_monthly()
            reward_reason = "As Non-Monthly VN"

        await interaction.client.RUN(
            ADD_READING_LOG_QUERY,
            (
                interaction.user.id,
                vndb_id,
                reward_reason,
                current_month,
                reward_points,
                comment,
                interaction.guild.id,
            ),
        )

        embed = await create_finished_vn_embed(interaction, vn_info, comment)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="vn_leaderboard", description="Print the leaderboard.")
    async def vn_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        results = await self.bot.GET(GET_ALL_USER_LOGS)

        if not results:
            await interaction.followup.send("No reading logs found.")
            return

        leaderboard = {}
        for row in results:
            (
                user_id,
                vndb_id,
                reward_reason,
                reward_month,
                points,
                comment,
                logged_in_guild,
            ) = row
            username = await get_username_db(self.bot, user_id)
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


async def setup(bot: VNClubBot):
    await bot.add_cog(VNUserCommands(bot))
