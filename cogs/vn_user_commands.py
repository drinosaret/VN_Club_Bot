import asyncio
import discord
import discord.app_commands as app_commands
import json
import logging
from datetime import datetime
from typing import List, Optional, Tuple
from discord.ext import commands
from lib import style
from lib.autocomplete import HELP_JSON_PATH
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, VN_Entry
from lib.jiten_client import JitenClient, JitenInfo
from lib.pagination import BasePaginationView, GenericPaginationView, PAGE_ROWS
from lib.utils import (
    link_label,
    send_error,
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
    handle_command_error,
    truncate_text,
    inert_text,
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
    is_manager,
)
from lib.embeds import EmbedBuilder
from lib.autocomplete import vn_autocomplete, user_logs_autocomplete, month_autocomplete, month_picker_past_autocomplete, year_autocomplete, server_autocomplete, help_command_autocomplete
from lib.ratings import (
    DEFAULT_SCALE,
    SCALE_LABELS,
    format_rating,
    get_saved_scale,
    get_user_scale,
    mark_scale_notice_seen,
    needs_scale_notice,
    set_user_scale,
    validate_rating,
)
from lib.vndb_links import build_vndb_profile_view, get_link
from lib.visibility import PUBLIC_OPTION_HELP, defer_reply
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
    app_commands.Choice(name="Winter (Jan to Mar)", value="winter"),
    app_commands.Choice(name="Spring (Apr to Jun)", value="spring"),
    app_commands.Choice(name="Summer (Jul to Sep)", value="summer"),
    app_commands.Choice(name="Fall (Oct to Dec)",   value="fall"),
]


LEADERBOARD_TIMEFRAME_CHOICES = [
    app_commands.Choice(name="Current season (default)", value="current_season"),
    app_commands.Choice(name="All-time", value="all_time"),
]


# ==================== VIEW CLASSES ====================


# Display order, label and one-line summary for each help category. Ordering
# lives in code (not JSON) so presentation can change without touching
# content. Categories not listed here are grouped under "Other" so a mistyped
# category in the JSON still shows up rather than silently disappearing.
_HELP_CATEGORY_ORDER = [
    ("club",    "🌸 VN Club", "Log reads, ratings, profiles, leaderboards, settings"),
    ("vndb",    "🔗 VNDB", "Look up VNs, link your VNDB account, VNDB rankings"),
    ("picks",   "🗓️ Picks & voting", "This server's picks, banners, themes and votes"),
    ("manager", "🛠️ Manager", "Tools for server managers"),
]
_HELP_CATEGORY_LABELS = {key: label for key, label, _ in _HELP_CATEGORY_ORDER}
_HELP_OTHER = ("other", "📂 Other", "Everything else")
# Categories left off the home screen: /help describing itself adds nothing.
_HELP_HIDDEN_CATEGORIES = {"help"}
# Discord caps a select at 25 options.
HELP_SELECT_CAP = 25
HELP_PRIVATE_NOTE = "Replies are private except in open channels. Managers can add public: True to post one."


def _help_categories(help_data: list, include_manager: bool) -> list[tuple[str, str, str, list]]:
    """(key, label, summary, commands) for every category that has commands,
    in display order. Manager tools are listed only for managers."""
    grouped: dict[str, list] = {}
    for cmd in help_data:
        key = cmd.get("category") or "other"
        if key not in _HELP_CATEGORY_LABELS and key not in _HELP_HIDDEN_CATEGORIES:
            key = "other"
        grouped.setdefault(key, []).append(cmd)
    out = []
    for key, label, summary in [*_HELP_CATEGORY_ORDER, _HELP_OTHER]:
        if key == "manager" and not include_manager:
            continue
        if grouped.get(key):
            out.append((key, label, summary, grouped[key]))
    return out


def _build_help_home_embed(categories: list) -> discord.Embed:
    """A few lines: one per category. The command lists live one level down."""
    lines = [
        f"**{label}**{style.SEP}{summary} ({len(cmds)})"
        for _key, label, summary, cmds in categories
    ]
    embed = create_base_embed(
        title=style.title("❓", "Hikaru commands"),
        description=(
            "\n".join(lines)
            + "\n\nPick a category below, or use `/help command:<name>`."
        ),
    )
    embed.set_footer(text=HELP_PRIVATE_NOTE)
    return embed


def _build_help_category_embed(label: str, cmds: list) -> discord.Embed:
    lines = [f"**{c['name']}**{style.SEP}{c.get('short_description') or ''}" for c in cmds]
    embed = create_base_embed(
        title=label,
        description="\n".join(lines) + "\n\nPick a command below for full detail.",
    )
    embed.set_footer(text=HELP_PRIVATE_NOTE)
    return embed


def _build_help_detail_embed(cmd: dict) -> discord.Embed:
    """Full detail embed for a single command: usage, description, params, example."""
    embed = create_base_embed(
        title=style.title("❓", cmd['name']),
        description=cmd.get("description") or "",
    )
    embed.add_field(name="Usage", value=f"`{cmd.get('usage', '')}`", inline=False)
    if cmd.get("parameters"):
        embed.add_field(name="Parameters", value=cmd["parameters"], inline=False)
    if cmd.get("example"):
        embed.add_field(name="Example", value=f"`{cmd['example']}`", inline=False)
    embed.set_footer(text=HELP_PRIVATE_NOTE)
    return embed


