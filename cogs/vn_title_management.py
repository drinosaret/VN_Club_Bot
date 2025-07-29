from datetime import datetime
import os
import discord
import discord.app_commands as app_commands
import logging
from typing import Optional
from discord.ext import commands
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry, CREATE_VNDB_CACHE_TABLE

_log = logging.getLogger(__name__)


CREATE_MONTHLY_VN_TABLE = """
CREATE TABLE IF NOT EXISTS vn_titles (
    vndb_id TEXT PRIMARY KEY,
    start_month TEXT NOT NULL,
    end_month TEXT NOT NULL,
    is_monthly_points INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);"""

ADD_MONTHLY_VN_QUERY = """
INSERT INTO vn_titles (vndb_id, start_month, end_month, is_monthly_points)
VALUES (?, ?, ?, ?);
"""

GET_A_SINGLE_MONTHLY_VN = """
SELECT vndb_id, start_month, end_month, is_monthly_points FROM vn_titles WHERE vndb_id = ?;
"""

GET_ALL_MONTHLY_VN_QUERY = """
SELECT * FROM vn_titles ORDER BY start_month DESC;
"""

GET_CURRENT_MONTHLY_VN_QUERY = """
SELECT * FROM vn_titles WHERE start_month <= ? AND end_month >= ? ORDER BY start_month DESC;
"""

DELETE_MONTHLY_VN_QUERY = """
DELETE FROM vn_titles WHERE vndb_id = ?;
"""

ALLOWED_USER_IDS = [
    int(user_id) for user_id in os.getenv("VN_MANAGER_USER_IDS", "").split(",")
]

ALLOWED_ROLE_IDS = [
    int(role_id) for role_id in os.getenv("VN_MANAGER_ROLE_IDS", "").split(",")
]


async def get_single_monthly_vn(bot: VNClubBot, vndb_id: str):
    result = await bot.GET_ONE(GET_A_SINGLE_MONTHLY_VN, (vndb_id,))
    if result:
        return result
    return None


async def get_vn_month(interaction: discord.Interaction, month: str | None) -> str:
    if month is None:
        return discord.utils.utcnow().strftime("%Y-%m")
    if not len(month) == 7:
        await interaction.followup.send(
            "Invalid month format. Please use YYYY-MM format.",
        )
        return None
    try:
        datetime.strptime(month, "%Y-%m")
        return month
    except ValueError:
        await interaction.followup.send(
            "Invalid month format. Please use YYYY-MM format.",
        )


def user_is_allowed(user_id: int, role_ids: list[int]) -> bool:
    if user_id in ALLOWED_USER_IDS:
        return True
    is_manager = any(role_id in ALLOWED_ROLE_IDS for role_id in role_ids)
    return is_manager


async def validate_user(interaction: discord.Interaction):
    role_ids = [role.id for role in interaction.user.roles]
    if not user_is_allowed(interaction.user.id, role_ids):
        await interaction.followup.send(
            "You are not authorized to use this command.",
        )
        return False
    return True


async def check_if_already_exists(
    interaction: discord.Interaction, vndb_id: str
) -> bool:
    result = await interaction.client.GET_ONE(GET_A_SINGLE_MONTHLY_VN, (vndb_id,))
    if result:
        await interaction.followup.send(
            f"VN title with ID `{vndb_id}` already exists in the database."
        )
        return True
    return False


async def check_if_not_exists(interaction: discord.Interaction, vndb_id: str) -> bool:
    result = await interaction.client.GET_ONE(GET_A_SINGLE_MONTHLY_VN, (vndb_id,))
    if not result:
        await interaction.followup.send(
            f"VN title with ID `{vndb_id}` does not exist in the database."
        )
        return True
    return False


