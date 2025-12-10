"""
Shared autocomplete functions for the VN Club Bot.
"""

import discord
import logging
from typing import List
from lib.utils import DatabaseQueries
from lib.vndb_search import search_visual_novel, create_autocomplete_value, parse_autocomplete_value

logger = logging.getLogger(__name__)


async def vn_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """
    Autocomplete function for VN titles using VNDB search.

    Args:
        interaction: Discord interaction
        current: Current user input

    Returns:
        List of choices for autocomplete
    """
    query = (current or "").strip()

    # Handle already-selected autocomplete value (user clicked back on the field)
    if query.startswith("${") and query.endswith("}"):
        parsed = parse_autocomplete_value(query)
        if parsed:
            vndb_id, field, source = parsed
            # Try to get VN info from cache to show the title
            try:
                from lib.vndb_api import from_vndb_id
                vn_info = await from_vndb_id(interaction.client, vndb_id)
                if vn_info:
                    display_title = vn_info.title_ja or vn_info.title_en or vndb_id
                    return [discord.app_commands.Choice(name=display_title, value=query)]
            except Exception:
                pass
            # Fallback: return the raw ID as a choice
            return [discord.app_commands.Choice(name=f"Selected: {vndb_id}", value=query)]

    if len(query) < 2:
        return []

    try:
        results = await search_visual_novel(query, limit=25)
    except Exception as exc:
        logger.warning("VNDB autocomplete failed for '%s': %s", query, exc)
        return []

    choices: List[discord.app_commands.Choice[str]] = []

    for vn in results:
        vn_id = vn.get("id")
        titles = vn.get("titles") or {}
        display_title = vn.get("display") or titles.get("primary")

        if not vn_id or not display_title:
            continue

        # Determine which title key we are using for display
        field_key = "primary"
        for key, option in (("en", "en"), ("ja", "jp"), ("romaji", "romaji")):
            if titles.get(key) and titles.get(key) == display_title:
                field_key = option
                break

        choice_label = display_title

        rating = vn.get("rating")
        released = vn.get("released")
        badge_parts: List[str] = []
        if isinstance(released, str) and released:
            badge_parts.append(released)
        if isinstance(rating, (int, float)) and rating > 0:
            badge_parts.append(f"{rating / 10:.1f}/10")

        # Build label with VN ID suffix to survive Discord token replacement
        vn_id_suffix = f" [{vn_id}]"
        if badge_parts:
            choice_label = f"{display_title} — {' • '.join(badge_parts)}{vn_id_suffix}"
        else:
            choice_label = f"{display_title}{vn_id_suffix}"

        # Truncate title if needed, but preserve the VN ID suffix
        if len(choice_label) > 100:
            if badge_parts:
                badge_str = f" — {' • '.join(badge_parts)}"
                available_for_title = 100 - len(badge_str) - len(vn_id_suffix) - 1
                truncated_title = display_title[:available_for_title] + "…"
                choice_label = f"{truncated_title}{badge_str}{vn_id_suffix}"
            else:
                truncated_title = display_title[:100 - len(vn_id_suffix) - 1] + "…"
                choice_label = f"{truncated_title}{vn_id_suffix}"

        value = create_autocomplete_value(vn_id, field_key, source="vndb")
        choices.append(discord.app_commands.Choice(name=choice_label, value=value))

    return choices[:25]


async def user_logs_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """
    Autocomplete function for user logs.

    Args:
        interaction: Discord interaction
        current: Current user input

    Returns:
        List of choices for autocomplete
    """
    try:
        # Get member from namespace if provided, otherwise use the current user
        member = getattr(interaction.namespace, "member", None) or interaction.user

        results = await interaction.client.GET(
            DatabaseQueries.USER_LOGS_AUTOCOMPLETE,
            (member.id,)
        )

        if not results:
            return []

        choices = []
        for log_id, vndb_id, reward_month, reward_reason, points, vn_title in results[:25]:
            # Use VN title if available, otherwise show reward reason for non-VN logs
            display_name = vn_title or reward_reason or "Unknown"
            label = f"#{log_id} | {display_name} ({reward_month}, {points}点)"
            # Truncate if needed (Discord limit is 100 chars)
            if len(label) > 100:
                label = label[:97] + "..."
            choices.append(discord.app_commands.Choice(name=label, value=log_id))

        return choices
    except Exception:
        # Return empty list if autocomplete fails
        return []


async def month_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """
    Autocomplete function for available months.
    
    Args:
        interaction: Discord interaction
        current: Current user input
        
    Returns:
        List of choices for autocomplete
    """
    try:
        # Get distinct months from reading logs
        results = await interaction.client.GET(DatabaseQueries.GET_DISTINCT_MONTHS)
        
        if not results:
            return []

        months = [month[0] for month in results if month[0]]
        
        if not current:
            return [
                discord.app_commands.Choice(name=month, value=month)
                for month in months[:25]
            ]
        else:
            # Filter by current input
            filtered_months = [
                month for month in months
                if current.lower() in month.lower()
            ]
            return [
                discord.app_commands.Choice(name=month, value=month)
                for month in filtered_months[:25]
            ]
    except Exception:
        return []


async def server_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """
    Autocomplete function for servers with reading logs.

    Args:
        interaction: Discord interaction
        current: Current user input

    Returns:
        List of choices for autocomplete
    """
    try:
        # Get distinct guilds from reading logs
        results = await interaction.client.GET(DatabaseQueries.GET_DISTINCT_SERVERS)

        if not results:
            return []

        choices = []
        for (guild_id,) in results:
            guild = interaction.client.get_guild(guild_id)
            guild_name = guild.name if guild else f"Unknown Server ({guild_id})"

            if not current or current.lower() in guild_name.lower():
                choices.append(discord.app_commands.Choice(name=guild_name, value=str(guild_id)))

        return choices[:25]
    except Exception:
        return []


async def vn_pool_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """
    Autocomplete function for VNs in the monthly title pool.

    Args:
        interaction: Discord interaction
        current: Current user input

    Returns:
        List of choices for autocomplete (VNs already in the database)
    """
    try:
        # Get all VN titles from the pool with their cached info
        results = await interaction.client.GET(DatabaseQueries.VN_AUTOCOMPLETE)

        if not results:
            return []

        query = (current or "").strip().lower()
        choices = []

        for vndb_id, title_ja in results:
            display_title = title_ja or vndb_id

            # Filter by current input if provided
            if query and query not in display_title.lower() and query not in vndb_id.lower():
                continue

            label = f"{display_title} ({vndb_id})"
            if len(label) > 100:
                label = label[:97] + "..."

            choices.append(discord.app_commands.Choice(name=label, value=vndb_id))

        return choices[:25]
    except Exception:
        return []


# Rating choices for consistent use across commands
RATING_CHOICES = [
    discord.app_commands.Choice(name="⭐ 1 - Terrible", value=1),
    discord.app_commands.Choice(name="⭐⭐ 2 - Bad", value=2),
    discord.app_commands.Choice(name="⭐⭐⭐ 3 - Average", value=3),
    discord.app_commands.Choice(name="⭐⭐⭐⭐ 4 - Good", value=4),
    discord.app_commands.Choice(name="⭐⭐⭐⭐⭐ 5 - Masterpiece", value=5),
]