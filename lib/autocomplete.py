"""
Shared autocomplete functions for the VN Club Bot.
"""

import json
import discord
import logging
from pathlib import Path
from typing import List
from lib.utils import DatabaseQueries
from lib.vndb_search import search_visual_novel, create_autocomplete_value, parse_autocomplete_value

logger = logging.getLogger(__name__)

HELP_JSON_PATH = Path(__file__).resolve().parent.parent / "help_commands.json"


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
    except Exception as exc:
        logger.warning("user_logs autocomplete failed: %s", exc)
        return []


def _month_window(back: int, forward: int) -> List[str]:
    """Return YYYY-MM strings spanning ``back`` months prior through
    ``forward`` months ahead of the current month, ordered by absolute
    distance from now (current → -1 / +1 → -2 / +2 → ...).

    This interleaving means both "backfill last month" and "schedule next
    month" land at the top of the dropdown without scrolling. Past months
    appear before future months at each ring so backfill cases (most
    common: ``/manage_log reward_month:``) get the closest hit
    immediately above the current month.
    """
    from datetime import datetime
    now = datetime.now()
    cur_y, cur_m = now.year, now.month

    def shift(offset: int) -> str:
        idx = cur_y * 12 + (cur_m - 1) + offset
        return f"{idx // 12:04d}-{idx % 12 + 1:02d}"

    out: list[str] = [shift(0)]  # current first
    for d in range(1, max(back, forward) + 1):
        if d <= back:
            out.append(shift(-d))
        if d <= forward:
            out.append(shift(d))
    return out


def _month_picker_finish(
    needle: str, suggestions: List[str],
) -> List[discord.app_commands.Choice[str]]:
    """Shared filter + echo + cap logic for the month-picker variants below."""
    import re
    from datetime import datetime
    if needle:
        suggestions = [m for m in suggestions if needle in m]

    # Echo a typed-but-not-suggested valid YYYY-MM at the top so admins can
    # pick e.g. 2025-09 even though it's outside the default window.
    if re.fullmatch(r"\d{4}-\d{2}", needle) and needle not in suggestions:
        try:
            datetime.strptime(needle, "%Y-%m")
            suggestions.insert(0, needle)
        except ValueError:
            pass

    return [
        discord.app_commands.Choice(name=m, value=m)
        for m in suggestions[:25]
    ]


async def month_picker_autocomplete(
    interaction: discord.Interaction, current: str,
) -> List[discord.app_commands.Choice[str]]:
    """Suggest YYYY-MM picks for *date-setter* inputs around the current
    month — e.g. `/manage_pool start_month` / `end_month`. Symmetric ±12
    so corrections to past entries and scheduling future entries both
    work. For *backfill* inputs that should not allow future months,
    use ``month_picker_past_autocomplete``.

    Always echoes the user's literal input if it parses as a valid YYYY-MM
    so they aren't constrained to the suggested window.
    """
    needle = (current or "").strip()
    suggestions = _month_window(back=12, forward=12)
    return _month_picker_finish(needle, suggestions)


async def month_picker_past_autocomplete(
    interaction: discord.Interaction, current: str,
) -> List[discord.app_commands.Choice[str]]:
    """Past + current months only. Used for `/manage_log reward_month:`
    where logging into a future month is nonsensical (admins backfill a
    completion that already happened). 25 entries: current + 24 past.
    """
    needle = (current or "").strip()
    suggestions = _month_window(back=24, forward=0)
    return _month_picker_finish(needle, suggestions)


async def month_picker_future_autocomplete(
    interaction: discord.Interaction, current: str,
) -> List[discord.app_commands.Choice[str]]:
    """Current + future months in chronological order. Used for
    ``/nominate target_month`` where users almost always nominate
    forward.

    The interleaved past/future ordering of ``month_picker_autocomplete``
    is confusing for forward-only flows (the dropdown would jump
    2026-05, 2026-04, 2026-06, 2026-03 …). This variant shows a clean
    linear "this month → next → +2 → +3 …" sequence.

    Past months are still pickable: type any valid YYYY-MM and it echoes
    at the top of the suggestions so the picker isn't a hard cap.
    """
    needle = (current or "").strip()
    # _month_window's interleaving doesn't matter when one side is 0 —
    # it ends up just "current, +1, +2, …" naturally. 24 forward gives
    # a 25-item window (matches Discord's choice cap).
    suggestions = _month_window(back=0, forward=24)
    return _month_picker_finish(needle, suggestions)


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


