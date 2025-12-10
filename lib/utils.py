"""
Shared utilities and constants for the VN Club Bot.
"""

import os
import re
import discord
import logging
from typing import Optional, Union, List, Tuple, Any
from datetime import datetime

_log = logging.getLogger(__name__)

# ==================== CONSTANTS ====================

# Database limits and formatting
MAX_COMMENT_LENGTH = 1000
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELD = 1024
MAX_DISCORD_MESSAGE = 2000
EMBED_DESCRIPTION_BUFFER = 100

# Points and rating constants
MIN_RATING = 1
MAX_RATING = 5
DEFAULT_MONTHLY_POINTS = 10
NON_MONTHLY_MULTIPLIER = 0.6

# Pagination defaults
DEFAULT_PER_PAGE = 10
DEFAULT_TIMEOUT = 300

# User permission constants
ALLOWED_USER_IDS = [
    int(user_id) for user_id in os.getenv("VN_MANAGER_USER_IDS", "").split(",") 
    if user_id.strip()
]

ALLOWED_ROLE_IDS = [
    int(role_id) for role_id in os.getenv("VN_MANAGER_ROLE_IDS", "").split(",")
    if role_id.strip()
]

# ==================== UTILITY FUNCTIONS ====================

def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate text to specified length, adding suffix if truncated.
    
    Args:
        text: Text to truncate
        max_length: Maximum length including suffix
        suffix: Suffix to add if text is truncated
        
    Returns:
        Truncated text with suffix if needed
    """
    if not text or len(text) <= max_length:
        return text or ""
    return text[:max_length - len(suffix)] + suffix


def get_current_month() -> str:
    """Get current month in YYYY-MM format."""
    return discord.utils.utcnow().strftime("%Y-%m")


def validate_month_format(month: str) -> bool:
    """Validate month format (YYYY-MM)."""
    if not month or len(month) != 7:
        return False
    try:
        datetime.strptime(month, "%Y-%m")
        return True
    except ValueError:
        return False


def user_has_permission(user_id: int, role_ids: List[int]) -> bool:
    """
    Check if user has permission based on user ID or roles.
    
    Args:
        user_id: Discord user ID
        role_ids: List of user's role IDs
        
    Returns:
        True if user has permission
    """
    return (user_id in ALLOWED_USER_IDS or 
            any(role_id in ALLOWED_ROLE_IDS for role_id in role_ids))


def is_month_in_range(current_month: str, start_month: str, end_month: str) -> bool:
    """Check if current month is within the specified range."""
    return start_month <= current_month <= end_month


def calculate_non_monthly_points(monthly_points: int) -> int:
    """Calculate non-monthly points from monthly points."""
    return max(1, int(monthly_points * NON_MONTHLY_MULTIPLIER))


def safe_int_conversion(value: Any, default: int = 0) -> int:
    """Safely convert value to int with fallback."""
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def format_points_display(current_points: int, new_points: int) -> str:
    """Format points display for embeds."""
    return f"**{current_points:,}** ➔ **{new_points:,}**"


def format_rating_display(rating: int) -> str:
    """Format rating display with stars."""
    stars = "⭐" * rating
    return f"**{rating}/5** {stars}"


def create_vndb_link(vndb_id: str) -> str:
    """Create VNDB link from ID."""
    return f"https://vndb.org/{vndb_id}"


def split_text_for_discord(text: str, max_length: int = MAX_DISCORD_MESSAGE) -> List[str]:
    """Split text into chunks that fit Discord's message limits."""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        
        # Find a good break point (newline, space, etc.)
        break_point = max_length
        for delimiter in ['\n\n', '\n', ' ']:
            last_delim = text.rfind(delimiter, 0, max_length)
            if last_delim != -1:
                break_point = last_delim + len(delimiter)
                break
        
        chunks.append(text[:break_point].rstrip())
        text = text[break_point:].lstrip()
    
    return chunks


# ==================== VN INPUT RESOLUTION ====================


