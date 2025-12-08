import discord
import discord.app_commands as app_commands
import logging
from typing import Optional
from discord.ext import commands
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry, CREATE_VNDB_CACHE_TABLE
from lib.pagination import BasePaginationView
from lib.utils import (
    DatabaseQueries,
    validate_user_permission,
    validate_month_input,
    handle_command_error,
    get_current_month,
    is_month_in_range,
    BotError,
    ValidationError,
    DEFAULT_MONTHLY_POINTS,
    resolve_vn_from_input
)
from lib.embeds import EmbedBuilder
from lib.autocomplete import vn_autocomplete, vn_pool_autocomplete
from math import ceil

_log = logging.getLogger(__name__)


# ==================== VIEW CLASSES ====================


class VNListView(BasePaginationView):
    """Paginated view for VN list with navigation buttons"""
    
    def __init__(self, vn_data, title, per_page=10):
        super().__init__(vn_data, title, per_page)
    
    def create_embed(self):
        """Create an embed for the current page"""
        from lib.utils import create_base_embed, add_pagination_footer, get_current_month
        
        embed = create_base_embed(
            title=self.title,
            color=discord.Color.blurple(),
            author_name="Visual Novel Club"
        )
        
        page_data = self.get_page_data()
        current_month = get_current_month()
        
        description_strings = []
        for vndb_id, start_month, end_month, is_monthly_points, vn_info in page_data:
            if vn_info:
                link = f"https://vndb.org/{vndb_id}"
                non_monthly_points = max(1, int(is_monthly_points * 0.6))  # Estimate non-monthly points
                
                # Check if this is currently monthly
                is_current_monthly = start_month <= current_month <= end_month
                
                # Create clean date display
                if start_month == end_month:
                    date_display = start_month
                else:
                    date_display = f"{start_month} to {end_month}"
                
                # Create monthly indicator with modern styling
                if is_current_monthly:
                    monthly_indicator = f"🔥 **{date_display}** *(Current Monthly)*"
                else:
                    monthly_indicator = f"➤ **{date_display}**"
                # Prioritize Japanese title, fallback to English title, or generic text if both are empty
                display_title = vn_info.title_ja or vn_info.title_en or "View on VNDB"
                
                # Create clean VN entry with better formatting
                description_string = f"{monthly_indicator}\n└ [{display_title}]({link}) `{vndb_id}` • **{is_monthly_points}**点 *(monthly)* • **{non_monthly_points}**点 *(regular)*"
                description_strings.append(description_string)
        
        embed.description = "\n\n".join(description_strings) if description_strings else "No VNs found on this page."
        add_pagination_footer(embed, self.current_page, self.max_pages, len(self.data))
        
        return embed


# ==================== HELPER FUNCTIONS ====================


async def get_single_monthly_vn(bot: VNClubBot, vndb_id: str):
    result = await bot.GET_ONE(DatabaseQueries.GET_VN_TITLE, (vndb_id,))
    if result:
        return result
    return None


async def get_vn_month(interaction: discord.Interaction, month: str | None) -> str:
    try:
        return await validate_month_input(interaction, month)
    except ValidationError as e:
        await interaction.followup.send(e.user_message)
        return None


async def check_if_already_exists(
    interaction: discord.Interaction, vndb_id: str
) -> bool:
    result = await interaction.client.GET_ONE(DatabaseQueries.GET_VN_TITLE, (vndb_id,))
    if result:
        await interaction.followup.send(
            f"VN title with ID `{vndb_id}` already exists in the database."
        )
        return True
    return False


async def check_if_not_exists(interaction: discord.Interaction, vndb_id: str) -> bool:
    result = await interaction.client.GET_ONE(DatabaseQueries.GET_VN_TITLE, (vndb_id,))
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
        raise ValidationError(
            f"Error fetching VNDB info: {e}",
            "An error occurred while fetching VNDB information. Report this."
        )
    if not vndb_response:
        raise ValidationError(
            f"VNDB ID {vndb_id} not found",
            "Failed to fetch VN information from VNDB. Please check the ID and try again."
        )
    return vndb_response


# ==================== MAIN COG CLASS ====================