def _find_help_entry(help_data: list, name: str) -> Optional[dict]:
    """Case-insensitive lookup that ignores leading slashes."""
    needle = (name or "").strip().lower().lstrip("/")
    for cmd in help_data:
        if (cmd.get("name") or "").lstrip("/").lower() == needle:
            return cmd
    return None


class HelpHomeView(discord.ui.View):
    """Category picker. Every screen edits the same private message in place,
    so browsing help never posts anything new to the channel."""

    def __init__(self, help_data: list, include_manager: bool):
        super().__init__(timeout=300)
        self._help_data = help_data
        self._include_manager = include_manager
        self._categories = _help_categories(help_data, include_manager)
        select = discord.ui.Select(
            placeholder="Pick a category…",
            options=[
                discord.SelectOption(label=label, value=key, description=summary[:100])
                for key, label, summary, _cmds in self._categories
            ][:HELP_SELECT_CAP],
        )
        select.callback = self._on_pick
        self.add_item(select)

    def create_embed(self) -> discord.Embed:
        return _build_help_home_embed(self._categories)

    async def _on_pick(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        for cat_key, label, _summary, cmds in self._categories:
            if cat_key == key:
                view = HelpCategoryView(self._help_data, self._include_manager, label, cmds)
                await interaction.response.edit_message(
                    embed=_build_help_category_embed(label, cmds), view=view,
                )
                return
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


class HelpCategoryView(discord.ui.View):
    """One category's commands, a picker for full detail, and a way back."""

    def __init__(self, help_data: list, include_manager: bool, label: str, cmds: list):
        super().__init__(timeout=300)
        self._help_data = help_data
        self._include_manager = include_manager
        self._label = label
        self._cmds = cmds
        select = discord.ui.Select(
            placeholder="Pick a command for full detail…",
            options=[
                discord.SelectOption(
                    label=c["name"][:100], value=c["name"],
                    description=(c.get("short_description") or "")[:100] or None,
                )
                for c in cmds[:HELP_SELECT_CAP]
            ],
        )
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction):
        cmd = _find_help_entry(self._help_data, interaction.data["values"][0])
        if not cmd:
            await interaction.response.edit_message(
                embed=_build_help_category_embed(self._label, self._cmds), view=self,
            )
            return
        back = HelpBackView(
            lambda: HelpCategoryView(self._help_data, self._include_manager, self._label, self._cmds),
            lambda: _build_help_category_embed(self._label, self._cmds),
            label=f"‹ {self._label}",
        )
        await interaction.response.edit_message(embed=_build_help_detail_embed(cmd), view=back)

    @discord.ui.button(label="‹ Categories", style=discord.ButtonStyle.secondary, row=1)
    async def back_home(self, interaction: discord.Interaction, _button: discord.ui.Button):
        home = HelpHomeView(self._help_data, self._include_manager)
        await interaction.response.edit_message(embed=home.create_embed(), view=home)


class HelpBackView(discord.ui.View):
    """Single back button under a command's detail."""

    def __init__(self, make_view, make_embed, label: str):
        super().__init__(timeout=300)
        self._make_view = make_view
        self._make_embed = make_embed
        button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary)
        button.callback = self._back
        self.add_item(button)

    async def _back(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self._make_embed(), view=self._make_view())


RATING_SCALE_LABELS = SCALE_LABELS


def _scale_changed_note(scale: int) -> str:
    return style.ok(
        f"New ratings are now on a **1-{scale}** scale. Ratings you already logged keep "
        "the scale they were written on; `/log_edit` re-rates a log on your new scale."
    )