async def get_vndb_info(
    interaction: discord.Interaction, vndb_id: str
) -> Optional[VN_Entry]:
    try:
        vndb_response = await from_vndb_id(interaction.client, vndb_id)
    except Exception as e:
        _log.error(f"Error fetching VNDB info for ID {vndb_id}: {e}")
        await interaction.followup.send(
            "An error occurred while fetching VNDB information. Report this."
        )
        return None
    if not vndb_response:
        await interaction.followup.send(
            "Failed to fetch VN information from VNDB. Please check the ID and try again."
        )
        return None
    return vndb_response


async def create_added_monthly_vn_embed(
    vn_info: VN_Entry, start_month: str, end_month: str, points: int
) -> discord.Embed:
    vndb_link = await vn_info.get_vndb_link()
    points_not_monthly = await vn_info.get_points_not_monthly()

    embed = discord.Embed(
        title=f"VN Added: {vn_info.title_ja}",
        color=discord.Color.green(),
    )
    embed.add_field(name="VNDB ID", value=vn_info.vndb_id, inline=True)
    embed.add_field(name="Start Month", value=start_month, inline=True)
    embed.add_field(name="End Month", value=end_month, inline=True)
    embed.add_field(name="Points (Monthly)", value=str(points), inline=True)
    embed.add_field(
        name="Points (Not Monthly)", value=str(points_not_monthly), inline=True
    )
    embed.add_field(
        name="VNDB Link", value=f"[View on VNDB]({vndb_link})", inline=False
    )

    description = await vn_info.get_normalized_description()
    embed.add_field(
        name="Description",
        value=description,
        inline=False,
    )
    if not vn_info.thumbnail_is_nsfw:
        embed.set_thumbnail(url=vn_info.thumbnail_url)

    embed.set_footer(text="Visual Novel Club")
    return embed


async def create_current_monthly_embed(
    vn_info: VN_Entry, start_month: str, end_month: str, points: int
) -> discord.Embed:
    vndb_link = await vn_info.get_vndb_link()
    points_not_monthly = await vn_info.get_points_not_monthly()

    embed = discord.Embed(
        title=f"Current Monthly VN: {vn_info.title_ja}",
        color=discord.Color.blue(),
    )
    embed.add_field(name="VNDB ID", value=vn_info.vndb_id, inline=True)
    embed.add_field(name="Start Month", value=start_month, inline=True)
    embed.add_field(name="End Month", value=end_month, inline=True)
    embed.add_field(name="Points (Monthly)", value=str(points), inline=True)
    embed.add_field(
        name="Points (Not Monthly)", value=str(points_not_monthly), inline=True
    )
    embed.add_field(
        name="VNDB Link", value=f"[View on VNDB]({vndb_link})", inline=False
    )

    description = await vn_info.get_normalized_description()
    embed.add_field(
        name="Description",
        value=description,
        inline=False,
    )
    if not vn_info.thumbnail_is_nsfw:
        embed.set_thumbnail(url=vn_info.thumbnail_url)

    embed.set_footer(text="Visual Novel Club")
    return embed