class VNTitleManagement(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.RUN(DatabaseQueries.CREATE_VN_TITLES_TABLE)
        await self.bot.RUN(CREATE_VNDB_CACHE_TABLE)

    @app_commands.command(
        name="add_vn", description="Add a new VN title to the database."
    )
    @app_commands.describe(
        title="Search for a VN by title (type at least 2 characters).",
        start_month="The month the title should be added to. Format: YYYY-MM (optional, defaults to current month).",
        end_month="The month the title should end. Format: YYYY-MM (optional, defaults to start month).",
        is_monthly_points="How many points to receive if read during the specified period (optional, defaults to 10).",
    )
    @app_commands.autocomplete(title=vn_autocomplete)
    @app_commands.guild_only()
    async def add_vn(
        self,
        interaction: discord.Interaction,
        title: str,
        start_month: str = None,
        end_month: str = None,
        is_monthly_points: int = DEFAULT_MONTHLY_POINTS,
    ):
        await interaction.response.defer()

        try:
            await validate_user_permission(interaction)

            # Resolve VN ID from various input formats (autocomplete value, display format, raw ID)
            vndb_id = await resolve_vn_from_input(title)
            if not vndb_id:
                raise ValidationError("Could not determine VN from input. Please try selecting from the autocomplete dropdown.")

            _log.info(
                f"User {interaction.user.name} is trying to add a VN title with ID {vndb_id}."
            )

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
                DatabaseQueries.ADD_VN_TITLE,
                (vn_info.vndb_id, start_month, end_month, is_monthly_points),
            )

            _log.info(f"The following VN was added as a monthly title: {vn_info}")

            embed = await EmbedBuilder.create_vn_info_embed(
                vn_info, start_month, end_month, is_monthly_points,
                title_prefix="VN Added: ", color=discord.Color.green()
            )

            await interaction.followup.send(embed=embed)

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            await handle_command_error(interaction, e)

    @app_commands.command(
        name="remove_vn", description="Remove a VN title from the database."
    )
    @app_commands.describe(title="Select a VN from the pool to remove.")
    @app_commands.autocomplete(title=vn_pool_autocomplete)
    @app_commands.guild_only()
    async def remove_vn(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer()

        try:
            await validate_user_permission(interaction)

            # The autocomplete returns the vndb_id directly, but handle other formats too
            vndb_id = (title or "").strip()
            if not vndb_id:
                raise ValidationError("Could not determine VN from input. Please try selecting from the autocomplete dropdown.")
            if not vndb_id.startswith("v"):
                vndb_id = f"v{vndb_id}"

            _log.info(
                f"User {interaction.user.name} is trying to remove a VN title with ID {vndb_id}."
            )

            if await check_if_not_exists(interaction, vndb_id):
                return

            await self.bot.RUN(
                DatabaseQueries.DELETE_VN_TITLE,
                (vndb_id,),
            )

            _log.info(f"VN title with ID {vndb_id} removed successfully.")

            await interaction.followup.send(
                f"VN title with ID `{vndb_id}` removed successfully.",
            )

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            await handle_command_error(interaction, e)

    @app_commands.command(name="vnpool", description="List all VN titles.")
    @app_commands.guild_only()
    async def list_vns(self, interaction: discord.Interaction):
        await interaction.response.defer()

        results = await self.bot.GET(DatabaseQueries.GET_ALL_VN_TITLES)

        if not results:
            await interaction.followup.send("No VN titles found in the database.")
            return

        # Process VN data for pagination
        vn_data = []
        
        for row in results:
            vndb_id, start_month, end_month, is_monthly_points, created_at = row
            
            try:
                vn_info = await from_vndb_id(interaction.client, vndb_id)
                if not vn_info:
                    _log.error(f"Failed to fetch VNDB info for ID {vndb_id}.")
                    continue
                    
                vn_data.append((vndb_id, start_month, end_month, is_monthly_points, vn_info))
                
            except Exception as e:
                _log.error(f"Error processing VN {vndb_id}: {e}")
                continue

        if not vn_data:
            await interaction.followup.send("No valid VN data found.")
            return

        # Sort VNs by monthly status (current monthly first), then by start date
        current_month = discord.utils.utcnow().strftime("%Y-%m")
        
        def sort_key(vn_tuple):
            vndb_id, start_month, end_month, is_monthly_points, vn_info = vn_tuple
            is_current_monthly = start_month <= current_month <= end_month
            # Current monthlies first
            # For start_month, use negative comparison to get descending order
            return (0 if is_current_monthly else 1, -1 * int(start_month.replace("-", "")))
        
        vn_data.sort(key=sort_key)

        # Create paginated view
        view = VNListView(
            vn_data=vn_data,
            title="📚 Visual Novels Library",
            per_page=10
        )
        
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(
        name="get_current_monthly", description="Show current monthly VNs."
    )
    async def get_current_monthly(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        current_month = get_current_month()

        results = await self.bot.GET(
            DatabaseQueries.GET_CURRENT_MONTHLY_VNS,
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
                await EmbedBuilder.create_vn_info_embed(
                    vn_info, start_month, end_month, is_monthly_points,
                    title_prefix="Current Monthly VN: ", color=discord.Color.blue()
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