class SettingsView(discord.ui.View):
    """Private settings panel: one control per setting, changes applied in
    place. A new setting is one more field in build_embed and one more
    control here."""

    def __init__(self, bot, user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        scale_select = discord.ui.Select(
            placeholder="Change rating scale…",
            options=[
                discord.SelectOption(label=f"Rate on {label}", value=str(value))
                for value, label in RATING_SCALE_LABELS.items()
            ],
        )
        scale_select.callback = self._on_scale
        self.add_item(scale_select)

    async def build_embed(self, note: Optional[str] = None) -> discord.Embed:
        saved = await get_saved_scale(self.bot, self.user_id)
        scale = saved or DEFAULT_SCALE
        embed = create_base_embed(
            title=style.title("⚙️", "Your settings"),
            description=note or "Pick a setting below to change it.",
        )
        embed.add_field(
            name="⭐ Rating scale",
            value=(
                f"**1-{scale}**" + ("" if saved else " (default)")
                + "\nNew ratings are read on this scale. Ratings you already logged "
                "keep the scale they were written on."
            ),
            inline=False,
        )
        return embed

    async def _on_scale(self, interaction: discord.Interaction):
        scale = int(interaction.data["values"][0])
        await set_user_scale(self.bot, self.user_id, scale)
        _log.info("settings: user=%s rating_scale=%s", self.user_id, scale)
        await interaction.response.edit_message(
            embed=await self.build_embed(_scale_changed_note(scale)), view=self,
        )


LOGS_PER_PAGE = 6
# /logs is the compact record; comments are previewed on one line and read in
# full through /ratings.
LOG_COMMENT_PREVIEW = 80

# Stored reward reasons name the pool kind a read counted under. Members see
# only the kind, as a short word; an everyday read shows nothing.
_PICK_LABELS = (
    ("as monthly vn", "Monthly pick"),
    ("as seasonal vn", "Seasonal pick"),
    ("as special vn", "Special pick"),
)


def _pick_label(reward_reason: Optional[str]) -> Optional[str]:
    reason = (reward_reason or "").lower()
    for needle, label in _PICK_LABELS:
        if needle in reason:
            return label
    return None


def _already_logged_message(month: str) -> str:
    return style.info(
        f"You've already logged this VN for **{style.month_short(month)}**. "
        "Re-reads in a different month are fine."
    )


class ReadingLogsView(BasePaginationView):
    """Paginated view for user reading logs"""

    def __init__(self, logs_data, member, per_page=LOGS_PER_PAGE):
        self.member = member
        super().__init__(logs_data, f"📚 Reading log · {member.display_name}", per_page)

    def create_embed(self):
        """Create an embed for the current page"""
        embed = create_base_embed(title=self.title)

        page_data = self.get_page_data()

        if not page_data:
            embed.description = "No logs found on this page."
        else:
            combined_description = "\n\n".join(page_data)
            if len(combined_description) > MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER:
                combined_description = combined_description[:MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER - 1] + "…"
            embed.description = combined_description

        embed.set_footer(text=style.footer(
            self.current_page, self.max_pages,
            style.plural(len(self.data), "log"),
            f"Reviews: /ratings user:{self.member.name}",
        ))
        return embed


async def _aggregate_leaderboard_rows(bot, rows) -> list[dict]:
    """Bucket reading_logs rows into per-user (points, completions) entries
    sorted by points desc, completions desc.

    Centralized so the leaderboard slash command and the season-nav button
    callbacks share the exact same aggregation rules; without this, the
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
    ``period_label`` is a short human label for the time window, such as a
    season or "All time".
    ``is_default_season`` flags that the current-season default kicked in
    because the user didn't pass a timeframe; purely informational, with no
    effect on the output.
    """

    def __init__(
        self,
        leaderboard_data,
        title,
        per_page: int = PAGE_ROWS,
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

    The buttons re-query for the adjacent season, replace the rows and
    ``self.title`` in place, then re-render the embed. Pagination state
    resets to page 0 so the user always lands on the podium.
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
        and leaves the main embed untouched, matching /server_leaderboard's
        nav behavior so users don't page into an empty board.
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
                style.info(f"No reading logs for {slabel}."), ephemeral=True,
            )
            return
        sorted_entries = await _aggregate_leaderboard_rows(self._bot, rows)
        if self._server_id is not None:
            guild = self._bot.get_guild(self._server_id)
            srv_name = guild.name if guild else f"Server {self._server_id}"
            plabel = f"{slabel} · {srv_name}"
        else:
            plabel = slabel

        self._season_value = new_season
        self._season_year = new_year
        self.period_label = plabel
        self.title = style.title("🏆", "Leaderboard", plabel)
        self.set_data(sorted_entries)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


class _PrevSeasonLeaderboardButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="‹ Previous season",
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
            label="Next season ›",
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
    same render, keeping button-driven re-renders visually identical to
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
        title=style.title("🏆", "Server standings", period_label),
        color=style.LEADERBOARD,
    )

    TOP_USERS_PER_SERVER = 5
    podium_emojis = ["🥇", "🥈", "🥉"]
    podium_blocks: list[str] = []
    for i, (guild_id, total) in enumerate(server_totals[:3]):
        display = truncate_text(_server_name(guild_id), 60)
        top_users = _top_users_lines(guild_id, TOP_USERS_PER_SERVER)
        user_lines = [
            f"  `{n}.` {inert_text(uname, 40)}{style.SEP}{pts:,}点"
            for n, (uname, pts) in enumerate(top_users, start=1)
        ] or ["  No readers"]
        podium_blocks.append(
            f"{podium_emojis[i]} **{display}**{style.SEP}**{total:,}**点\n"
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
            f"`{n}.` {inert_text(uname, 40)}{style.SEP}{pts:,}点"
            for n, (uname, pts) in enumerate(top_users, start=1)
        ]
        embed.add_field(
            name=f"#{i} {display}{style.SEP}{total:,}点",
            value="\n".join(value_lines) if value_lines else "No readers",
            inline=False,
        )
    if len(remaining) > max_overflow_fields:
        embed.add_field(
            name="More servers",
            value=f"And {style.plural(len(remaining) - max_overflow_fields, 'more server')}",
            inline=False,
        )

    embed.set_footer(text=style.footer(
        0, 1,
        style.plural(len(server_totals), "server"),
        f"{total_points_all:,} total points",
    ))
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
                style.info(f"No server standings for {period_label}."), ephemeral=True,
            )
            return
        self._season_value = new_season
        self._season_year = new_year
        await interaction.response.edit_message(embed=embed, view=self)