async def month_int_autocomplete(
    interaction: discord.Interaction, current: str,
) -> List[discord.app_commands.Choice[int]]:
    """Suggest months 1-12 with their English names. Used for params typed
    as `int` representing a month-of-year (e.g. ``/pool month:``).
    """
    needle = (current or "").strip().lower()
    choices: list[discord.app_commands.Choice[int]] = []
    for i, name in enumerate(_MONTH_NAMES, start=1):
        label = f"{i} — {name}"
        if needle and needle not in label.lower() and needle != str(i):
            continue
        choices.append(discord.app_commands.Choice(name=label, value=i))
    return choices[:25]


async def year_autocomplete(
    interaction: discord.Interaction, current: str,
) -> List[discord.app_commands.Choice[int]]:
    """Suggest years near the current year. Used by season-aware
    leaderboards. Echoes a typed 4-digit year not in the suggested
    range so far-past or far-future years are still selectable.
    """
    from datetime import datetime
    needle = (current or "").strip()
    cur = datetime.now().year
    years = [cur, cur + 1, cur - 1, cur + 2, cur - 2, cur + 3, cur - 3]

    if needle:
        years = [y for y in years if needle in str(y)]

    if needle.isdigit() and len(needle) == 4:
        n = int(needle)
        if n not in years:
            years.insert(0, n)

    return [
        discord.app_commands.Choice(name=str(y), value=y)
        for y in years[:25]
    ]


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
    except Exception as exc:
        logger.warning("month autocomplete failed: %s", exc)
        return []


async def bot_guilds_autocomplete(
    interaction: discord.Interaction, current: str,
) -> List[discord.app_commands.Choice]:
    """Autocomplete picker over every guild the bot is currently in.

    Used by admin commands that take a ``guild_id`` parameter for explicit
    cross-guild targeting (``/manage_managers``, ``/manage_pool``). Unlike
    ``server_autocomplete`` which filters to guilds-with-reading-logs, this
    one lists every guild the bot is a member of — including newly-joined
    clubs that haven't had any /finish activity yet, so a host can seed
    their pool right after the invite.

    Filters by case-insensitive substring match on guild name or numeric
    ID, capped at Discord's 25-choice limit.
    """
    bot = interaction.client
    needle = (current or "").strip().lower()
    choices: List[discord.app_commands.Choice] = []
    for guild in bot.guilds:
        label = f"{guild.name} ({guild.id})"
        if needle and needle not in guild.name.lower() and needle not in str(guild.id):
            continue
        if len(label) > 100:
            label = label[:99] + "…"
        choices.append(discord.app_commands.Choice(name=label, value=str(guild.id)))
        if len(choices) >= 25:
            break
    return choices


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
    except Exception as exc:
        logger.warning("server autocomplete failed: %s", exc)
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
    except Exception as exc:
        logger.warning("vn_pool autocomplete failed: %s", exc)
        return []


async def help_command_autocomplete(interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice]:
    """Autocomplete for the /help `command` argument.

    Suggests command names from help_commands.json. Matches `current`
    case-insensitively against name and short_description so a user typing
    "log" surfaces /logs / /log_undo / /log_edit, and "rank" surfaces
    /leaderboard via its short description.
    """
    try:
        with open(HELP_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("commands", [])
    except Exception as e:  # noqa: BLE001
        logger.warning("help autocomplete: failed to load %s: %s", HELP_JSON_PATH, e)
        return []

    needle = (current or "").strip().lower().lstrip("/")
    choices: List[discord.app_commands.Choice[str]] = []
    for cmd in entries:
        name = cmd.get("name") or ""
        short = cmd.get("short_description") or ""
        if needle and needle not in name.lower() and needle not in short.lower():
            continue
        # Display label: `name — short_description`, truncated to Discord's 100-char limit.
        label = f"{name} — {short}" if short else name
        if len(label) > 100:
            label = label[:99] + "…"
        choices.append(discord.app_commands.Choice(name=label, value=name.lstrip("/")))
        if len(choices) >= 25:
            break
    return choices


# Rating choices for consistent use across commands
RATING_CHOICES = [
    discord.app_commands.Choice(name="⭐ 1 - Terrible", value=1),
    discord.app_commands.Choice(name="⭐⭐ 2 - Bad", value=2),
    discord.app_commands.Choice(name="⭐⭐⭐ 3 - Average", value=3),
    discord.app_commands.Choice(name="⭐⭐⭐⭐ 4 - Good", value=4),
    discord.app_commands.Choice(name="⭐⭐⭐⭐⭐ 5 - Masterpiece", value=5),
]