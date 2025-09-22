import discord
import discord.app_commands as app_commands
import json
import logging
from discord.ext import commands
from cogs.vn_title_management import get_single_monthly_vn
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry
from lib.pagination import BasePaginationView, GenericPaginationView
from lib.utils import (
    DatabaseQueries, 
    get_current_month,
    is_month_in_range,
    validate_user_permission,
    validate_rating_input,
    validate_comment_length,
    handle_command_error,
    truncate_text,
    BotError,
    ValidationError,
    MAX_DISCORD_MESSAGE,
    MAX_EMBED_DESCRIPTION,
    EMBED_DESCRIPTION_BUFFER,
    create_base_embed,
    add_pagination_footer
)
from lib.embeds import EmbedBuilder
from lib.autocomplete import vn_autocomplete, user_logs_autocomplete, month_autocomplete, server_autocomplete, RATING_CHOICES
from .username_fetcher import get_username_db
from math import ceil

_log = logging.getLogger(__name__)


# ==================== VIEW CLASSES ====================


class HelpView(BasePaginationView):
    """Paginated view for help information"""
    
    def __init__(self, help_data, per_page=3):
        super().__init__(help_data, "📖 Visual Novel Club Bot - Commands Help", per_page)
    
    def create_embed(self):
        """Create an embed for the current page"""
        embed = create_base_embed(
            title=self.title,
            description="Here are all the available commands for the Visual Novel Club Bot:",
            color=discord.Color.blue()
        )
        embed.set_author(name="Visual Novel Club Bot")
        
        page_data = self.get_page_data()
        
        for cmd_data in page_data:
            # Create field value with parameters and description
            field_value = f"**Usage:** `{cmd_data['usage']}`\n"
            field_value += f"**Description:** {cmd_data['description']}\n"
            
            if cmd_data.get('parameters'):
                field_value += f"**Parameters:**\n{cmd_data['parameters']}\n"
            
            if cmd_data.get('example'):
                field_value += f"**Example:** `{cmd_data['example']}`"
            
            embed.add_field(
                name=f"🔹 {cmd_data['name']}",
                value=field_value,
                inline=False
            )
        
        add_pagination_footer(embed, self.current_page, self.max_pages, len(self.data))
        return embed


class ReadingLogsView(BasePaginationView):
    """Paginated view for user reading logs"""
    
    def __init__(self, logs_data, member, per_page=5):
        self.member = member
        super().__init__(logs_data, f"📚 Reading Logs for {member.name}", per_page)
    
    def create_embed(self):
        """Create an embed for the current page"""
        embed = create_base_embed(
            title=self.title, 
            color=discord.Color.blue(),
            author_name=self.member.name,
            author_icon=self.member.display_avatar.url
        )
        
        page_data = self.get_page_data()
        
        if not page_data:
            embed.description = "No logs found on this page."
        else:
            # Join all log entries for this page
            combined_description = "\n\n".join(page_data)
            
            # Ensure description doesn't exceed Discord's limit
            if len(combined_description) > MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER:
                combined_description = combined_description[:MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER - 3] + "..."
            
            embed.description = combined_description
        
        add_pagination_footer(embed, self.current_page, self.max_pages, len(self.data))
        return embed


class LeaderboardView(BasePaginationView):
    """Paginated view for leaderboard with navigation buttons"""
    
    def __init__(self, leaderboard_data, title, per_page=20):
        super().__init__(leaderboard_data, title, per_page)
    
    def create_embed(self):
        """Create an embed for the current page"""
        return EmbedBuilder.create_leaderboard_embed(
            self.title,
            self.data,
            self.current_page,
            self.max_pages,
            self.per_page
        )


class VNRatingsView(BasePaginationView):
    """Paginated view for VN ratings"""
    
    def __init__(self, ratings_data, vn_title, average_rating, total_ratings, per_page=10):
        self.vn_title = vn_title
        self.average_rating = average_rating
        self.total_ratings = total_ratings
        super().__init__(ratings_data, f"⭐ User Ratings for {vn_title}", per_page)
    
    def create_embed(self):
        """Create an embed for the current page"""
        embed = create_base_embed(
            title=self.title,
            color=discord.Color.blue()
        )
        
        page_data = self.get_page_data()
        
        if not page_data:
            embed.description = "No ratings found on this page."
        else:
            # Join all rating entries for this page
            combined_description = "\n\n".join(page_data)
            
            # Add average rating info to page 1
            if self.current_page == 0:
                average_info = f"Average Rating: **{self.average_rating:.1f}/5** ⭐ ({self.total_ratings} ratings)\n\n"
                combined_description = average_info + combined_description
            
            # Ensure description doesn't exceed Discord's limit
            if len(combined_description) > MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER:
                combined_description = combined_description[:MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER - 3] + "..."
            
            embed.description = combined_description
        
        add_pagination_footer(embed, self.current_page, self.max_pages, len(self.data))
        return embed