async def resolve_vn_from_input(raw_value: str) -> str | None:
    """
    Resolve a VN ID from various input formats.

    Handles:
    - Autocomplete value format: ${vndb|v11:jp}
    - Autocomplete display format (user clicked back on field): "Title — YYYY-MM-DD • rating/10"
    - Raw VNDB ID: v11 or 11

    Returns:
        VNDB ID string (e.g., "v11") or None if not found
    """
    # Import here to avoid circular imports
    from lib.vndb_search import parse_autocomplete_value, search_visual_novel

    if not raw_value:
        return None

    raw_value = raw_value.strip()

    # Try to parse as autocomplete value format first
    parsed = parse_autocomplete_value(raw_value)
    if parsed:
        vndb_id = parsed[0]  # (item_id, field, source)
        if vndb_id and not vndb_id.startswith("v"):
            vndb_id = f"v{vndb_id}"
        return vndb_id

    # Check if this looks like an autocomplete display value that Discord sent
    # Format: "Title — YYYY-MM-DD • rating/10 [vXXXXX]"

    # First try to extract VN ID from [vXXXXX] pattern (most reliable)
    vn_id_match = re.search(r'\[v(\d+)\]', raw_value)
    if vn_id_match:
        vndb_id = f"v{vn_id_match.group(1)}"
        _log.info(f"Recovered VN ID from display format: {vndb_id}")
        return vndb_id

    # Fall back to title search for legacy autocomplete values without [vXXXXX]
    has_em_dash = " — " in raw_value
    has_badge_chars = "•" in raw_value or "/" in raw_value
    has_date_pattern = bool(re.search(r'\d{4}-\d{2}-\d{2}', raw_value))
    if has_em_dash and (has_badge_chars or has_date_pattern):
        # Extract the title part before the " — " separator
        title_part = raw_value.split(" — ")[0].strip()
        if title_part:
            try:
                # Search VNDB for this exact title
                search_results = await search_visual_novel(title_part, limit=5)
                if search_results:
                    # Use the first result (best match)
                    first_match = search_results[0]
                    vndb_id = first_match.get("id")
                    if vndb_id:
                        _log.info(f"Recovered VN from autocomplete display format: {title_part} -> {vndb_id}")
                        if not vndb_id.startswith("v"):
                            vndb_id = f"v{vndb_id}"
                        return vndb_id
            except Exception as e:
                _log.warning(f"Failed to recover VN from display format: {e}")

    # Treat as raw VNDB ID
    vndb_id = raw_value
    if vndb_id and not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"
    return vndb_id


# ==================== ERROR HANDLING UTILITIES ====================

class BotError(Exception):
    """Base exception for bot-related errors."""
    def __init__(self, message: str, user_message: str = None):
        super().__init__(message)
        self.user_message = user_message or message


class ValidationError(BotError):
    """Exception for validation failures."""
    pass


class DatabaseError(BotError):
    """Exception for database-related errors."""
    pass


async def handle_command_error(
    interaction: discord.Interaction, 
    error: Exception, 
    custom_message: str = None
) -> None:
    """
    Centralized error handling for commands.
    
    Args:
        interaction: Discord interaction
        error: Exception that occurred
        custom_message: Custom error message to display
    """
    _log.error(f"Error in command {interaction.command.name if interaction.command else 'unknown'}: {error}")
    
    if isinstance(error, BotError):
        message = error.user_message
    else:
        message = custom_message or "An unexpected error occurred. Please try again later."
    
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
    except Exception as e:
        _log.error(f"Failed to send error message: {e}")


# ==================== VALIDATION HELPERS ====================

async def validate_user_permission(interaction: discord.Interaction, custom_message: str = None) -> bool:
    """
    Validate if user has permission to use management commands.

    Args:
        interaction: Discord interaction
        custom_message: Custom error message to show user if validation fails

    Returns:
        True if user has permission

    Raises:
        ValidationError: If user lacks permission
    """
    role_ids = [role.id for role in interaction.user.roles] if hasattr(interaction.user, 'roles') else []

    if not user_has_permission(interaction.user.id, role_ids):
        raise ValidationError(
            f"User {interaction.user.id} lacks permission",
            custom_message or "You are not authorized to use this command."
        )
    return True


async def validate_month_input(interaction: discord.Interaction, month: str = None) -> str:
    """
    Validate and return month string.
    
    Args:
        interaction: Discord interaction
        month: Month string to validate (optional)
        
    Returns:
        Validated month string
        
    Raises:
        ValidationError: If month format is invalid
    """
    if month is None:
        return get_current_month()
    
    if not validate_month_format(month):
        raise ValidationError(
            f"Invalid month format: {month}",
            "Invalid month format. Please use YYYY-MM format."
        )
    
    return month


