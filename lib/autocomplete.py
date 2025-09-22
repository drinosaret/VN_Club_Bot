"""
Shared autocomplete functions for the VN Club Bot.
"""

import discord
from typing import List
from lib.utils import DatabaseQueries


async def vn_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """
    Autocomplete function for VN titles.
    
    Args:
        interaction: Discord interaction
        current: Current user input
        
    Returns:
        List of choices for autocomplete
    """
    try:
        current_vns = await interaction.client.GET(DatabaseQueries.VN_AUTOCOMPLETE)
        
        if not current:
            # Return first 25 VNs if no input
            return [
                discord.app_commands.Choice(name=f"{name} ({vndb_id})", value=vndb_id)
                for (vndb_id, name) in current_vns[:25]
            ]
        else:
            # Filter by current input
            filtered_vns = [
                (vndb_id, name) for (vndb_id, name) in current_vns
                if (current.lower() in name.lower() or 
                    current.lower() in vndb_id.lower())
            ]
            return [
                discord.app_commands.Choice(name=f"{name} ({vndb_id})", value=vndb_id)
                for (vndb_id, name) in filtered_vns[:25]
            ]
    except Exception:
        # Return empty list if autocomplete fails
        return []


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
        # Get member from namespace
        member = getattr(interaction.namespace, "member", None)
        if not member:
            return []

        results = await interaction.client.GET(
            DatabaseQueries.USER_LOGS_AUTOCOMPLETE, 
            (member.id,)
        )
        
        if not results:
            return []

        return [
            discord.app_commands.Choice(
                name=f"{vndb_id or 'No VNDB ID'} | {reward_month} - ({reward_reason} - {points}点)",
                value=log_id,
            )
            for log_id, vndb_id, reward_month, reward_reason, points in results[:25]
        ]
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


# Rating choices for consistent use across commands
RATING_CHOICES = [
    discord.app_commands.Choice(name="⭐ 1 - Terrible", value=1),
    discord.app_commands.Choice(name="⭐⭐ 2 - Bad", value=2),
    discord.app_commands.Choice(name="⭐⭐⭐ 3 - Average", value=3),
    discord.app_commands.Choice(name="⭐⭐⭐⭐ 4 - Good", value=4),
    discord.app_commands.Choice(name="⭐⭐⭐⭐⭐ 5 - Masterpiece", value=5),
]