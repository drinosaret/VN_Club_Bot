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
    MAX_EMBED_DESCRIPTION
)
from lib.monthly_banner import format_length_tier
from lib.vndb_api import VN_Entry
from lib.jiten_client import resolve_display_cover

_log = logging.getLogger(__name__)


def build_vn_links_view(vndb_id: str, jiten_deck_id: Optional[int]) -> discord.ui.View:
    """VNDB + jiten.moe link buttons for any single-VN embed/banner."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="VNDB",
        style=discord.ButtonStyle.link,
        url=f"https://vndb.org/{vndb_id}",
    ))
    if jiten_deck_id is not None:
        view.add_item(discord.ui.Button(
            label="jiten.moe",
            style=discord.ButtonStyle.link,
            url=f"https://jiten.moe/decks/media/{jiten_deck_id}/detail",
        ))
    return view


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
        log_id: int,
        jiten_data=None,
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
            jiten_data: optional JitenInfo; lets an NSFW VNDB cover fall back
                to the guaranteed-SFW jiten cover instead of being hidden.

        Returns:
            Configured embed for VN completion
        """
        display_title = vn_info.title_ja or vn_info.title_en or vn_info.vndb_id
        points_earned = new_points - current_points

        embed = create_base_embed(
            title=f"Finished reading **{display_title}**",
            color=discord.Color.green(),
            author_name=user.name,
            author_icon=user.display_avatar.url
        )

        cover_url, cover_is_nsfw = resolve_display_cover(vn_info, jiten_data)
        if not cover_is_nsfw and cover_url:
            embed.set_thumbnail(url=cover_url)

        header = "**Comment**\n"
        embed.description = header + truncate_text(comment, MAX_EMBED_DESCRIPTION - len(header))

        embed.set_footer(text=f"⭐ {rating}/5 • +{points_earned:,} pts • Log #{log_id}")
        embed.timestamp = discord.utils.utcnow()

        return embed

    @staticmethod
    async def create_nominee_card_embed(
        vn_info: VN_Entry,
        jiten_data=None,
        vote_count: Optional[int] = None,
        footer_phase: Optional[str] = None,
        nominator: Optional[discord.User] = None,
    ) -> discord.Embed:
        """
        Card embed for a single nomination, used during the nomination and
        voting phases. Reuses VN_Entry fields and optionally enriches with
        jiten char count when present.

        footer_phase: 'nominations' | 'voting' | None — drives footer text.
        vote_count:  if not None, an inline "Votes" field is added (voting phase).
        nominator:   if provided, shown as the embed author so people can see
                     who put the nomination forward.
        """
        display_title = vn_info.title_ja or vn_info.title_en or vn_info.vndb_id
        vndb_link = await vn_info.get_vndb_link()

        embed = create_base_embed(
            title=display_title,
            color=discord.Color.blurple(),
        )
        embed.url = vndb_link

        if nominator is not None:
            embed.set_author(name=f"Nominated by {nominator.name}",
                             icon_url=nominator.display_avatar.url)

        cover_url, cover_is_nsfw = resolve_display_cover(vn_info, jiten_data)
        if not cover_is_nsfw and cover_url:
            embed.set_image(url=cover_url)

        embed.add_field(name="VNDB", value=f"[{vn_info.vndb_id}]({vndb_link})", inline=True)

        if vn_info.length_minutes:
            hours = round(vn_info.length_minutes / 60)
            length_text = f"{hours} {'hr' if hours == 1 else 'hrs'}"
        elif vn_info.length_rating:
            # VNDB falls back to a 1-5 category code when no precise
            # minute count is available. Reuse the banner's tier helper
            # so embed and banner stay consistent.
            length_text = format_length_tier(vn_info.length_rating) or str(vn_info.length_rating)
        else:
            length_text = "—"
        embed.add_field(name="Length", value=length_text, inline=True)

        if jiten_data is not None and getattr(jiten_data, "character_count", 0) > 0:
            embed.add_field(name="Characters",
                            value=f"{jiten_data.character_count:,}", inline=True)

        if vote_count is not None:
            embed.add_field(name="Votes", value=str(vote_count), inline=True)

        description = await vn_info.get_normalized_description(max_length=400)
        if description and description != "No description available.":
            embed.add_field(name="Description", value=description, inline=False)

        if footer_phase == "nominations":
            embed.set_footer(text="VN Club · Nominations open")
        elif footer_phase == "voting":
            embed.set_footer(text="VN Club · Voting open")
        else:
            embed.set_footer(text="VN Club")

        return embed

    @staticmethod
    async def create_vn_info_embed(
        vn_info: VN_Entry,
        start_month: str,
        end_month: str,
        points: int,
        title_prefix: str = "",
        color: discord.Color = discord.Color.blue(),
        pool_id: Optional[int] = None,
        jiten_data=None,
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
            pool_id: Pool entry ID. When provided, surfaced as a "Pool ID"
                field so users/admins can reference the row in chat or with
                `/manage_pool action:remove`.
            jiten_data: optional JitenInfo; lets an NSFW VNDB cover fall back
                to the guaranteed-SFW jiten cover instead of being hidden.

        Returns:
            Configured embed for VN information
        """
        points_not_monthly = await vn_info.get_points_not_monthly()

        display_title = vn_info.title_ja or vn_info.title_en or vn_info.vndb_id
        title = f"{title_prefix}{display_title}" if title_prefix else display_title
        embed = create_base_embed(title=title, color=color)

        if pool_id is not None:
            embed.add_field(name="Pool ID", value=f"#{pool_id}", inline=True)
        embed.add_field(name="VNDB ID", value=vn_info.vndb_id, inline=True)
        embed.add_field(name="Start Month", value=start_month, inline=True)
        embed.add_field(name="End Month", value=end_month, inline=True)
        embed.add_field(name="Points (Monthly)", value=str(points), inline=True)
        embed.add_field(name="Points (Not Monthly)", value=str(points_not_monthly), inline=True)

        description = await vn_info.get_normalized_description()
        embed.add_field(name="Description", value=description, inline=False)
        
        cover_url, cover_is_nsfw = resolve_display_cover(vn_info, jiten_data)
        if not cover_is_nsfw and cover_url:
            embed.set_thumbnail(url=cover_url)

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
        leaderboard_data: List[dict],
        current_page: int,
        max_pages: int,
        per_page: int = 20,
        *,
        period_label: Optional[str] = None,
        is_default_season: bool = False,
    ) -> discord.Embed:
        """
        Create leaderboard embed with rankings.

        Args:
            title: Leaderboard title
            leaderboard_data: List of dicts with keys ``username``, ``points``,
                ``completions`` (already sorted highest-first).
            current_page: Current page number (0-indexed)
            max_pages: Total number of pages
            per_page: Items per page
            period_label: Optional human-readable period (e.g. "Spring 2026"),
                rendered above the podium on page 0.
            is_default_season: True when the caller defaulted to current season
                because the user didn't pass a timeframe. Currently unused for
                visuals — kept on the signature so callers can consistently
                signal intent and we can wire in a hint later if desired.

        Returns:
            Configured leaderboard embed
        """
        embed = create_base_embed(title=title, color=discord.Color.gold())

        total_users = len(leaderboard_data)
        total_completions = sum(d.get("completions", 0) for d in leaderboard_data)
        total_points = sum(d.get("points", 0) for d in leaderboard_data)

        start_idx = current_page * per_page
        end_idx = min(start_idx + per_page, total_users)
        page_slice = leaderboard_data[start_idx:end_idx]

        def _vn_word(n: int) -> str:
            return "VN" if n == 1 else "VNs"

        # Page 0 gets a top-3 podium block (only if we actually have 3+ rows on
        # this page from the top). For pages > 0, skip the podium and just
        # render a numbered list. This keeps subsequent pages clean.
        podium_lines: list[str] = []
        list_entries: list[dict] = []
        list_start_rank: int

        if current_page == 0 and page_slice:
            podium_emojis = ["🥇", "🥈", "🥉"]
            podium_count = min(3, len(page_slice))
            for i in range(podium_count):
                d = page_slice[i]
                username = d.get("username") or "?"
                points = int(d.get("points", 0))
                completions = int(d.get("completions", 0))
                podium_lines.append(
                    f"{podium_emojis[i]} **{username}** · "
                    f"**{points:,}**点 · {completions} {_vn_word(completions)}"
                )
            list_entries = page_slice[podium_count:]
            list_start_rank = start_idx + podium_count + 1
        else:
            list_entries = page_slice
            list_start_rank = start_idx + 1

        # Compose description: just the podium. The period label is already
        # in the embed title (e.g. "VN Club Leaderboard — Spring 2026 ·
        # Season 4"), so re-rendering it as `*— {period} —*` above the
        # podium is purely redundant.
        if podium_lines:
            embed.description = "\n".join(podium_lines)

        # "Rankings" field: numbered list of the remaining (or all) page rows,
        # truncated with a friendly tail if it would overflow Discord's 1024
        # char field-value cap.
        if list_entries:
            FIELD_CAP = 1024
            built_lines: list[str] = []
            cur_len = 0
            truncated_remaining = 0
            for offset, d in enumerate(list_entries):
                rank = list_start_rank + offset
                username = d.get("username") or "?"
                points = int(d.get("points", 0))
                completions = int(d.get("completions", 0))
                line = (
                    f"`{rank}.` **{username}** — {points:,}点 · "
                    f"{completions} {_vn_word(completions)}"
                )
                # +1 for the join newline once we have at least one line.
                added = len(line) + (1 if built_lines else 0)
                # Reserve ~30 chars for a possible "…and N more" tail.
                if cur_len + added > FIELD_CAP - 30:
                    truncated_remaining = len(list_entries) - offset
                    break
                built_lines.append(line)
                cur_len += added

            if truncated_remaining:
                built_lines.append(f"…and {truncated_remaining} more")

            field_value = "\n".join(built_lines) if built_lines else "—"
            embed.add_field(name="Rankings", value=field_value, inline=False)

        embed.set_footer(
            text=(
                f"Page {current_page + 1}/{max_pages} · "
                f"{total_users:,} readers · "
                f"{total_completions:,} completions · "
                f"{total_points:,}点"
            )
        )

        return embed