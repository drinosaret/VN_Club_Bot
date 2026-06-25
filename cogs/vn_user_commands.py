import asyncio
import discord
import discord.app_commands as app_commands
import json
import logging
from datetime import datetime
from typing import List, Optional, Tuple
from discord.ext import commands
from lib.autocomplete import HELP_JSON_PATH
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry
from lib.jiten_client import JitenClient, JitenInfo, resolve_display_cover
from lib.pagination import BasePaginationView, GenericPaginationView
from lib.utils import (
    DatabaseQueries,
    get_current_month,
    get_single_monthly_vn,
    is_month_in_range,
    season_to_months,
    current_anime_season,
    format_season_label,
    prev_season,
    next_season,
    validate_user_permission,
    validate_rating_input,
    handle_command_error,
    truncate_text,
    BotError,
    ValidationError,
    MAX_DISCORD_MESSAGE,
    MAX_EMBED_DESCRIPTION,
    EMBED_DESCRIPTION_BUFFER,
    DEFAULT_TIMEOUT,
    create_base_embed,
    add_pagination_footer,
    resolve_vn_from_input,
    require_same_guild,
)
from lib.embeds import EmbedBuilder, build_vn_links_view
from lib.autocomplete import vn_autocomplete, user_logs_autocomplete, month_autocomplete, month_picker_past_autocomplete, year_autocomplete, server_autocomplete, help_command_autocomplete, RATING_CHOICES
from .username_fetcher import get_username_db
from math import ceil

_log = logging.getLogger(__name__)


async def _cache_jiten_character_count(bot: VNClubBot, vndb_id: str) -> Optional[JitenInfo]:
    """Background task: look up jiten character_count for ``vndb_id`` and
    persist it onto ``vndb_cache.character_count`` for future /club_stats
    aggregations. No-op when jiten doesn't have the VN or anything errors;
    /club_stats has its own lazy backfill pass for misses.

    Returns the full ``JitenInfo`` so callers can reuse its ``deck_id`` (link
    button) and ``cover_url`` (NSFW-cover fallback) without re-fetching, or
    ``None`` if the VN isn't on jiten / lookup failed.
    """
    try:
        async with JitenClient() as jiten:
            data = await jiten.get_by_vndb_id(vndb_id)
        if data and data.character_count and data.character_count > 0:
            await VN_Entry.set_cached_character_count(
                bot, vndb_id, data.character_count
            )
        return data
    except Exception as e:  # noqa: BLE001
        _log.debug("jiten char-count cache miss for %s: %s", vndb_id, e)
        return None


async def _user_rank_in(bot, user_id, query_const, params):
    """Return (rank, total) for `user_id` against the rows produced by the
    given leaderboard query, or None if the user has no rows in scope.
    Mirrors the per-user aggregation pattern used by /leaderboard."""
    rows = await bot.GET(query_const, params)
    totals: dict[int, int] = {}
    for row in rows:
        uid, _v, _r, _m, pts, _c, _g = row
        totals[uid] = totals.get(uid, 0) + pts
    if user_id not in totals:
        return None
    sorted_users = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    rank = next(i for i, (uid, _) in enumerate(sorted_users, 1) if uid == user_id)
    return rank, len(sorted_users)


SEASON_CHOICES = [
    app_commands.Choice(name="Winter (Jan–Mar)", value="winter"),
    app_commands.Choice(name="Spring (Apr–Jun)", value="spring"),
    app_commands.Choice(name="Summer (Jul–Sep)", value="summer"),
    app_commands.Choice(name="Fall (Oct–Dec)",   value="fall"),
]


LEADERBOARD_TIMEFRAME_CHOICES = [
    app_commands.Choice(name="Current season (default)", value="current_season"),
    app_commands.Choice(name="All-time", value="all_time"),
]


# ==================== VIEW CLASSES ====================


# Display order + emoji for each help category. Ordering lives in code (not
# JSON) so we can tweak presentation without touching content. Categories not
# listed here fall through to "Other" so a typo'd category in the JSON still
# renders rather than silently disappearing.
_HELP_CATEGORY_ORDER = [
    ("reading", "📖 READING"),
    ("stats",   "📊 STATS"),
    ("pool",    "🗓️ POOL & BANNERS"),
    ("voting",  "🗳️ VOTING"),
    ("admin",   "🛠️ ADMIN"),
    ("help",    "❓ HELP"),
]
_HELP_CATEGORY_LABELS = dict(_HELP_CATEGORY_ORDER)


def _build_help_compact_embed(help_data: list) -> discord.Embed:
    """Categorized one-page overview. One embed field per category, value lists
    each command in the category as `**/name** — short description`."""
    embed = create_base_embed(
        title="📖 Visual Novel Club Bot — Commands",
        description="Pick from the dropdown for full detail, or use `/help command:<name>`.",
        color=discord.Color.blue(),
    )
    embed.set_author(name="Visual Novel Club Bot")

    grouped: dict[str, list] = {}
    for cmd in help_data:
        cat = cmd.get("category") or "other"
        grouped.setdefault(cat, []).append(cmd)

    seen: set[str] = set()
    for cat_key, cat_label in _HELP_CATEGORY_ORDER:
        cmds = grouped.get(cat_key)
        if not cmds:
            continue
        seen.add(cat_key)
        lines = [
            f"**{c['name']}** — {c.get('short_description') or ''}"
            for c in cmds
        ]
        embed.add_field(name=cat_label, value="\n".join(lines), inline=False)

    # Any leftover categories the JSON used but the order list doesn't know about.
    leftover = [k for k in grouped if k not in seen]
    for cat_key in leftover:
        cmds = grouped[cat_key]
        lines = [
            f"**{c['name']}** — {c.get('short_description') or ''}"
            for c in cmds
        ]
        embed.add_field(name=f"📂 {cat_key.upper()}", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"{len(help_data)} commands")
    return embed


def _build_help_detail_embed(cmd: dict) -> discord.Embed:
    """Full detail embed for a single command — usage, description, params, example."""
    embed = create_base_embed(
        title=f"📖 {cmd['name']}",
        description=cmd.get("description") or "",
        color=discord.Color.blue(),
    )
    embed.set_author(name="Visual Novel Club Bot")
    embed.add_field(name="Usage", value=f"`{cmd.get('usage', '')}`", inline=False)
    if cmd.get("parameters"):
        embed.add_field(name="Parameters", value=cmd["parameters"], inline=False)
    if cmd.get("example"):
        embed.add_field(name="Example", value=f"`{cmd['example']}`", inline=False)
    embed.set_footer(text="Use /help to see all commands")
    return embed


def _find_help_entry(help_data: list, name: str) -> Optional[dict]:
    """Case-insensitive lookup that ignores leading slashes."""
    needle = (name or "").strip().lower().lstrip("/")
    for cmd in help_data:
        if (cmd.get("name") or "").lstrip("/").lower() == needle:
            return cmd
    return None