class VNTitleManagement(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.RUN(CREATE_MONTHLY_VN_TABLE)
        await self.bot.RUN(CREATE_VNDB_CACHE_TABLE)

    @app_commands.command(
        name="add_vn", description="Add a new VN title to the database."
    )
    @app_commands.describe(
        vndb_id="The VNDB ID of the title to add.",
        start_month="The month the title should be added to. Format: YYYY-MM (optional, defaults to current month).",
        end_month="The month the title should end. Format: YYYY-MM (optional, defaults to start month).",
        is_monthly_points="How many points to receive if read during the specified period (optional, defaults to 10).",
    )
    @app_commands.guild_only()
    async def add_vn(
        self,
        interaction: discord.Interaction,
        vndb_id: str,
        start_month: str = None,
        end_month: str = None,
        is_monthly_points: int = 10,
    ):
        _log.info(
            f"User {interaction.user.name} is trying to add a VN title with ID {vndb_id}."
        )

        await interaction.response.defer()

        if not await validate_user(interaction):
            return

        if await check_if_already_exists(interaction, vndb_id):
            return

        start_month = await get_vn_month(interaction, start_month)
        if not start_month:
            return

        if not end_month:
            end_month = start_month
        else:
            end_month = await get_vn_month(interaction, end_month)
        if not end_month:
            return

        vn_info = await get_vndb_info(interaction, vndb_id)
        if not vn_info:
            return

        await self.bot.RUN(
            ADD_MONTHLY_VN_QUERY,
            (vn_info.vndb_id, start_month, end_month, is_monthly_points),
        )

        _log.info(f"The following VN was added as a monthly title: {vn_info}")

        embed = await create_added_monthly_vn_embed(
            vn_info, start_month, end_month, is_monthly_points
        )

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="remove_vn", description="Remove a VN title from the database."
    )
    @app_commands.describe(vndb_id="The VNDB ID of the title to remove.")
    @app_commands.guild_only()
    async def remove_vn(self, interaction: discord.Interaction, vndb_id: str):
        _log.info(
            f"User {interaction.user.name} is trying to remove a VN title with ID {vndb_id}."
        )

        await interaction.response.defer()

        if not await validate_user(interaction):
            return

        if await check_if_not_exists(interaction, vndb_id):
            return

        await self.bot.RUN(
            DELETE_MONTHLY_VN_QUERY,
            (vndb_id,),
        )

        _log.info(f"VN title with ID {vndb_id} removed successfully.")

        await interaction.followup.send(
            f"VN title with ID `{vndb_id}` removed successfully.",
        )

    @app_commands.command(name="list_vns", description="List all VN titles.")
    @app_commands.guild_only()
    async def list_vns(self, interaction: discord.Interaction):
        await interaction.response.defer()

        results = await self.bot.GET(GET_ALL_MONTHLY_VN_QUERY)

        if not results:
            await interaction.followup.send("No VN titles found in the database.")
            return

        embed = discord.Embed(
            title="📚 Visual Novels Library",
            color=discord.Color.blurple(),
        )
        embed.set_author(name="Visual Novel Club")

        description_strings = []

        for row in results:
            vndb_id, start_month, end_month, is_monthly_points, created_at = row
            vn_info = await from_vndb_id(interaction.client, vndb_id)
            if not vn_info:
                _log.error(f"Failed to fetch VNDB info for ID {vndb_id}.")
                continue

            link = await vn_info.get_vndb_link()

            description_string = f"**{start_month} TO {end_month}**: [{vn_info.title_ja}]({link}) (ID: {vndb_id}) (M: {is_monthly_points}点 | NM: {await vn_info.get_points_not_monthly()}点)"

            description_strings.append(description_string)

        embed.description = "\n".join(description_strings)

        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="get_current_monthly", description="Show current monthly VNs."
    )
    async def get_current_monthly(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        current_month = discord.utils.utcnow().strftime("%Y-%m")

        results = await self.bot.GET(
            GET_CURRENT_MONTHLY_VN_QUERY,
            (current_month, current_month),
        )

        vn_embeds = []
        for row in results:
            vndb_id, start_month, end_month, is_monthly_points, created_at = row
            vn_info = await from_vndb_id(interaction.client, vndb_id)
            if not vn_info:
                _log.error(f"Failed to fetch VNDB info for ID {vndb_id}.")
                continue
            vn_embeds.append(
                await create_current_monthly_embed(
                    vn_info, start_month, end_month, is_monthly_points
                )
            )

        if not vn_embeds:
            await interaction.followup.send(
                "No current monthly VNs found for this month."
            )
            return

        else:
            await interaction.followup.send(
                content="Posting monthly VNs for the current month:",
                ephemeral=True,
            )

        for embed in vn_embeds:
            await interaction.channel.send(embed=embed)


async def setup(bot: VNClubBot):
    await bot.add_cog(VNTitleManagement(bot))
