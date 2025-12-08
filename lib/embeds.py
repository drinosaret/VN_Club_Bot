"""
Shared embed builders for the VN Club Bot.
"""

import discord
import logging
from typing import Optional, List
from lib.utils import (
    create_base_embed, 
    format_points_display, 
    format_rating_display,
    create_vndb_link,
    truncate_text,
    MAX_EMBED_FIELD
)
from lib.vndb_api import VN_Entry

_log = logging.getLogger(__name__)


class EmbedBuilder:
    """Centralized embed creation utilities."""
    
    @staticmethod
    async def create_vn_completion_embed(
        user: discord.User,
        vn_info: VN_Entry,
        comment: str,
        current_points: int,
        new_points: int,
        rating: int,
        log_id: int
    ) -> discord.Embed:
        """
        Create embed for VN completion.

        Args:
            user: Discord user who completed the VN
            vn_info: VN information
            comment: User's comment
            current_points: User's points before completion
            new_points: User's points after completion
            rating: User's rating (1-5)
            log_id: Database log entry ID

        Returns:
            Configured embed for VN completion
        """
        display_title = vn_info.title_ja or vn_info.title_en or vn_info.vndb_id
        embed = create_base_embed(
            title=f"Finished reading **{display_title}**",
            color=discord.Color.green(),
            author_name=user.name,
            author_icon=user.display_avatar.url
        )

        if not vn_info.thumbnail_is_nsfw and vn_info.thumbnail_url:
            embed.set_thumbnail(url=vn_info.thumbnail_url)

        embed.add_field(
            name="Comment",
            value=truncate_text(comment, MAX_EMBED_FIELD),
            inline=False
        )

        embed.add_field(
            name="Points",
            value=format_points_display(current_points, new_points),
            inline=False
        )

        embed.add_field(
            name="Rating",
            value=f"**{rating}/5**",
            inline=False
        )

        # Set timestamp and footer
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text=f"Log #{log_id}")

        return embed

    @staticmethod
    async def create_vn_info_embed(
        vn_info: VN_Entry,
        start_month: str,
        end_month: str,
        points: int,
        title_prefix: str = "",
        color: discord.Color = discord.Color.blue()
    ) -> discord.Embed:
        """
        Create embed for VN information display.
        
        Args:
            vn_info: VN information
            start_month: Start month for monthly status
            end_month: End month for monthly status
            points: Points awarded for monthly reading
            title_prefix: Prefix for embed title
            color: Embed color
            
        Returns:
            Configured embed for VN information
        """
        vndb_link = await vn_info.get_vndb_link()
        points_not_monthly = await vn_info.get_points_not_monthly()

        display_title = vn_info.title_ja or vn_info.title_en or vn_info.vndb_id
        title = f"{title_prefix}{display_title}" if title_prefix else display_title
        embed = create_base_embed(title=title, color=color)
        
        embed.add_field(name="VNDB ID", value=vn_info.vndb_id, inline=True)
        embed.add_field(name="Start Month", value=start_month, inline=True)
        embed.add_field(name="End Month", value=end_month, inline=True)
        embed.add_field(name="Points (Monthly)", value=str(points), inline=True)
        embed.add_field(name="Points (Not Monthly)", value=str(points_not_monthly), inline=True)
        embed.add_field(name="VNDB Link", value=f"[View on VNDB]({vndb_link})", inline=False)
        
        description = await vn_info.get_normalized_description()
        embed.add_field(name="Description", value=description, inline=False)
        
        if not vn_info.thumbnail_is_nsfw and vn_info.thumbnail_url:
            embed.set_thumbnail(url=vn_info.thumbnail_url)
        
        embed.set_footer(text="Visual Novel Club")
        return embed

    @staticmethod
    def create_user_profile_embed(
        user: discord.User,
        total_entries: int,
        total_points: int,
        monthly_entries: int,
        vn_entries: int,
        most_active_server: str,
        most_active_count: int,
        recent_activity: List = None,
        average_rating: float = 0.0,
        rating_count: int = 0
    ) -> discord.Embed:
        """
        Create embed for user profile display.

        Args:
            user: Discord user or member
            total_entries: Total number of entries
            total_points: Total points earned
            monthly_entries: Number of monthly VN entries
            vn_entries: Number of VN entries
            most_active_server: Name of most active server
            most_active_count: Number of entries in most active server
            recent_activity: List of recent activity data
            average_rating: User's average rating across all VNs
            rating_count: Number of VNs the user has rated

        Returns:
            Configured embed for user profile
        """
        # display_name works for both User and Member
        display_name = getattr(user, 'display_name', user.name)
        embed = create_base_embed(
            title=f"📊 User Profile: {user.name}",
            color=discord.Color.blue(),
            author_name=display_name,
            author_icon=user.display_avatar.url
        )

        embed.set_thumbnail(url=user.display_avatar.url)
        
        # Main statistics
        embed.add_field(
            name="💰 Total Points",
            value=f"```\n{total_points or 0:,}\n```",
            inline=True
        )
        
        embed.add_field(
            name="📚 VN Completions",
            value=f"```\n{vn_entries}\n```",
            inline=True
        )
        
        embed.add_field(
            name="🔥 Monthly VNs",
            value=f"```\n{monthly_entries}\n```",
            inline=True
        )
        
        # Average rating field
        if rating_count > 0:
            embed.add_field(
                name="⭐ Average Rating",
                value=f"```\n{average_rating:.1f}/5\n```",
                inline=True
            )
        else:
            embed.add_field(
                name="⭐ Average Rating",
                value=f"```\nNo ratings yet\n```",
                inline=True
            )
        
        # Server activity
        embed.add_field(
            name="🏠 Most Active Server",
            value=f"{most_active_server}\n({most_active_count} completions)",
            inline=True
        )
        
        # Add spacer field
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        
        # Recent activity chart if available
        if recent_activity:
            activity_text = []
            for month, count in recent_activity[:6]:  # Last 6 months
                bar_length = min(20, max(1, count))  # Scale bar length
                bar = "█" * bar_length
                activity_text.append(f"`{month}` {bar} ({count})")
            
            if activity_text:
                embed.add_field(
                    name="📈 Recent Activity (Last 6 Months)",
                    value="\n".join(activity_text),
                    inline=False
                )
        
        # Footer with join date (only available for Members, not Users)
        joined_at = getattr(user, 'joined_at', None)
        if joined_at:
            join_date = joined_at.strftime('%B %Y')
            embed.set_footer(text=f"Member since {join_date}")
        else:
            embed.set_footer(text="Visual Novel Club")
        
        return embed

    @staticmethod
    def create_error_embed(
        title: str = "Error",
        description: str = "An error occurred",
        color: discord.Color = discord.Color.red()
    ) -> discord.Embed:
        """
        Create standardized error embed.
        
        Args:
            title: Error title
            description: Error description
            color: Embed color
            
        Returns:
            Configured error embed
        """
        return create_base_embed(title=title, description=description, color=color)

    @staticmethod
    def create_success_embed(
        title: str = "Success",
        description: str = "Operation completed successfully",
        color: discord.Color = discord.Color.green()
    ) -> discord.Embed:
        """
        Create standardized success embed.
        
        Args:
            title: Success title
            description: Success description
            color: Embed color
            
        Returns:
            Configured success embed
        """
        return create_base_embed(title=title, description=description, color=color)

    @staticmethod
    def create_info_embed(
        title: str,
        description: str = None,
        color: discord.Color = discord.Color.blue()
    ) -> discord.Embed:
        """
        Create standardized info embed.
        
        Args:
            title: Info title
            description: Info description
            color: Embed color
            
        Returns:
            Configured info embed
        """
        return create_base_embed(title=title, description=description, color=color)

    @staticmethod
    def create_leaderboard_embed(
        title: str,
        leaderboard_data: List,
        current_page: int,
        max_pages: int,
        per_page: int = 20
    ) -> discord.Embed:
        """
        Create leaderboard embed with rankings.
        
        Args:
            title: Leaderboard title
            leaderboard_data: List of (username, points) tuples
            current_page: Current page number (0-indexed)
            max_pages: Total number of pages
            per_page: Items per page
            
        Returns:
            Configured leaderboard embed
        """
        embed = create_base_embed(title=title, color=discord.Color.gold())
        
        start_idx = current_page * per_page
        end_idx = min(start_idx + per_page, len(leaderboard_data))
        
        description_strings = []
        for i in range(start_idx, end_idx):
            username, points = leaderboard_data[i]
            rank = i + 1
            
            # Add medal emojis for top 3
            if rank == 1:
                medal = "🥇"
            elif rank == 2:
                medal = "🥈"
            elif rank == 3:
                medal = "🥉"
            else:
                medal = "◆"
            
            description_strings.append(f"{medal} **{rank}.** {username}: **{points:,}**点")
        
        embed.description = "\n".join(description_strings)
        embed.set_footer(text=f"Page {current_page + 1}/{max_pages} • {len(leaderboard_data):,} total users")
        
        return embed