class HelpCommandSelect(discord.ui.Select):
    """Dropdown attached to the compact view. Picking an option swaps the
    message in place to that command's detail view (with a back button)."""

    def __init__(self, help_data: list):
        self._help_data = help_data
        # Build options grouped roughly by the category order so the dropdown
        # reads in the same order as the embed fields above it.
        ordered: list[dict] = []
        grouped: dict[str, list] = {}
        for cmd in help_data:
            grouped.setdefault(cmd.get("category") or "other", []).append(cmd)
        for cat_key, _ in _HELP_CATEGORY_ORDER:
            ordered.extend(grouped.get(cat_key, []))
        # Append any leftover categories so nothing is dropped from the picker.
        for cat_key, cmds in grouped.items():
            if cat_key not in _HELP_CATEGORY_LABELS:
                ordered.extend(cmds)

        options: list[discord.SelectOption] = []
        for cmd in ordered[:25]:  # Discord cap
            short = cmd.get("short_description") or ""
            options.append(discord.SelectOption(
                label=cmd["name"][:100],
                value=cmd["name"],
                description=short[:100] if short else None,
            ))
        super().__init__(
            placeholder="Pick a command for full detail…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        picked_name = self.values[0]
        cmd = _find_help_entry(self._help_data, picked_name)
        if not cmd:
            await interaction.response.send_message(
                "❌ Couldn't find that command — try `/help` again.", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=_build_help_detail_embed(cmd),
            view=HelpDetailView(self._help_data),
        )


class HelpCompactView(discord.ui.View):
    """Default `/help` view — one embed with category sections + Select picker."""

    def __init__(self, help_data: list):
        super().__init__(timeout=300)
        self._help_data = help_data
        self.add_item(HelpCommandSelect(help_data))

    def create_embed(self) -> discord.Embed:
        return _build_help_compact_embed(self._help_data)


class HelpDetailView(discord.ui.View):
    """Detail view shown after picking from the dropdown. Single back button
    that re-renders the compact view in place."""

    def __init__(self, help_data: list):
        super().__init__(timeout=300)
        self._help_data = help_data

    @discord.ui.button(label="← Back to list", style=discord.ButtonStyle.secondary)
    async def back_to_list(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_build_help_compact_embed(self._help_data),
            view=HelpCompactView(self._help_data),
        )


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


async def _aggregate_leaderboard_rows(bot, rows) -> list[dict]:
    """Bucket reading_logs rows into per-user (points, completions) entries
    sorted by points desc, completions desc.

    Centralized so the leaderboard slash command and the season-nav button
    callbacks share the exact same aggregation rules — without this, the
    nav buttons would drift from the initial render's behavior over time.
    """
    agg: dict[int, dict] = {}
    for row in rows:
        (
            user_id,
            vndb_id,
            _reward_reason,
            _reward_month,
            points,
            _comment,
            _logged_in_guild,
        ) = row
        bucket = agg.setdefault(
            user_id, {"points": 0, "completions": 0, "username": None}
        )
        bucket["points"] += points
        if vndb_id:
            bucket["completions"] += 1
    for uid, data in agg.items():
        data["username"] = await get_username_db(bot, uid)
    return sorted(
        agg.values(),
        key=lambda d: (d["points"], d["completions"]),
        reverse=True,
    )


class LeaderboardView(BasePaginationView):
    """Paginated view for leaderboard with navigation buttons.

    ``leaderboard_data`` is a list of dicts with keys ``username``, ``points``,
    ``completions`` (already sorted by points desc, completions desc).
    ``period_label`` is a short human label for the time window
    (e.g. "Spring 2026" or "All-Time") shown above the podium on page 0.
    ``is_default_season`` flags that the current-season default kicked in
    because the user didn't pass a timeframe — purely informational, no
    behavioral effect right now (kept on the embed call for future tweaks).
    """

    def __init__(
        self,
        leaderboard_data,
        title,
        per_page: int = 20,
        *,
        period_label: Optional[str] = None,
        is_default_season: bool = False,
    ):
        super().__init__(leaderboard_data, title, per_page)
        self.period_label = period_label
        self.is_default_season = is_default_season

    def create_embed(self):
        """Create an embed for the current page"""
        return EmbedBuilder.create_leaderboard_embed(
            self.title,
            self.data,
            self.current_page,
            self.max_pages,
            self.per_page,
            period_label=self.period_label,
            is_default_season=self.is_default_season,
        )


class SeasonNavLeaderboardView(LeaderboardView):
    """LeaderboardView + prev/next anime-season navigation buttons. Used
    only when the leaderboard is season-scoped (either explicit
    ``/leaderboard season:`` or the default current-season fallback).

    The buttons re-query for the adjacent season, replace ``self.data``
    and ``self.title`` in place, then re-render the embed. Pagination
    state resets to page 0 so the user always lands on the podium.
    """

    def __init__(
        self,
        leaderboard_data,
        title,
        per_page: int,
        *,
        period_label: Optional[str],
        is_default_season: bool,
        bot,
        season_value: str,
        season_year: int,
        server_id: Optional[int],
    ):
        super().__init__(
            leaderboard_data, title, per_page,
            period_label=period_label,
            is_default_season=is_default_season,
        )
        self._bot = bot
        self._season_value = season_value
        self._season_year = season_year
        self._server_id = server_id
        # Add the nav buttons. row=1 keeps them on a separate row from the
        # inherited pagination buttons (which sit on row=0 by default).
        self.add_item(_PrevSeasonLeaderboardButton())
        self.add_item(_NextSeasonLeaderboardButton())

    async def _shift_season(self, interaction, new_year: int, new_season: str):
        """Re-fetch + re-render this view for ``(new_year, new_season)``.

        When the target season has zero logs, surfaces an ephemeral message
        and leaves the main embed untouched — matches /server_leaderboard's
        nav behavior so users don't accidentally page into an empty void.
        """
        months = season_to_months(new_season, new_year)
        if self._server_id is not None:
            rows = await self._bot.GET(
                DatabaseQueries.GET_LOGS_BY_SEASON_AND_SERVER,
                (*months, self._server_id),
            )
        else:
            rows = await self._bot.GET(
                DatabaseQueries.GET_LOGS_BY_SEASON, tuple(months),
            )
        slabel = await format_season_label(self._bot, new_year, new_season)
        if not rows:
            await interaction.response.send_message(
                f"No leaderboard data for {slabel}.", ephemeral=True,
            )
            return
        sorted_entries = await _aggregate_leaderboard_rows(self._bot, rows)
        if self._server_id is not None:
            guild = self._bot.get_guild(self._server_id)
            srv_name = guild.name if guild else f"Server {self._server_id}"
            plabel = f"{slabel} · {srv_name}"
        else:
            plabel = slabel

        # Mutate state + reset pagination, then re-render.
        self._season_value = new_season
        self._season_year = new_year
        self.period_label = plabel
        self.title = f"🏆 VN Club Leaderboard — {plabel}"
        self.data = sorted_entries
        self.current_page = 0
        # max_pages depends on data length — recompute via the base class's
        # own logic (BasePaginationView caches this; reseting current_page
        # alone is fine here because create_embed uses len(self.data)).
        try:
            self.max_pages = max(1, -(-len(sorted_entries) // self.per_page))
        except Exception:  # noqa: BLE001
            pass
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


class _PrevSeasonLeaderboardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="⬅",
            label="Prev season",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavLeaderboardView = self.view  # type: ignore
        new_year, new_season = prev_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


class _NextSeasonLeaderboardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="➡",
            label="Next season",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavLeaderboardView = self.view  # type: ignore
        new_year, new_season = next_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


async def _build_server_standings_embed(
    bot, period_label: str, rows,
) -> Optional[discord.Embed]:
    """Compose the server-standings embed (top-3 podium block + overflow
    field rows). Returns None when there's no server data to display.

    Centralized so the handler and the season-nav view share the exact
    same render — keeps button-driven re-renders visually identical to
    the initial post.
    """
    per_server: dict[int, dict[int, int]] = {}
    for row in rows:
        (
            user_id, vndb_id, _reward_reason, _reward_month,
            points, _comment, logged_in_guild,
        ) = row
        if logged_in_guild is None:
            continue
        srv = per_server.setdefault(logged_in_guild, {})
        srv[user_id] = srv.get(user_id, 0) + points

    server_totals: list[tuple[int, int]] = []
    all_user_ids: set[int] = set()
    for guild_id, users in per_server.items():
        if not users:
            continue
        server_totals.append((guild_id, sum(users.values())))
        all_user_ids.update(users.keys())
    if not server_totals:
        return None

    username_cache: dict[int, str] = {}
    for uid in all_user_ids:
        username_cache[uid] = await get_username_db(bot, uid)

    server_totals.sort(key=lambda x: x[1], reverse=True)
    total_points_all = sum(t for _, t in server_totals)

    def _top_users_lines(guild_id: int, n: int) -> list[tuple[str, int]]:
        users = per_server[guild_id]
        sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
        return [
            (username_cache.get(uid, f"User {uid}"), pts)
            for uid, pts in sorted_users[:n]
        ]

    def _server_name(guild_id: int) -> str:
        guild = bot.get_guild(guild_id)
        return guild.name if guild else f"Server {guild_id}"

    embed = discord.Embed(
        title=f"🏆 Server Standings — {period_label}",
        color=discord.Color.gold(),
    )

    TOP_USERS_PER_SERVER = 5
    podium_emojis = ["🥇", "🥈", "🥉"]
    podium_blocks: list[str] = []
    for i, (guild_id, total) in enumerate(server_totals[:3]):
        display = truncate_text(_server_name(guild_id), 60)
        top_users = _top_users_lines(guild_id, TOP_USERS_PER_SERVER)
        user_lines = [
            f"  `{n}.` {uname} — {pts:,}点"
            for n, (uname, pts) in enumerate(top_users, start=1)
        ] or ["  —"]
        podium_blocks.append(
            f"{podium_emojis[i]} **{display}** · **{total:,}**点\n"
            + "\n".join(user_lines)
        )
    if podium_blocks:
        embed.description = "\n\n".join(podium_blocks)

    MAX_FIELDS_TOTAL = 25
    remaining = server_totals[3:]
    max_overflow_fields = MAX_FIELDS_TOTAL - 1
    shown = remaining[:max_overflow_fields]
    for i, (guild_id, total) in enumerate(shown, start=4):
        display = truncate_text(_server_name(guild_id), 50)
        top_users = _top_users_lines(guild_id, TOP_USERS_PER_SERVER)
        value_lines = [
            f"`{n}.` {uname} — {pts:,}点"
            for n, (uname, pts) in enumerate(top_users, start=1)
        ]
        embed.add_field(
            name=f"#{i} {display} — {total:,}点",
            value="\n".join(value_lines) if value_lines else "—",
            inline=False,
        )
    if len(remaining) > max_overflow_fields:
        embed.add_field(
            name="...",
            value=f"And {len(remaining) - max_overflow_fields} more servers",
            inline=False,
        )

    embed.set_footer(
        text=f"{len(server_totals)} servers · {total_points_all:,} total points"
    )
    return embed


class SeasonNavServerStandingsView(discord.ui.View):
    """Single-season server standings view with prev/next-season nav."""

    def __init__(self, bot, season_value: str, season_year: int):
        super().__init__(timeout=300)
        self._bot = bot
        self._season_value = season_value
        self._season_year = season_year
        self.add_item(_PrevSeasonServerStandingsButton())
        self.add_item(_NextSeasonServerStandingsButton())

    async def _shift_season(self, interaction, new_year: int, new_season: str):
        months = season_to_months(new_season, new_year)
        rows = await self._bot.GET(
            DatabaseQueries.GET_LOGS_BY_SEASON, tuple(months),
        )
        period_label = await format_season_label(
            self._bot, new_year, new_season,
        )
        embed = await _build_server_standings_embed(
            self._bot, period_label, rows or [],
        )
        if embed is None:
            await interaction.response.send_message(
                f"No server data for {period_label}.", ephemeral=True,
            )
            return
        self._season_value = new_season
        self._season_year = new_year
        await interaction.response.edit_message(embed=embed, view=self)


class _PrevSeasonServerStandingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="⬅", label="Prev season",
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavServerStandingsView = self.view  # type: ignore
        new_year, new_season = prev_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


class _NextSeasonServerStandingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="➡", label="Next season",
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavServerStandingsView = self.view  # type: ignore
        new_year, new_season = next_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


class VNRatingsView(BasePaginationView):
    """Paginated view for VN ratings"""

    def __init__(
        self,
        ratings_data,
        vn_title,
        average_rating,
        total_ratings,
        per_page=10,
        thumbnail_url: Optional[str] = None,
    ):
        self.vn_title = vn_title
        self.average_rating = average_rating
        self.total_ratings = total_ratings
        # Stash so the thumbnail survives page-flip re-renders — the calling
        # handler used to set_thumbnail() once on the initial embed, and the
        # cover would vanish on next/prev because create_embed() built a
        # fresh embed each time.
        self.thumbnail_url = thumbnail_url
        super().__init__(ratings_data, f"⭐ User Ratings for {vn_title}", per_page)

    def create_embed(self):
        """Create an embed for the current page"""
        embed = create_base_embed(
            title=self.title,
            color=discord.Color.blue()
        )

        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)

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


class UndoLogView(discord.ui.View):
    """View with Undo button + VNDB / jiten.moe link buttons for VN completion embed."""

    def __init__(
        self,
        log_id: int,
        user_id: int,
        vndb_url: str,
        bot: VNClubBot,
        jiten_deck_id: Optional[int] = None,
    ):
        super().__init__(timeout=DEFAULT_TIMEOUT)  # 5 minute timeout
        self.log_id = log_id
        self.user_id = user_id
        self.bot = bot
        self.message = None

        self.add_item(discord.ui.Button(
            label="VNDB",
            style=discord.ButtonStyle.link,
            url=vndb_url
        ))
        if jiten_deck_id is not None:
            self.add_item(discord.ui.Button(
                label="jiten.moe",
                style=discord.ButtonStyle.link,
                url=f"https://jiten.moe/decks/media/{jiten_deck_id}/detail",
            ))

    @discord.ui.button(label="Undo Log", style=discord.ButtonStyle.danger, emoji="↩️")
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the undo button press."""
        # Check if the user pressing the button is the one who created the log
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "You can only undo your own logs!",
                ephemeral=True
            )
            return

        # Delete the log
        await self.bot.RUN(DatabaseQueries.DELETE_LOG_BY_ID, (self.log_id,))

        _log.info(
            f"Log #{self.log_id} undone via button by user {interaction.user.name} ({interaction.user.id})"
        )

        # Disable the button and update label
        button.disabled = True
        button.label = "Undone"
        button.style = discord.ButtonStyle.secondary
        button.emoji = None

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"Log #{self.log_id} has been deleted.",
            ephemeral=True
        )

    async def on_timeout(self):
        """Disable the button when the view times out."""
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.style != discord.ButtonStyle.link:
                item.disabled = True
                item.style = discord.ButtonStyle.secondary

        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


# ==================== HELPER FUNCTIONS ====================


async def log_already_exists(
    interaction: discord.Interaction,
    user_id: int,
    vndb_id: str,
    reward_month: str,
) -> bool:
    """Check whether the user already has a log for this VN in this month.

    Re-reads in *different* months are allowed (mirrors how the same VN can
    be in the pool multiple times across periods); only a same-month
    duplicate is rejected.
    """
    result = await interaction.client.GET_ONE(
        DatabaseQueries.GET_USER_VN_LOG_FOR_MONTH,
        (user_id, vndb_id, reward_month),
    )
    if result:
        await interaction.followup.send(
            f"You've already logged this VN for **{reward_month}**. "
            "Re-reads in a different month are fine."
        )
        return True
    return False


# ==================== MAIN COG CLASS ====================

class VNUserCommands(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot
        self._help_data: Optional[list] = None  # populated lazily, cached for the life of the cog

    async def cog_load(self):
        await self.bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
        # Create the reading_logs indexes here (idempotent via IF NOT
        # EXISTS) because migrations run *before* cog_load and the
        # corresponding migration step early-returns when reading_logs
        # doesn't yet exist. Without this, fresh deploys would run the
        # /profile, /club_stats, /leaderboard, and /logs queries as
        # full-table scans. Also creates the partial unique index that
        # makes ADD_READING_LOG_OR_IGNORE race-safe.
        for stmt in DatabaseQueries.CREATE_READING_LOGS_INDEXES:
            await self.bot.RUN(stmt)
        # Pre-load help data so the first /help isn't paying disk + decode cost.
        # Use the absolute path constant so the cog works regardless of CWD
        # (systemd / Docker entrypoint may not start at the project root).
        try:
            with open(HELP_JSON_PATH, "r", encoding="utf-8") as f:
                self._help_data = json.load(f).get("commands", [])
        except Exception as e:  # noqa: BLE001
            _log.error("Failed to preload help_commands.json: %s", e)
            self._help_data = None

    @app_commands.command(name="help", description="Show the command list. Pass `command:` for full detail.")
    @app_commands.describe(command="Optional: jump straight to one command's full detail.")
    @app_commands.autocomplete(command=help_command_autocomplete)
    async def help_command(
        self,
        interaction: discord.Interaction,
        command: Optional[str] = None,
    ):
        await interaction.response.defer()

        if self._help_data is None:
            await interaction.followup.send(
                "❌ Help data unavailable. Please contact an administrator."
            )
            return
        help_data = self._help_data

        if command:
            cmd = _find_help_entry(help_data, command)
            if not cmd:
                await interaction.followup.send(
                    f"❌ No such command `{command}`. Try `/help` for the full list.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                embed=_build_help_detail_embed(cmd),
                view=HelpDetailView(help_data),
            )
            return

        view = HelpCompactView(help_data)
        await interaction.followup.send(embed=view.create_embed(), view=view)

    @app_commands.command(name="finish", description="Mark a VN as finished.")
    @app_commands.describe(
        title="Search for a VN by title (type at least 2 characters).",
        comment="Your comment/review about the VN (max 2000 characters).",
        rating="Your personal rating for the VN (1-5) 1=Terrible; 5=Masterpiece.",
    )
    @app_commands.autocomplete(title=vn_autocomplete)
    @app_commands.choices(rating=RATING_CHOICES)
    @app_commands.guild_only()
    async def finish(
        self,
        interaction: discord.Interaction,
        title: str,
        comment: app_commands.Range[str, 1, 2000],
        rating: int,
    ):
        await interaction.response.defer()

        try:
            # Resolve VN ID from various input formats (autocomplete value, display format, raw ID)
            vndb_id = await resolve_vn_from_input(title)
            if not vndb_id:
                raise ValidationError("Could not determine VN from input. Please try selecting from the autocomplete dropdown.")

            # Validate inputs (comment length is enforced by Range on the param)
            await validate_rating_input(rating)

            current_month = get_current_month()

            # Check if this VN is currently in this server's pool window.
            # Legacy rows with guild_id IS NULL also match (treated as global).
            # The kind ('monthly' / 'seasonal' / 'special') drives the
            # reward_reason label below — points logic itself is unchanged.
            result = await get_single_monthly_vn(
                interaction.client, vndb_id,
                guild_id=interaction.guild.id if interaction.guild else None,
            )
            entry_status = None
            if result:
                _, start_month, end_month, is_monthly_points, entry_status = result
                read_in_pool_window = is_month_in_range(current_month, start_month, end_month)
            else:
                read_in_pool_window = False

            # Get VN info
            vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
            if not vn_info:
                # `from_vndb_id` returns None for both "VN doesn't exist on
                # VNDB" and "VNDB API was unreachable" (after retries). Most
                # of the time it's the latter — the autocomplete already
                # validated the ID exists. Pitch the retry option so users
                # don't think their input was bad.
                raise ValidationError(
                    f"VNDB lookup failed for {vndb_id}",
                    "Couldn't fetch that VN from VNDB. This usually means VNDB "
                    "is temporarily unreachable — try again in a moment. If "
                    "the error persists, double-check the ID.",
                )

            # Check for a same-month duplicate. Re-reads in different months
            # are allowed — mirrors how a VN can be in the pool across multiple
            # periods.
            if await log_already_exists(
                interaction, interaction.user.id, vndb_id, current_month,
            ):
                return

            # Calculate points + craft reward_reason. When the VN is in its
            # pool window, the reason names the kind (Monthly/Seasonal/Special).
            # Otherwise it's "As Normal VN" — the implicit catch-all for VNs
            # not currently curated.
            if read_in_pool_window:
                reward_points = is_monthly_points
                kind_label = {
                    "monthly": "Monthly",
                    "seasonal": "Seasonal",
                    "special": "Special",
                }.get(entry_status or "monthly", "Monthly")
                reward_reason = f"As {kind_label} VN"
            else:
                reward_points = await vn_info.get_points_not_monthly()
                reward_reason = "As Normal VN"

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

            # Snapshot badge state BEFORE the log insert so we can diff after
            # and announce any new unlocks. compute_user_badges is best-effort —
            # a failure here just means we skip the celebration line.
            try:
                from lib.badges import compute_user_badges, BADGE_BY_ID
                before_badges = await compute_user_badges(
                    self.bot, interaction.user.id, scope_guild_id=None
                )
            except Exception as e:  # noqa: BLE001
                _log.debug("badge before-snapshot failed: %s", e)
                before_badges = None

            # Add log to database and get the log_id. OR IGNORE +
            # partial unique index on (user_id, vndb_id, reward_month)
            # closes the TOCTOU gap between log_already_exists above
            # and this insert: if another /finish for the same key
            # raced past the pre-check, the constraint catches it and
            # log_id comes back as 0 (no row inserted on a fresh
            # connection's lastrowid).
            log_id = await self.bot.RUN_RETURNING_ID(
                DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
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
            if not log_id:
                # Race: a concurrent /finish for the same VN in the
                # same month inserted first. Same message as the
                # pre-check fast path so the UX is consistent.
                await interaction.followup.send(
                    f"You've already logged this VN for **{current_month}**. "
                    "Re-reads in a different month are fine."
                )
                return

            new_total_points = current_total_points + reward_points

            # Cache the jiten character_count synchronously (with a short
            # timeout) BEFORE the after-badge snapshot so character-count
            # badges can unlock on the same /finish that pushes the user
            # over a threshold. Falls back to fire-and-forget when slow.
            # Also captures the jiten info for the link button (deck id) and
            # the NSFW-cover fallback (cover url) below.
            jiten_info = None
            try:
                jiten_info = await asyncio.wait_for(
                    _cache_jiten_character_count(self.bot, vndb_id),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                # Re-launch as a background task so the cache still gets
                # populated eventually, even though this /finish won't see
                # the resulting badges.
                asyncio.create_task(
                    _cache_jiten_character_count(self.bot, vndb_id),
                    name=f"jiten-char-cache-late-{vndb_id}",
                )
            jiten_deck_id = jiten_info.deck_id if jiten_info else None

            # AFTER snapshot — set difference reveals newly-unlocked badges.
            newly_unlocked: list[str] = []
            if before_badges is not None:
                try:
                    after_badges = await compute_user_badges(
                        self.bot, interaction.user.id, scope_guild_id=None
                    )
                    new_ids = after_badges - before_badges
                    newly_unlocked = [
                        f"{BADGE_BY_ID[b].emoji} {BADGE_BY_ID[b].name}"
                        for b in new_ids
                        if b in BADGE_BY_ID
                    ]
                except Exception as e:  # noqa: BLE001
                    _log.debug("badge after-snapshot failed: %s", e)

            # Create embed with log_id
            embed = await EmbedBuilder.create_vn_completion_embed(
                interaction.user,
                vn_info,
                comment,
                current_total_points,
                new_total_points,
                rating,
                log_id,
                jiten_data=jiten_info,
            )

            # Create view with undo button and VNDB / jiten.moe link buttons
            vndb_url = await vn_info.get_vndb_link()
            view = UndoLogView(
                log_id, interaction.user.id, vndb_url, self.bot,
                jiten_deck_id=jiten_deck_id,
            )

            # Add a celebration line to the followup when this /finish unlocks
            # one or more new badges. Single line, comma-joined, no spam on
            # re-earns (set difference handles that).
            content_text: Optional[str] = None
            if newly_unlocked:
                content_text = "🎉 Earned: " + ", ".join(newly_unlocked)

            message = await interaction.followup.send(
                content=content_text, embed=embed, view=view,
            )
            view.message = message

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("Unexpected error in finish_vn")
            await handle_command_error(interaction, e, "An error occurred while processing your VN completion.")
            raise

    @app_commands.command(name="leaderboard", description="Show the leaderboard. Defaults to the current season.")
    @app_commands.describe(
        timeframe="Default scope when no specific month/season is given. Defaults to current season.",
        month="Optional: Filter by specific month (e.g., '2025-09'). Cannot be combined with season.",
        season="Optional: Filter by season (3-month range). Cannot be combined with month.",
        year="Optional: Year for the season filter. Defaults to the current calendar year.",
        server="Optional: Filter by specific server"
    )
    @app_commands.choices(season=SEASON_CHOICES, timeframe=LEADERBOARD_TIMEFRAME_CHOICES)
    @app_commands.autocomplete(
        month=month_autocomplete,
        year=year_autocomplete,
        server=server_autocomplete,
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        timeframe: app_commands.Choice[str] = None,
        month: str = None,
        season: app_commands.Choice[str] = None,
        year: int = None,
        server: str = None,
    ):
        await interaction.response.defer()

        try:
            # Conflict / well-formedness checks before any DB work.
            if month and season:
                await interaction.followup.send(
                    "❌ Pick either `month` or `season`, not both.",
                    ephemeral=True,
                )
                return
            if year is not None and season is None:
                await interaction.followup.send(
                    "❌ Pick a `season` too — `year` alone isn't a filter.",
                    ephemeral=True,
                )
                return

            # Resolve effective filters. Priority:
            #   explicit season > explicit month > timeframe (default current_season)
            # When the user passes nothing we fall back to current anime season —
            # that's the new default. `using_default_season` flags the implicit
            # case so we can still show a concrete label like "Spring 2026"
            # in the title rather than the abstract "current season".
            season_months: Optional[List[str]] = None
            season_label: Optional[str] = None
            # Track the (season_value, year) tuple so the SeasonNavLeaderboardView
            # can step backward / forward to adjacent seasons. Stays None when
            # the leaderboard is month-specific or all-time (no nav buttons
            # rendered in those modes).
            season_value: Optional[str] = None
            season_year: Optional[int] = None
            using_default_season = False
            use_all_time = False

            if season is not None:
                effective_year = year if year is not None else discord.utils.utcnow().year
                season_value = season.value
                season_year = effective_year
                season_months = season_to_months(season_value, season_year)
                season_label = await format_season_label(
                    self.bot, season_year, season_value
                )
            elif month:
                # Explicit month wins over timeframe default; no season filter.
                pass
            else:
                tf_value = timeframe.value if timeframe is not None else "current_season"
                if tf_value == "all_time":
                    use_all_time = True
                else:
                    using_default_season = True
                    cur_season, cur_year = current_anime_season()
                    season_value = cur_season
                    season_year = cur_year
                    season_months = season_to_months(cur_season, cur_year)
                    season_label = await format_season_label(
                        self.bot, cur_year, cur_season
                    )

            # Choose the appropriate query based on the resolved filters.
            if season_months is not None and server:
                results = await self.bot.GET(
                    DatabaseQueries.GET_LOGS_BY_SEASON_AND_SERVER,
                    (*season_months, int(server)),
                )
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                filter_description = f"for **{season_label}** in **{server_name}**"
            elif season_months is not None:
                results = await self.bot.GET(
                    DatabaseQueries.GET_LOGS_BY_SEASON,
                    tuple(season_months),
                )
                filter_description = f"for **{season_label}** (all servers)"
            elif month and server:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH_AND_SERVER, (month, int(server)))
                filter_description = f"for **{month}** in server"
            elif month:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH, (month,))
                filter_description = f"for **{month}** (all servers)"
            elif server:
                # All-time + server scope.
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_SERVER, (int(server),))
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                filter_description = f"for **{server_name}** (all time)"
            else:
                # All-time, all servers.
                results = await self.bot.GET(DatabaseQueries.GET_ALL_LOGS)
                filter_description = "(all time, all servers)"

            if not results:
                filter_msg = f" {filter_description}" if filter_description != "(all time, all servers)" else ""
                await interaction.followup.send(f"No reading logs found{filter_msg}.")
                return

            # Build leaderboard. _aggregate_leaderboard_rows centralizes the
            # points + completions bucketing and the username resolution so
            # the season-nav button handler can share it without drift.
            sorted_entries = await _aggregate_leaderboard_rows(self.bot, results)

            # Build a concrete period_label for the embed header.
            if season_months is not None and server:
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                period_label = f"{season_label} · {server_name}"
            elif season_months is not None:
                period_label = season_label
            elif month and server:
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                period_label = f"{month} · {server_name}"
            elif month:
                period_label = month
            elif server:
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
                period_label = f"All-Time · {server_name}"
            else:
                period_label = "All-Time"

            title = f"🏆 VN Club Leaderboard — {period_label}"

            # Create paginated view. When the leaderboard is season-scoped
            # (explicit /season: or default current season), the season-aware
            # subclass adds Prev/Next Season nav buttons; otherwise the plain
            # paginator is enough.
            if season_value is not None and season_year is not None:
                view = SeasonNavLeaderboardView(
                    leaderboard_data=sorted_entries,
                    title=title,
                    per_page=20,
                    period_label=period_label,
                    is_default_season=using_default_season,
                    bot=self.bot,
                    season_value=season_value,
                    season_year=season_year,
                    server_id=int(server) if server else None,
                )
            else:
                view = LeaderboardView(
                    leaderboard_data=sorted_entries,
                    title=title,
                    per_page=20,
                    period_label=period_label,
                    is_default_season=using_default_season,
                )

            embed = view.create_embed()
            await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            await handle_command_error(interaction, e)

    @app_commands.command(
        name="server_leaderboard",
        description="Server standings. Defaults to the current season.",
    )
    @app_commands.describe(
        timeframe="Default scope when no specific month/season is given. Defaults to current season.",
        month="Optional: Filter by specific month (e.g., '2025-09'). Cannot be combined with season.",
        season="Optional: Filter by season (3-month range). Cannot be combined with month.",
        year="Optional: Year for the season filter. Defaults to the current calendar year.",
    )
    @app_commands.choices(season=SEASON_CHOICES, timeframe=LEADERBOARD_TIMEFRAME_CHOICES)
    @app_commands.autocomplete(month=month_autocomplete, year=year_autocomplete)
    async def server_leaderboard(
        self,
        interaction: discord.Interaction,
        timeframe: app_commands.Choice[str] = None,
        month: str = None,
        season: app_commands.Choice[str] = None,
        year: int = None,
    ):
        await interaction.response.defer()

        # Conflict / well-formedness checks before any DB work.
        if month and season:
            await interaction.followup.send(
                "❌ Pick either `month` or `season`, not both.",
                ephemeral=True,
            )
            return
        if year is not None and season is None:
            await interaction.followup.send(
                "❌ Pick a `season` too — `year` alone isn't a filter.",
                ephemeral=True,
            )
            return

        # Resolve filters with the same precedence as /leaderboard:
        # explicit season > explicit month > timeframe (default current_season).
        season_months: Optional[List[str]] = None
        season_label: Optional[str] = None
        period_label: str
        empty_msg: str
        # Capture (season_value, season_year) when season-scoped so the nav
        # view can step adjacent. Stays None for month / all-time scopes.
        season_value_for_nav: Optional[str] = None
        season_year_for_nav: Optional[int] = None

        if season is not None:
            effective_year = year if year is not None else discord.utils.utcnow().year
            season_months = season_to_months(season.value, effective_year)
            season_label = await format_season_label(
                self.bot, effective_year, season.value
            )
            period_label = season_label
            season_value_for_nav = season.value
            season_year_for_nav = effective_year
        elif month:
            period_label = month
        else:
            tf_value = timeframe.value if timeframe is not None else "current_season"
            if tf_value == "all_time":
                period_label = "All-Time"
            else:
                cur_season, cur_year = current_anime_season()
                season_months = season_to_months(cur_season, cur_year)
                season_label = await format_season_label(
                    self.bot, cur_year, cur_season
                )
                period_label = season_label
                season_value_for_nav = cur_season
                season_year_for_nav = cur_year

        if season_months is not None:
            results = await self.bot.GET(
                DatabaseQueries.GET_LOGS_BY_SEASON, tuple(season_months)
            )
            empty_msg = f"No reading logs found for {period_label}."
        elif month:
            results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH, (month,))
            empty_msg = f"No reading logs found for {month}."
        else:
            results = await self.bot.GET(DatabaseQueries.GET_ALL_LOGS)
            empty_msg = "No reading logs found."

        if not results:
            await interaction.followup.send(empty_msg)
            return

        embed = await _build_server_standings_embed(
            self.bot, period_label, results,
        )
        if embed is None:
            await interaction.followup.send("No server data found.")
            return

        if season_value_for_nav is not None and season_year_for_nav is not None:
            view = SeasonNavServerStandingsView(
                self.bot, season_value_for_nav, season_year_for_nav,
            )
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="profile", description="View user statistics and profile.")
    @app_commands.describe(
        user="The user whose profile you want to view (can be a mention or user ID).",
        embed="Also send the legacy text embed (off by default; image card is the default).",
    )
    async def user_profile(
        self,
        interaction: discord.Interaction,
        user: discord.User = None,
        embed: bool = False,
    ):
        await interaction.response.defer()

        if user is None:
            user = interaction.user

        # Get basic user statistics
        stats_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_STATS, (user.id,))
        if not stats_result:
            await interaction.followup.send(f"No data found for {user.name}.")
            return

        total_entries, total_points, monthly_entries, vn_entries = stats_result

        if total_entries == 0:
            await interaction.followup.send(
                f"{user.name} hasn't logged any finished VNs yet. Use /finish to log your first one!"
            )
            return

        # Get most active server
        most_active_server_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_MOST_ACTIVE_SERVER, (user.id,))
        most_active_server = "Unknown"
        most_active_count = 0
        if most_active_server_result:
            guild_id, entry_count = most_active_server_result
            most_active_count = entry_count
            guild = self.bot.get_guild(guild_id)
            most_active_server = guild.name if guild else f"Server {guild_id}"

        # Get recent activity (last 12 months)
        recent_activity = await self.bot.GET(DatabaseQueries.GET_USER_RECENT_ACTIVITY, (user.id,))

        # Most-recent /finish for "last log" subtitle. completed_at is stored
        # as a CURRENT_TIMESTAMP string ("YYYY-MM-DD HH:MM:SS"); slice the
        # date portion. None for users without any logs (caller already
        # short-circuits earlier if total_entries == 0, so practically
        # always populated by the time we hit the renderer).
        last_log_row = await self.bot.GET_ONE(DatabaseQueries.GET_USER_LAST_LOG, (user.id,))
        last_log: Optional[str] = None
        if last_log_row and last_log_row[0]:
            last_log = str(last_log_row[0])[:10]

        # Reading-streak metric — longest consecutive-month run in the user's
        # full log history (unbounded, unlike the 12-cap chart query) so
        # a long-time member's best streak isn't artificially clipped.
        log_month_rows = await self.bot.GET(
            DatabaseQueries.GET_USER_LOG_MONTHS, (user.id,)
        )
        streak_months = 0
        if log_month_rows:
            try:
                ms = sorted(
                    datetime.strptime(str(r[0]), "%Y-%m") for r in log_month_rows
                )
                longest = current = 1
                for prev, curr in zip(ms, ms[1:]):
                    gap = (curr.year - prev.year) * 12 + (curr.month - prev.month)
                    if gap == 1:
                        current += 1
                        longest = max(longest, current)
                    else:
                        current = 1
                streak_months = longest
            except (ValueError, TypeError):
                streak_months = 0

        # Get user's average rating
        avg_rating_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_AVERAGE_RATING, (user.id,))
        average_rating = 0.0
        rating_count = 0
        if avg_rating_result and avg_rating_result[0] is not None:
            average_rating = avg_rating_result[0]
            rating_count = avg_rating_result[1]

        # Calculate additional statistics
        non_monthly_entries = vn_entries - monthly_entries

        # Generate the image profile card (default output)
        try:
            from lib.profile_card import ProfileCardGenerator
            from lib.badges import BADGE_BY_ID, BADGE_DEFS, compute_user_badges

            avatar_url = user.display_avatar.replace(format="png", size=512).url
            joined_at = getattr(user, "joined_at", None)
            member_since = joined_at.strftime("%Y-%m-%d") if joined_at else None
            display_name = getattr(user, "display_name", user.name) or user.name

            # Badges strip on the profile card. We compute the unlocked set
            # globally (matches /badges semantics) and surface up to 3
            # "latest" names — sorted by BADGE_DEFS order so the highest-tier
            # / most-impressive unlocks bubble up. Failure is non-fatal: skip
            # the strip rather than fail the whole card.
            badge_summary: Optional[Tuple[int, int, List[str]]] = None
            try:
                unlocked_set = await compute_user_badges(self.bot, user.id, scope_guild_id=None)
                ordered_unlocked = [b for b in BADGE_DEFS if b.id in unlocked_set]
                latest_names = [b.name for b in reversed(ordered_unlocked)][:3]
                badge_summary = (len(unlocked_set), len(BADGE_DEFS), latest_names)
            except Exception as e:  # noqa: BLE001
                _log.debug("badge summary skipped on profile card: %s", e)

            # Voting stats — pluck from aggregate_user_stats (already used by
            # the badge system).
            voting_stats: Optional[dict] = None
            try:
                from lib.badges import aggregate_user_stats
                agg = await aggregate_user_stats(self.bot, user.id, scope_guild_id=None)
                voting_stats = {
                    "votes_cast": int(agg.get("votes_cast", 0)),
                    "nominations_made": int(agg.get("nominations_made", 0)),
                    "tastemaker_wins": int(agg.get("tastemaker_wins", 0)),
                }
            except Exception as e:  # noqa: BLE001
                _log.debug("voting_stats fetch failed: %s", e)

            # Dual-axis ranking: current season vs all-time x server vs global.
            ranks: Optional[dict] = None
            try:
                season_value, season_year = current_anime_season()
                season_months = season_to_months(season_value, season_year)
                # Plain "Spring 2026" (no Season N suffix) on the profile
                # card specifically — the rank row's period column is only
                # 130*S wide, so "Spring 2026 · Season 4" bleeds into the
                # adjacent server-rank column. Other commands still get the
                # full season-numbered label since they have horizontal room.
                season_label = f"{season_value.capitalize()} {season_year}"
                guild_id = interaction.guild.id if interaction.guild else None

                # Build the four query specs. server queries skipped in DM.
                tasks = {}
                tasks["cs_global"] = _user_rank_in(
                    self.bot, user.id,
                    DatabaseQueries.GET_LOGS_BY_SEASON, tuple(season_months),
                )
                tasks["at_global"] = _user_rank_in(
                    self.bot, user.id, DatabaseQueries.GET_ALL_LOGS, (),
                )
                if guild_id is not None:
                    tasks["cs_server"] = _user_rank_in(
                        self.bot, user.id,
                        DatabaseQueries.GET_LOGS_BY_SEASON_AND_SERVER,
                        (*season_months, guild_id),
                    )
                    tasks["at_server"] = _user_rank_in(
                        self.bot, user.id,
                        DatabaseQueries.GET_LOGS_BY_SERVER, (guild_id,),
                    )

                results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                resolved = dict(zip(tasks.keys(), results))
                # Replace exceptions with None so the renderer treats them as off-board.
                for k, v in resolved.items():
                    if isinstance(v, Exception):
                        _log.debug("rank query %s failed: %s", k, v)
                        resolved[k] = None

                ranks = {
                    "season_label": season_label,
                    "current_season": {
                        "server": resolved.get("cs_server"),
                        "global": resolved.get("cs_global"),
                    },
                    "all_time": {
                        "server": resolved.get("at_server"),
                        "global": resolved.get("at_global"),
                    },
                }
                # If we never queried server (DM), drop the 'server' keys so
                # the renderer skips the slot entirely instead of showing dash.
                if guild_id is None:
                    ranks["current_season"].pop("server", None)
                    ranks["all_time"].pop("server", None)
            except Exception as e:  # noqa: BLE001
                _log.debug("ranks fetch failed: %s", e)

            async with ProfileCardGenerator() as gen:
                card_buf = await gen.generate(
                    username=user.name,
                    display_name=display_name,
                    avatar_url=avatar_url,
                    total_points=total_points or 0,
                    vn_entries=vn_entries,
                    monthly_entries=monthly_entries,
                    average_rating=average_rating,
                    rating_count=rating_count,
                    most_active_server=most_active_server,
                    most_active_count=most_active_count,
                    recent_activity=recent_activity or [],
                    member_since=member_since,
                    last_log=last_log,
                    streak_months=streak_months,
                    badge_summary=badge_summary,
                    voting_stats=voting_stats,
                    ranks=ranks,
                )
            file = discord.File(card_buf, filename=f"profile-{user.id}.png")

            if embed:
                # Caller asked for the legacy embed too; send both in one message.
                profile_embed = EmbedBuilder.create_user_profile_embed(
                    user,
                    total_entries,
                    total_points,
                    monthly_entries,
                    vn_entries,
                    most_active_server,
                    most_active_count,
                    recent_activity,
                    average_rating,
                    rating_count,
                )
                await interaction.followup.send(file=file, embed=profile_embed)
            else:
                await interaction.followup.send(file=file)
        except Exception:
            _log.exception("profile card generation failed; falling back to embed")
            profile_embed = EmbedBuilder.create_user_profile_embed(
                user,
                total_entries,
                total_points,
                monthly_entries,
                vn_entries,
                most_active_server,
                most_active_count,
                recent_activity,
                average_rating,
                rating_count,
            )
            await interaction.followup.send(embed=profile_embed)

    @app_commands.command(name="logs", description="View your reading logs.")
    @app_commands.describe(user="The user whose logs you want to view (can be a mention or user ID).")
    async def user_logs(
        self, interaction: discord.Interaction, user: discord.User = None
    ):
        await interaction.response.defer()

        if user is None:
            user = interaction.user

        results = await self.bot.GET(DatabaseQueries.GET_USER_LOGS, (user.id,))
        if not results:
            await interaction.followup.send(f"No reading logs found for {user.name}.")
            return

        # Process logs into formatted strings
        log_entries = []
        for row in results:
            (
                log_id,
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
                display_comment = comment or 'No comment provided.'

                if vn_info:
                    link = await vn_info.get_vndb_link()
                    # Prioritize Japanese title, fallback to English title, or generic text if both are empty
                    display_title = vn_info.title_ja or vn_info.title_en or "View on VNDB"
                    log_entry = (
                        f"`#{log_id}` **{reward_month}**: [{display_title}]({link}) - {points}点 ({reward_reason})\n"
                        f"Comment: {display_comment} | Rating: {user_rating or 'No rating provided.'}/5"
                    )
                else:
                    # VN info failed to load - show vndb_id as fallback
                    log_entry = (
                        f"`#{log_id}` **{reward_month}**: {vndb_id} - {points}点 ({reward_reason})\n"
                        f"Comment: {display_comment} | Rating: {user_rating or 'No rating provided.'}/5"
                    )
            else:
                # Display full comment for non-VN entries too
                display_comment = comment or 'No comment provided.'

                log_entry = (
                    f"`#{log_id}` **{reward_month}**: No VN specified - {points}点 ({reward_reason})\n"
                    f"Comment: {display_comment}"
                )

            log_entries.append(log_entry)

        # Create paginated view for logs (5 per page)
        combined_description = "\n\n".join(log_entries)
        
        # If we have 5 or fewer logs AND the combined description fits in Discord's limit, show all at once
        if len(log_entries) <= 5 and len(combined_description) <= 4090:
            # Show all logs without pagination
            embed = discord.Embed(
                title=f"📚 Reading Logs for {user.name}", color=discord.Color.blue()
            )
            embed.set_author(name=user.name, icon_url=user.display_avatar.url)

            embed.description = combined_description
            embed.set_footer(text=f"{len(log_entries)} total logs")
            await interaction.followup.send(embed=embed)
        else:
            # Use pagination for more than 5 logs OR if description is too long
            view = ReadingLogsView(log_entries, user, per_page=5)
            embed = view.create_embed()
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="manage_reward_points", description="Reward user with points (admin).")
    @app_commands.describe(
        member="The member to reward points to.",
        points="The number of points to reward.",
        reason="The reason for the points reward.",
    )
    @app_commands.guild_only()
    async def manage_reward_points(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        points: app_commands.Range[int, 1, 10_000],
        reason: app_commands.Range[str, 1, 2000],
    ):
        # Ephemeral defer so non-admins don't see a "(Admin) is
        # thinking…" indicator. The actual followup ("Rewarded N
        # points to @user") stays public so the channel sees the
        # award itself.
        await interaction.response.defer(ephemeral=True)

        # validate_user_permission raises ValidationError on denial;
        # the global on_application_command_error handler unwraps the
        # BotError and surfaces user_message ephemerally. No `if not`
        # check needed — the call alone gates the rest of the function.
        await validate_user_permission(interaction)

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

    @app_commands.command(
        name="manage_log",
        description="Record a VN completion on behalf of another user (admin).",
    )
    @app_commands.describe(
        member="The member to log a completion for.",
        title="Search for a VN by title (autocomplete).",
        rating="Their rating for the VN (1=Terrible, 5=Masterpiece).",
        comment="The comment/review to attach to the log.",
        reward_month="Optional override (YYYY-MM). Defaults to current month.",
        points="Optional override. Defaults to the same pool-window calculation /finish uses.",
    )
    @app_commands.autocomplete(
        title=vn_autocomplete,
        reward_month=month_picker_past_autocomplete,
    )
    @app_commands.choices(rating=RATING_CHOICES)
    @app_commands.guild_only()
    async def manage_log(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        title: str,
        rating: int,
        comment: app_commands.Range[str, 1, 2000],
        reward_month: Optional[str] = None,
        points: Optional[app_commands.Range[int, 0, 10_000]] = None,
    ):
        """Admin-only manual log insertion. Mirrors `/finish`'s points logic
        so admin-backfilled logs award the same amount a self-finish would.
        """
        await interaction.response.defer()
        try:
            await validate_user_permission(interaction)
            await validate_rating_input(rating)

            vndb_id = await resolve_vn_from_input(title)
            if not vndb_id:
                raise ValidationError(
                    "couldn't resolve VN",
                    "Could not determine VN from input. Pick from the autocomplete dropdown.",
                )

            vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
            if not vn_info:
                raise ValidationError(
                    f"VNDB ID {vndb_id} not found",
                    "VNDB ID not found or invalid.",
                )

            # Resolve reward_month (default: current). validate_month_input
            # raises ValidationError on bad format, which the outer catch
            # translates to a friendly message.
            from lib.utils import validate_month_input
            effective_month = await validate_month_input(interaction, reward_month) \
                if reward_month else get_current_month()

            # Reject same-month duplicates only. The same VN can be re-logged
            # for a different reward_month (re-reads), matching /finish's
            # per-month rule and how the pool can hold multiple entries for
            # the same VN across periods.
            existing = await self.bot.GET_ONE(
                DatabaseQueries.GET_USER_VN_LOG_FOR_MONTH,
                (member.id, vndb_id, effective_month),
            )
            if existing:
                raise ValidationError(
                    f"log already exists for user={member.id} vndb={vndb_id} "
                    f"month={effective_month}",
                    f"{member.mention} already has a log for this VN in "
                    f"**{effective_month}**. Pass a different `reward_month` "
                    "to log a re-read, or use `/log_edit` to update the "
                    "existing one.",
                )

            # Compute points the same way /finish does: pool-window match
            # awards the configured is_monthly_points; otherwise the VN's
            # not-monthly fallback. Admin can override with explicit `points`.
            if points is None:
                pool_match = await get_single_monthly_vn(
                    interaction.client, vndb_id,
                    guild_id=interaction.guild.id if interaction.guild else None,
                )
                entry_status = None
                if pool_match:
                    _, start_month, end_month, is_monthly_points, entry_status = pool_match
                    in_window = is_month_in_range(effective_month, start_month, end_month)
                else:
                    in_window = False

                if in_window:
                    reward_points = is_monthly_points
                    kind_label = {
                        "monthly": "Monthly",
                        "seasonal": "Seasonal",
                        "special": "Special",
                    }.get(entry_status or "monthly", "Monthly")
                    reward_reason = f"As {kind_label} VN (admin backfill)"
                else:
                    reward_points = await vn_info.get_points_not_monthly()
                    reward_reason = "As Normal VN (admin backfill)"
            else:
                reward_points = int(points)
                reward_reason = "Admin backfill"

            # OR IGNORE pairs with the partial unique index on
            # (user_id, vndb_id, reward_month) to make backfills
            # idempotent — admin re-running the same /manage_log
            # against an already-logged month resolves to a no-op
            # instead of stacking duplicate rows.
            log_id = await self.bot.RUN_RETURNING_ID(
                DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
                (
                    member.id,
                    vndb_id,
                    rating,
                    reward_reason,
                    effective_month,
                    reward_points,
                    comment,
                    interaction.guild.id,
                ),
            )
            if not log_id:
                await interaction.followup.send(
                    f"ℹ️ {member.mention} already has a log for this VN in "
                    f"**{effective_month}** — nothing to add. (Re-reads in a "
                    f"different month would be fine.)",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            _log.info(
                "Admin %s (%s) backfilled log #%s for user %s (%s) — "
                "vndb=%s rating=%s month=%s points=%s",
                interaction.user.name, interaction.user.id, log_id,
                member.name, member.id, vndb_id, rating, effective_month, reward_points,
            )

            display_title = vn_info.title_ja or vn_info.title_en or vndb_id
            await interaction.followup.send(
                f"✅ Recorded **{display_title}** for {member.mention} as log "
                f"`#{log_id}` · {reward_month or effective_month} · "
                f"**{reward_points}**点 · ⭐ {rating}/5\n"
                f"_Reason: {reward_reason}_"
            )
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/manage_log failed")
            await handle_command_error(
                interaction, e,
                "An error occurred while recording the log.",
            )

    @app_commands.command(name="log_undo", description="Delete a reading log.")
    @app_commands.describe(
        log_id="The ID of the log to delete.",
    )
    @app_commands.autocomplete(log_id=user_logs_autocomplete)
    @app_commands.guild_only()
    async def delete_log(
        self, interaction: discord.Interaction, log_id: int
    ):
        await interaction.response.defer()

        # Check if the log exists
        result = await self.bot.GET_ONE(DatabaseQueries.GET_LOG_BY_ID, (log_id,))
        if not result:
            await interaction.followup.send("❌ Log not found.")
            return

        (
            user_id,
            vndb_id,
            _user_rating,  # Not used in delete
            reward_reason,
            reward_month,
            points,
            comment,
            logged_in_guild,
        ) = result

        # Check permissions: user can delete their own logs, or admins can delete any log
        is_own_log = user_id == interaction.user.id
        if not is_own_log:
            await validate_user_permission(interaction, "You can only delete your own logs.")
            # Per-guild VN managers are scoped to their own server's logs.
            # AUTHORIZED_USERS (bot operators) bypass this check.
            require_same_guild(interaction, logged_in_guild, entity_name="log")

        # Get VN title for display if available
        display_title = vndb_id or "N/A"
        if vndb_id:
            vn_info = await from_vndb_id(self.bot, vndb_id)
            if vn_info:
                display_title = vn_info.title_ja or vn_info.title_en or vndb_id

        # Delete the log
        deleted_by = "owner" if is_own_log else "admin"
        _log.info(
            f"Log #{log_id} deleted by {deleted_by} {interaction.user.name} ({interaction.user.id}) - "
            f"Log owner: {user_id}, VNDB ID: {vndb_id}, Reward Reason: {reward_reason}, "
            f"Reward Month: {reward_month}, Points: {points}"
        )
        await self.bot.RUN(DatabaseQueries.DELETE_LOG_BY_ID, (log_id,))

        # Truncate all display values to ensure message doesn't exceed Discord's 2000 char limit
        display_title = truncate_text(display_title, 200)
        display_reason = truncate_text(reward_reason or "N/A", 200)
        display_comment = truncate_text(comment or 'No comment provided.', 500)

        try:
            await interaction.followup.send(
                f"✅ Deleted log #{log_id} for <@{user_id}>:\n"
                f"**Title:** {display_title}\n"
                f"**Reward Reason:** {display_reason}\n"
                f"**Reward Month:** {reward_month}\n"
                f"**Points:** {points}\n"
                f"**Comment:** {display_comment}"
            )
        except discord.HTTPException as e:
            if e.code == 50035:  # Invalid Form Body (message too long)
                # Fallback with minimal information
                _log.warning("Discord message length error in delete_log (log #%s): %s", log_id, e)
                await interaction.followup.send(
                    f"✅ Deleted log #{log_id} for <@{user_id}>.\n"
                    f"Title: {display_title} | Month: {reward_month} | Points: {points}"
                )
            else:
                # Re-raise other HTTP exceptions
                raise
        except Exception:
            _log.exception("Unexpected error in delete_log")
            raise

    @app_commands.command(name="log_edit", description="Edit a reading log's comment or rating.")
    @app_commands.describe(
        log_id="The ID of the log to edit.",
        comment="New comment (max 2000 characters). Leave empty to keep current.",
        rating="New rating (1-5). Leave empty to keep current.",
    )
    @app_commands.autocomplete(log_id=user_logs_autocomplete)
    @app_commands.choices(rating=RATING_CHOICES)
    @app_commands.guild_only()
    async def log_edit(
        self,
        interaction: discord.Interaction,
        log_id: int,
        comment: Optional[app_commands.Range[str, 1, 2000]] = None,
        rating: int = None,
    ):
        await interaction.response.defer()

        try:
            # Check if at least one field is being updated
            if comment is None and rating is None:
                await interaction.followup.send("❌ You must provide at least a new comment or rating to update.")
                return

            # Check if the log exists
            result = await self.bot.GET_ONE(DatabaseQueries.GET_LOG_BY_ID, (log_id,))
            if not result:
                await interaction.followup.send("❌ Log not found.")
                return

            (
                user_id,
                vndb_id,
                current_rating,
                reward_reason,
                reward_month,
                points,
                current_comment,
                logged_in_guild,
            ) = result

            # Owners can always edit their own logs; admins can override
            # via validate_user_permission. Mirrors /log_undo (~line 1398).
            if user_id != interaction.user.id:
                await validate_user_permission(
                    interaction,
                    "Only the log owner or an admin can edit this log.",
                )
                # Per-guild VN managers are scoped to their own server's logs.
                # AUTHORIZED_USERS (bot operators) bypass this check.
                require_same_guild(interaction, logged_in_guild, entity_name="log")

            # Use current values for fields not being updated
            new_comment = comment if comment is not None else current_comment
            new_rating = rating if rating is not None else current_rating

            # Update the log
            await self.bot.RUN(
                DatabaseQueries.UPDATE_LOG_COMMENT_RATING,
                (new_comment, new_rating, log_id),
            )

            # Log the edit
            edit_details = []
            if comment is not None:
                edit_details.append(f"comment changed")
            if rating is not None:
                edit_details.append(f"rating: {current_rating} -> {rating}")
            _log.info(
                f"Log #{log_id} edited by {interaction.user.name} ({interaction.user.id}) - "
                f"VNDB ID: {vndb_id}, Changes: {', '.join(edit_details)}"
            )

            # Build response message
            updates = []
            if comment is not None:
                updates.append(f"**Comment:** {truncate_text(comment, 200)}")
            if rating is not None:
                updates.append(f"**Rating:** {rating}/5")

            await interaction.followup.send(
                f"✅ Updated log #{log_id}:\n" + "\n".join(updates)
            )

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("Unexpected error in log_edit")
            await handle_command_error(interaction, e, "An error occurred while editing the log.")
            raise

    @app_commands.command(name="ratings", description="View ratings for a VN.")
    @app_commands.describe(
        title="Search for a VN by title (type at least 2 characters)."
    )
    @app_commands.autocomplete(title=vn_autocomplete)
    async def ratings(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer()

        try:
            # Resolve VN ID from various input formats (autocomplete value, display format, raw ID)
            vndb_id = await resolve_vn_from_input(title)
            if not vndb_id:
                raise ValidationError("Could not determine VN from input. Please try selecting from the autocomplete dropdown.")

            # Get VN info
            vn_info: VN_Entry = await from_vndb_id(self.bot, vndb_id)
            if not vn_info:
                raise ValidationError(
                    f"VNDB lookup failed for {vndb_id}",
                    "Couldn't fetch that VN from VNDB. VNDB is most likely "
                    "temporarily unreachable — try again in a moment.",
                )

            # Get all ratings for this VN
            ratings = await self.bot.GET(DatabaseQueries.GET_ALL_VN_RATINGS, (vn_info.vndb_id,))

            if not ratings:
                display_title = vn_info.title_ja or vn_info.title_en or vn_info.vndb_id
                await interaction.followup.send(f"No ratings found for **{display_title}**.")
                return

            # Process ratings into formatted strings.
            # A user can have multiple rows (re-reads in different reward_months)
            # — each is a distinct rating event. Pre-count rows per user so we
            # only annotate the month when there's ambiguity (single-rating
            # users don't need the month tag cluttering their entry).
            user_rating_counts: dict[int, int] = {}
            for uid, *_ in ratings:
                user_rating_counts[uid] = user_rating_counts.get(uid, 0) + 1

            rating_entries = []
            total_ratings = 0
            total_score = 0

            for user_id, user_rating, comment, reward_month in ratings:
                user_name = await get_username_db(self.bot, user_id)

                total_ratings += 1
                total_score += user_rating

                stars = "⭐" * user_rating
                month_tag = (
                    f" · *{reward_month}*"
                    if user_rating_counts.get(user_id, 1) > 1
                    else ""
                )
                rating_entry = f"**{user_name}**: {user_rating}/5 {stars}{month_tag}"

                if comment:
                    # Show the full comment; pagination keeps page size manageable
                    # and create_embed truncates the combined page if it would
                    # overflow Discord's 4096-char description limit.
                    rating_entry += f"\n*\"{comment}\"*"

                rating_entries.append(rating_entry)

            # Calculate average rating
            average_rating = total_score / total_ratings if total_ratings > 0 else 0

            jiten_deck_id: Optional[int] = None
            jiten_data = None
            try:
                async with JitenClient() as jiten:
                    jiten_data = await jiten.get_by_vndb_id(vn_info.vndb_id)
                if jiten_data:
                    jiten_deck_id = jiten_data.deck_id
            except Exception as e:  # noqa: BLE001
                _log.warning("jiten lookup failed for %s: %s", vn_info.vndb_id, e)

            # Swap an NSFW VNDB cover for the guaranteed-SFW jiten cover when
            # available; otherwise keep the existing hide-on-NSFW behavior.
            display_cover_url, display_is_nsfw = resolve_display_cover(vn_info, jiten_data)
            display_thumb = (
                display_cover_url if (display_cover_url and not display_is_nsfw) else None
            )

            # If we have 5 or fewer ratings, show all at once without pagination.
            # 5 chosen so each rating gets meaningful breathing room within
            # Discord's 4096-char embed description (one full-cap 2000-char
            # comment alone could fill half of it).
            if len(rating_entries) <= 5:
                display_title = vn_info.title_ja or vn_info.title_en or "VN"
                description = (
                    f"Average Rating: **{average_rating:.1f}/5** ⭐ ({total_ratings} ratings)\n\n"
                    + "\n\n".join(rating_entries)
                )
                # Defensive cap — a few full-cap comments stacked together can
                # exceed Discord's 4096-char description limit.
                if len(description) > MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER:
                    description = description[:MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER - 3] + "..."
                embed = create_base_embed(
                    title=f"⭐ User Ratings for **{display_title}**",
                    description=description,
                    color=discord.Color.blue()
                )

                # display_thumb already holds the jiten cover when the VNDB one is NSFW (see above)
                if display_thumb:
                    embed.set_thumbnail(url=display_thumb)

                embed.set_footer(text=f"{len(rating_entries)} total ratings")
                view = build_vn_links_view(vn_info.vndb_id, jiten_deck_id)
                await interaction.followup.send(embed=embed, view=view)
            else:
                # Use pagination for more than 5 ratings
                display_title = vn_info.title_ja or vn_info.title_en or "VN"
                view = VNRatingsView(
                    rating_entries, display_title, average_rating, total_ratings,
                    per_page=5, thumbnail_url=display_thumb,
                )
                embed = view.create_embed()

                # Stack VNDB / jiten link buttons alongside the pagination row.
                view.add_item(discord.ui.Button(
                    label="VNDB",
                    style=discord.ButtonStyle.link,
                    url=f"https://vndb.org/{vn_info.vndb_id}",
                ))
                if jiten_deck_id is not None:
                    view.add_item(discord.ui.Button(
                        label="jiten.moe",
                        style=discord.ButtonStyle.link,
                        url=f"https://jiten.moe/decks/media/{jiten_deck_id}/detail",
                    ))

                await interaction.followup.send(embed=embed, view=view)

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            await handle_command_error(interaction, e, "An error occurred while fetching ratings.")


async def setup(bot: VNClubBot):
    await bot.add_cog(VNUserCommands(bot))