# ==================== HELPER FUNCTIONS ====================


async def log_already_exists(
    interaction: discord.Interaction, user_id: int, vndb_id: str
) -> bool:
    """Check if user already logged reading this VN."""
    result = await interaction.client.GET_ONE(
        DatabaseQueries.GET_USER_VN_LOG, (user_id, vndb_id)
    )
    if result:
        await interaction.followup.send("You have already logged reading this VN!")
        return True
    return False


# ==================== MAIN COG CLASS ====================

class VNUserCommands(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        await self.bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)

    @app_commands.command(name="help", description="Show detailed help for all commands.")
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Load help data from JSON file
        help_file_path = "help_commands.json"
        try:
            with open(help_file_path, 'r', encoding='utf-8') as f:
                help_json = json.load(f)
            
            help_data = help_json["commands"]
            
        except FileNotFoundError:
            await interaction.followup.send("❌ Help file not found. Please contact an administrator.")
            return
        except json.JSONDecodeError:
            await interaction.followup.send("❌ Help file is corrupted. Please contact an administrator.")
            return
        except Exception as e:
            _log.error(f"Error loading help data: {e}")
            await interaction.followup.send("❌ An error occurred while loading help information.")
            return

        # Create paginated view
        view = HelpView(help_data, per_page=3)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="finish_vn", description="Mark a VN as finished.")
    @app_commands.describe(
        vndb_id="The VNDB ID of the title you finished.",
        comment="Your comment/review about the VN (max 1000 characters).",
        rating="Your personal rating for the VN (1-5) 1=Terrible; 5=Masterpiece.",
    )
    @app_commands.autocomplete(vndb_id=vn_autocomplete)
    @app_commands.choices(rating=RATING_CHOICES)
    @app_commands.guild_only()
    async def finish_vn(
        self, interaction: discord.Interaction, vndb_id: str, comment: str, rating: int
    ):
        await interaction.response.defer()

        try:
            # Validate inputs
            await validate_comment_length(comment)
            await validate_rating_input(rating)

            current_month = get_current_month()

            # Check if this VN is currently monthly
            result = await get_single_monthly_vn(interaction.client, vndb_id)
            if result:
                _, start_month, end_month, is_monthly_points = result
                read_in_monthly = is_month_in_range(current_month, start_month, end_month)
            else:
                read_in_monthly = False

            # Get VN info
            vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
            if not vn_info:
                raise ValidationError("VNDB ID not found or invalid.")

            # Check if already logged
            if await log_already_exists(interaction, interaction.user.id, vndb_id):
                return

            # Calculate points
            if read_in_monthly:
                reward_points = is_monthly_points
                reward_reason = "As Monthly VN"
            else:
                reward_points = await vn_info.get_points_not_monthly()
                reward_reason = "As Non-Monthly VN"

            # Get current points
            current_total_result = await self.bot.GET_ONE(
                DatabaseQueries.GET_USER_TOTAL_POINTS, (interaction.user.id,)
            )
            current_total_points = current_total_result[0] if current_total_result and current_total_result[0] else 0

            _log.info(
                f"Adding reading log for user {interaction.user.id} ({interaction.user.name}) - "
                f"VNDB ID: {vndb_id}, Rating: {rating}, Reward Reason: {reward_reason}, "
                f"Reward Month: {current_month}, Points: {reward_points}, Comment: {comment}"
            )

            # Add log to database
            await self.bot.RUN(
                DatabaseQueries.ADD_READING_LOG,
                (
                    interaction.user.id,
                    vndb_id,
                    rating,
                    reward_reason,
                    current_month,
                    reward_points,
                    comment,
                    interaction.guild.id,
                ),
            )

            new_total_points = current_total_points + reward_points

            # Create and send embed
            embed = await EmbedBuilder.create_vn_completion_embed(
                interaction.user,
                vn_info,
                comment,
                current_total_points,
                new_total_points,
                rating,
            )
            await interaction.followup.send(embed=embed)

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.error(f"Unexpected error in finish_vn: {e}")
            await handle_command_error(interaction, e, "An error occurred while processing your VN completion.")
            raise

    @app_commands.command(name="vn_leaderboard", description="Print the leaderboard.")
    @app_commands.describe(
        month="Optional: Filter by specific month (e.g., '2025-09')",
        server="Optional: Filter by specific server"
    )
    @app_commands.autocomplete(month=month_autocomplete, server=server_autocomplete)
    async def vn_leaderboard(
        self, 
        interaction: discord.Interaction, 
        month: str = None, 
        server: str = None
    ):
        await interaction.response.defer()

        try:
            # Choose the appropriate query based on filters
            if month and server:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH_AND_SERVER, (month, int(server)))
                filter_description = f"for **{month}** in server"
            elif month:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH, (month,))
                filter_description = f"for **{month}** (all servers)"
            elif server:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_SERVER, (int(server),))
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                filter_description = f"for **{server_name}** (all time)"
            else:
                results = await self.bot.GET(DatabaseQueries.GET_ALL_LOGS)
                filter_description = "(all time, all servers)"

            if not results:
                filter_msg = f" {filter_description}" if filter_description != "(all time, all servers)" else ""
                await interaction.followup.send(f"No reading logs found{filter_msg}.")
                return

            # Build leaderboard
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

            # Sort the leaderboard by points (descending)
            sorted_leaderboard = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
            
            # Create dynamic title based on filters
            if month and server:
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                title = f"📚 VN Leaderboard - {month} - {server_name}"
            elif month:
                title = f"📚 VN Leaderboard - {month}"
            elif server:
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                title = f"📚 VN Leaderboard - {server_name}"
            else:
                title = "📚 Visual Novel Reading Logs Leaderboard"
            
            # Create paginated view
            view = LeaderboardView(
                leaderboard_data=sorted_leaderboard,
                title=title,
                per_page=20
            )
            
            embed = view.create_embed()
            await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await handle_command_error(interaction, e)

    @app_commands.command(
        name="vn_server_leaderboard",
        description="Print the leaderboard for each server.",
    )
    async def vn_server_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        results = await self.bot.GET(DatabaseQueries.GET_ALL_LOGS)

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
            if logged_in_guild not in leaderboard:
                leaderboard[logged_in_guild] = {}
            if username not in leaderboard[logged_in_guild]:
                leaderboard[logged_in_guild][username] = 0
            leaderboard[logged_in_guild][username] += points

        # Sort by total points
        filtered_leaderboard = {}
        server_totals = []
        
        for guild_id, users in leaderboard.items():
            if users:  # Only include servers with users
                guild_total = sum(users.values())
                server_totals.append((guild_id, guild_total))
                filtered_leaderboard[guild_id] = users
        
        # Sort servers by total points (descending)
        server_totals.sort(key=lambda x: x[1], reverse=True)
        sorted_server_data = {guild_id: filtered_leaderboard[guild_id] for guild_id, _ in server_totals}

        if not sorted_server_data:
            await interaction.followup.send("No server data found.")
            return

        embed = discord.Embed(
            title="🏆 Server Leaderboard",
            description="Servers ranked by total points with top 5 contributors",
            color=discord.Color.gold(),
        )

        for rank, (guild_id, guild_total) in enumerate(server_totals, start=1):
            users_data = sorted_server_data[guild_id]
            guild = self.bot.get_guild(guild_id)
            guild_name = guild.name if guild else f"Server {guild_id}"
            
            # Get top 5 users for this server
            sorted_users = sorted(users_data.items(), key=lambda x: x[1], reverse=True)
            top_users = sorted_users[:5]
            
            # Create user list
            user_lines = []
            for i, (username, points) in enumerate(top_users, start=1):
                user_lines.append(f"`{i}.` {username} — {points}点")
            
            user_list = "\n".join(user_lines)
            
            # Add as embed field
            embed.add_field(
                name=f"#{rank} {guild_name} — {guild_total}点",
                value=user_list,
                inline=False
            )

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="user", description="View user statistics and profile.")
    @app_commands.describe(member="The member whose profile you want to view (defaults to yourself).")
    async def user_profile(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        await interaction.response.defer()

        if member is None:
            member = interaction.user

        # Get basic user statistics
        stats_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_STATS, (member.id,))
        if not stats_result:
            await interaction.followup.send(f"No data found for {member.name}.")
            return

        total_entries, total_points, monthly_entries, vn_entries = stats_result

        if total_entries == 0:
            await interaction.followup.send(f"{member.name} has no reading logs yet.")
            return

        # Get most active server
        most_active_server_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_MOST_ACTIVE_SERVER, (member.id,))
        most_active_server = "Unknown"
        most_active_count = 0
        if most_active_server_result:
            guild_id, entry_count = most_active_server_result
            most_active_count = entry_count
            guild = self.bot.get_guild(guild_id)
            most_active_server = guild.name if guild else f"Server {guild_id}"

        # Get recent activity (last 6 months)
        recent_activity = await self.bot.GET(DatabaseQueries.GET_USER_RECENT_ACTIVITY, (member.id,))

        # Get user's average rating
        avg_rating_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_AVERAGE_RATING, (member.id,))
        average_rating = 0.0
        rating_count = 0
        if avg_rating_result and avg_rating_result[0] is not None:
            average_rating = avg_rating_result[0]
            rating_count = avg_rating_result[1]

        # Calculate additional statistics
        non_monthly_entries = vn_entries - monthly_entries
        
        # Create the embed using EmbedBuilder
        embed = EmbedBuilder.create_user_profile_embed(
            member,
            total_entries,
            total_points,
            monthly_entries,
            vn_entries,
            most_active_server,
            most_active_count,
            recent_activity,
            average_rating,
            rating_count
        )

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="user_logs", description="View your reading logs.")
    @app_commands.describe(member="The member whose logs you want to view.")
    async def user_logs(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        await interaction.response.defer()

        if member is None:
            member = interaction.user

        results = await self.bot.GET(DatabaseQueries.GET_USER_LOGS, (member.id,))
        if not results:
            await interaction.followup.send(f"No reading logs found for {member.name}.")
            return

        # Process logs into formatted strings
        log_entries = []
        for row in results:
            (
                user_id,
                vndb_id,
                user_rating,
                reward_reason,
                reward_month,
                points,
                comment,
                logged_in_guild,
            ) = row

            if vndb_id:
                vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
                link = await vn_info.get_vndb_link()
                
                # Display full comment - length validation prevents overly long comments
                display_comment = comment or 'No comment provided.'
                # Prioritize Japanese title, fallback to English title, or generic text if both are empty
                display_title = vn_info.title_ja or vn_info.title_en or "View on VNDB"
                
                log_entry = (
                    f"**{reward_month}**: [{display_title}]({link}) - {points}点 ({reward_reason})\n"
                    f"Comment: {display_comment} | Rating: {user_rating or 'No rating provided.'}/5"
                )
            else:
                # Display full comment for non-VN entries too
                display_comment = comment or 'No comment provided.'
                    
                log_entry = (
                    f"**{reward_month}**: No VN specified - {points}点 ({reward_reason})\n"
                    f"Comment: {display_comment}"
                )
            
            log_entries.append(log_entry)

        # Create paginated view for logs (5 per page)
        combined_description = "\n\n".join(log_entries)
        
        # If we have 5 or fewer logs AND the combined description fits in Discord's limit, show all at once
        if len(log_entries) <= 5 and len(combined_description) <= 4090:
            # Show all logs without pagination
            embed = discord.Embed(
                title=f"📚 Reading Logs for {member.name}", color=discord.Color.blue()
            )
            embed.set_author(name=member.name, icon_url=member.display_avatar.url)
            
            embed.description = combined_description
            embed.set_footer(text=f"{len(log_entries)} total logs")
            await interaction.followup.send(embed=embed)
        else:
            # Use pagination for more than 5 logs OR if description is too long
            view = ReadingLogsView(log_entries, member, per_page=5)
            embed = view.create_embed()
            await interaction.followup.send(embed=embed, view=view)

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

        if not await validate_user_permission(interaction):
            return

        await self.bot.RUN(
            DatabaseQueries.REWARD_USER_POINTS,
            (
                member.id,
                reason,
                get_current_month(),
                points,
                interaction.guild.id,
            ),
        )
        
        # Truncate reason to ensure message doesn't exceed Discord's limits
        truncated_reason = truncate_text(reason, 1900)  # Leave room for other content
        
        await interaction.followup.send(
            f"Rewarded **{points}** points to {member.mention} for the following reason: `{truncated_reason}`"
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

        if not await validate_user_permission(interaction):
            return

        # Check if the log exists
        result = await self.bot.GET_ONE(DatabaseQueries.GET_LOG_BY_ID, (log_id,))
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
        _log.info(
            f"Deleting log {log_id} for user {member.id} ({member.name}) - "
            f"VNDB ID: {vndb_id}, Reward Reason: {reward_reason}, "
            f"Reward Month: {reward_month}, Points: {points}, Comment: {comment}"
        )
        await self.bot.RUN(DatabaseQueries.DELETE_LOG_BY_ID, (log_id,))
        
        # Truncate comment to ensure message doesn't exceed Discord's 2000 char limit
        display_comment = comment or 'No comment provided.'
        # Calculate remaining space for comment after other content
        base_message = (
            f"Deleted the following log for {member.mention}:\n"
            f"**VNDB ID:** {vndb_id}\n"
            f"**Reward Reason:** {reward_reason}\n"
            f"**Reward Month:** {reward_month}\n"
            f"**Points:** {points}\n"
            f"**Comment:** "
        )
        max_comment_length = 1990 - len(base_message)  # Leave some buffer
        truncated_comment = truncate_text(display_comment, max_comment_length)
        
        # Add indicator if comment was truncated
        comment_display = truncated_comment
        if len(display_comment) > max_comment_length:
            comment_display += f" *[Comment truncated - was {len(display_comment)} characters]*"
        
        try:
            await interaction.followup.send(
                f"Deleted the following log for {member.mention}:\n"
                f"**VNDB ID:** {vndb_id}\n"
                f"**Reward Reason:** {reward_reason}\n"
                f"**Reward Month:** {reward_month}\n"
                f"**Points:** {points}\n"
                f"**Comment:** {comment_display}"
            )
        except discord.HTTPException as e:
            if e.code == 50035:  # Invalid Form Body (message too long)
                # Fallback with minimal information
                _log.error(f"Discord message length error in delete_log: {e}")
                await interaction.followup.send(
                    f"✅ Successfully deleted log {log_id} for {member.mention}.\n"
                    f"VNDB ID: {vndb_id} | Month: {reward_month} | Points: {points}\n"
                    f"*Note: Comment was too long to display.*"
                )
            else:
                # Re-raise other HTTP exceptions
                raise
        except Exception as e:
            _log.error(f"Unexpected error in delete_log: {e}")
            raise

    @app_commands.command(name="ratings", description="View ratings for a VN.")
    @app_commands.describe(
        vndb_id="The VNDB ID of the title you want to view ratings for."
    )
    @app_commands.autocomplete(vndb_id=vn_autocomplete)
    async def ratings(self, interaction: discord.Interaction, vndb_id: str):
        await interaction.response.defer()

        try:
            # Get VN info first
            vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
            if not vn_info:
                raise ValidationError("VNDB ID not found or invalid.")

            # Get all ratings for this VN
            ratings = await self.bot.GET(DatabaseQueries.GET_ALL_VN_RATINGS, (vndb_id,))

            if not ratings:
                await interaction.followup.send(f"No ratings found for **{vn_info.title_ja}**.")
                return

            # Process ratings into formatted strings
            rating_entries = []
            total_ratings = 0
            total_score = 0
            
            for user_id, user_rating, comment in ratings:
                user_name = await get_username_db(self.bot, user_id)
                
                # Calculate average
                total_ratings += 1
                total_score += user_rating
                
                # Format rating with stars
                stars = "⭐" * user_rating
                rating_entry = f"**{user_name}**: {user_rating}/5 {stars}"
                
                if comment:
                    # Truncate comment if too long to fit nicely
                    truncated_comment = truncate_text(comment, 150)
                    rating_entry += f"\n*\"{truncated_comment}\"*"
                
                rating_entries.append(rating_entry)

            # Calculate average rating
            average_rating = total_score / total_ratings if total_ratings > 0 else 0

            # If we have 10 or fewer ratings, show all at once without pagination
            if len(rating_entries) <= 10:
                display_title = vn_info.title_ja or vn_info.title_en or "VN"
                embed = create_base_embed(
                    title=f"⭐ User Ratings for **{display_title}**",
                    description=f"Average Rating: **{average_rating:.1f}/5** ⭐ ({total_ratings} ratings)\n\n" + "\n\n".join(rating_entries),
                    color=discord.Color.blue()
                )
                
                # Add VN thumbnail if not NSFW
                if not vn_info.thumbnail_is_nsfw:
                    embed.set_thumbnail(url=vn_info.thumbnail_url)
                
                embed.set_footer(text=f"{len(rating_entries)} total ratings")
                await interaction.followup.send(embed=embed)
            else:
                # Use pagination for more than 10 ratings
                display_title = vn_info.title_ja or vn_info.title_en or "VN"
                view = VNRatingsView(rating_entries, display_title, average_rating, total_ratings, per_page=10)
                embed = view.create_embed()
                
                # Add VN thumbnail if not NSFW
                if not vn_info.thumbnail_is_nsfw:
                    embed.set_thumbnail(url=vn_info.thumbnail_url)
                
                await interaction.followup.send(embed=embed, view=view)

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            await handle_command_error(interaction, e, "An error occurred while fetching ratings.")


async def setup(bot: VNClubBot):
    await bot.add_cog(VNUserCommands(bot))