async def validate_rating_input(rating: int) -> int:
    """
    Validate rating input.
    
    Args:
        rating: Rating to validate
        
    Returns:
        Validated rating
        
    Raises:
        ValidationError: If rating is invalid
    """
    if not rating or rating < MIN_RATING or rating > MAX_RATING:
        raise ValidationError(
            f"Invalid rating: {rating}",
            f"Please provide a valid rating between {MIN_RATING} and {MAX_RATING}."
        )
    return rating


async def validate_comment_length(comment: str) -> str:
    """
    Validate comment length.
    
    Args:
        comment: Comment to validate
        
    Returns:
        Validated comment
        
    Raises:
        ValidationError: If comment is too long
    """
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ValidationError(
            f"Comment too long: {len(comment)} characters",
            f"Your comment is too long ({len(comment)} characters). "
            f"Please keep comments under {MAX_COMMENT_LENGTH} characters. "
            f"Your current comment exceeds the limit by {len(comment) - MAX_COMMENT_LENGTH} characters."
        )
    return comment


# ==================== EMBED UTILITIES ====================

def create_base_embed(
    title: str,
    description: str = None,
    color: discord.Color = discord.Color.blue(),
    author_name: str = None,
    author_icon: str = None
) -> discord.Embed:
    """
    Create a base embed with common styling.
    
    Args:
        title: Embed title
        description: Embed description (optional)
        color: Embed color
        author_name: Author name (optional)
        author_icon: Author icon URL (optional)
        
    Returns:
        Configured discord.Embed
    """
    embed = discord.Embed(title=title, color=color)
    
    if description:
        embed.description = truncate_text(description, MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER)
    
    if author_name:
        embed.set_author(name=author_name, icon_url=author_icon)
    
    return embed


def add_pagination_footer(embed: discord.Embed, current_page: int, max_pages: int, total_items: int) -> None:
    """Add pagination information to embed footer."""
    embed.set_footer(text=f"Page {current_page + 1}/{max_pages} • {total_items:,} total items")


# ==================== DATABASE UTILITIES ====================

