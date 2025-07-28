import os
import discord
import discord.app_commands as app_commands
import logging
from discord.ext import commands
from lib.bot import VNClubBot
from lib.vndb_api import get_vn_info

_log = logging.getLogger(__name__)

CREATE_VN_TABLE = """
CREATE TABLE IF NOT EXISTS vn_titles (
    vndb_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    month TEXT NOT NULL,
    thumbnail_url TEXT,
    thumbnail_is_nsfw BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);"""

ADD_VN_QUERY = """
INSERT INTO vn_titles (vndb_id, title, month, thumbnail_url, thumbnail_is_nsfw)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(vndb_id) DO UPDATE SET
    title=excluded.title,
    month=excluded.month,
    thumbnail_url=excluded.thumbnail_url,
    thumbnail_is_nsfw=excluded.thumbnail_is_nsfw;
"""

GET_SINGLE_VN_QUERY = """
SELECT * FROM vn_titles WHERE vndb_id = ?;
"""

GET_ALL_VN_QUERY = """
SELECT * FROM vn_titles ORDER BY month DESC;
"""

DELETE_VN_QUERY = """
DELETE FROM vn_titles WHERE vndb_id = ?;
"""

ALLOWED_USER_IDS = [
    int(user_id) for user_id in os.getenv("VN_MANAGER_USER_IDS", "").split(",")
]


def user_is_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


async def get_vn_month(interaction: discord.Interaction, month: str | None) -> str:
    if month is None:
        return discord.utils.utcnow().strftime("%Y-%m")
    try:
        discord.utils.parse_time(month, "%Y-%m")
        return month
    except ValueError:
        await interaction.followup.send(
            "Invalid month format. Please use YYYY-MM format.",
        )


async def validate_user_id(interaction: discord.Interaction):
    if not user_is_allowed(interaction.user.id):
        await interaction.followup.send(
            "You are not authorized to use this command.",
        )
        return False
    return True


async def get_vndb_info(interaction: discord.Interaction, vndb_id: str):
    try:
        vndb_response = await get_vn_info(vndb_id)
    except Exception as e:
        _log.error(f"Error fetching VNDB info for ID {vndb_id}: {e}")
        await interaction.followup.send(
            "An error occurred while fetching VNDB information. Please try again later."
        )
        return None
    if not vndb_response:
        await interaction.followup.send(
            "Failed to fetch VN information from VNDB. Please check the ID and try again."
        )
        return None
    return vndb_response


class VNTitleManagement(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.RUN(CREATE_VN_TABLE)

    @app_commands.command(
        name="add_vn", description="Add a new VN title to the database."
    )
    @app_commands.describe(
        vndb_id="The VNDB ID of the title to add.",
        month="The month the title should be added to. Format: YYYY-MM (optional, defaults to current month).",
    )
    @app_commands.guild_only()
    async def add_vn(
        self, interaction: discord.Interaction, vndb_id: str, month: str = None
    ):
        _log.info(
            f"User {interaction.user.name} is trying to add a VN title with ID {vndb_id}."
        )

        await interaction.response.defer()

        if not await validate_user_id(interaction):
            return

        result = await self.bot.GET_ONE(
            GET_SINGLE_VN_QUERY,
            (vndb_id,),
        )
        if result:
            await interaction.followup.send(
                f"VN title with ID `{vndb_id}` already exists in the database."
            )
            return

        month = await get_vn_month(interaction, month)
        if not month:
            return

        vndb_response = await get_vndb_info(interaction, vndb_id)
        if not vndb_response:
            _log.warning(f"Failed to fetch VNDB info for ID {vndb_id}.")
            return

        vid, title, thumbnail_url, thumbnail_is_nsfw = vndb_response
        thumbnail_is_nsfw = int(thumbnail_is_nsfw)

        await self.bot.RUN(
            ADD_VN_QUERY,
            (vid, title, month, thumbnail_url, thumbnail_is_nsfw),
        )

        _log.info(f"VN title {title} with ID {vndb_id} added successfully.")
        await interaction.followup.send(
            f"VN title **{title}** with ID `{vndb_id}` added successfully for month **{month}**.",
        )

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

        if not await validate_user_id(interaction):
            return

        result = await self.bot.GET_ONE(
            GET_SINGLE_VN_QUERY,
            (vndb_id,),
        )

        if result is None:
            await interaction.followup.send(
                f"No VN title found with ID `{vndb_id}`.",
            )
            return

        await self.bot.RUN(
            DELETE_VN_QUERY,
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

        results = await self.bot.GET(GET_ALL_VN_QUERY)

        if not results:
            await interaction.followup.send("No VN titles found in the database.")
            return

        # Build a cooler embed
        embed = discord.Embed(
            title="📚 Visual Novels Library",
            color=discord.Color.blurple(),
        )
        embed.set_author(name="Visual Novel Club")

        description_strings = []

        for row in results:
            vndb_id, title, month, thumbnail_url, thumbnail_is_nsfw, created_at = row

            link = f"https://vndb.org/{vndb_id}"

            description_string = f"**{month}**: [{title}]({link}) (ID: {vndb_id})"

            description_strings.append(description_string)

        embed.description = "\n".join(description_strings)

        await interaction.followup.send(embed=embed)


async def setup(bot: VNClubBot):
    await bot.add_cog(VNTitleManagement(bot))
