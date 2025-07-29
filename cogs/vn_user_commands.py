import discord.app_commands as app_commands
import logging
import discord
from discord.ext import commands
from cogs.vn_title_management import get_single_monthly_vn, validate_user
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry
from .username_fetcher import get_username_db

_log = logging.getLogger(__name__)

CREATE_READING_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS reading_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    vndb_id TEXT,
    reward_reason TEXT NOT NULL,
    reward_month TEXT NOT NULL,
    points INTEGER NOT NULL,
    comment TEXT,
    logged_in_guild INTEGER
);"""

ADD_READING_LOG_QUERY = """
INSERT INTO reading_logs (user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""

GET_ONE_VNDB_READING_LOG_QUERY = """
SELECT * FROM reading_logs WHERE user_id = ? AND vndb_id = ?;
"""

GET_ONE_READING_LOG_QUERY_BY_ID = """
SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild FROM reading_logs WHERE log_id = ?;
"""

GET_USER_TOTAL_POINTS = """
SELECT SUM(points) FROM reading_logs WHERE user_id = ?;
"""

GET_ALL_USER_LOGS = """SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild FROM reading_logs ORDER BY reward_month DESC;
"""

GET_ONE_USER_LOGS = """
SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild FROM reading_logs WHERE user_id = ? ORDER BY reward_month DESC;
"""

REWARD_USER_POINTS = """INSERT INTO reading_logs (user_id, reward_reason, reward_month, points, logged_in_guild)
VALUES (?, ?, ?, ?, ?);
"""

VN_AUTOCOMPLETE_QUERY = """
SELECT vn.vndb_id, vndb.title_ja FROM vn_titles vn 
INNER JOIN vndb_cache vndb ON vndb.vndb_id = vn.vndb_id
"""

USER_LOGS_AUTOCOMPLETE_QUERY = """
SELECT log_id, vndb_id, reward_month, reward_reason, points FROM reading_logs
WHERE user_id = ? ORDER BY reward_month DESC;
"""


async def is_current_month(
    current_month: str, start_month: str, end_month: str
) -> bool:
    return start_month <= current_month <= end_month


async def log_already_exists(
    interaction: discord.Interaction, user_id: int, vndb_id: str
) -> bool:
    result = await interaction.client.GET_ONE(
        GET_ONE_VNDB_READING_LOG_QUERY, (user_id, vndb_id)
    )
    if result:
        await interaction.followup.send("You have already logged reading this VN!")
        return True
    return False


async def create_finished_vn_embed(
    interaction: discord.Interaction,
    vn_info: VN_Entry,
    comment: str,
    current_total: int,
    new_total: int,
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
    embed.add_field(
        name="Points", value=f"**{current_total}** ➔ **{new_total}**", inline=False
    )
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


async def user_logs_autocomplete(interaction: discord.Interaction, current: str):
    try:
        member = interaction.namespace["member"]
    except KeyError:
        return []

    if not member:
        return []

    results = await interaction.client.GET(USER_LOGS_AUTOCOMPLETE_QUERY, (member.id,))
    if not results:
        return []

    return [
        discord.app_commands.Choice(
            name=f"{vndb_id or 'No VNDB ID'} | {reward_month} - ({reward_reason} - {points})",
            value=log_id,
        )
        for log_id, vndb_id, reward_month, reward_reason, points in results
    ]


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

        current_month = discord.utils.utcnow().strftime("%Y-%m")

        result = await get_single_monthly_vn(interaction.client, vndb_id)
        if result:
            _, start_month, end_month, is_monthly_points = result
            read_in_monthly = await is_current_month(
                current_month, start_month, end_month
            )
        else:
            read_in_monthly = False

        vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
        if not vn_info:
            await interaction.followup.send("VNDB ID not found or invalid.")
            return

        if await log_already_exists(interaction, interaction.user.id, vndb_id):
            return

        if read_in_monthly:
            reward_points = is_monthly_points
            reward_reason = "As Monthly VN"
        else:
            reward_points = await vn_info.get_points_not_monthly()
            reward_reason = "As Non-Monthly VN"

        current_total_points = await self.bot.GET_ONE(
            GET_USER_TOTAL_POINTS, (interaction.user.id,)
        )
        if current_total_points is None:
            current_total_points = 0
        else:
            current_total_points = current_total_points[0] or 0

        await self.bot.RUN(
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

        new_total_points = current_total_points + reward_points

        embed = await create_finished_vn_embed(
            interaction, vn_info, comment, current_total_points, new_total_points
        )
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

    @app_commands.command(name="user_logs", description="View your reading logs.")
    @app_commands.describe(member="The member whose logs you want to view.")
    async def user_logs(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        await interaction.response.defer()

        if member is None:
            member = interaction.user

        results = await self.bot.GET(GET_ONE_USER_LOGS, (member.id,))
        if not results:
            await interaction.followup.send(f"No reading logs found for {member.name}.")
            return

        embed = discord.Embed(
            title=f"📚 Reading Logs for {member.name}", color=discord.Color.blue()
        )

        description_strings = []
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

            if vndb_id:
                vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
                link = await vn_info.get_vndb_link()
                description_strings.append(
                    f"**{reward_month}**: [{vn_info.title_ja}]({link}) - {points}点 ({reward_reason})\n"
                    f"Comment: {comment or 'No comment provided.'}"
                )
            else:
                description_strings.append(
                    f"**{reward_month}**: No VN specified - {points}点 ({reward_reason})\n"
                    f"Comment: {comment or 'No comment provided.'}"
                )

        embed.description = "\n".join(description_strings)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="reward_points", description="Reward user with points.")
    @app_commands.describe(
        member="The member to reward points to.",
        points="The number of points to reward.",
        reason="The reason for the points reward.",
    )
    async def reward_points(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        points: int,
        reason: str,
    ):
        await interaction.response.defer()

        if not await validate_user(interaction):
            return

        await self.bot.RUN(
            REWARD_USER_POINTS,
            (
                member.id,
                reason,
                discord.utils.utcnow().strftime("%Y-%m"),
                points,
                interaction.guild.id,
            ),
        )
        await interaction.followup.send(
            f"Rewarded **{points}** points to {member.mention} for the following reason: `{reason}`"
        )

    @app_commands.command(name="delete_log", description="Delete a reading log.")
    @app_commands.describe(
        member="The member whose log you want to delete.",
        log_id="The ID of the log to delete.",
    )
    @app_commands.autocomplete(log_id=user_logs_autocomplete)
    async def delete_log(
        self, interaction: discord.Interaction, member: discord.Member, log_id: int
    ):
        await interaction.response.defer()

        if not await validate_user(interaction):
            return

        # Check if the log exists
        result = await self.bot.GET_ONE(GET_ONE_READING_LOG_QUERY_BY_ID, (log_id,))
        if not result:
            await interaction.followup.send("Log not found.")
            return

        (
            user_id,
            vndb_id,
            reward_reason,
            reward_month,
            points,
            comment,
            logged_in_guild,
        ) = result

        # Delete the log
        await self.bot.RUN("DELETE FROM reading_logs WHERE log_id = ?", (log_id,))
        await interaction.followup.send(
            f"Deleted the following log for {member.mention}:\n"
            f"**VNDB ID:** {vndb_id}\n"
            f"**Reward Reason:** {reward_reason}\n"
            f"**Reward Month:** {reward_month}\n"
            f"**Points:** {points}\n"
            f"**Comment:** {comment or 'No comment provided.'}"
        )


async def setup(bot: VNClubBot):
    await bot.add_cog(VNUserCommands(bot))