class _PrevSeasonServerStandingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="‹ Previous season",
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavServerStandingsView = self.view  # type: ignore
        new_year, new_season = prev_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


class _NextSeasonServerStandingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Next season ›",
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavServerStandingsView = self.view  # type: ignore
        new_year, new_season = next_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


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

    @discord.ui.button(label="Undo log", style=discord.ButtonStyle.danger)
    async def undo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle the undo button press."""
        # Check if the user pressing the button is the one who created the log
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                style.error("You can only undo your own logs."),
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

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            style.ok(f"Log #{self.log_id} deleted."),
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
        await interaction.followup.send(_already_logged_message(reward_month))
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

    @app_commands.command(name="help", description="Browse the commands (only you see the reply).")
    @app_commands.describe(command="Optional: jump straight to one command's full detail.")
    @app_commands.autocomplete(command=help_command_autocomplete)
    async def help_command(
        self,
        interaction: discord.Interaction,
        command: Optional[str] = None,
    ):
        # Help is for the person asking, so every reply is ephemeral and the
        # browsing below edits that one message instead of posting new ones.
        await interaction.response.defer(ephemeral=True)

        if self._help_data is None:
            await send_error(interaction, "❌ Help isn't available right now. Try again in a minute.")
            return
        help_data = self._help_data

        if command:
            cmd = _find_help_entry(help_data, command)
            if not cmd:
                await send_error(interaction, f"❌ No such command `{command}`. Try `/help` to browse them.")
                return
            await interaction.followup.send(embed=_build_help_detail_embed(cmd))
            return

        try:
            include_manager = await is_manager(interaction)
        except Exception as e:  # noqa: BLE001
            _log.debug("help manager check failed: %s", e)
            include_manager = False
        view = HelpHomeView(help_data, include_manager)
        await interaction.followup.send(embed=view.create_embed(), view=view)

    @app_commands.command(name="finish", description="Mark a VN as finished.")
    @app_commands.describe(
        title="Search for a VN by title (type at least 2 characters).",
        comment="Your comment/review about the VN (max 2000 characters).",
        rating="Your rating on your scale (1-10 unless changed in /settings).",
        public=PUBLIC_OPTION_HELP,
    )
    @app_commands.autocomplete(title=vn_autocomplete)
    @app_commands.guild_only()
    async def finish(
        self,
        interaction: discord.Interaction,
        title: str,
        comment: app_commands.Range[str, 1, 2000],
        rating: app_commands.Range[int, 1, 100],
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)

        try:
            # Resolve VN ID from various input formats (autocomplete value, display format, raw ID)
            vndb_id = await resolve_vn_from_input(title)
            if not vndb_id:
                raise ValidationError("Could not determine VN from input. Please try selecting from the autocomplete dropdown.")

            # Validate inputs (comment length is enforced by Range on the param)
            scale = await get_user_scale(self.bot, interaction.user.id)
            validate_rating(rating, scale)

            current_month = get_current_month()

            # Check if this VN is currently in this server's pool window.
            # Legacy rows with guild_id IS NULL also match (treated as global).
            # The kind ('monthly' / 'seasonal' / 'special') drives the
            # reward_reason label below; points logic itself is unchanged.
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
                # of the time it's the latter; the autocomplete already
                # validated the ID exists. Pitch the retry option so users
                # don't think their input was bad.
                raise ValidationError(
                    f"VNDB lookup failed for {vndb_id}",
                    "Couldn't fetch that VN from VNDB. VNDB may be briefly "
                    "unreachable; try again in a minute.",
                )

            # Check for a same-month duplicate. Re-reads in different months
            # are allowed; mirrors how a VN can be in the pool across multiple
            # periods.
            if await log_already_exists(
                interaction, interaction.user.id, vndb_id, current_month,
            ):
                return

            # Calculate points + craft reward_reason. When the VN is in its
            # pool window, the reason names the kind (Monthly/Seasonal/Special).
            # Otherwise it's "As Normal VN"; the implicit catch-all for VNs
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
                f"VNDB ID: {vndb_id}, Rating: {rating}/{scale}, Reward Reason: {reward_reason}, "
                f"Reward Month: {current_month}, Points: {reward_points}, Comment: {comment}"
            )

            # Snapshot badge state BEFORE the log insert so we can diff after
            # and announce any new unlocks. compute_user_badges is best-effort;
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
                    scale,
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
                await interaction.followup.send(_already_logged_message(current_month))
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

            # AFTER snapshot: set difference reveals newly-unlocked badges.
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
                format_rating(rating, scale),
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
            content_lines: list[str] = []
            if newly_unlocked:
                content_lines.append("🎉 Earned: " + ", ".join(newly_unlocked))
            # Members who rated on the old 5-point scale are told once that the
            # default changed, with the value as saved so a habitual "4" is
            # easy to spot and fix.
            try:
                if await needs_scale_notice(self.bot, interaction.user.id):
                    content_lines.append(
                        f"ℹ️ Ratings are now out of {scale} by default, so this one "
                        f"was saved as **{format_rating(rating, scale)}**. Use "
                        "`/settings` to pick 5, 10 or 100, and `/log_edit` "
                        f"to change log #{log_id}."
                    )
                    await mark_scale_notice_seen(self.bot, interaction.user.id)
            except Exception as e:  # noqa: BLE001
                _log.debug("scale notice check failed: %s", e)
            content_text: Optional[str] = "\n".join(content_lines) or None

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
        month="Optional: Filter by specific month (YYYY-MM). Cannot be combined with season.",
        season="Optional: Filter by season (3-month range). Cannot be combined with month.",
        year="Optional: Year for the season filter. Defaults to the current calendar year.",
        server="Optional: Filter by specific server",
        public=PUBLIC_OPTION_HELP,
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
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)

        try:
            # Conflict / well-formedness checks before any DB work.
            if month and season:
                await send_error(interaction, "❌ Pick either `month` or `season`, not both.")
                return
            if year is not None and season is None:
                await send_error(interaction, "❌ Pick a `season` too; `year` only works with one.")
                return

            # Resolve effective filters. Priority:
            #   explicit season > explicit month > timeframe (default current_season)
            # When the user passes nothing we fall back to current anime season;
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
            server_name: Optional[str] = None
            if server:
                guild = self.bot.get_guild(int(server))
                server_name = guild.name if guild else f"Server {server}"
            month_label = style.month_long(month) if month else None
            if season_months is not None and server:
                results = await self.bot.GET(
                    DatabaseQueries.GET_LOGS_BY_SEASON_AND_SERVER,
                    (*season_months, int(server)),
                )
                filter_description = f"**{season_label}** in **{server_name}**"
            elif season_months is not None:
                results = await self.bot.GET(
                    DatabaseQueries.GET_LOGS_BY_SEASON,
                    tuple(season_months),
                )
                filter_description = f"**{season_label}**"
            elif month and server:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH_AND_SERVER, (month, int(server)))
                filter_description = f"**{month_label}** in **{server_name}**"
            elif month:
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH, (month,))
                filter_description = f"**{month_label}**"
            elif server:
                # All-time + server scope.
                results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_SERVER, (int(server),))
                filter_description = f"**{server_name}**"
            else:
                # All-time, all servers.
                results = await self.bot.GET(DatabaseQueries.GET_ALL_LOGS)
                filter_description = None

            if not results:
                scope_text = f" for {filter_description}" if filter_description else ""
                await interaction.followup.send(style.info(f"No reading logs{scope_text}."))
                return

            # Build leaderboard. _aggregate_leaderboard_rows centralizes the
            # points + completions bucketing and the username resolution so
            # the season-nav button handler can share it without drift.
            sorted_entries = await _aggregate_leaderboard_rows(self.bot, results)

            # Build a concrete period_label for the embed header.
            if season_months is not None and server:
                period_label = f"{season_label}{style.SEP}{server_name}"
            elif season_months is not None:
                period_label = season_label
            elif month and server:
                period_label = f"{month_label}{style.SEP}{server_name}"
            elif month:
                period_label = month_label
            elif server:
                period_label = f"All time{style.SEP}{server_name}"
            else:
                period_label = "All time"

            title = style.title("🏆", "Leaderboard", period_label)

            # Create paginated view. When the leaderboard is season-scoped
            # (explicit /season: or default current season), the season-aware
            # subclass adds Prev/Next Season nav buttons; otherwise the plain
            # paginator is enough.
            if season_value is not None and season_year is not None:
                view = SeasonNavLeaderboardView(
                    leaderboard_data=sorted_entries,
                    title=title,
                    per_page=PAGE_ROWS,
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
                    per_page=PAGE_ROWS,
                    period_label=period_label,
                    is_default_season=using_default_season,
                )

            embed = view.create_embed()
            if view.children:
                view.message = await interaction.followup.send(embed=embed, view=view)
            else:
                view.stop()
                await interaction.followup.send(embed=embed)

        except Exception as e:
            await handle_command_error(interaction, e)

    @app_commands.command(
        name="server_leaderboard",
        description="Server standings. Defaults to the current season.",
    )
    @app_commands.describe(
        timeframe="Default scope when no specific month/season is given. Defaults to current season.",
        month="Optional: Filter by specific month (YYYY-MM). Cannot be combined with season.",
        season="Optional: Filter by season (3-month range). Cannot be combined with month.",
        year="Optional: Year for the season filter. Defaults to the current calendar year.",
        public=PUBLIC_OPTION_HELP,
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
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)

        # Conflict / well-formedness checks before any DB work.
        if month and season:
            await send_error(interaction, "❌ Pick either `month` or `season`, not both.")
            return
        if year is not None and season is None:
            await send_error(interaction, "❌ Pick a `season` too; `year` only works with one.")
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
            period_label = style.month_long(month)
        else:
            tf_value = timeframe.value if timeframe is not None else "current_season"
            if tf_value == "all_time":
                period_label = "All time"
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
            empty_msg = style.info(f"No reading logs for {period_label}.")
        elif month:
            results = await self.bot.GET(DatabaseQueries.GET_LOGS_BY_MONTH, (month,))
            empty_msg = style.info(f"No reading logs for {period_label}.")
        else:
            results = await self.bot.GET(DatabaseQueries.GET_ALL_LOGS)
            empty_msg = style.info("No reading logs yet.")

        if not results:
            await interaction.followup.send(empty_msg)
            return

        embed = await _build_server_standings_embed(
            self.bot, period_label, results,
        )
        if embed is None:
            await interaction.followup.send(style.info("No server standings to show yet."))
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
        embed="Also send the profile as a text embed under the image card.",
        public=PUBLIC_OPTION_HELP,
    )
    async def user_profile(
        self,
        interaction: discord.Interaction,
        user: discord.User = None,
        embed: bool = False,
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)

        if user is None:
            user = interaction.user

        # Get basic user statistics
        stats_result = await self.bot.GET_ONE(DatabaseQueries.GET_USER_STATS, (user.id,))
        if not stats_result:
            await interaction.followup.send(style.info(f"No reading data for {user.name} yet."))
            return

        total_entries, total_points, monthly_entries, vn_entries = stats_result

        if total_entries == 0:
            await interaction.followup.send(style.info(
                f"{user.name} hasn't logged a finished VN yet. Use `/finish` to log one."
            ))
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

        # Reading-streak metric: longest consecutive-month run in the user's
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

        vndb_account = await get_link(self.bot, user.id)
        vndb_username = vndb_account[1] if vndb_account else None
        link_view = build_vndb_profile_view(vndb_account) if vndb_account else None

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
            # "latest" names, sorted by BADGE_DEFS order so the highest-tier
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

            # Voting stats: pluck from aggregate_user_stats (already used by
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
                # card specifically; the rank row's period column is only
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
                    vndb_username=vndb_username,
                )
            file = discord.File(card_buf, filename=f"profile-{user.id}.png")
            send_kwargs = {"file": file}
            if link_view is not None:
                send_kwargs["view"] = link_view

            if embed:
                # Caller asked for the text embed too; send both in one message.
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
                    vndb_account=vndb_account,
                )
                await interaction.followup.send(embed=profile_embed, **send_kwargs)
            else:
                await interaction.followup.send(**send_kwargs)
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
                vndb_account=vndb_account,
            )
            if link_view is not None:
                await interaction.followup.send(embed=profile_embed, view=link_view)
            else:
                await interaction.followup.send(embed=profile_embed)

    @app_commands.command(name="logs", description="Your reading record: what you logged, when, and the points it earned.")
    @app_commands.describe(user="The user whose logs you want to view (can be a mention or user ID).", public=PUBLIC_OPTION_HELP)
    async def user_logs(
        self, interaction: discord.Interaction, user: discord.User = None,
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)

        if user is None:
            user = interaction.user

        results = await self.bot.GET(DatabaseQueries.GET_USER_LOGS, (user.id,))
        if not results:
            await interaction.followup.send(style.info(f"No reading logs for {user.name} yet."))
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
                rating_scale,
            ) = row

            # One compact line per log: id (for /log_edit and /log_undo),
            # month, what was read, points, rating, and the pick kind when
            # the read counted as one. A points award without a VN shows the
            # manager's reason. A short comment preview follows.
            parts = [f"`#{log_id}` **{style.month_short(reward_month)}**"]
            if vndb_id:
                vn_info: Optional[VN_Entry] = await from_vndb_id(self.bot, vndb_id)
                if vn_info:
                    display_title = vn_info.title_ja or vn_info.title_en or vndb_id
                    parts.append(f"[{link_label(display_title, 60)}](https://vndb.org/{vndb_id})")
                else:
                    parts.append(f"[Unknown VN](https://vndb.org/{vndb_id})")
            elif reward_reason:
                parts.append(inert_text(reward_reason, 60))
            parts.append(f"{points:,}点")
            if user_rating:
                parts.append(f"⭐ {format_rating(user_rating, rating_scale)}")
            pick = _pick_label(reward_reason) if vndb_id else None
            if pick:
                parts.append(f"*{pick}*")
            log_entry = style.SEP.join(parts)
            if comment:
                log_entry += f"\n↳ {inert_text(comment, LOG_COMMENT_PREVIEW)}"
            log_entries.append(log_entry)

        view = ReadingLogsView(log_entries, user)
        if view.children:
            view.message = await interaction.followup.send(embed=view.create_embed(), view=view)
        else:
            view.stop()
            await interaction.followup.send(embed=view.create_embed())

    @app_commands.command(name="manage_reward_points", description="[MANAGER] Reward user with points.")
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
        # thinking…" indicator. The actual followup ("Gave N
        # points to @user") stays public so the channel sees the
        # award itself.
        await interaction.response.defer(ephemeral=True)

        # validate_user_permission raises ValidationError on denial;
        # the global on_application_command_error handler unwraps the
        # BotError and surfaces user_message ephemerally. No `if not`
        # check needed; the call alone gates the rest of the function.
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
        truncated_reason = inert_text(reason, 1800)  # Leave room for other content

        await interaction.followup.send(
            style.ok(f"Gave **{points:,}** points to {member.mention}: {truncated_reason}")
        )

    @app_commands.command(
        name="manage_log",
        description="[MANAGER] Record a VN completion on behalf of another user.",
    )
    @app_commands.describe(
        member="The member to log a completion for.",
        title="Search for a VN by title (autocomplete).",
        rating="Their rating, on the member's own scale (1-10 unless they changed it).",
        comment="The comment/review to attach to the log.",
        reward_month="Optional override (YYYY-MM). Defaults to current month.",
        points="Optional override. Defaults to the same pool-window calculation /finish uses.",
    )
    @app_commands.autocomplete(
        title=vn_autocomplete,
        reward_month=month_picker_past_autocomplete,
    )
    @app_commands.guild_only()
    async def manage_log(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        title: str,
        rating: app_commands.Range[int, 1, 100],
        comment: app_commands.Range[str, 1, 2000],
        reward_month: Optional[str] = None,
        points: Optional[app_commands.Range[int, 0, 10_000]] = None,
    ):
        """Admin-only manual log insertion. Mirrors `/finish`'s points logic
        so admin-backfilled logs award the same amount a self-finish would.
        """
        # Ephemeral defer, same as /manage_reward_points: a permission denial
        # must not post publicly, and the confirmation stays private too.
        await interaction.response.defer(ephemeral=True)
        try:
            await validate_user_permission(interaction)
            scale = await get_user_scale(self.bot, member.id)
            validate_rating(rating, scale)

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
                    f"**{style.month_short(effective_month)}**. Pass a different `reward_month` "
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
            # idempotent: admin re-running the same /manage_log
            # against an already-logged month resolves to a no-op
            # instead of stacking duplicate rows.
            log_id = await self.bot.RUN_RETURNING_ID(
                DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
                (
                    member.id,
                    vndb_id,
                    rating,
                    scale,
                    reward_reason,
                    effective_month,
                    reward_points,
                    comment,
                    interaction.guild.id,
                ),
            )
            if not log_id:
                await interaction.followup.send(
                    style.info(
                        f"{member.mention} already has a log for this VN in "
                        f"**{style.month_short(effective_month)}**, so nothing was added. "
                        "A re-read in a different month would be fine."
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            _log.info(
                "Admin %s (%s) backfilled log #%s for user %s (%s): "
                "vndb=%s rating=%s/%s month=%s points=%s",
                interaction.user.name, interaction.user.id, log_id,
                member.name, member.id, vndb_id, rating, scale, effective_month, reward_points,
            )

            display_title = vn_info.title_ja or vn_info.title_en or vndb_id
            await interaction.followup.send(style.ok(
                f"Logged **{display_title}** for {member.mention} as log #{log_id}"
                + style.SEP + style.SEP.join((
                    style.month_short(effective_month),
                    f"{reward_points:,}点",
                    f"⭐ {format_rating(rating, scale)}",
                ))
            ))
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
        # Ephemeral defer so a permission denial (deleting someone else's log
        # without manager perms) doesn't post publicly. Success stays public.
        await interaction.response.defer(ephemeral=True)

        # Check if the log exists
        result = await self.bot.GET_ONE(DatabaseQueries.GET_LOG_BY_ID, (log_id,))
        if not result:
            await send_error(interaction, "❌ Log not found.")
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
            _rating_scale,
        ) = result

        # Check permissions: user can delete their own logs, or admins can delete any log
        is_own_log = user_id == interaction.user.id
        if not is_own_log:
            await validate_user_permission(interaction, "You can only delete your own logs.")
            # Per-guild VN managers are scoped to their own server's logs.
            # AUTHORIZED_USERS (bot operators) bypass this check.
            require_same_guild(interaction, logged_in_guild, entity_name="log")

        # Get VN title for display if available. A points award has no VN, so
        # the manager's reason names it instead.
        if vndb_id:
            display_title = "Unknown VN"
            vn_info = await from_vndb_id(self.bot, vndb_id)
            if vn_info:
                display_title = vn_info.title_ja or vn_info.title_en or vndb_id
        else:
            display_title = reward_reason or "Points award"

        # Delete the log
        deleted_by = "owner" if is_own_log else "admin"
        _log.info(
            f"Log #{log_id} deleted by {deleted_by} {interaction.user.name} ({interaction.user.id}) - "
            f"Log owner: {user_id}, VNDB ID: {vndb_id}, Reward Reason: {reward_reason}, "
            f"Reward Month: {reward_month}, Points: {points}"
        )
        await self.bot.RUN(DatabaseQueries.DELETE_LOG_BY_ID, (log_id,))

        # Truncate all display values to ensure message doesn't exceed Discord's 2000 char limit
        display_title = inert_text(display_title, 200)
        summary = style.SEP.join((
            f"**{display_title}**",
            style.month_short(reward_month),
            f"{points or 0:,}点",
        ))
        comment_line = f"\n↳ {inert_text(comment, 500)}" if comment else ""

        try:
            await interaction.followup.send(style.ok(
                f"Deleted log #{log_id} for <@{user_id}>: {summary}{comment_line}"
            ))
        except discord.HTTPException as e:
            if e.code == 50035:  # Invalid Form Body (message too long)
                # Fallback with minimal information
                _log.warning("Discord message length error in delete_log (log #%s): %s", log_id, e)
                await interaction.followup.send(style.ok(
                    f"Deleted log #{log_id} for <@{user_id}>: {summary}"
                ))
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
        rating="New rating on the log owner's current scale. Leave empty to keep current.",
    )
    @app_commands.autocomplete(log_id=user_logs_autocomplete)
    @app_commands.guild_only()
    async def log_edit(
        self,
        interaction: discord.Interaction,
        log_id: int,
        comment: Optional[app_commands.Range[str, 1, 2000]] = None,
        rating: Optional[app_commands.Range[int, 1, 100]] = None,
    ):
        # Ephemeral defer so a permission denial (editing someone else's log
        # without manager perms) doesn't post publicly. Success stays public.
        await interaction.response.defer(ephemeral=True)

        try:
            # Check if at least one field is being updated
            if comment is None and rating is None:
                await send_error(interaction, "❌ Give a new comment, a new rating, or both.")
                return

            # Check if the log exists
            result = await self.bot.GET_ONE(DatabaseQueries.GET_LOG_BY_ID, (log_id,))
            if not result:
                await send_error(interaction, "❌ Log not found.")
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
                current_scale,
            ) = result

            # Owners can always edit their own logs; admins can override
            # via validate_user_permission. Mirrors /log_undo (~line 1398).
            if user_id != interaction.user.id:
                await validate_user_permission(
                    interaction,
                    "Only the log owner or a VN manager can edit this log.",
                )
                # Per-guild VN managers are scoped to their own server's logs.
                # AUTHORIZED_USERS (bot operators) bypass this check.
                require_same_guild(interaction, logged_in_guild, entity_name="log")

            # Use current values for fields not being updated. A comment-only
            # edit keeps the rating on the scale it was written on; a new
            # rating is read on the owner's current scale and re-stamps it.
            new_comment = comment if comment is not None else current_comment
            new_rating, new_scale = current_rating, current_scale
            show_scale_notice = False
            if rating is not None:
                new_scale = await get_user_scale(self.bot, user_id)
                new_rating = validate_rating(rating, new_scale)
                # Checked before the write: re-rating the last legacy log
                # would otherwise clear the condition the notice looks for.
                if user_id == interaction.user.id:
                    try:
                        show_scale_notice = await needs_scale_notice(self.bot, user_id)
                    except Exception as e:  # noqa: BLE001
                        _log.debug("scale notice check failed: %s", e)

            # Update the log
            await self.bot.RUN(
                DatabaseQueries.UPDATE_LOG_COMMENT_RATING,
                (new_comment, new_rating, new_scale, log_id),
            )

            # Log the edit
            edit_details = []
            if comment is not None:
                edit_details.append(f"comment changed")
            if rating is not None:
                edit_details.append(
                    f"rating: {format_rating(current_rating, current_scale)} "
                    f"-> {format_rating(new_rating, new_scale)}"
                )
            _log.info(
                f"Log #{log_id} edited by {interaction.user.name} ({interaction.user.id}) - "
                f"VNDB ID: {vndb_id}, Changes: {', '.join(edit_details)}"
            )

            # Build response message
            updates = []
            if comment is not None:
                updates.append(f"**Comment:** {truncate_text(comment, 200)}")
            if rating is not None:
                if current_rating is not None and current_scale != new_scale:
                    # Re-rating moves the log onto the owner's current scale;
                    # showing both values makes an unintended scale change visible.
                    updates.append(
                        f"**Rating:** {format_rating(current_rating, current_scale)} → "
                        f"{format_rating(new_rating, new_scale)} (now on the 1-{new_scale} scale)"
                    )
                else:
                    updates.append(f"**Rating:** {format_rating(new_rating, new_scale)}")
                if show_scale_notice:
                    updates.append(
                        f"ℹ️ Ratings are now out of {new_scale} by default. Use "
                        "`/settings` to pick 5, 10 or 100."
                    )
                    try:
                        await mark_scale_notice_seen(self.bot, user_id)
                    except Exception as e:  # noqa: BLE001
                        _log.debug("scale notice mark failed: %s", e)

            await interaction.followup.send(
                style.ok(f"Updated log #{log_id}.") + "\n" + "\n".join(updates)
            )

        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("Unexpected error in log_edit")
            await handle_command_error(interaction, e, "An error occurred while editing the log.")
            raise

    @app_commands.command(name="settings", description="Your personal settings (only you see the reply).")
    @app_commands.describe(
        rating_scale="Optional: set the scale you rate on directly. Leave empty to open the settings panel.",
    )
    @app_commands.choices(rating_scale=[
        app_commands.Choice(name=label, value=value) for value, label in RATING_SCALE_LABELS.items()
    ])
    async def settings(
        self,
        interaction: discord.Interaction,
        rating_scale: Optional[app_commands.Choice[int]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        note = None
        if rating_scale is not None:
            await set_user_scale(self.bot, interaction.user.id, rating_scale.value)
            _log.info("settings: user=%s rating_scale=%s", interaction.user.id, rating_scale.value)
            note = _scale_changed_note(rating_scale.value)
        view = SettingsView(self.bot, interaction.user.id)
        await interaction.followup.send(embed=await view.build_embed(note), view=view)

async def setup(bot: VNClubBot):
    await bot.add_cog(VNUserCommands(bot))