class DatabaseQueries:
    """Centralized database queries."""
    
    # Reading logs queries
    CREATE_READING_LOGS_TABLE = """
    CREATE TABLE IF NOT EXISTS reading_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        vndb_id TEXT,
        user_rating INTEGER,
        reward_reason TEXT NOT NULL,
        reward_month TEXT NOT NULL,
        points INTEGER NOT NULL,
        comment TEXT,
        logged_in_guild INTEGER
    );"""
    
    ADD_READING_LOG = """
    INSERT INTO reading_logs (user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """
    
    GET_USER_VN_LOG = """
    SELECT * FROM reading_logs WHERE user_id = ? AND vndb_id = ?;
    """
    
    GET_LOG_BY_ID = """
    SELECT user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild
    FROM reading_logs WHERE log_id = ?;
    """
    
    GET_USER_TOTAL_POINTS = """
    SELECT SUM(points) FROM reading_logs WHERE user_id = ?;
    """
    
    GET_USER_LOGS = """
    SELECT log_id, user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild
    FROM reading_logs WHERE user_id = ? ORDER BY reward_month DESC, log_id DESC;
    """
    
    GET_ALL_LOGS = """
    SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild 
    FROM reading_logs ORDER BY reward_month DESC;
    """
    
    GET_LOGS_BY_MONTH = """
    SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild 
    FROM reading_logs WHERE reward_month = ? ORDER BY reward_month DESC;
    """
    
    GET_LOGS_BY_SERVER = """
    SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild 
    FROM reading_logs WHERE logged_in_guild = ? ORDER BY reward_month DESC;
    """
    
    GET_LOGS_BY_MONTH_AND_SERVER = """
    SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild 
    FROM reading_logs WHERE reward_month = ? AND logged_in_guild = ? ORDER BY reward_month DESC;
    """
    
    REWARD_USER_POINTS = """
    INSERT INTO reading_logs (user_id, reward_reason, reward_month, points, logged_in_guild)
    VALUES (?, ?, ?, ?, ?);
    """
    
    DELETE_LOG_BY_ID = """
    DELETE FROM reading_logs WHERE log_id = ?;
    """

    UPDATE_LOG_COMMENT_RATING = """
    UPDATE reading_logs SET comment = ?, user_rating = ? WHERE log_id = ?;
    """

    GET_USER_RATINGS = """
    SELECT user_id, vndb_id, user_rating, comment FROM reading_logs WHERE user_id = ? AND vndb_id = ?;
    """
    
    GET_ALL_VN_RATINGS = """
    SELECT user_id, user_rating, comment FROM reading_logs 
    WHERE vndb_id = ? AND user_rating IS NOT NULL 
    ORDER BY user_rating DESC, user_id;
    """
    
    GET_USER_AVERAGE_RATING = """
    SELECT AVG(CAST(user_rating AS REAL)) as avg_rating, COUNT(user_rating) as rating_count
    FROM reading_logs 
    WHERE user_id = ? AND user_rating IS NOT NULL;
    """
    
    GET_DISTINCT_MONTHS = """
    SELECT DISTINCT reward_month FROM reading_logs ORDER BY reward_month DESC;
    """
    
    GET_DISTINCT_SERVERS = """
    SELECT DISTINCT logged_in_guild FROM reading_logs WHERE logged_in_guild IS NOT NULL;
    """
    
    # VN titles queries
    CREATE_VN_TITLES_TABLE = """
    CREATE TABLE IF NOT EXISTS vn_titles (
        vndb_id TEXT PRIMARY KEY,
        start_month TEXT NOT NULL,
        end_month TEXT NOT NULL,
        is_monthly_points INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );"""
    
    ADD_VN_TITLE = """
    INSERT INTO vn_titles (vndb_id, start_month, end_month, is_monthly_points)
    VALUES (?, ?, ?, ?);
    """
    
    GET_VN_TITLE = """
    SELECT vndb_id, start_month, end_month, is_monthly_points FROM vn_titles WHERE vndb_id = ?;
    """
    
    GET_ALL_VN_TITLES = """
    SELECT * FROM vn_titles ORDER BY start_month DESC;
    """
    
    GET_CURRENT_MONTHLY_VNS = """
    SELECT * FROM vn_titles WHERE start_month <= ? AND end_month >= ? ORDER BY start_month DESC;
    """
    
    DELETE_VN_TITLE = """
    DELETE FROM vn_titles WHERE vndb_id = ?;
    """
    
    # Autocomplete queries
    VN_AUTOCOMPLETE = """
    SELECT vn.vndb_id, vndb.title_ja FROM vn_titles vn 
    INNER JOIN vndb_cache vndb ON vndb.vndb_id = vn.vndb_id
    """
    
    USER_LOGS_AUTOCOMPLETE = """
    SELECT rl.log_id, rl.vndb_id, rl.reward_month, rl.reward_reason, rl.points,
           COALESCE(vc.title_ja, vc.title_en) as vn_title
    FROM reading_logs rl
    LEFT JOIN vndb_cache vc ON vc.vndb_id = rl.vndb_id OR vc.vndb_id = 'v' || rl.vndb_id
    WHERE rl.user_id = ? ORDER BY rl.log_id DESC;
    """
    
    # Statistics queries
    GET_USER_STATS = """
    SELECT 
        COUNT(*) as total_entries,
        SUM(points) as total_points,
        COUNT(CASE WHEN reward_reason = 'As Monthly VN' THEN 1 END) as monthly_entries,
        COUNT(CASE WHEN vndb_id IS NOT NULL THEN 1 END) as vn_entries
    FROM reading_logs 
    WHERE user_id = ?;
    """
    
    GET_USER_MOST_ACTIVE_SERVER = """
    SELECT logged_in_guild, COUNT(*) as entry_count
    FROM reading_logs 
    WHERE user_id = ? AND logged_in_guild IS NOT NULL
    GROUP BY logged_in_guild 
    ORDER BY entry_count DESC 
    LIMIT 1;
    """
    
    GET_USER_RECENT_ACTIVITY = """
    SELECT reward_month, COUNT(*) as monthly_count
    FROM reading_logs 
    WHERE user_id = ?
    GROUP BY reward_month 
    ORDER BY reward_month DESC 
    LIMIT 6;
    """