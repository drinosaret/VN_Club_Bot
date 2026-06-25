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
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELD = 1024
MAX_DISCORD_MESSAGE = 2000
EMBED_DESCRIPTION_BUFFER = 100

# Points and rating constants
MIN_RATING = 1
MAX_RATING = 5
DEFAULT_MONTHLY_POINTS = 10
NON_MONTHLY_MULTIPLIER = 0.6

# Pool entry kinds for vn_titles.status. Driven by app_commands.Choice on
# /manage_pool, validated at the app layer (no DB-level CHECK so the column
# stays easy to ALTER for future kinds).
POOL_STATUSES = ("monthly", "seasonal", "special")
DEFAULT_POOL_STATUS = "monthly"

# Pagination defaults
DEFAULT_PER_PAGE = 10
DEFAULT_TIMEOUT = 300

# User permission constants
#
# Two-tier model:
#   AUTHORIZED_USERS — bot operators (e.g. the host). Loaded from env.
#                      Global scope: unconditional bypass for both
#                      `validate_user_permission` (manager checks) and
#                      `require_same_guild` (cross-guild row mutations).
#                      Also gates `/manage_managers` itself — no other
#                      principal can invoke it.
#
#   guild_managers (DB table) — per-guild VN-manager principals (users
#                      or roles). Managed via `/manage_managers add`/
#                      `remove`/`list`. Looked up by
#                      `validate_user_permission` for every admin
#                      command. Replaces the old global env lists
#                      `VN_MANAGER_USER_IDS` / `VN_MANAGER_ROLE_IDS`,
#                      which are no longer read.
AUTHORIZED_USER_IDS = [
    int(user_id) for user_id in os.getenv("AUTHORIZED_USERS", "").split(",")
    if user_id.strip()
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


# Anime-season convention (Winter Jan–Mar, Spring Apr–Jun, Summer Jul–Sep,
# Fall Oct–Dec). Whole calendar months so seasons map cleanly onto the
# YYYY-MM strings stored in reading_logs.reward_month.
ANIME_SEASONS = {
    "winter": (1, 2, 3),
    "spring": (4, 5, 6),
    "summer": (7, 8, 9),
    "fall":   (10, 11, 12),
}


def season_to_months(season: str, year: int) -> List[str]:
    """Return the three YYYY-MM strings that make up `season` in `year`.

    >>> season_to_months("spring", 2026)
    ['2026-04', '2026-05', '2026-06']
    """
    months = ANIME_SEASONS[season.lower()]
    return [f"{year:04d}-{m:02d}" for m in months]


def current_anime_season() -> tuple[str, int]:
    """(season_name, year) for today — used to default `/season_overview`."""
    now = datetime.now()
    for name, months in ANIME_SEASONS.items():
        if now.month in months:
            return name, now.year
    return "winter", now.year  # unreachable; satisfies type checker


# Season ordering used by season_index. Calendar order — winter (Q1) is the
# first season of a given year, fall (Q4) is the last.
ANIME_SEASON_ORDER = ("winter", "spring", "summer", "fall")


def month_to_season_name(month: int) -> str:
    """Map a 1-12 calendar month to its anime-season name."""
    for name, months in ANIME_SEASONS.items():
        if month in months:
            return name
    raise ValueError(f"month {month!r} doesn't map to any season")


def season_index(year: int, season_name: str) -> int:
    """Linear ordinal of a season — ``year * 4 + slot`` where slot ∈
    {0:winter, 1:spring, 2:summer, 3:fall}. Used for season-number
    arithmetic (subtract two indices to get a count of seasons between
    them)."""
    return year * 4 + ANIME_SEASON_ORDER.index(season_name.lower())


def prev_season(year: int, season_name: str) -> Tuple[int, str]:
    """Step back one anime-season. ``winter Y → fall Y-1``."""
    idx = ANIME_SEASON_ORDER.index(season_name.lower())
    if idx == 0:
        return year - 1, "fall"
    return year, ANIME_SEASON_ORDER[idx - 1]


def next_season(year: int, season_name: str) -> Tuple[int, str]:
    """Step forward one anime-season. ``fall Y → winter Y+1``."""
    idx = ANIME_SEASON_ORDER.index(season_name.lower())
    if idx == 3:
        return year + 1, "winter"
    return year, ANIME_SEASON_ORDER[idx + 1]


async def get_season_number(bot, year: int, season_name: str) -> Optional[int]:
    """1-based count of how many anime-seasons have started since the
    earliest reading_log entry, inclusive (so the season containing the
    first-ever log is "Season 1"). Returns ``None`` when there are no logs
    yet, or the requested season is earlier than the first log's season.

    Uncached: SQLite already has ``idx_reading_logs_reward_month``, so
    ``MIN(reward_month)`` is a fast index probe (~µs). Caching would only
    matter if /profile etc. start running tens of thousands of times per
    second, which they don't.
    """
    row = await bot.GET_ONE(
        "SELECT MIN(reward_month) FROM reading_logs WHERE reward_month IS NOT NULL"
    )
    if not row or not row[0]:
        return None
    yyyy_mm = str(row[0])
    try:
        y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    except (ValueError, IndexError):
        return None
    earliest = season_index(y, month_to_season_name(m))
    target = season_index(year, season_name)
    if target < earliest:
        return None
    return target - earliest + 1


async def format_season_label(bot, year: int, season_name: str) -> str:
    """Human-friendly label: ``"Spring 2026 · Season 4"``. Falls back to
    just the season + year (no Season N suffix) when the bot has no logs
    yet, or the asked-for season predates the first log.
    """
    base = f"{season_name.capitalize()} {year}"
    n = await get_season_number(bot, year, season_name)
    if n is None or n < 1:
        return base
    return f"{base} · Season {n}"


async def format_season_label_from_yyyy_mm(bot, yyyy_mm: str) -> str:
    """Convenience: derive (year, season_name) from a YYYY-MM start month
    and call ``format_season_label``. Used by helpers that already have a
    target_month string handy (cycle rows, vn_titles rows)."""
    try:
        y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    except (ValueError, IndexError, TypeError):
        return str(yyyy_mm)
    return await format_season_label(bot, y, month_to_season_name(m))


def validate_month_format(month: str) -> bool:
    """Validate month format (YYYY-MM)."""
    if not month or len(month) != 7:
        return False
    try:
        datetime.strptime(month, "%Y-%m")
        return True
    except ValueError:
        return False


def is_month_in_range(current_month: str, start_month: str, end_month: str) -> bool:
    """Check if current month is within the specified range."""
    return start_month <= current_month <= end_month


async def get_single_monthly_vn(bot, vndb_id: str, guild_id: int | None = None):
    """Look up a vn_titles row for this VN that's *currently active*.

    "Active" = current month falls within [start_month, end_month]. When
    multiple rows exist for the same VN (admins can register the same VN
    for different periods), prefer the active one. Falls back to the
    most-recent row when nothing is active so the caller can still see
    is_monthly_points and decide whether to credit the bonus via its own
    range check.

    Returns a 4-tuple (vndb_id, start_month, end_month, is_monthly_points)
    matching ``DatabaseQueries.GET_VN_TITLE`` shape.
    """
    if guild_id is not None:
        rows = await bot.GET(DatabaseQueries.GET_VN_TITLE_FOR_GUILD, (vndb_id, guild_id))
    else:
        rows = await bot.GET(DatabaseQueries.GET_VN_TITLE, (vndb_id,))
    if not rows:
        return None
    current = get_current_month()
    for row in rows:
        # row: (vndb_id, start_month, end_month, is_monthly_points)
        if row[1] <= current <= row[2]:
            return row
    return rows[0]


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

    # Defensive: an autocomplete-template substring like ${vndb|v39169:jp}
    # can show up embedded in a garbled input string (e.g. Discord client
    # schema-cache hiccups). If we find one anywhere in the input, prefer
    # that over the looser fallbacks below.
    embedded = re.search(r'\$\{(?:[^|}]+\|)?v?(\d+):[^}]+\}', raw_value)
    if embedded:
        return f"v{embedded.group(1)}"

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

    # Treat as raw VNDB ID — but only if it actually looks like one. The
    # earlier branches handled every "structured" input we know about;
    # anything that gets here should be either `v<digits>` or bare digits.
    # Without this validation the fallback would rubber-stamp arbitrary
    # text and send it to the VNDB API as a malformed ID.
    raw_id_match = re.fullmatch(r'v?(\d+)', raw_value)
    if raw_id_match:
        return f"v{raw_id_match.group(1)}"

    _log.warning("resolve_vn_from_input: unrecognized input %r", raw_value)
    return None


def require_same_guild(
    interaction,
    row_guild_id: Optional[int],
    entity_name: str = "entry",
) -> None:
    """Reject a mutation when the target row belongs to a different guild.

    Used by every admin command that fetches a row by primary-key id and
    then mutates it (``/manage_pool`` remove/edit, ``/log_undo``,
    ``/log_edit``). Without this guard, a per-server manager in guild A
    can wipe or rewrite guild B's data by guessing the id, because the
    global id space pairs with global env-loaded admin lists.

    Two-tier exemption:
      * If the requester is in ``AUTHORIZED_USERS`` (bot operators),
        cross-guild access is allowed and this check no-ops. Hosts
        sometimes need to fix data across servers.
      * If the row's guild_id is NULL (legacy/global row inherited from
        the pre-overhaul schema), it's in-scope for any guild — there's
        no scoping intent to enforce.

    Otherwise the row's guild_id must match the interaction's guild.
    """
    if interaction.user.id in AUTHORIZED_USER_IDS:
        return  # bot operator — global scope
    if interaction.guild_id is None:
        # Reached when the command runs in a DM. The
        # @app_commands.guild_only() decorator usually catches this
        # first; defensive double-check.
        raise ValidationError(
            f"{entity_name} requires guild context",
            "This command must be run inside a server.",
        )
    if row_guild_id is None:
        return  # legacy/global row — accessible from anywhere
    if row_guild_id != interaction.guild_id:
        raise ValidationError(
            f"{entity_name} cross-guild access denied "
            f"(row_guild={row_guild_id}, requester_guild={interaction.guild_id})",
            f"That {entity_name} doesn't belong to this server.",
        )


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
    _log.error(
        "Error in command %s",
        interaction.command.name if interaction.command else "unknown",
        exc_info=error,
    )

    if isinstance(error, BotError):
        message = error.user_message
    else:
        message = custom_message or "An unexpected error occurred. Please try again later."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
    except Exception:
        _log.exception("Failed to send error message")


# ==================== VALIDATION HELPERS ====================

async def validate_user_permission(interaction: discord.Interaction, custom_message: str = None) -> bool:
    """
    Validate if the requester is a VN manager for the *current* guild.

    Permission model:
      1. ``AUTHORIZED_USERS`` (bot operators, env): unconditional bypass.
         Used by the host to grant manager rights via ``/manage_managers``
         and to administer any guild from anywhere.
      2. ``guild_managers`` table: per-(guild, principal) grants. A row
         matching either the requester's user_id OR any of the
         requester's role IDs in the current guild passes the check.
      3. Everyone else: ValidationError.

    The pre-overhaul global ``VN_MANAGER_USER_IDS`` /
    ``VN_MANAGER_ROLE_IDS`` env lists are no longer read. The host
    seeds each guild's manager list manually via ``/manage_managers``
    after deploy — see the plan for the rationale (one-off
    re-grant beats a fragile migration heuristic).

    Args:
        interaction: Discord interaction
        custom_message: Custom error message to show user if validation fails

    Returns:
        True if user has permission

    Raises:
        ValidationError: If user lacks permission
    """
    # Tier 1: global bypass for bot operators.
    if interaction.user.id in AUTHORIZED_USER_IDS:
        return True

    # DM context — no guild to scope against; manager perms are
    # inherently per-guild so this can't pass.
    if interaction.guild_id is None:
        command_name = getattr(getattr(interaction, "command", None), "name", "<no-command>")
        _log.info(
            "auth_denied: user=%s command=%s reason=dm-context",
            interaction.user.id, command_name,
        )
        raise ValidationError(
            f"User {interaction.user.id} attempted manager command in DM",
            custom_message or "This command must be run inside a server.",
        )

    bot = interaction.client

    # Tier 2a: per-guild user grant.
    user_hit = await bot.GET_ONE(
        "SELECT 1 FROM guild_managers WHERE guild_id = ? "
        "AND principal_type = 'user' AND principal_id = ? LIMIT 1",
        (interaction.guild_id, interaction.user.id),
    )
    if user_hit:
        return True

    # Tier 2b: per-guild role grant. interaction.user is discord.Member
    # under @guild_only() so .roles is always present.
    role_ids = [r.id for r in interaction.user.roles] if hasattr(interaction.user, "roles") else []
    if role_ids:
        placeholders = ",".join("?" * len(role_ids))
        role_hit = await bot.GET_ONE(
            f"SELECT 1 FROM guild_managers WHERE guild_id = ? "
            f"AND principal_type = 'role' AND principal_id IN ({placeholders}) "
            f"LIMIT 1",
            (interaction.guild_id, *role_ids),
        )
        if role_hit:
            return True

    command_name = getattr(getattr(interaction, "command", None), "name", "<no-command>")
    _log.info(
        "auth_denied: user=%s guild=%s command=%s reason=not-manager",
        interaction.user.id, interaction.guild_id, command_name,
    )
    raise ValidationError(
        f"User {interaction.user.id} lacks manager permission in guild {interaction.guild_id}",
        custom_message or "You don't have permission to use this command in this server.",
    )


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

# Shared SELECT prefix for the three "full row" queries against vn_titles.
# Keeping this as a module-level constant means the column list + winner_flag
# subquery only exist in one place, so adding/renaming columns later doesn't
# require touching three identical query bodies.
#
# winner_flag note: this returns the currently *leading* nomination (most
# votes) for an open cycle, or the actual winner for a closed cycle (since
# _close_voting locks the row by flipping status). Ties broken arbitrarily
# by LIMIT 1 — acceptable for display, not for correctness-critical logic.
_VN_TITLES_FULL_SELECT = """
SELECT vt.id, vt.vndb_id, vt.guild_id, vt.start_month, vt.end_month,
       vt.is_monthly_points, vt.created_at, vc.title_ja, vc.title_en,
       vt.status, vt.cycle_id, vt.nominator_user_id, vt.title_cache,
       c.phase, c.kind, c.target_month, c.target_end_month,
       CASE WHEN vt.cycle_id IS NOT NULL AND vt.id = (
           SELECT v.nomination_id FROM vn_votes v
           WHERE v.cycle_id = vt.cycle_id
           GROUP BY v.nomination_id
           ORDER BY COUNT(*) DESC
           LIMIT 1
       ) THEN 1 ELSE 0 END AS winner_flag
FROM vn_titles vt
LEFT JOIN vndb_cache vc ON vc.vndb_id = vt.vndb_id
LEFT JOIN vn_cycles c ON c.id = vt.cycle_id
"""

# Stable status-bucket ordering for /pool. Picks first (monthly → seasonal →
# special), then nominations. Explicit CASE so adding new statuses doesn't
# silently reorder via alphabetical fallback.
_POOL_STATUS_ORDER = (
    "CASE vt.status "
    "WHEN 'monthly' THEN 0 "
    "WHEN 'seasonal' THEN 1 "
    "WHEN 'special' THEN 2 "
    "WHEN 'nominated' THEN 3 "
    "ELSE 4 END"
)


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
        logged_in_guild INTEGER,
        completed_at TIMESTAMP
    );"""

    # Indexes for the reading_logs hot paths. /profile, /club_stats,
    # /leaderboard, /logs and the MIN(reward_month) season-number lookup
    # all filter or sort on these columns. The migration's
    # _add_reading_logs_indexes covers the legacy-upgrade path, but it
    # early-returns when the table doesn't yet exist (fresh install)
    # because migrations run before any cog. So the cog has to create
    # them itself on fresh deploys; both paths converge to the same
    # five indexes via CREATE INDEX IF NOT EXISTS.
    #
    # The partial unique index on (user_id, vndb_id, reward_month) is
    # what makes the OR IGNORE pattern in ADD_READING_LOG_OR_IGNORE
    # race-safe — without it, two concurrent /finish for the same
    # (user, vn, month) both pass the check-then-insert dedup. The
    # partial WHERE excludes admin reward_points rows (vndb_id NULL)
    # from the constraint so multiple reward-only logs in one month
    # stay legal.
    CREATE_READING_LOGS_INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_user_id ON reading_logs (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_reward_month ON reading_logs (reward_month)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_logged_in_guild ON reading_logs (logged_in_guild)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_user_month ON reading_logs (user_id, reward_month)",
        "CREATE INDEX IF NOT EXISTS idx_reading_logs_vndb_id ON reading_logs (vndb_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reading_logs_user_vn_month "
        "ON reading_logs (user_id, vndb_id, reward_month) WHERE vndb_id IS NOT NULL",
    )

    ADD_READING_LOG = """
    INSERT INTO reading_logs (user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild, completed_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
    """

    # Race-safe INSERT for the /finish and /manage_log paths. Pairs with
    # the partial unique index on (user_id, vndb_id, reward_month) above:
    # two concurrent inserts for the same key resolve to one row
    # silently. RUN_RETURNING_ID's lastrowid is 0 on the ignored case
    # (no row inserted), which the caller checks to surface an
    # "already logged" message instead of pretending the insert
    # happened.
    ADD_READING_LOG_OR_IGNORE = """
    INSERT OR IGNORE INTO reading_logs (user_id, vndb_id, user_rating, reward_reason, reward_month, points, comment, logged_in_guild, completed_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
    """
    
    GET_USER_VN_LOG = """
    SELECT * FROM reading_logs WHERE user_id = ? AND vndb_id = ?;
    """

    # Per-month dedup check used by /finish and /manage_log. A user can
    # log the same VN multiple times across different months (e.g. re-reads),
    # but not twice in the same reward_month — that pattern was historically
    # the path to accidental double-rewards.
    # Bind: (user_id, vndb_id, reward_month).
    GET_USER_VN_LOG_FOR_MONTH = """
    SELECT 1 FROM reading_logs
    WHERE user_id = ? AND vndb_id = ? AND reward_month = ?
    LIMIT 1;
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

    # Season filters span 3 months; see season_to_months() for the YYYY-MM list.
    GET_LOGS_BY_SEASON = """
    SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild
    FROM reading_logs WHERE reward_month IN (?, ?, ?) ORDER BY reward_month DESC;
    """

    GET_LOGS_BY_SEASON_AND_SERVER = """
    SELECT user_id, vndb_id, reward_reason, reward_month, points, comment, logged_in_guild
    FROM reading_logs WHERE reward_month IN (?, ?, ?) AND logged_in_guild = ? ORDER BY reward_month DESC;
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
    
    # Returns one row per rating *event* — a user who re-read the VN in a
    # different reward_month appears once per read so consumers can show
    # the rating evolution. reward_month is included so the UI can label
    # re-reads.
    GET_ALL_VN_RATINGS = """
    SELECT user_id, user_rating, comment, reward_month FROM reading_logs
    WHERE vndb_id = ? AND user_rating IS NOT NULL
    ORDER BY user_rating DESC, user_id, reward_month DESC;
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
    # Schema: synthetic id PK, no UNIQUE on (vndb_id, guild_id) — the same VN
    # may legitimately appear multiple times (different start/end months).
    # guild_id IS NULL means the entry applies globally (legacy semantics for
    # rows that pre-date the per-server migration). `status` distinguishes
    # the kind of pool entry — 'monthly' (default), 'seasonal', or 'special'.
    # Migration in lib/migrations.py reshapes existing tables to this form.
    CREATE_VN_TITLES_TABLE = """
    CREATE TABLE IF NOT EXISTS vn_titles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vndb_id TEXT NOT NULL,
        guild_id INTEGER,
        start_month TEXT NOT NULL,
        end_month TEXT NOT NULL,
        is_monthly_points INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'monthly',
        cycle_id INTEGER,
        nominator_user_id INTEGER,
        title_cache TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );"""
    
    ADD_VN_TITLE = """
    INSERT INTO vn_titles (vndb_id, start_month, end_month, is_monthly_points)
    VALUES (?, ?, ?, ?);
    """
    
    GET_VN_TITLE = """
    SELECT vndb_id, start_month, end_month, is_monthly_points, status FROM vn_titles WHERE vndb_id = ?;
    """
    
    # Autocomplete queries
    #
    # Discord caps Choice lists at 25 and the autocomplete request
    # itself has a 3s ack budget — anything above 25 rows is wasted
    # work and large pools without a LIMIT will simply blow the
    # deadline. Filtering + LIMIT 25 at the SQL layer keeps the
    # autocomplete callback fast regardless of pool size.
    VN_AUTOCOMPLETE = """
    SELECT vn.vndb_id, vndb.title_ja
    FROM vn_titles vn
    INNER JOIN vndb_cache vndb ON vndb.vndb_id = vn.vndb_id
    LIMIT 25
    """

    # Same shape as VN_AUTOCOMPLETE but filtered to a single user. The
    # join on vc.vndb_id used to have an OR-branch matching
    # 'v' || rl.vndb_id for legacy rows that stored the id without the
    # 'v' prefix; the bot has been normalizing IDs to v-prefixed for
    # long enough that we can drop the slow branch (it killed the
    # vndb_cache PK index by making the join non-sargable). LIMIT 25
    # mirrors Discord's choice cap.
    USER_LOGS_AUTOCOMPLETE = """
    SELECT rl.log_id, rl.vndb_id, rl.reward_month, rl.reward_reason, rl.points,
           COALESCE(vc.title_ja, vc.title_en) as vn_title
    FROM reading_logs rl
    LEFT JOIN vndb_cache vc ON vc.vndb_id = rl.vndb_id
    WHERE rl.user_id = ?
    ORDER BY rl.log_id DESC
    LIMIT 25;
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
      AND vndb_id IS NOT NULL
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
    LIMIT 12;
    """

    # Most-recent log timestamp for the user, surfaced as "last log YYYY-MM-DD"
    # on the profile card subtitle. Returns NULL if the user has no logs.
    GET_USER_LAST_LOG = """
    SELECT MAX(completed_at) FROM reading_logs WHERE user_id = ?;
    """

    # All distinct log-months for the user (no LIMIT). Used to compute the
    # READING STREAK widget so a long-time member's streak isn't artificially
    # capped at 12 by the chart query above.
    GET_USER_LOG_MONTHS = """
    SELECT DISTINCT reward_month
    FROM reading_logs
    WHERE user_id = ?
    ORDER BY reward_month DESC;
    """

    # ---------------- guild_settings ----------------
    # Per-guild defaults: default_voting_role_id (fallback allowed_role on
    # Open voting) and default_vote_ui (fallback dropdown/buttons choice).
    # Returns an empty result when no row exists for the guild — the cog
    # treats that as "no defaults set".
    GET_GUILD_SETTINGS = """
    SELECT guild_id, default_voting_role_id, default_vote_ui
    FROM guild_settings
    WHERE guild_id = ?;
    """

    # Upsert pattern — INSERT-OR-UPDATE so admins can flip the default
    # without us having to check existence first. Touches updated_at on
    # both insert and update for audit trail.
    UPSERT_DEFAULT_VOTING_ROLE = """
    INSERT INTO guild_settings (guild_id, default_voting_role_id)
    VALUES (?, ?)
    ON CONFLICT(guild_id) DO UPDATE SET
        default_voting_role_id = excluded.default_voting_role_id,
        updated_at = CURRENT_TIMESTAMP;
    """

    # Explicit clear — ensures a row exists with NULL rather than deleting
    # so any future fields on guild_settings stay intact.
    CLEAR_DEFAULT_VOTING_ROLE = """
    INSERT INTO guild_settings (guild_id, default_voting_role_id)
    VALUES (?, NULL)
    ON CONFLICT(guild_id) DO UPDATE SET
        default_voting_role_id = NULL,
        updated_at = CURRENT_TIMESTAMP;
    """

    UPSERT_DEFAULT_VOTE_UI = """
    INSERT INTO guild_settings (guild_id, default_vote_ui)
    VALUES (?, ?)
    ON CONFLICT(guild_id) DO UPDATE SET
        default_vote_ui = excluded.default_vote_ui,
        updated_at = CURRENT_TIMESTAMP;
    """

    # ---------------- guild_managers ----------------
    # Per-guild VN-manager principals (users or roles). Looked up by
    # `validate_user_permission` for every /manage_* command and the
    # admin paths of /log_undo / /log_edit. Managed exclusively by
    # AUTHORIZED_USERS via /manage_managers.

    # Bind: (guild_id, principal_type, principal_id, added_by_user_id).
    # OR IGNORE makes re-adding the same principal a friendly no-op
    # rather than an error.
    INSERT_GUILD_MANAGER = """
    INSERT OR IGNORE INTO guild_managers
        (guild_id, principal_type, principal_id, added_by_user_id)
    VALUES (?, ?, ?, ?);
    """

    # Bind: (guild_id, principal_type, principal_id). Caller checks
    # cursor.rowcount to distinguish "removed" from "wasn't there".
    DELETE_GUILD_MANAGER = """
    DELETE FROM guild_managers
    WHERE guild_id = ? AND principal_type = ? AND principal_id = ?;
    """

    # Bind: (guild_id,). Used by /manage_managers list.
    LIST_GUILD_MANAGERS = """
    SELECT principal_type, principal_id, added_by_user_id, added_at
    FROM guild_managers
    WHERE guild_id = ?
    ORDER BY added_at ASC;
    """

    # ---------------- /club_stats aggregates ----------------
    # Each query is scope-aware: pass (None, None) to skip the guild filter
    # (global mode) or (guild_id, guild_id) to filter to a single server.
    # The `(? IS NULL OR logged_in_guild = ?)` pattern keeps everything in one
    # parameterized statement instead of branching SQL strings in Python.

    # Headline tiles for /club_stats. Total chars used to be here but jiten
    # doesn't index every VN, so the number was always partial — replaced
    # with total_points (the real "club output" the leaderboard is built on).
    CLUB_STATS_TOTALS = """
    SELECT
        COUNT(CASE WHEN vndb_id IS NOT NULL THEN 1 END) AS total_completions,
        COUNT(DISTINCT vndb_id) AS unique_vns,
        COUNT(DISTINCT user_id) AS active_members,
        COALESCE(SUM(points), 0) AS total_points
    FROM reading_logs
    WHERE (? IS NULL OR logged_in_guild = ?);
    """

    CLUB_STATS_TOP_CONTRIBUTORS = """
    SELECT user_id, SUM(points) AS total_points,
           COUNT(CASE WHEN vndb_id IS NOT NULL THEN 1 END) AS completions
    FROM reading_logs
    WHERE (? IS NULL OR logged_in_guild = ?)
    GROUP BY user_id
    ORDER BY total_points DESC
    LIMIT ?;
    """

    CLUB_STATS_RATING_DIST = """
    SELECT user_rating, COUNT(*)
    FROM reading_logs
    WHERE (? IS NULL OR logged_in_guild = ?)
      AND user_rating IS NOT NULL
    GROUP BY user_rating
    ORDER BY user_rating;
    """

    CLUB_STATS_MONTHLY_TREND = """
    SELECT reward_month, COUNT(*)
    FROM reading_logs
    WHERE (? IS NULL OR logged_in_guild = ?)
      AND vndb_id IS NOT NULL
    GROUP BY reward_month
    ORDER BY reward_month DESC
    LIMIT 12;
    """

    # ---------------- Cycle / nomination / voting (overhaul) ----------------

    # `kind` is the cycle's target pool kind ('monthly' or 'seasonal'). A guild
    # can run a monthly and a seasonal cycle at the same time, so the UNIQUE
    # is on (guild_id, target_month, kind) — same start month is fine across
    # different kinds. `target_end_month` is the inclusive end of the active
    # window; for monthly cycles it equals target_month, for seasonal it's
    # the third month of the season.
    CREATE_VN_CYCLES_TABLE = """
    CREATE TABLE IF NOT EXISTS vn_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('nominating','closed_nominating','voting','closed')),
        kind TEXT NOT NULL DEFAULT 'monthly',
        vote_choice_mode TEXT,
        vote_winner_count INTEGER,
        target_month TEXT NOT NULL,
        target_end_month TEXT,
        announcement_channel_id INTEGER,
        announcement_message_id INTEGER,
        opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        closed_at TIMESTAMP,
        closes_at TIMESTAMP,
        vote_ui TEXT,
        allowed_role_id INTEGER
    );"""

    CREATE_VN_VOTES_TABLE = """
    CREATE TABLE IF NOT EXISTS vn_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        nomination_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(cycle_id) REFERENCES vn_cycles(id),
        FOREIGN KEY(nomination_id) REFERENCES vn_titles(id),
        UNIQUE(cycle_id, user_id, nomination_id)
    );"""

    # Cycle CRUD. The SELECT shape across all three queries below is identical
    # so the cog's CYCLE_* index constants stay in sync. `kind` and
    # `target_end_month` are appended at the end of the SELECT list.

    # Direct-to-voting cycle insert. Admins run Open voting directly,
    # which creates a phase='voting' cycle and sweeps the existing
    # nominated VNs into it. The legacy 'nominating' phase value is still
    # accepted by the CHECK constraint for back-compat with old data, just
    # not produced by any new INSERT.
    INSERT_CYCLE = """
    INSERT INTO vn_cycles (guild_id, phase, target_month, announcement_channel_id, kind, target_end_month)
    VALUES (?, 'voting', ?, ?, ?, ?);
    """

    GET_ACTIVE_CYCLE = """
    SELECT id, guild_id, phase, vote_choice_mode, vote_winner_count, target_month,
           announcement_channel_id, announcement_message_id, opened_at, closed_at,
           kind, target_end_month, closes_at, vote_ui, allowed_role_id
    FROM vn_cycles
    WHERE guild_id = ? AND kind = ? AND phase != 'closed'
    ORDER BY opened_at DESC LIMIT 1;
    """

    GET_CYCLE_BY_ID = """
    SELECT id, guild_id, phase, vote_choice_mode, vote_winner_count, target_month,
           announcement_channel_id, announcement_message_id, opened_at, closed_at,
           kind, target_end_month, closes_at, vote_ui, allowed_role_id
    FROM vn_cycles WHERE id = ?;
    """

    SET_CYCLE_PHASE = """
    UPDATE vn_cycles SET phase = ? WHERE id = ?;
    """

    CLOSE_CYCLE = """
    UPDATE vn_cycles SET phase = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = ?;
    """

    SET_VOTING_SETTINGS = """
    UPDATE vn_cycles
    SET vote_choice_mode = ?, vote_winner_count = ?,
        announcement_channel_id = ?, announcement_message_id = ?,
        closes_at = ?, vote_ui = ?, allowed_role_id = ?
    WHERE id = ?;
    """

    LIST_VOTING_CYCLES = """
    SELECT id, guild_id, announcement_channel_id, announcement_message_id,
           vote_choice_mode, vote_winner_count, kind, target_end_month,
           closes_at, vote_ui, allowed_role_id
    FROM vn_cycles WHERE phase = 'voting';
    """

    # Closed cycles that still have an announcement message. Used on boot to
    # re-attach a participants-only view so the Participants button keeps
    # working on closed votes across restarts (LIST_VOTING_CYCLES only covers
    # the full VoteView for cycles still in voting).
    LIST_CLOSED_CYCLES = """
    SELECT id
    FROM vn_cycles
    WHERE phase = 'closed' AND announcement_message_id IS NOT NULL;
    """

    # Cycles in voting phase that have a closes_at in the past — picked up
    # by the background auto-close task. Limit to a sane batch so a long
    # outage doesn't try to close 500 cycles at once on first tick.
    LIST_EXPIRED_VOTING_CYCLES = """
    SELECT id, guild_id, kind
    FROM vn_cycles
    WHERE phase = 'voting'
      AND closes_at IS NOT NULL
      AND closes_at <= CURRENT_TIMESTAMP
    ORDER BY closes_at ASC
    LIMIT 25;
    """

    # Voters for one nomination, used by the Participants button. Includes
    # the user_id (so we can mention) and created_at (so the panel can sort
    # by recency the same way EasyPoll does).
    GET_VOTERS_FOR_NOMINATION = """
    SELECT user_id, created_at FROM vn_votes
    WHERE cycle_id = ? AND nomination_id = ?
    ORDER BY created_at DESC;
    """

    # Every vote in a cycle, joined with the nominee's title — used by the
    # admin "Manage votes" sub-panel so a mod can remove troll/duplicate
    # votes without having to drop into raw SQL. Includes vote.id so the
    # caller can pass it to DELETE_VOTE_BY_ID. Bind: (cycle_id,).
    GET_ALL_VOTES_IN_CYCLE = """
    SELECT v.id, v.user_id, v.nomination_id,
           COALESCE(vt.title_cache, vt.vndb_id) AS title,
           v.created_at
    FROM vn_votes v
    JOIN vn_titles vt ON vt.id = v.nomination_id
    WHERE v.cycle_id = ?
    ORDER BY v.created_at DESC;
    """

    # Votes. INSERT OR IGNORE so concurrent click-races don't surface the
    # UNIQUE(cycle_id, user_id, nomination_id) constraint as a generic error
    # — the second insert just no-ops and we still report success.
    INSERT_VOTE = """
    INSERT OR IGNORE INTO vn_votes (cycle_id, user_id, guild_id, nomination_id)
    VALUES (?, ?, ?, ?);
    """

    DELETE_VOTE_BY_ID = """
    DELETE FROM vn_votes WHERE id = ?;
    """

    DELETE_USER_VOTES_IN_CYCLE = """
    DELETE FROM vn_votes WHERE cycle_id = ? AND user_id = ?;
    """

    GET_USER_VOTES_IN_CYCLE = """
    SELECT id, nomination_id FROM vn_votes WHERE cycle_id = ? AND user_id = ?;
    """

    COUNT_USER_VOTES_IN_CYCLE = """
    SELECT COUNT(*) FROM vn_votes WHERE cycle_id = ? AND user_id = ?;
    """

    # Tally: stable sort — count DESC, then earliest nomination first.
    #
    # The LEFT JOIN matches `v.cycle_id = vt.cycle_id` (not just
    # nomination_id). Without that, votes cast in a *previous* cycle for
    # the same nomination_id leak into the new cycle's tally — when a
    # vote is cancelled / re-run for the same month, the swept nominations
    # carry their id forward but their old vn_votes rows stay tied to the
    # closed cycle. We want only votes belonging to the cycle we're tallying.
    TALLY_VOTES = """
    SELECT vt.id AS nomination_id, vt.vndb_id,
           COALESCE(vt.title_cache, vt.vndb_id) AS title,
           vt.nominator_user_id AS user_id, vt.guild_id, vt.created_at,
           COUNT(v.id) AS votes
    FROM vn_titles vt
    LEFT JOIN vn_votes v ON v.nomination_id = vt.id AND v.cycle_id = vt.cycle_id
    WHERE vt.cycle_id = ? AND vt.status IN ('nominated', 'monthly', 'seasonal')
    GROUP BY vt.id
    ORDER BY votes DESC, vt.id ASC;
    """

    # Per-guild vn_titles queries (legacy ADD_VN_TITLE / GET_CURRENT_MONTHLY_VNS
    # remain for backwards compat — they treat all rows as global and ignore
    # guild_id, which is correct for legacy NULL-guild rows.)
    ADD_VN_TITLE_FOR_GUILD = """
    INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, is_monthly_points, status)
    VALUES (?, ?, ?, ?, ?, ?);
    """

    # Whitelist of vn_titles columns that `/manage_pool action:edit` will
    # change. The cog assembles a `SET col = ?` clause from the subset the
    # admin actually passed. External admin tooling that writes to the same
    # DB shares this field set — keep the contract stable.
    UPDATE_VN_TITLE_EDITABLE_FIELDS = (
        "start_month", "end_month", "is_monthly_points", "status", "guild_id",
    )

    # Guild-scoped existence check + active-pool lookup. Matches a row for THIS
    # guild specifically, plus legacy global rows (guild_id IS NULL).
    # Returns 5 columns: (vndb_id, start_month, end_month, is_monthly_points, status).
    GET_VN_TITLE_FOR_GUILD = """
    SELECT vndb_id, start_month, end_month, is_monthly_points, status
    FROM vn_titles
    WHERE vndb_id = ? AND (guild_id IS NULL OR guild_id = ?);
    """

    # Used by /monthly — only returns 'monthly' status entries that are in
    # period. Other kinds (seasonal/special) appear in /pool but don't show
    # in /monthly. Explicit column list (NOT `SELECT *`) so the row shape is
    # deterministic — fresh installs and migrated installs have different
    # physical column orders for vn_titles, but both have these named columns.
    GET_CURRENT_MONTHLY_VNS_FOR_GUILD = """
    SELECT id, vndb_id, guild_id, start_month, end_month, is_monthly_points, status, created_at
    FROM vn_titles
    WHERE start_month <= ? AND end_month >= ?
      AND status = 'monthly'
      AND (guild_id IS NULL OR guild_id = ?)
    ORDER BY start_month DESC;
    """

    # /seasonal mirror — same shape, filters status='seasonal'.
    GET_CURRENT_SEASONAL_VNS_FOR_GUILD = """
    SELECT id, vndb_id, guild_id, start_month, end_month, is_monthly_points, status, created_at
    FROM vn_titles
    WHERE start_month <= ? AND end_month >= ?
      AND status = 'seasonal'
      AND (guild_id IS NULL OR guild_id = ?)
    ORDER BY start_month DESC;
    """

    # Active pool entries for one VN whose range overlaps a proposed range.
    # Used by /manage_pool action:add to refuse duplicate adds. Excludes
    # 'nominated' since those are votes-in-progress, not picks.
    #
    # Guild-scoping: when proposed_guild_id is non-NULL, match NULL-guild
    # (global) rows OR same-guild rows. When proposed_guild_id is NULL
    # (operator adding a global entry), match EVERY row for the VN, since a
    # global entry conflicts with all guilds. SQLite's `col = NULL` is
    # always NULL, so the `? IS NULL` short-circuit is required.
    #
    # Params: (vndb_id, proposed_guild_id, proposed_guild_id,
    #          proposed_end_month, proposed_start_month).
    GET_OVERLAPPING_POOL_ENTRIES = """
    SELECT id, vndb_id, guild_id, start_month, end_month, status
    FROM vn_titles
    WHERE vndb_id = ?
      AND (? IS NULL OR guild_id IS NULL OR guild_id = ?)
      AND start_month <= ?
      AND end_month >= ?
      AND status IN ('monthly', 'seasonal', 'special')
    ORDER BY start_month;
    """

    DELETE_VN_TITLE_FOR_GUILD = """
    DELETE FROM vn_titles WHERE vndb_id = ? AND (guild_id IS NULL OR guild_id = ?);
    """

    # Pool-entry-by-ID queries — used by /manage_pool action:remove so admins
    # can target a specific entry when multiple exist for the same VN.
    # By-ID lookups are NOT guild-scoped at the SQL layer — pool_id is
    # globally unique. The cog enforces per-guild scope via
    # `require_same_guild` on the returned row before mutating, and
    # AUTHORIZED_USERS intentionally bypass that check so the host can
    # admin any guild's entries by ID. Listing surfaces (/pool, /monthly,
    # /seasonal) stay per-guild via separate queries.
    GET_VN_TITLE_BY_ID = """
    SELECT id, vndb_id, guild_id, start_month, end_month, is_monthly_points, status
    FROM vn_titles
    WHERE id = ?;
    """

    # Bind: (pool_id, observed_guild_id). The observed_guild_id is the
    # row's `guild_id` AS READ by the caller — including NULL for
    # legacy/global rows. SQLite's `IS` operator is NULL-safe equality:
    # `guild_id IS ?` matches when both are NULL, both are the same
    # number, or neither. This closes the TOCTOU between the GET-row
    # and the DELETE — if another process moved the row's guild_id
    # between read and write, the DELETE hits zero rows and the cog
    # tells the admin instead of silently mutating the relocated row.
    DELETE_VN_TITLE_BY_ID = """
    DELETE FROM vn_titles WHERE id = ? AND guild_id IS ?;
    """

    # Full pool entries for a guild, joined with VNDB cache for the title.
    # Used by /pool listing and the ID-aware autocomplete on /manage_pool.
    # Trailing column is `status` so VNListView can show a kind badge.
    GET_POOL_ENTRIES_FOR_GUILD = """
    SELECT vn.id, vn.vndb_id, vn.guild_id, vn.start_month, vn.end_month,
           vn.is_monthly_points, vn.created_at, vc.title_ja, vc.title_en, vn.status
    FROM vn_titles vn
    LEFT JOIN vndb_cache vc ON vc.vndb_id = vn.vndb_id
    WHERE vn.guild_id IS NULL OR vn.guild_id = ?
    ORDER BY vn.start_month DESC, vn.id DESC;
    """

    # All pool entries across every guild — used by `/pool all_servers:true` so
    # admins can see what other servers have configured. Same column shape as
    # GET_POOL_ENTRIES_FOR_GUILD so the rendering code can stay unified.
    GET_POOL_ENTRIES_GLOBAL = """
    SELECT vn.id, vn.vndb_id, vn.guild_id, vn.start_month, vn.end_month,
           vn.is_monthly_points, vn.created_at, vc.title_ja, vc.title_en, vn.status
    FROM vn_titles vn
    LEFT JOIN vndb_cache vc ON vc.vndb_id = vn.vndb_id
    ORDER BY vn.start_month DESC, vn.id DESC;
    """

    # Insert a nomination as a 'nominated'-status pool row.
    # Bind: (vndb_id, guild_id, start_month, end_month, is_monthly_points,
    #        cycle_id, nominator_user_id, title_cache).
    # ``cycle_id`` may be NULL — nominations created via /nominate in the
    # decoupled model start unattached and get swept onto a cycle when
    # Open voting fires for their target month.
    #
    # OR IGNORE + partial unique index on
    # (nominator_user_id, guild_id, start_month, end_month) WHERE
    # status='nominated' closes the TOCTOU race between the cog's
    # SELECT-then-decide guard and this insert: a duplicate concurrent
    # /nominate no-ops silently and the caller checks lastrowid=0 to
    # surface a "race resolved" path.
    INSERT_NOMINATION_AS_PICK = """
    INSERT OR IGNORE INTO vn_titles
        (vndb_id, guild_id, start_month, end_month, is_monthly_points,
         status, cycle_id, nominator_user_id, title_cache)
    VALUES (?, ?, ?, ?, ?, 'nominated', ?, ?, ?);
    """

    # Find any active voting cycle of the SAME KIND whose target window
    # overlaps a given [start_month, end_month] range. Kind-filtered
    # because monthly and seasonal voting tracks are independent — a
    # monthly June vote shouldn't block a seasonal Spring nomination.
    # Used by /nominate to reject new nominations that would land
    # *during* an active vote of the same series — those wouldn't be in
    # the vote anyway (the sweep only runs at cycle-open time), so
    # silently accepting them misleads the user.
    # Bind: (guild_id, kind, nom_end_month, nom_start_month).
    GET_ACTIVE_VOTING_OVERLAPPING_MONTH = """
    SELECT id, kind, target_month, target_end_month
    FROM vn_cycles
    WHERE guild_id = ?
      AND phase = 'voting'
      AND kind = ?
      AND target_month <= ?
      AND COALESCE(target_end_month, target_month) >= ?
    LIMIT 1;
    """

    # Per-user-per-month dup check joined with the attached cycle's phase.
    # Returns the existing nomination row's identity + cycle phase so the
    # caller can decide between "update in place" (no active vote) and
    # "reject" (active vote, can't change mid-stream). LEFT JOIN — phase
    # is NULL when cycle_id is NULL (never swept) or the cycle row is
    # missing. LIMIT 1 because the cog's invariant is one nomination per
    # (user, month-range, guild); defensive against historical duplicates.
    # Bind: (nominator_user_id, target_month, target_month, guild_id).
    # Find a user's existing nomination for the EXACT period (start +
    # end) they're nominating for. Bind: (user_id, start_month,
    # end_month, guild_id).
    #
    # Exact-period match (not overlap) so monthly and seasonal lanes
    # stay independent — a user can hold one monthly-May nom AND one
    # seasonal-Spring (Apr-Jun) nom without one displacing the other.
    # Matches the scoping used by SWEEP_NOMINATIONS_TO_CYCLE: a cycle
    # only ever picks up nominations with start_month/end_month matching
    # its own window, so the dedupe needs to use the same key.
    GET_USER_NOM_IN_MONTH_WITH_CYCLE = """
    SELECT vt.id, vt.vndb_id, vt.cycle_id, c.phase
    FROM vn_titles vt
    LEFT JOIN vn_cycles c ON c.id = vt.cycle_id
    WHERE vt.nominator_user_id = ?
      AND vt.status = 'nominated'
      AND vt.start_month = ? AND vt.end_month = ?
      AND (vt.guild_id IS NULL OR vt.guild_id = ?)
    LIMIT 1;
    """

    # Nominator-blind sibling of the dedupe query above: find any pending
    # nomination of a given VN for the EXACT period in this guild, no matter
    # who nominated it. The per-user query can't catch a *second* user
    # nominating an already-nominated VN; without this the same VN stacks
    # twice onto one ballot (vote-splitting + double-counts the 25 cap).
    # Exact-period match keeps monthly and seasonal lanes independent, same
    # as the per-user dedupe. Bind: (vndb_id, start_month, end_month, guild_id).
    GET_NOMINATION_BY_VN_AND_PERIOD = """
    SELECT id, nominator_user_id
    FROM vn_titles
    WHERE vndb_id = ?
      AND status = 'nominated'
      AND start_month = ? AND end_month = ?
      AND (guild_id IS NULL OR guild_id = ?)
    LIMIT 1;
    """

    # Pool-wide sibling of GET_NOMINATION_BY_VN_AND_PERIOD: find ANY pool entry
    # for a VN in the EXACT period for this guild, whatever the status (a
    # pending nomination OR an already-decided monthly/seasonal/special pick).
    # /nominate uses this to block re-nominating a VN that is already in the
    # pool for that exact period, and branches the message on the returned
    # status. Picks sort before nominations so a legacy pick+nomination
    # coexistence reports the pick deterministically rather than relying on
    # LIMIT 1 order. Exact-period match keeps monthly and seasonal lanes
    # independent. NULL-guild (legacy global) picks intentionally match in
    # every guild, mirroring how /pool and /monthly already surface them, so
    # keep the IS NULL clause rather than narrowing to a strict guild_id = ?.
    # Bind: (vndb_id, start_month, end_month, guild_id).
    GET_POOL_ENTRY_BY_VN_AND_PERIOD = """
    SELECT id, status, nominator_user_id
    FROM vn_titles
    WHERE vndb_id = ?
      AND start_month = ? AND end_month = ?
      AND (guild_id IS NULL OR guild_id = ?)
    ORDER BY (status = 'nominated') ASC, id ASC
    LIMIT 1;
    """

    # Re-point an existing 'nominated' vn_titles row at a different VN.
    # Used when a user re-runs /nominate for a month they've already
    # nominated for and the cycle isn't actively voting — replace the
    # VN reference in place rather than rejecting. Keeps the same
    # pool_id so external references (links, manage_pool entries) stay
    # valid. Bind: (new_vndb_id, new_title_cache, vn_titles.id).
    UPDATE_NOMINATION_VN = """
    UPDATE vn_titles
    SET vndb_id = ?, title_cache = ?
    WHERE id = ?;
    """

    # Nominees of a cycle (post-sweep). After Open voting sweeps the
    # status='nominated' rows for the target month onto the new cycle,
    # this query returns them in nomination order. Same column shape the
    # cog uses today via NOM_* indices: (id, cycle_id, vndb_id, user_id,
    # guild_id, title, created_at). Title preference matches the pool
    # view: live vndb_cache.title_ja first, then title_en, then the
    # nomination-time title_cache, then vndb_id. Older nominations whose
    # title_cache was captured as romaji still surface in JA once the
    # cache has the row.
    GET_CYCLE_NOMINEES = """
    SELECT vt.id, vt.cycle_id, vt.vndb_id, vt.nominator_user_id, vt.guild_id,
           COALESCE(vc.title_ja, vc.title_en, vt.title_cache, vt.vndb_id) AS title,
           vt.created_at
    FROM vn_titles vt
    LEFT JOIN vndb_cache vc ON vc.vndb_id = vt.vndb_id
    WHERE vt.cycle_id = ? AND vt.status = 'nominated'
    ORDER BY vt.created_at ASC;
    """

    # Same as GET_CYCLE_NOMINEES but keeps promoted winners (a winner's status
    # flips to 'monthly'/'seasonal' on close while it retains its cycle_id, so
    # the status='nominated' filter would otherwise drop it). Used by the
    # Participants panel so it can still show who voted for the winning title
    # after a vote closes. Status set mirrors TALLY_VOTES; same column shape
    # as GET_CYCLE_NOMINEES (the NOM_* indices), so callers are unchanged.
    GET_CYCLE_NOMINEES_ALL = """
    SELECT vt.id, vt.cycle_id, vt.vndb_id, vt.nominator_user_id, vt.guild_id,
           COALESCE(vc.title_ja, vc.title_en, vt.title_cache, vt.vndb_id) AS title,
           vt.created_at
    FROM vn_titles vt
    LEFT JOIN vndb_cache vc ON vc.vndb_id = vt.vndb_id
    WHERE vt.cycle_id = ? AND vt.status IN ('nominated', 'monthly', 'seasonal')
    ORDER BY vt.created_at ASC;
    """

    # Sweep all status='nominated' rows whose [start_month, end_month]
    # window EXACTLY matches the cycle's window onto a freshly-created
    # cycle. Run by Open voting so the new cycle picks up every existing
    # nomination for that period — including ones that lost previous
    # votes, which is the user-facing "nominations are persistent"
    # behaviour. Winners (status='monthly'/'seasonal') aren't touched.
    #
    # Exact period match (start_month = ? AND end_month = ?) keeps
    # monthly + seasonal noms in their own lanes:
    #   - Monthly cycle (target=Jun, end=Jun) sweeps only nominations
    #     with start=Jun, end=Jun — i.e. monthly noms.
    #   - Seasonal cycle (target=Apr, end=Jun) sweeps only nominations
    #     with start=Apr, end=Jun — i.e. seasonal noms for that season.
    # An overlap-based sweep would mistakenly pull a Spring seasonal
    # nom (Apr-Jun) into a monthly Jun cycle and vice versa.
    # Bind: (new_cycle_id, target_month, target_end_month, guild_id).
    SWEEP_NOMINATIONS_TO_CYCLE = """
    UPDATE vn_titles SET cycle_id = ?
    WHERE status = 'nominated'
      AND start_month = ? AND end_month = ?
      AND (guild_id IS NULL OR guild_id = ?);
    """

    # Pre-count of pending nominations matching SWEEP's WHERE clause, used
    # by /open_voting to validate "no nominees" and "too many nominees"
    # BEFORE the atomic cycle+sweep+settings transaction commits. The
    # predicate must match SWEEP_NOMINATIONS_TO_CYCLE exactly so the
    # count reflects what the sweep will actually move. Bind:
    # (start_month, end_month, guild_id).
    COUNT_PENDING_NOMINATIONS_FOR_PERIOD = """
    SELECT COUNT(*) FROM vn_titles
    WHERE status = 'nominated'
      AND start_month = ? AND end_month = ?
      AND (guild_id IS NULL OR guild_id = ?);
    """

    # Find a nomination by its (now vn_titles) id. Same column shape as above.
    GET_NOMINATION_BY_ID = """
    SELECT id, cycle_id, vndb_id, nominator_user_id, guild_id,
           COALESCE(title_cache, vndb_id) AS title, created_at
    FROM vn_titles
    WHERE id = ?;
    """

    # Promote a winning nomination row in place: change status from
    # 'nominated' to the cycle kind. Used by _close_voting.
    # Bind: (new_status, vn_titles.id).
    PROMOTE_NOMINATION_TO_PICK = """
    UPDATE vn_titles SET status = ? WHERE id = ?;
    """

    # Reopen a previously-closed cycle in place. Flips phase back to
    # 'voting', clears announcement_*/closed_at/closes_at so the panel
    # treats it as freshly-opened (admin posts a new vote menu via
    # Repost). Cycle id is unique, no guild_id check at the SQL layer —
    # the cog validates guild ownership before calling.
    # Bind: (cycle_id,).
    REOPEN_CYCLE = """
    UPDATE vn_cycles SET
        phase = 'voting',
        closed_at = NULL,
        announcement_message_id = NULL,
        announcement_channel_id = NULL,
        closes_at = NULL
    WHERE id = ?;
    """

    # Companion to REOPEN_CYCLE: undo the winner promotion(s) so the
    # previous winner(s) are back as candidates rather than being
    # skipped by the sweep (status='nominated' is what the sweep and
    # tally look at).
    #
    # Filters by PERIOD (start_month + end_month) and guild rather than
    # the original cycle_id, because subsequent cycles for the same
    # period can sweep rows away from the cycle being reopened — by
    # the time we reopen cycle 7, the row's cycle_id may have been
    # overwritten to a later cycle's id by SWEEP. Period match catches
    # the rows regardless. Only touches vote-produced entries
    # (cycle_id IS NOT NULL) so admin-set pool picks aren't demoted.
    # Skips 'special' too — that's an admin status, not a vote outcome.
    # Bind: (start_month, end_month, guild_id).
    DEMOTE_PERIOD_PICKS_TO_NOMINATIONS = """
    UPDATE vn_titles SET status = 'nominated'
    WHERE status IN ('monthly', 'seasonal')
      AND cycle_id IS NOT NULL
      AND start_month = ? AND end_month = ?
      AND (guild_id IS NULL OR guild_id = ?);
    """

    # Reopen helper, runs just before DEMOTE. After a close the winner is
    # status='monthly'/'seasonal', invisible to the /nominate dup guard (which
    # only sees 'nominated'), so a user can re-nominate that exact VN for the
    # same period, creating a second 'nominated' row. DEMOTE would then flip
    # the winner back to 'nominated' too and the two rows collide on
    # idx_vn_titles_vn_period_dedup, rolling the whole reopen back. Drop the
    # standalone re-nomination first: it is cycle_id IS NULL (never swept, so
    # it carries no votes) and the VN returns to the ballot via the demoted
    # winner, so nothing is lost. Rows with votes (cycle_id set) are never
    # touched, so a multi-attached edge just leaves the index unformed rather
    # than deleting a voted row.
    # Bind: (start_month, end_month, guild_id, start_month, end_month, guild_id).
    DELETE_REDUNDANT_NOMINATIONS_FOR_REOPEN = """
    DELETE FROM vn_titles
    WHERE status = 'nominated'
      AND cycle_id IS NULL
      AND start_month = ? AND end_month = ?
      AND (guild_id IS NULL OR guild_id = ?)
      AND vndb_id IN (
          SELECT vndb_id FROM vn_titles
          WHERE status IN ('monthly', 'seasonal')
            AND cycle_id IS NOT NULL
            AND start_month = ? AND end_month = ?
            AND (guild_id IS NULL OR guild_id = ?)
      );
    """

    # Drop any still-nominated rows tied to a cancelled cycle. Without this
    # they'd linger forever in vn_titles with status='nominated' and surface
    # in /pool as "lost vote" — misleading because no vote ever happened.
    # Bind: (cycle_id,).
    DELETE_CYCLE_NOMINATIONS = """
    DELETE FROM vn_titles WHERE cycle_id = ? AND status = 'nominated';
    """

    # Unified month-scoped query for /pool. Returns pool rows AND nomination
    # rows whose [start_month, end_month] window covers the displayed month.
    # Joined to vn_cycles for phase + winner_flag (used to render tags for
    # nominated rows).
    # Bind: (displayed_month, displayed_month, guild_id).
    GET_VN_TITLES_FOR_MONTH = _VN_TITLES_FULL_SELECT + f"""
    WHERE vt.start_month <= ? AND vt.end_month >= ?
      AND (vt.guild_id IS NULL OR vt.guild_id = ?)
    ORDER BY {_POOL_STATUS_ORDER}, vt.start_month DESC, vt.id DESC;
    """

    # Same as GET_VN_TITLES_FOR_MONTH but across every guild.
    # Bind: (displayed_month, displayed_month).
    GET_VN_TITLES_FOR_MONTH_GLOBAL = _VN_TITLES_FULL_SELECT + f"""
    WHERE vt.start_month <= ? AND vt.end_month >= ?
    ORDER BY {_POOL_STATUS_ORDER}, vt.start_month DESC, vt.id DESC;
    """

    # Full single-row fetch for /pool_entry. Same column shape as the
    # _FOR_MONTH variants but filtered to one row by primary key. LIMIT 1
    # is defensive — vt.id is unique by definition.
    # Bind: (id, guild_id).
    # Cross-guild by-ID lookup — see GET_VN_TITLE_BY_ID note. /pool_entry
    # accepts any pool_id and renders detail regardless of guild affiliation.
    GET_VN_TITLE_FULL = _VN_TITLES_FULL_SELECT + """
    WHERE vt.id = ?
    LIMIT 1;
    """

