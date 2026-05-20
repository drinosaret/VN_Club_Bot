import aiosqlite
import discord
import discord.app_commands as app_commands
import io
import logging
from collections import OrderedDict
from typing import Optional
from discord.ext import commands
from lib.bot import VNClubBot
from lib.vndb_api import from_vndb_id, fetch_vndb_extras, VN_Entry, CREATE_VNDB_CACHE_TABLE
from lib.pagination import BasePaginationView
from lib.utils import (
    ANIME_SEASONS,
    AUTHORIZED_USER_IDS,
    DatabaseQueries,
    validate_user_permission,
    validate_month_input,
    handle_command_error,
    get_current_month,
    get_single_monthly_vn,
    is_month_in_range,
    BotError,
    ValidationError,
    DEFAULT_MONTHLY_POINTS,
    resolve_vn_from_input,
    require_same_guild,
    season_to_months,
    current_anime_season,
    format_season_label,
    format_season_label_from_yyyy_mm,
    month_to_season_name,
    prev_season,
    next_season,
)
from lib.embeds import EmbedBuilder, build_vn_links_view
from lib.autocomplete import (
    vn_autocomplete, vn_pool_autocomplete,
    month_picker_autocomplete, month_int_autocomplete, year_autocomplete,
    bot_guilds_autocomplete,
)
from lib.jiten_client import JitenClient
from lib.monthly_banner import (
    MonthlyBannerGenerator,
    render_banner_for_vn_entry,
    render_season_overview,
    month_label_for,
)
from math import ceil

_log = logging.getLogger(__name__)


class SeasonNavOverviewView(discord.ui.View):
    """View attached to the /season_overview composite. Carries the existing
    VNDB / jiten link buttons (when a seasonal pick exists) PLUS prev/next
    season nav buttons. Clicking nav re-renders the entire payload (image
    + content + view) for the adjacent season.
    """

    def __init__(
        self,
        cog,
        guild_id: int,
        season_value: str,
        season_year: int,
        links_vndb_id: Optional[str],
        links_jiten_deck,
    ):
        super().__init__(timeout=300)
        self._cog = cog
        self._guild_id = guild_id
        self._season_value = season_value
        self._season_year = season_year
        # Embed the existing VNDB/jiten link buttons inline, when applicable,
        # so users still get one-click access to the seasonal pick's pages.
        if links_vndb_id is not None:
            link_view = build_vn_links_view(links_vndb_id, links_jiten_deck)
            for child in list(link_view.children):
                # Reparent: detach from the temp view and add to ours so
                # discord.py treats them as our children for routing.
                self.add_item(child)
        self.add_item(_PrevSeasonOverviewButton())
        self.add_item(_NextSeasonOverviewButton())

    async def _shift_season(self, interaction: discord.Interaction,
                            new_year: int, new_season: str):
        # Acknowledge the component interaction up front. The render below
        # (VNDB lookup + jiten fetch + up to 4 cover downloads + PIL
        # composite) can easily exceed Discord's 3-second response window;
        # without this defer we'd 10062 Unknown Interaction on slow seasons.
        # For component interactions defer() sends DEFERRED_UPDATE_MESSAGE
        # (type 6) by default, so the message stays put until we edit it.
        await interaction.response.defer()
        payload = await self._cog._get_or_build_season_overview_payload(
            self._guild_id, new_season, new_year, interaction.client,
        )
        if payload is None:
            await interaction.followup.send(
                "❌ Couldn't render the next season — VNDB lookup failed.",
                ephemeral=True,
            )
            return
        new_view = SeasonNavOverviewView(
            cog=self._cog,
            guild_id=self._guild_id,
            season_value=new_season,
            season_year=new_year,
            links_vndb_id=payload["vndb_id"],
            links_jiten_deck=payload["jiten_deck"],
        )
        file = discord.File(
            io.BytesIO(payload["buf_bytes"]), filename=payload["filename"],
        )
        await interaction.edit_original_response(
            content=payload["content"],
            attachments=[file],
            view=new_view,
        )


class _PrevSeasonOverviewButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="⬅", label="Prev season", row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavOverviewView = self.view  # type: ignore
        new_year, new_season = prev_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


class _NextSeasonOverviewButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji="➡", label="Next season", row=4,
        )

    async def callback(self, interaction: discord.Interaction):
        view: SeasonNavOverviewView = self.view  # type: ignore
        new_year, new_season = next_season(view._season_year, view._season_value)
        await view._shift_season(interaction, new_year, new_season)


def _seasonal_period_label(start_month: str) -> str:
    """'2026-04' → 'Spring 2026'. Falls back to the raw YYYY-MM if the start
    month doesn't match a known season's first month."""
    year_str, month_str = start_month.split("-")
    month = int(month_str)
    season_name = next(
        (name.capitalize() for name, months in ANIME_SEASONS.items() if month == months[0]),
        None,
    )
    return f"{season_name} {year_str}" if season_name else start_month


_SEASON_CHOICES = [
    app_commands.Choice(name="Winter (Jan–Mar)", value="winter"),
    app_commands.Choice(name="Spring (Apr–Jun)", value="spring"),
    app_commands.Choice(name="Summer (Jul–Sep)", value="summer"),
    app_commands.Choice(name="Fall (Oct–Dec)",   value="fall"),
]


POOL_FILTER_CHOICES = [
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Monthly picks only", value="monthly"),
    app_commands.Choice(name="Seasonal picks only", value="seasonal"),
    app_commands.Choice(name="Special picks only", value="special"),
    app_commands.Choice(name="Nominations only", value="nominations"),
]

# Per-row emoji prefixes for the month-scoped /pool listing. Picks come from
# vn_titles.status; nominations come from cycle phase + winner flag.
_PICK_KIND_EMOJI = {
    "monthly":  "🌙",
    "seasonal": "🌸",
    "special":  "✨",
}

# Per-page description budget for /pool. Discord caps embed descriptions
# at 4096 chars; leave headroom for the meta line and section headers we
# prepend at embed-build time.
_POOL_DESC_BUDGET = 3900
# Defensive cap so one pathologically long row can't render past 4096 by itself.
_POOL_ROW_HARD_CAP = 1000


def _nomination_tag(phase: str, winner_flag: int) -> tuple[str, str]:
    """Pick the (emoji, label) for a nomination row.

    Post-decoupling refactor: nominations no longer have a "lost vote"
    state. A row with status='nominated' is just nominated, regardless
    of which cycles it's been featured in. Winners flow through the
    sibling ``_pool_row_tag`` status branch (status='monthly'/'seasonal'),
    so we never reach the won-flag case here either.

    The single non-trivial case is "voting open right now" — when the row
    is currently a candidate in an active voting cycle, we surface that
    so /pool readers can tell at a glance.
    """
    if phase == "voting":
        return ("🗳️", "voting")
    return ("📝", "nominated")


def _pool_row_tag(row) -> tuple[str, str]:
    """Pick the (emoji, label) for any unified vn_titles row.

    Row shape (GET_VN_TITLES_FOR_MONTH): (id, vndb_id, guild_id,
    start_month, end_month, is_monthly_points, created_at, title_ja,
    title_en, status, cycle_id, nominator_user_id, title_cache,
    phase, kind, target_month, target_end_month, winner_flag).
    """
    status = row[9] or "monthly"
    if status != "nominated":
        # Pick row (admin-set or voting-promoted). Tag is just the status.
        return (_PICK_KIND_EMOJI.get(status, "📌"), status)
    # Nominated row — derive from cycle phase.
    phase = row[13]
    winner_flag = row[17]
    # Default to the live "nominating" phase when phase is NULL — happens
    # for orphan rows whose cycle was deleted; treating them as live
    # nominations keeps them visible until an admin cleans them up.
    return _nomination_tag(phase or "nominating", winner_flag)


# ==================== VIEW CLASSES ====================


class VNListView(BasePaginationView):
    """Paginated view for VN list with navigation buttons"""

    def __init__(self, vn_data, title, per_page=10, show_server=False, bot=None):
        # show_server + bot are set when /pool is invoked with all_servers:true
        # so each entry can be tagged with its source server.
        self.show_server = show_server
        self._bot = bot
        super().__init__(vn_data, title, per_page)

    def create_embed(self):
        """Create an embed for the current page"""
        from lib.utils import create_base_embed, add_pagination_footer, get_current_month

        embed = create_base_embed(
            title=self.title,
            color=discord.Color.blurple(),
            author_name="Visual Novel Club"
        )

        page_data = self.get_page_data()
        current_month = get_current_month()

        description_strings = []
        for entry in page_data:
            # entry shape: (pool_id, vndb_id, guild_id, start_month, end_month,
            #               is_monthly_points, status, vn_info)
            pool_id, vndb_id, guild_id, start_month, end_month, is_monthly_points, status, vn_info = entry
            if not vn_info:
                continue
            link = f"https://vndb.org/{vndb_id}"
            non_monthly_points = max(1, int(is_monthly_points * 0.6))  # Estimate non-monthly points

            # Check if this entry is in its active window right now
            is_active = start_month <= current_month <= end_month

            # Create clean date display
            if start_month == end_month:
                date_display = start_month
            else:
                date_display = f"{start_month} to {end_month}"

            # "Current X" label reflects the entry's status when active.
            kind_label = (status or "monthly").capitalize()
            if is_active:
                indicator = f"🔥 **{date_display}** *(Current {kind_label})*"
            else:
                indicator = f"➤ **{date_display}**"

            # Tags: status always shown; server only in all_servers mode.
            kind_tag = f" *[{status or 'monthly'}]*"
            server_tag = ""
            if self.show_server:
                if guild_id is None:
                    name = "Global (legacy)"
                elif self._bot is not None:
                    g = self._bot.get_guild(guild_id)
                    name = g.name if g else f"Server {guild_id}"
                else:
                    name = f"Server {guild_id}"
                server_tag = f" *[{name}]*"

            # Prioritize Japanese title, fallback to English title, or generic text if both are empty
            display_title = vn_info.title_ja or vn_info.title_en or "View on VNDB"

            # Pool ID is the first thing on the entry line so admins can use
            # it directly with /manage_pool action:remove.
            description_string = (
                f"{indicator}{kind_tag}{server_tag}\n"
                f"└ `#{pool_id}` [{display_title}]({link}) `{vndb_id}` "
                f"• **{is_monthly_points}**点 *(active)* • **{non_monthly_points}**点 *(regular)*"
            )
            description_strings.append(description_string)
        
        embed.description = "\n\n".join(description_strings) if description_strings else "No VNs found on this page."
        add_pagination_footer(embed, self.current_page, self.max_pages, len(self.data))

        return embed


class PoolNavigationView(discord.ui.View):
    """Pool navigation. Two view modes: ``monthly`` (single calendar month,
    Prev/Next steps by month) and ``seasonal`` (3-month season, Prev/Next
    steps by season). Toggle button swaps between them. Filter and
    `all_servers` are sticky across both navigation and mode toggles.

    Internal state tracks (month, year). In monthly mode month is 1-12;
    in seasonal mode it's snapped to the season's first month (1, 4, 7,
    or 10) so prev/next math is symmetric across the four seasons.
    """

    def __init__(
        self,
        *,
        cog: "VNTitleManagement",
        guild_id: int,
        month: int,
        year: int,
        filter_value: str,
        all_servers: bool,
        view_mode: str = "monthly",
    ):
        super().__init__(timeout=300)
        self._cog = cog
        self._guild_id = guild_id
        self.month = month
        self.year = year
        self.filter_value = filter_value
        self.all_servers = all_servers
        self.view_mode = view_mode
        # Cached page list for the current (month, view_mode, filter, scope)
        # state. Populated on first refresh (or pre-populated by the slash
        # command). Page-nav buttons reuse the cache without re-querying;
        # month/view nav rebuilds it.
        self._pages: list[discord.Embed] | None = None
        self.page: int = 0
        self._sync_button_labels()
        self._sync_page_button_states()

    def _sync_button_labels(self) -> None:
        """Adapt button labels to the active view mode."""
        if self.view_mode == "seasonal":
            self.today.label = "Current season"
            self.toggle_view.label = "📅 Monthly view"
        else:
            self.today.label = "Current month"
            self.toggle_view.label = "🌸 Seasonal view"

    def _sync_page_button_states(self) -> None:
        """Show page nav only when there's more than one page. With a single
        page the buttons are removed from the view entirely so they don't
        sit greyed-out and waste vertical space; re-added when a future
        refresh produces multiple pages."""
        total = len(self._pages) if self._pages else 1
        page_buttons = (self.first_page, self.prev_page, self.next_page, self.last_page)
        if total <= 1:
            for btn in page_buttons:
                if btn in self.children:
                    self.remove_item(btn)
            return
        for btn in page_buttons:
            if btn not in self.children:
                self.add_item(btn)
        self.first_page.disabled = self.page == 0
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= total - 1
        self.last_page.disabled = self.page >= total - 1

    async def _rebuild_pages(self, bot: VNClubBot) -> None:
        self._pages = await self._cog._build_pool_pages(
            bot,
            guild_id=self._guild_id,
            month=self.month,
            year=self.year,
            filter_value=self.filter_value,
            all_servers=self.all_servers,
            view_mode=self.view_mode,
        )
        if self.page >= len(self._pages):
            self.page = max(0, len(self._pages) - 1)

    async def _refresh(self, interaction: discord.Interaction):
        """Re-query and re-render. Used by month/season/view-mode nav,
        which all reset the page to 0 before calling this."""
        self._sync_button_labels()
        await self._rebuild_pages(interaction.client)  # type: ignore[arg-type]
        self._sync_page_button_states()
        await interaction.response.edit_message(
            embed=self._pages[self.page], view=self,
        )

    async def _refresh_page_only(self, interaction: discord.Interaction):
        """Flip pages from the cached page list without re-querying."""
        if self._pages is None:
            await self._rebuild_pages(interaction.client)  # type: ignore[arg-type]
        self._sync_page_button_states()
        await interaction.response.edit_message(
            embed=self._pages[self.page], view=self,
        )

    @discord.ui.button(label="← Previous", style=discord.ButtonStyle.secondary, row=0)
    async def previous_month(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self.view_mode == "seasonal":
            season_name = month_to_season_name(self.month)
            new_year, new_season = prev_season(self.year, season_name)
            self.month = int(season_to_months(new_season, new_year)[0].split("-")[1])
            self.year = new_year
        else:
            self.month -= 1
            if self.month < 1:
                self.month = 12
                self.year -= 1
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="Current month", style=discord.ButtonStyle.primary, row=0)
    async def today(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        from datetime import datetime
        if self.view_mode == "seasonal":
            cur_season, cur_year = current_anime_season()
            self.month = int(season_to_months(cur_season, cur_year)[0].split("-")[1])
            self.year = cur_year
        else:
            now = datetime.now()
            self.month = now.month
            self.year = now.year
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.secondary, row=0)
    async def next_month(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self.view_mode == "seasonal":
            season_name = month_to_season_name(self.month)
            new_year, new_season = next_season(self.year, season_name)
            self.month = int(season_to_months(new_season, new_year)[0].split("-")[1])
            self.year = new_year
        else:
            self.month += 1
            if self.month > 12:
                self.month = 1
                self.year += 1
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="🌸 Seasonal view", style=discord.ButtonStyle.success, row=0)
    async def toggle_view(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self.view_mode == "seasonal":
            self.view_mode = "monthly"
        else:
            self.view_mode = "seasonal"
            # Snap month to the first month of the season the user was
            # viewing, so prev/next steps are aligned to season boundaries.
            season_name = month_to_season_name(self.month)
            self.month = int(season_to_months(season_name, self.year)[0].split("-")[1])
        self.page = 0
        await self._refresh(interaction)

    @discord.ui.button(label="⏪", style=discord.ButtonStyle.secondary, row=1)
    async def first_page(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        self.page = 0
        await self._refresh_page_only(interaction)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self.page > 0:
            self.page -= 1
        await self._refresh_page_only(interaction)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self._pages is not None and self.page < len(self._pages) - 1:
            self.page += 1
        await self._refresh_page_only(interaction)

    @discord.ui.button(label="⏩", style=discord.ButtonStyle.secondary, row=1)
    async def last_page(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self._pages is not None:
            self.page = max(0, len(self._pages) - 1)
        await self._refresh_page_only(interaction)


class PoolBannerPaginator(discord.ui.View):
    """Page through multiple pool entries (used by /monthly and /seasonal
    when the server has more than one active pick of that kind).

    Each "page" carries a pre-rendered payload: either a banner ``BytesIO``
    + filename (banner mode) OR a legacy embed (embed mode) — never both.
    The same view holds the prev/next buttons plus the link buttons (VNDB
    + jiten.moe) for the *current* entry; clicking nav rebuilds the link
    buttons for the new entry.
    """

    NAV_PREV_ID = "pool_banner:prev"
    NAV_NEXT_ID = "pool_banner:next"

    def __init__(
        self,
        entries: list[dict],
        embed_mode: bool,
        kind_label: str,
    ):
        # Reasonable timeout so a stale paginator doesn't sit forever, but
        # long enough that admins can finish reading all picks.
        super().__init__(timeout=600)
        self.entries = entries
        self.index = 0
        self.embed_mode = embed_mode
        self.kind_label = kind_label  # "monthly" | "seasonal" | etc. (for caption)
        self._add_link_buttons()
        self._sync_nav_state()

    # ---- internal helpers ----

    def _current(self) -> dict:
        return self.entries[self.index]

    def _add_link_buttons(self) -> None:
        """Append VNDB / jiten link buttons for the current entry to row 1."""
        entry = self._current()
        self.add_item(discord.ui.Button(
            label="VNDB",
            style=discord.ButtonStyle.link,
            url=f"https://vndb.org/{entry['vndb_id']}",
            row=1,
        ))
        if entry.get("jiten_deck_id") is not None:
            self.add_item(discord.ui.Button(
                label="jiten.moe",
                style=discord.ButtonStyle.link,
                url=f"https://jiten.moe/decks/media/{entry['jiten_deck_id']}/detail",
                row=1,
            ))

    def _remove_link_buttons(self) -> None:
        """Remove existing link buttons (everything on row 1) so we can
        re-add the new entry's links on page change."""
        for child in list(self.children):
            if isinstance(child, discord.ui.Button) and child.style == discord.ButtonStyle.link:
                self.remove_item(child)

    def _sync_nav_state(self) -> None:
        """Disable prev at the first page and next at the last page."""
        for child in self.children:
            cid = getattr(child, "custom_id", None)
            if cid == self.NAV_PREV_ID:
                child.disabled = self.index == 0
            elif cid == self.NAV_NEXT_ID:
                child.disabled = self.index >= len(self.entries) - 1

    def _caption(self) -> str:
        entry = self._current()
        page = f"{self.index + 1}/{len(self.entries)}"
        return f"Pool entry **#{entry['pool_id']}** · *{page}*"

    async def _render(self, interaction: discord.Interaction) -> None:
        """Edit the message in place to reflect ``self.index``."""
        self._remove_link_buttons()
        self._add_link_buttons()
        self._sync_nav_state()

        entry = self._current()
        if self.embed_mode:
            await interaction.response.edit_message(
                content=self._caption(),
                embed=entry["legacy_embed"],
                attachments=[],
                view=self,
            )
        else:
            buf = entry["file_buf"]
            buf.seek(0)
            file = discord.File(buf, filename=entry["filename"])
            await interaction.response.edit_message(
                content=self._caption(),
                attachments=[file],
                view=self,
            )

    # ---- nav buttons ----

    @discord.ui.button(
        label="◀ Prev", style=discord.ButtonStyle.secondary,
        custom_id=NAV_PREV_ID, row=0,
    )
    async def prev_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self.index > 0:
            self.index -= 1
        await self._render(interaction)

    @discord.ui.button(
        label="Next ▶", style=discord.ButtonStyle.secondary,
        custom_id=NAV_NEXT_ID, row=0,
    )
    async def next_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ):
        if self.index < len(self.entries) - 1:
            self.index += 1
        await self._render(interaction)


# ==================== HELPER FUNCTIONS ====================


async def get_vn_month(interaction: discord.Interaction, month: str | None) -> str:
    try:
        return await validate_month_input(interaction, month)
    except ValidationError as e:
        await interaction.followup.send(e.user_message)
        return None


def _parse_pool_id(raw: str | int | None) -> int:
    """Parse a pool_id input flexibly. Accepts ``8``, ``#8``, ``"#8"``, etc.
    Raises ``ValidationError`` on anything that doesn't resolve to a positive int.
    """
    if raw is None:
        raise ValidationError(
            "missing pool id",
            "Pick a pool entry from the autocomplete dropdown, or type its ID (e.g. `8` or `#8`).",
        )
    if isinstance(raw, int):
        if raw <= 0:
            raise ValidationError(f"bad pool id {raw}", "Pool ID must be a positive integer.")
        return raw
    text = str(raw).strip().lstrip("#").strip()
    if not text:
        raise ValidationError(
            "missing pool id",
            "Pick a pool entry from the autocomplete dropdown, or type its ID (e.g. `8` or `#8`).",
        )
    try:
        n = int(text)
    except ValueError:
        raise ValidationError(
            f"bad pool id {raw!r}",
            f"Pool ID must be a number — got `{raw}`. Try `8` or `#8`.",
        )
    if n <= 0:
        raise ValidationError(f"bad pool id {n}", "Pool ID must be a positive integer.")
    return n


async def check_if_already_exists(
    interaction: discord.Interaction, vndb_id: str
) -> bool:
    guild_id = interaction.guild.id if interaction.guild else None
    if guild_id is not None:
        result = await interaction.client.GET_ONE(
            DatabaseQueries.GET_VN_TITLE_FOR_GUILD, (vndb_id, guild_id)
        )
    else:
        result = await interaction.client.GET_ONE(
            DatabaseQueries.GET_VN_TITLE, (vndb_id,)
        )
    if result:
        await interaction.followup.send(
            f"VN title with ID `{vndb_id}` already exists for this server."
        )
        return True
    return False


async def check_if_not_exists(interaction: discord.Interaction, vndb_id: str) -> bool:
    guild_id = interaction.guild.id if interaction.guild else None
    if guild_id is not None:
        result = await interaction.client.GET_ONE(
            DatabaseQueries.GET_VN_TITLE_FOR_GUILD, (vndb_id, guild_id)
        )
    else:
        result = await interaction.client.GET_ONE(
            DatabaseQueries.GET_VN_TITLE, (vndb_id,)
        )
    if not result:
        await interaction.followup.send(
            f"VN title with ID `{vndb_id}` does not exist for this server."
        )
        return True
    return False


async def get_vndb_info(
    interaction: discord.Interaction, vndb_id: str
) -> Optional[VN_Entry]:
    try:
        vndb_response = await from_vndb_id(interaction.client, vndb_id)
    except Exception as e:
        _log.exception("Error fetching VNDB info for ID %s", vndb_id)
        raise ValidationError(
            f"Error fetching VNDB info: {e}",
            "An error occurred while fetching VNDB information. Report this."
        )
    if not vndb_response:
        raise ValidationError(
            f"VNDB ID {vndb_id} not found",
            "Failed to fetch VN information from VNDB. Please check the ID and try again."
        )
    return vndb_response


# ==================== MAIN COG CLASS ====================

class VNTitleManagement(commands.Cog):
    # LRU cap for the season-overview render cache. Each entry holds one
    # composite PNG (~0.5–1.5 MiB) plus a few strings, so 12 entries is
    # ~10–20 MiB worst case — well below anything that matters on the
    # bot host. The cache only fills on actual /season_overview usage,
    # not on boot.
    _SEASON_OVERVIEW_CACHE_CAP = 12

    def __init__(self, bot: VNClubBot):
        self.bot = bot
        # Keyed by (guild_id, season_value, season_year). Stored value is
        # the raw render payload (bytes + filename + caption + link ids).
        # Shared across views so that a user paging back-and-forth, or
        # two users hitting the same season concurrently, both reuse the
        # render. See _get_or_build_season_overview_payload.
        self._season_overview_cache: "OrderedDict[tuple, dict]" = OrderedDict()

    async def cog_load(self):
        await self.bot.RUN(DatabaseQueries.CREATE_VN_TITLES_TABLE)
        await self.bot.RUN(CREATE_VNDB_CACHE_TABLE)

    POOL_ACTIONS = [
        app_commands.Choice(name="Add a VN to the pool", value="add"),
        app_commands.Choice(name="Edit an existing pool entry", value="edit"),
        app_commands.Choice(name="Remove a VN from the pool", value="remove"),
    ]

    POOL_STATUS_CHOICES = [
        app_commands.Choice(name="Monthly pick", value="monthly"),
        app_commands.Choice(name="Seasonal pick", value="seasonal"),
        app_commands.Choice(name="Special pick", value="special"),
        # Lets admins demote a wrongly-promoted entry back to a
        # nomination (mirrors what Reopen voting does automatically).
        app_commands.Choice(name="Nominated (demote a pick back to a nomination)", value="nominated"),
    ]

    async def _manage_pool_title_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Switch the `title` autocomplete based on which action is selected.

        - action=add (or unset): search all of VNDB so admins can pull in new titles.
        - action=remove / edit: list this guild's existing pool entries with
          their pool IDs so an admin can target a specific entry when multiple
          exist for the same VN. The choice value is the pool ID; the handler
          resolves it via the matching SQL.
        """
        action = getattr(interaction.namespace, "action", None)
        if action in ("remove", "edit"):
            return await self._pool_entry_autocomplete(interaction, current)
        return await vn_autocomplete(interaction, current)

    async def _pool_entry_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        rows = await interaction.client.GET(
            DatabaseQueries.GET_POOL_ENTRIES_FOR_GUILD, (interaction.guild.id,)
        )
        # Strip leading `#` so a user typing `#8` still gets the autocomplete match.
        query = (current or "").strip().lstrip("#").lower()
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            pool_id, vndb_id, _gid, start_m, end_m, _pts, _ca, title_ja, title_en, status = row
            title = title_ja or title_en or vndb_id
            period = start_m if start_m == end_m else f"{start_m}–{end_m}"
            kind_tag = f" [{status or 'monthly'}]"
            label = f"#{pool_id} · {title}{kind_tag} ({period})"
            if len(label) > 100:
                label = label[:99] + "…"
            if query and query not in label.lower() and query not in str(pool_id):
                continue
            choices.append(app_commands.Choice(name=label, value=str(pool_id)))
            if len(choices) >= 25:
                break
        return choices

    async def _pool_entry_lookup_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for /pool_entry's `id` param. Lists all vn_titles
        rows for the current guild (all statuses), label-rich enough that
        users can disambiguate without typing raw IDs.

        Values are strings so /pool_entry's param type can be `str` —
        which lets users hand-type either `8` or `#8` and have both work
        via _parse_pool_id.
        """
        if not interaction.guild:
            return []
        rows = await interaction.client.GET(
            DatabaseQueries.GET_POOL_ENTRIES_FOR_GUILD, (interaction.guild.id,)
        )
        # Row shape includes status; we synthesize a kind-aware label.
        # rows: (id, vndb_id, guild_id, start_month, end_month,
        #        is_monthly_points, created_at, title_ja, title_en, status)
        query = (current or "").strip().lstrip("#").lower()
        choices: list[app_commands.Choice[str]] = []
        for row in rows:
            rid, vndb_id, _gid, start_m, end_m, _pts, _ca, title_ja, title_en, status = row
            title = title_ja or title_en or vndb_id
            period = start_m if start_m == end_m else f"{start_m}–{end_m}"
            label = f"#{rid} · [{status or 'monthly'}] {title} ({period})"
            if len(label) > 100:
                label = label[:99] + "…"
            if query and query not in label.lower() and query not in str(rid):
                continue
            choices.append(app_commands.Choice(name=label, value=str(rid)))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(
        name="manage_pool",
        description="Add, edit, or remove pool entries (admin).",
    )
    @app_commands.choices(action=POOL_ACTIONS, status=POOL_STATUS_CHOICES)
    @app_commands.describe(
        action="Pool action to perform.",
        title="VN title or existing pool entry — autocomplete switches by action.",
        start_month="(add/edit) Active window start. YYYY-MM.",
        end_month="(add/edit) Active window end. YYYY-MM.",
        points="(add/edit) Points awarded during the active window. Default 10 on add.",
        status="(add/edit) Status: monthly, seasonal, special, or nominated (demote a pick back to a nomination).",
        guild_id="(add/edit) Target guild — integer or `NULL` for global. Bot operators only for cross-guild values.",
    )
    @app_commands.guild_only()
    async def manage_pool(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        title: Optional[str] = None,
        start_month: Optional[str] = None,
        end_month: Optional[str] = None,
        points: Optional[app_commands.Range[int, 0, 10_000]] = None,
        status: Optional[app_commands.Choice[str]] = None,
        guild_id: Optional[str] = None,
    ):
        # Ephemeral so non-admins in the channel don't see an
        # "(Admin) is thinking…" indicator that leaks the invocation.
        await interaction.response.defer(ephemeral=True)
        try:
            await validate_user_permission(interaction)
            status_value = status.value if status else None
            if action.value == "add":
                if not title:
                    raise ValidationError(
                        "title required",
                        "`title` is required for add — pick a VN from the autocomplete.",
                    )
                await self._pool_add(
                    interaction, title, start_month, end_month,
                    points if points is not None else DEFAULT_MONTHLY_POINTS,
                    status_value or "monthly",
                    guild_id=guild_id,
                )
            elif action.value == "edit":
                if not title:
                    raise ValidationError(
                        "title required",
                        "`title` is required for edit — pick the pool entry from the autocomplete.",
                    )
                await self._pool_edit(
                    interaction, title,
                    start_month=start_month, end_month=end_month,
                    points=points, status=status_value, guild_id=guild_id,
                )
            elif action.value == "remove":
                if not title:
                    raise ValidationError(
                        "title required",
                        "`title` is required for remove — pick the pool entry from the autocomplete.",
                    )
                await self._pool_remove(interaction, title)
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            await handle_command_error(interaction, e)

    @manage_pool.autocomplete("title")
    async def _manage_pool_title_autocomplete_callback(
        self, interaction: discord.Interaction, current: str
    ):
        return await self._manage_pool_title_autocomplete(interaction, current)

    @manage_pool.autocomplete("start_month")
    async def _manage_pool_start_month_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        return await month_picker_autocomplete(interaction, current)

    @manage_pool.autocomplete("end_month")
    async def _manage_pool_end_month_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        return await month_picker_autocomplete(interaction, current)

    @manage_pool.autocomplete("guild_id")
    async def _manage_pool_guild_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ):
        # Reuses the same picker /manage_managers uses so the cross-guild
        # target lookup feels consistent between the two admin commands.
        return await bot_guilds_autocomplete(interaction, current)

    async def _pool_add(
        self,
        interaction: discord.Interaction,
        title: str,
        start_month: Optional[str],
        end_month: Optional[str],
        points: int,
        status: str,
        *,
        guild_id: Optional[str] = None,
    ):
        # Resolve VN ID from various input formats (autocomplete value, display format, raw ID)
        vndb_id = await resolve_vn_from_input(title)
        if not vndb_id:
            raise ValidationError(
                "Could not determine VN from input. Please try selecting from the autocomplete dropdown."
            )

        _log.info(
            f"User {interaction.user.name} adding pool entry: vndb_id={vndb_id}, status={status}"
        )

        # No existence check — admins can add the same VN multiple times for
        # different periods. Each row gets a unique pool ID and is removable
        # individually via /manage_pool action:remove.

        # Seasonal defaults expand to the full calendar season, not a single month.
        # Three cases:
        #   - both unset → current calendar season's span
        #   - only start_month set → expand end_month to start_month's containing season
        #   - both set → trust the admin (allows non-standard spans)
        if status == "seasonal" and start_month is None and end_month is None:
            season_name, season_year = current_anime_season()
            months = season_to_months(season_name, season_year)
            start_month, end_month = months[0], months[-1]
        else:
            start_month = await get_vn_month(interaction, start_month)
            if not start_month:
                return
            if not end_month:
                if status == "seasonal":
                    yr = int(start_month.split("-")[0])
                    mnum = int(start_month.split("-")[1])
                    for sname, smonths in ANIME_SEASONS.items():
                        if mnum in smonths:
                            end_month = season_to_months(sname, yr)[-1]
                            break
                    else:
                        end_month = start_month  # defensive; ANIME_SEASONS covers 1-12
                else:
                    end_month = start_month
            else:
                end_month = await get_vn_month(interaction, end_month)
                if not end_month:
                    return

        vn_info = await get_vndb_info(interaction, vndb_id)
        if not vn_info:
            return

        # Resolve the target guild for the new row. Mirrors the parsing +
        # auth rules `_pool_edit` enforces for the same `guild_id`
        # parameter: AUTHORIZED_USERS (bot operators) can target any
        # guild or create a NULL-guild (legacy/global) row; per-guild
        # managers can only add to their own guild and get a clear
        # ValidationError if they pass anything else. Without this guard
        # the slash-command UI lets a per-guild manager pass guild_id and
        # the bot silently falls back to interaction.guild.id, which is
        # confusing — every other `/manage_pool` action already
        # distinguishes these cases.
        target_guild_id: Optional[int] = interaction.guild.id
        if guild_id is not None:
            g = guild_id.strip()
            if g.upper() == "NULL" or g == "":
                if interaction.user.id not in AUTHORIZED_USER_IDS:
                    raise ValidationError(
                        f"guild_id=NULL add by non-operator {interaction.user.id}",
                        "Only bot operators can create global (NULL-guild) "
                        "pool entries.",
                    )
                target_guild_id = None
            else:
                try:
                    g_int = int(g)
                except ValueError:
                    raise ValidationError(
                        f"bad guild_id {g!r}",
                        "`guild_id` must be an integer or the literal `NULL`.",
                    )
                if g_int != interaction.guild.id and interaction.user.id not in AUTHORIZED_USER_IDS:
                    raise ValidationError(
                        f"cross-guild add by non-operator {interaction.user.id} "
                        f"(target={g_int}, requester_guild={interaction.guild.id})",
                        "Only bot operators can add pool entries to other "
                        "servers. Run `/manage_pool` from inside the target "
                        "server instead, or omit `guild_id`.",
                    )
                target_guild_id = g_int

        new_pool_id = await self.bot.RUN_RETURNING_ID(
            DatabaseQueries.ADD_VN_TITLE_FOR_GUILD,
            (vn_info.vndb_id, target_guild_id, start_month, end_month, points, status),
        )

        _log.info(f"Added VN to pool ({status}, pool_id={new_pool_id}): {vn_info}")

        embed = await EmbedBuilder.create_vn_info_embed(
            vn_info, start_month, end_month, points,
            title_prefix=f"VN Added ({status}): ", color=discord.Color.green(),
            pool_id=new_pool_id,
        )

        jiten_deck_id: Optional[int] = None
        try:
            async with JitenClient() as jiten:
                jiten_data = await jiten.get_by_vndb_id(vn_info.vndb_id)
            if jiten_data:
                jiten_deck_id = jiten_data.deck_id
        except Exception as e:  # noqa: BLE001
            _log.warning("jiten lookup failed for %s: %s", vn_info.vndb_id, e)

        view = build_vn_links_view(vn_info.vndb_id, jiten_deck_id)
        await interaction.followup.send(
            content=(
                f"-# Added as pool entry **#{new_pool_id}** — "
                f"`/pool_entry id:{new_pool_id}` for full detail."
            ),
            embed=embed,
            view=view,
        )

    async def _pool_remove(self, interaction: discord.Interaction, title: str):
        # Accepts the autocomplete value (a numeric string), or hand-typed
        # `8` / `#8` formats. _parse_pool_id raises ValidationError on anything
        # else, including non-numeric input.
        pool_id = _parse_pool_id(title)

        row = await self.bot.GET_ONE(
            DatabaseQueries.GET_VN_TITLE_BY_ID, (pool_id,)
        )
        if not row:
            raise ValidationError(
                f"No pool entry #{pool_id}",
                f"No pool entry with ID #{pool_id}.",
            )
        # row: (id, vndb_id, guild_id, start_month, end_month, is_monthly_points, status)
        _id, vndb_id, _gid, start_m, end_m, _pts, status = row
        # Block cross-guild removes: a per-server admin in guild A
        # shouldn't be able to wipe guild B's pool entry by knowing the
        # id. AUTHORIZED_USERS (bot operators) bypass this check.
        require_same_guild(interaction, _gid, entity_name="pool entry")

        _log.info(
            f"User {interaction.user.name} removing pool entry #{pool_id} "
            f"(vndb_id={vndb_id}, status={status}, period={start_m}–{end_m})"
        )

        # Pass the observed guild_id so the DELETE only hits the row
        # we read. If a concurrent admin (or an external admin tool with
        # direct DB access) moved the row's guild between our GET and
        # this DELETE, the statement no-ops and we tell the admin to
        # retry rather than silently mutating someone else's data.
        async with aiosqlite.connect(self.bot.path_to_db) as _db:
            _cur = await _db.execute(
                DatabaseQueries.DELETE_VN_TITLE_BY_ID, (pool_id, _gid),
            )
            _rowcount = _cur.rowcount
            await _db.commit()
        if _rowcount == 0:
            raise ValidationError(
                f"pool entry #{pool_id} moved between read and delete",
                f"Pool entry **#{pool_id}** changed under us (its server "
                f"affiliation was modified by another admin or the web "
                f"console while we were reading it). Re-run the command.",
            )

        period = start_m if start_m == end_m else f"{start_m}–{end_m}"
        kind_tag = f" [{status or 'monthly'}]"
        await interaction.followup.send(
            f"Pool entry **#{pool_id}**{kind_tag} (`{vndb_id}` · {period}) removed."
        )

    async def _pool_edit(
        self,
        interaction: discord.Interaction,
        title: str,
        *,
        start_month: Optional[str],
        end_month: Optional[str],
        points: Optional[int],
        status: Optional[str],
        guild_id: Optional[str],
    ):
        """Edit fields on a single pool entry by pool_id.

        Any field omitted stays unchanged. `guild_id="NULL"` clears to NULL
        (legacy-global semantics); an integer string sets a specific guild.

        The editable field set is shared with external admin tooling that
        writes to the same DB — see ``DatabaseQueries.UPDATE_VN_TITLE_EDITABLE_FIELDS``.
        """
        # `title` here is the pool_id encoded by the autocomplete, but admins
        # can also hand-type `8` or `#8`. _parse_pool_id handles both.
        pool_id = _parse_pool_id(title)

        existing = await self.bot.GET_ONE(
            DatabaseQueries.GET_VN_TITLE_BY_ID, (pool_id,)
        )
        if not existing:
            raise ValidationError(
                f"No pool entry #{pool_id}",
                f"No pool entry with ID #{pool_id}.",
            )
        # existing: (id, vndb_id, guild_id, start_month, end_month, is_monthly_points, status)
        # Block cross-guild edits — same reasoning as _pool_remove.
        # AUTHORIZED_USERS (bot operators) bypass.
        require_same_guild(interaction, existing[2], entity_name="pool entry")

        # Validate any month inputs (raise on bad format).
        if start_month is not None:
            start_month = await get_vn_month(interaction, start_month)
            if not start_month:
                return
        if end_month is not None:
            end_month = await get_vn_month(interaction, end_month)
            if not end_month:
                return
        if status is not None and status not in ("monthly", "seasonal", "special", "nominated"):
            raise ValidationError(
                f"bad status {status!r}",
                "`status` must be one of monthly, seasonal, special, or nominated.",
            )

        # Build dynamic UPDATE — only set columns the admin actually passed.
        # `applied` mirrors `sets` with the bound value substituted in for
        # logging clarity (so the audit log shows the new values, not the
        # raw SQL placeholders).
        sets: list[str] = []
        params: list = []
        applied: list[str] = []
        if start_month is not None:
            sets.append("start_month = ?")
            params.append(start_month)
            applied.append(f"start_month={start_month!r}")
        if end_month is not None:
            sets.append("end_month = ?")
            params.append(end_month)
            applied.append(f"end_month={end_month!r}")
        if points is not None:
            sets.append("is_monthly_points = ?")
            params.append(int(points))
            applied.append(f"is_monthly_points={int(points)}")
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            applied.append(f"status={status!r}")
        if guild_id is not None:
            # Reassigning a pool entry's guild_id is a meta-administrative
            # decision — moving the row to another server, or making it
            # global via NULL (which makes it visible to every guild),
            # is outside the authority of a per-guild manager. Without
            # this guard a manager in guild A could pollute guild B's
            # pool by passing B's id here, or escalate one of their
            # entries to global by passing NULL.
            if interaction.user.id not in AUTHORIZED_USER_IDS:
                raise ValidationError(
                    f"guild_id edit by non-operator {interaction.user.id}",
                    "Only bot operators can change a pool entry's server. "
                    "Per-server managers can edit start_month / end_month "
                    "/ points / status, but moving an entry between "
                    "servers (or clearing it to global) requires "
                    "bot-operator access.",
                )
            g = guild_id.strip()
            if g.upper() == "NULL" or g == "":
                sets.append("guild_id = NULL")
                applied.append("guild_id=NULL")
            else:
                try:
                    g_int = int(g)
                except ValueError:
                    raise ValidationError(
                        f"bad guild_id {g!r}",
                        "`guild_id` must be an integer or the literal `NULL`.",
                    )
                sets.append("guild_id = ?")
                params.append(g_int)
                applied.append(f"guild_id={g_int}")

        if not sets:
            raise ValidationError(
                "no fields to update",
                "Pass at least one field to change "
                "(start_month, end_month, points, status, or guild_id).",
            )

        # The UPDATE filters on (id, observed guild_id) so a concurrent
        # write (from another admin, or from external admin tooling with
        # direct DB access) that moves the row between our GET and the
        # write hits zero rows instead of silently mutating the relocated
        # row. `guild_id IS ?` is SQLite's NULL-safe equality, so legacy
        # NULL-guild rows are also handled.
        # AUTHORIZED_USERS (bot operators) intentionally read with the
        # row's actual guild_id and pass it back unchanged, so the same
        # SQL works for them — they're not blocked from cross-guild
        # admin work, they just have to read+write a stable row.
        observed_guild_id = existing[2]  # row's guild_id as we read it
        params.extend([pool_id, observed_guild_id])
        sql = (
            f"UPDATE vn_titles SET {', '.join(sets)} "
            f"WHERE id = ? AND guild_id IS ?"
        )
        async with aiosqlite.connect(self.bot.path_to_db) as _db:
            _cur = await _db.execute(sql, tuple(params))
            _rowcount = _cur.rowcount
            await _db.commit()
        if _rowcount == 0:
            raise ValidationError(
                f"pool entry #{pool_id} moved between read and edit",
                f"Pool entry **#{pool_id}** changed under us (its server "
                f"affiliation was modified by another admin or the web "
                f"console while we were reading it). Re-run the command.",
            )

        _log.info(
            "User %s edited pool entry #%s: %s",
            interaction.user.name, pool_id, ", ".join(applied),
        )

        # Re-fetch the row so the followup reflects the post-edit state.
        # If guild_id was changed away from this guild, the row may no
        # longer be visible via the per-guild query — fall back to id-only.
        updated = await self.bot.GET_ONE(
            "SELECT id, vndb_id, guild_id, start_month, end_month, "
            "is_monthly_points, status FROM vn_titles WHERE id = ?",
            (pool_id,),
        )
        if not updated:
            await interaction.followup.send(
                f"Pool entry **#{pool_id}** updated.", ephemeral=True
            )
            return
        _id, vndb_id, gid, sm, em, pts, st = updated
        period = sm if sm == em else f"{sm}–{em}"
        gid_str = "NULL (global)" if gid is None else str(gid)
        await interaction.followup.send(
            f"✅ Pool entry **#{pool_id}** updated:\n"
            f"  • `{vndb_id}` · {period} · **{pts}**点 · `{st or 'monthly'}` · guild={gid_str}"
        )


    @app_commands.command(
        name="pool",
        description="Browse picks and nominations for a given month.",
    )
    @app_commands.describe(
        month="Target month (1-12). Defaults to current month.",
        year="Target year. Defaults to current year.",
        filter="Restrict to a single kind. Defaults to all.",
        all_servers="Include entries from every server the bot is in (default: this server only).",
    )
    @app_commands.choices(filter=POOL_FILTER_CHOICES)
    @app_commands.autocomplete(month=month_int_autocomplete, year=year_autocomplete)
    @app_commands.guild_only()
    async def pool(
        self,
        interaction: discord.Interaction,
        month: Optional[int] = None,
        year: Optional[int] = None,
        filter: Optional[app_commands.Choice[str]] = None,
        all_servers: bool = False,
    ):
        await interaction.response.defer()

        from datetime import datetime
        now = datetime.now()
        m = month if month is not None else now.month
        y = year if year is not None else now.year
        if not (1 <= m <= 12):
            await interaction.followup.send("❌ `month` must be between 1 and 12.", ephemeral=True)
            return

        filter_value = filter.value if filter else "all"

        pages = await self._build_pool_pages(
            self.bot,
            guild_id=interaction.guild.id,
            month=m,
            year=y,
            filter_value=filter_value,
            all_servers=all_servers,
        )
        view = PoolNavigationView(
            cog=self,
            guild_id=interaction.guild.id,
            month=m,
            year=y,
            filter_value=filter_value,
            all_servers=all_servers,
        )
        # Pre-populate the cached pages so the first page-button click doesn't
        # re-query, and re-sync the page button states (constructor ran with
        # _pages=None which assumes a single page).
        view._pages = pages
        view._sync_page_button_states()
        await interaction.followup.send(embed=pages[0], view=view)

    @app_commands.command(
        name="pool_entry",
        description="Show full details for a pool entry by ID.",
    )
    @app_commands.describe(
        id="The pool entry ID. Accepts `8` or `#8`. Use autocomplete to pick from this server's entries.",
    )
    @app_commands.guild_only()
    async def pool_entry(
        self,
        interaction: discord.Interaction,
        id: str,
    ):
        await interaction.response.defer()
        try:
            pool_id = _parse_pool_id(id)
        except ValidationError as e:
            await interaction.followup.send(f"❌ {e.user_message}", ephemeral=True)
            return
        row = await self.bot.GET_ONE(
            DatabaseQueries.GET_VN_TITLE_FULL,
            (pool_id,),
        )
        if not row:
            await interaction.followup.send(
                f"❌ No pool entry `#{pool_id}`.", ephemeral=True,
            )
            return

        # Row shape matches GET_VN_TITLES_FOR_MONTH (18 columns).
        (rid, vndb_id, gid, start_m, end_m, pts, _ca, title_ja, title_en,
         status, cycle_id, nominator_user_id, title_cache,
         phase, kind, _target_m, _target_end_m, winner_flag) = row

        vn_info = await from_vndb_id(self.bot, vndb_id)
        tag = _pool_row_tag(row)[1]
        display_title = (
            (vn_info.title_ja if vn_info else None)
            or (vn_info.title_en if vn_info else None)
            or title_ja or title_en or title_cache or vndb_id
        )
        period = start_m if start_m == end_m else f"{start_m}–{end_m}"

        embed = discord.Embed(
            title=f"#{rid} · {display_title}",
            description=(
                await vn_info.get_normalized_description(max_length=600)
                if vn_info else "No description available."
            ),
            color=discord.Color.blurple(),
            url=f"https://vndb.org/{vndb_id}",
        )
        embed.set_author(name="Visual Novel Club")
        embed.add_field(name="Status", value=f"`[{tag}]`", inline=True)
        embed.add_field(name="Period", value=period, inline=True)
        embed.add_field(name="Points", value=f"{pts}点", inline=True)
        if cycle_id is not None:
            embed.add_field(
                name="Voting",
                value=f"Cycle `#{cycle_id}` · Phase `{phase}` · Kind `{kind or 'monthly'}`",
                inline=False,
            )
        if nominator_user_id:
            # Always-mention so rendering doesn't depend on the bot's
            # user cache; allowed_mentions=none() prevents pings.
            embed.add_field(
                name="Nominated by",
                value=f"<@{nominator_user_id}>",
                inline=True,
            )
        if vn_info and getattr(vn_info, "thumbnail_url", None):
            embed.set_thumbnail(url=vn_info.thumbnail_url)

        # Top completers (last 5) for this VN in this guild.
        try:
            log_rows = await self.bot.GET(
                "SELECT user_id, user_rating, comment FROM reading_logs "
                "WHERE vndb_id = ? AND logged_in_guild = ? "
                "ORDER BY log_id DESC LIMIT 5",
                (vndb_id, interaction.guild.id),
            )
        except Exception:
            log_rows = []
        if log_rows:
            # Greedy pack within the 1024-char field cap. Rows arrive newest
            # first; dropped overflow is always the oldest of the fetched
            # batch, so the tail notice can phrase it accurately.
            FIELD_CAP = 1000
            lines: list[str] = []
            cur_len = 0
            rendered = 0
            for uid, rating, comment in log_rows:
                u = self.bot.get_user(uid)
                name = f"@{u.name}" if u else f"<@{uid}>"
                snippet = (comment or "").strip().replace("\n", " ")
                if len(snippet) > 80:
                    snippet = snippet[:79] + "…"
                line = f"{name} · {rating}/5{(' · ' + snippet) if snippet else ''}"
                added = len(line) + (1 if lines else 0)
                if cur_len + added > FIELD_CAP - 40:  # reserve for tail notice
                    break
                lines.append(line)
                cur_len += added
                rendered += 1
            if rendered < len(log_rows):
                lines.append(
                    f"_…and {len(log_rows) - rendered} older entries hidden._"
                )
            embed.add_field(
                name=f"Recent completions ({len(log_rows)})",
                value="\n".join(lines),
                inline=False,
            )

        # VNDB + jiten link buttons (mirrors /monthly).
        jiten_deck_id: Optional[int] = None
        try:
            async with JitenClient() as jiten:
                data = await jiten.get_by_vndb_id(vndb_id)
            jiten_deck_id = data.deck_id if data else None
        except Exception as e:  # noqa: BLE001
            _log.warning("jiten lookup failed for /pool_entry %s: %s", vndb_id, e)
        view = build_vn_links_view(vndb_id, jiten_deck_id)
        await interaction.followup.send(embed=embed, view=view)

    @pool_entry.autocomplete("id")
    async def _pool_entry_id_autocomplete_callback(
        self, interaction: discord.Interaction, current: str,
    ):
        return await self._pool_entry_lookup_autocomplete(interaction, current)

    async def _build_pool_pages(
        self,
        bot: VNClubBot,
        *,
        guild_id: int,
        month: int,
        year: int,
        filter_value: str,
        all_servers: bool,
        view_mode: str = "monthly",
    ) -> list[discord.Embed]:
        """Render the pool view as a list of embeds, each safely under
        Discord's per-embed limits. The list always has at least one
        embed (an empty-state page when no rows match).

        Two view modes:
          - ``monthly``: one calendar month; only single-month entries
            (monthly picks/noms, special) appear.
          - ``seasonal``: one 3-month season; only multi-month entries
            (seasonal picks/noms, multi-month specials) appear.

        Shared between the slash command and the PoolNavigationView so
        toggling and navigation re-use the exact same pipeline.
        """
        if view_mode == "seasonal":
            season_name = month_to_season_name(month)
            season_months = season_to_months(season_name, year)
            # Probe the first month of the season; any 3-month entry
            # whose period is exactly this season covers it.
            displayed_month = season_months[0]
            # `format_season_label` adds the "· Season N" suffix derived
            # from the earliest reading_logs entry, matching the suffix
            # on the vote-message header and the seasonal banner.
            period_label = await format_season_label(bot, year, season_name)
            empty_label = period_label
        else:
            displayed_month = f"{year:04d}-{month:02d}"
            period_label = month_label_for(displayed_month)
            empty_label = period_label

        if all_servers:
            rows = await bot.GET(
                DatabaseQueries.GET_VN_TITLES_FOR_MONTH_GLOBAL,
                (displayed_month, displayed_month),
            )
        else:
            rows = await bot.GET(
                DatabaseQueries.GET_VN_TITLES_FOR_MONTH,
                (displayed_month, displayed_month, guild_id),
            )

        # View-mode lane: period span keeps monthly/seasonal entries
        # cleanly separated. Single-month rows (start_month == end_month)
        # belong to monthly view; multi-month rows to seasonal view.
        # Indices: start_month=3, end_month=4.
        if view_mode == "seasonal":
            rows = [r for r in rows if r[3] != r[4]]
        else:
            rows = [r for r in rows if r[3] == r[4]]

        # Apply filter. Status indices: see GET_VN_TITLES_FOR_MONTH column 9.
        if filter_value == "nominations":
            rows = [r for r in rows if r[9] == "nominated"]
        elif filter_value in ("monthly", "seasonal", "special"):
            rows = [r for r in rows if r[9] == filter_value]
        # 'all' → no filter

        # Split into pick rows (status != nominated) and nomination rows.
        picks = [r for r in rows if r[9] != "nominated"]
        noms = [r for r in rows if r[9] == "nominated"]

        scope_label = "All Servers" if all_servers else "This Server"
        view_label = "Seasonal" if view_mode == "seasonal" else "Monthly"
        meta = f"*View: {view_label} · Scope: {scope_label} · Filter: {filter_value}*"

        if not picks and not noms:
            embed = discord.Embed(
                title=f"📚 Pool — {period_label}",
                description=f"{meta}\n\n_No entries match for {empty_label}._",
                color=discord.Color.blurple(),
            )
            embed.set_author(name="Visual Novel Club")
            embed.set_footer(text="Tip: /pool_entry id:<#> shows full detail for any entry below.")
            return [embed]

        # `expected_start` / `expected_end` define the view's period —
        # rows whose period exactly matches it skip the redundant date
        # segment in their formatted line. Edge cases (custom-span
        # specials etc.) still surface their period.
        if view_mode == "seasonal":
            expected_start = season_months[0]
            expected_end = season_months[-1]
        else:
            expected_start = expected_end = displayed_month

        # Build (section_header, lines) tuples in render order. Monthly noms
        # render before seasonal noms since the monthly cadence is the more
        # common cycle members watch week-to-week.
        monthly_noms = [r for r in noms if r[3] == r[4]]
        seasonal_noms = [r for r in noms if r[3] != r[4]]
        sections: list[tuple[str, list[str]]] = []
        for label_emoji, label_text, section_rows in (
            ("📌", "Picks", picks),
            ("🗳️", "Monthly Nominations", monthly_noms),
            ("🗳️", "Seasonal Nominations", seasonal_noms),
        ):
            if not section_rows:
                continue
            lines = self._format_pool_lines(
                bot, section_rows, all_servers,
                expected_start=expected_start, expected_end=expected_end,
            )
            sections.append((f"### {label_emoji} {label_text} ({len(section_rows)})", lines))

        # Greedy line-by-line packer. Each page's body string lives in
        # ``page_bodies``; the meta line is prepended at embed-build time.
        # When a section spills past the budget, the continuation page
        # repeats the section header with a "(cont.)" suffix so readers
        # arriving via page nav always have context.
        page_bodies: list[str] = []
        buf: list[str] = []
        buf_len = 0

        def flush() -> None:
            nonlocal buf, buf_len
            if buf:
                page_bodies.append("\n".join(buf))
                buf = []
                buf_len = 0

        for header, lines in sections:
            # Blank line between sections for readability when stacking on
            # the same page (skip the leading blank on the first section).
            sep = [""] if buf else []
            sep_cost = sum(len(s) + 1 for s in sep)
            header_cost = len(header) + (1 if buf else 0)
            # Require room for the header AND at least one row so we never
            # orphan a section header at the bottom of a page.
            first_line_cost = (len(lines[0]) + 1) if lines else 0
            if buf and buf_len + sep_cost + header_cost + first_line_cost > _POOL_DESC_BUDGET:
                flush()
                sep = []
                sep_cost = 0
                header_cost = len(header)
            buf.extend(sep)
            buf.append(header)
            buf_len += sep_cost + header_cost
            for line in lines:
                line_cost = len(line) + 1  # always preceded by header or earlier line
                if buf_len + line_cost > _POOL_DESC_BUDGET:
                    flush()
                    cont = header + " (cont.)" if not header.endswith(" (cont.)") else header
                    buf.append(cont)
                    buf_len = len(cont)
                    line_cost = len(line) + 1
                buf.append(line)
                buf_len += line_cost

        flush()

        pages: list[discord.Embed] = []
        total = len(page_bodies)
        for i, body in enumerate(page_bodies):
            embed = discord.Embed(
                title=f"📚 Pool — {period_label}",
                description=f"{meta}\n\n{body}",
                color=discord.Color.blurple(),
            )
            embed.set_author(name="Visual Novel Club")
            if total > 1:
                embed.set_footer(
                    text=f"Page {i + 1}/{total} · Tip: /pool_entry id:<#> shows full detail.",
                )
            else:
                embed.set_footer(
                    text="Tip: /pool_entry id:<#> shows full detail for any entry below.",
                )
            pages.append(embed)
        return pages

    def _format_pool_lines(
        self, bot: VNClubBot, rows: list, all_servers: bool,
        *,
        expected_start: Optional[str] = None,
        expected_end: Optional[str] = None,
    ) -> list[str]:
        """One formatted line per vn_titles row.

        When ``expected_start`` / ``expected_end`` match a row's period
        the date segment is dropped — the view header already conveys it.
        Rows with a non-matching period (custom-span specials, etc.)
        keep the date segment so the anomaly is still visible.

        Pagination/budgeting is handled by the caller (see
        ``_build_pool_pages``); this method emits every row.
        """
        lines: list[str] = []
        for row in rows:
            (rid, vndb_id, gid, start_m, end_m, pts, _ca, title_ja, title_en,
             status, cycle_id, nominator_user_id, title_cache,
             phase, kind, _target_m, _target_end_m, _winner_flag) = row
            emoji, tag = _pool_row_tag(row)
            display_title = title_ja or title_en or title_cache or vndb_id
            link = f"https://vndb.org/{vndb_id}"
            period = start_m if start_m == end_m else f"{start_m}–{end_m}"
            server_tag = self._server_tag(bot, gid) if all_servers else ""
            # Hide the period segment when it matches the view's range.
            period_segment = (
                "" if (start_m == expected_start and end_m == expected_end)
                else f" · {period}"
            )
            if status == "nominated":
                nominator = (
                    f"<@{nominator_user_id}>" if nominator_user_id else "unknown"
                )
                # Monthly vs seasonal is implicit in the section header
                # the row is rendered under, so we don't repeat it here.
                line = (
                    f"{emoji} `[{tag}]` **#{rid}** [{display_title}]({link}) "
                    f"`{vndb_id}`{period_segment} · {nominator}{server_tag}"
                )
            else:
                line = (
                    f"{emoji} `[{tag}]` **#{rid}** [{display_title}]({link}) "
                    f"`{vndb_id}`{period_segment} · **{pts}**点{server_tag}"
                )
            # Truncate the title if a single row would exceed the per-row cap.
            if len(line) > _POOL_ROW_HARD_CAP:
                overflow = len(line) - _POOL_ROW_HARD_CAP + 1  # +1 for ellipsis
                truncated_title = display_title[: max(1, len(display_title) - overflow)] + "…"
                line = line.replace(f"[{display_title}]", f"[{truncated_title}]", 1)
            lines.append(line)
        return lines

    @staticmethod
    def _server_tag(bot: VNClubBot, guild_id: Optional[int]) -> str:
        """Trailing ` [Server]` annotation used in `all_servers:true` views."""
        if guild_id is None:
            return " *[Global]*"
        guild = bot.get_guild(guild_id)
        return f" *[{guild.name if guild else f'Server {guild_id}'}]*"

    @app_commands.command(
        name="monthly",
        description="Show this server's current monthly VN(s) as a banner card.",
    )
    @app_commands.describe(
        embed="Switch to the legacy text embed instead of the banner image (off by default).",
    )
    @app_commands.guild_only()
    async def monthly(
        self,
        interaction: discord.Interaction,
        embed: bool = False,
    ):
        await self._post_pool_kind_banners(
            interaction, kind="monthly", embed=embed,
        )

    @app_commands.command(
        name="seasonal",
        description="Show this server's current seasonal VN(s) as a banner card.",
    )
    @app_commands.describe(
        embed="Switch to the legacy text embed instead of the banner image (off by default).",
    )
    @app_commands.guild_only()
    async def seasonal(
        self,
        interaction: discord.Interaction,
        embed: bool = False,
    ):
        await self._post_pool_kind_banners(
            interaction, kind="seasonal", embed=embed,
        )

    async def _post_pool_kind_banners(
        self,
        interaction: discord.Interaction,
        kind: str,
        embed: bool,
    ):
        """Shared body for /monthly and /seasonal. Pre-renders every active
        ``kind`` row into a payload, then posts a single message — paginated
        with PoolBannerPaginator when there's more than one — instead of
        spamming the channel with N separate banner messages.
        """
        await interaction.response.defer()

        current_month = get_current_month()

        if kind == "seasonal":
            query = DatabaseQueries.GET_CURRENT_SEASONAL_VNS_FOR_GUILD
            empty_msg = "No current seasonal VNs found for this server."
            file_kind_tag = "season"
            eyebrow_suffix = "VN OF THE SEASON"
            embed_title_prefix = "Current Seasonal VN: "
        else:
            query = DatabaseQueries.GET_CURRENT_MONTHLY_VNS_FOR_GUILD
            empty_msg = "No current monthly VNs found for this server this month."
            file_kind_tag = "month"
            eyebrow_suffix = "VN OF THE MONTH"
            embed_title_prefix = "Current Monthly VN: "

        results = await self.bot.GET(
            query, (current_month, current_month, interaction.guild.id),
        )
        if not results:
            await interaction.followup.send(empty_msg)
            return

        # Pre-render each row into a payload dict. Rows whose VNDB lookup
        # fails are dropped (and logged) so a partial-failure VNDB outage
        # doesn't sink the entire command — but the paginator still works
        # for the remaining entries.
        entries: list[dict] = []
        async with JitenClient() as jiten, MonthlyBannerGenerator() as banner_gen:
            for row in results:
                # GET_CURRENT_*_VNS_FOR_GUILD column shape:
                # (id, vndb_id, guild_id, start_month, end_month,
                #  is_monthly_points, status, created_at)
                _id, vndb_id, _guild_id, start_month, end_month, is_monthly_points, _status, _created_at = row
                vn_info: Optional[VN_Entry] = await from_vndb_id(interaction.client, vndb_id)
                if not vn_info:
                    _log.error("Failed to fetch VNDB info for ID %s", vndb_id)
                    continue

                jiten_data = None
                try:
                    jiten_data = await jiten.get_by_vndb_id(vndb_id)
                except Exception as e:  # noqa: BLE001
                    _log.warning("jiten lookup failed for %s: %s", vndb_id, e)
                jiten_deck_id = jiten_data.deck_id if jiten_data else None

                payload: dict = {
                    "pool_id": _id,
                    "vndb_id": vndb_id,
                    "jiten_deck_id": jiten_deck_id,
                }
                if embed:
                    payload["legacy_embed"] = await EmbedBuilder.create_vn_info_embed(
                        vn_info, start_month, end_month, is_monthly_points,
                        title_prefix=embed_title_prefix,
                        color=discord.Color.blue(),
                        pool_id=_id,
                    )
                else:
                    vndb_extras = await fetch_vndb_extras(vndb_id)
                    if kind == "seasonal":
                        period_label = await format_season_label_from_yyyy_mm(
                            self.bot, start_month
                        )
                    else:
                        period_label = None
                    buf = await render_banner_for_vn_entry(
                        banner_gen, vn_info, jiten_data, start_month,
                        vndb_extras=vndb_extras,
                        target_end_month=end_month,
                        eyebrow_label=eyebrow_suffix,
                        period_label_override=period_label,
                    )
                    payload["file_buf"] = buf
                    payload["filename"] = f"vn-of-the-{file_kind_tag}-{vndb_id}.png"
                entries.append(payload)

        if not entries:
            await interaction.followup.send(
                f"Couldn't render any {kind} VNs (VNDB lookups failed)."
            )
            return

        # Single-entry path: keep the original simple message shape so a
        # 1-pick server doesn't suddenly grow nav buttons it can't use.
        first = entries[0]
        link_view = build_vn_links_view(first["vndb_id"], first["jiten_deck_id"])
        if len(entries) == 1:
            if embed:
                await interaction.followup.send(
                    embed=first["legacy_embed"], view=link_view,
                )
            else:
                buf = first["file_buf"]
                buf.seek(0)
                file = discord.File(buf, filename=first["filename"])
                await interaction.followup.send(
                    content=f"Pool entry **#{first['pool_id']}**",
                    file=file, view=link_view,
                )
            return

        # Multi-entry path: paginator with prev/next + link buttons that
        # update per page.
        paginator = PoolBannerPaginator(
            entries=entries, embed_mode=embed, kind_label=kind,
        )
        if embed:
            await interaction.followup.send(
                content=paginator._caption(),
                embed=first["legacy_embed"],
                view=paginator,
            )
        else:
            buf = first["file_buf"]
            buf.seek(0)
            file = discord.File(buf, filename=first["filename"])
            await interaction.followup.send(
                content=paginator._caption(),
                file=file, view=paginator,
            )

    @app_commands.command(
        name="season_overview",
        description="Show the seasonal VN on top with each month's monthly VN(s) below.",
    )
    @app_commands.describe(
        season="Optional: season (defaults to the current season).",
        year="Optional: year for the season (defaults to the current year).",
    )
    @app_commands.choices(season=_SEASON_CHOICES)
    @app_commands.autocomplete(year=year_autocomplete)
    @app_commands.guild_only()
    async def season_overview(
        self,
        interaction: discord.Interaction,
        season: Optional[app_commands.Choice[str]] = None,
        year: Optional[int] = None,
    ):
        """Composite image: seasonal banner up top + per-month monthly picks
        on a strip underneath. Multiple monthlies in the same month stack.

        Defaults to the current season when no args are passed. If a
        seasonal pick exists for the period it goes on top; otherwise the
        command falls back to a text response (the bottom strip alone isn't
        worth posting as an image)."""
        await interaction.response.defer()

        if year is not None and season is None:
            await interaction.followup.send(
                "❌ Pick a `season` too — `year` alone isn't a filter.",
                ephemeral=True,
            )
            return

        if season is None:
            season_value, season_year = current_anime_season()
        else:
            season_value = season.value
            season_year = year if year is not None else current_anime_season()[1]

        payload = await self._get_or_build_season_overview_payload(
            interaction.guild.id, season_value, season_year, interaction.client,
        )
        if payload is None:
            await interaction.followup.send(
                "❌ Couldn't fetch the seasonal VN's VNDB info."
            )
            return

        view = SeasonNavOverviewView(
            cog=self,
            guild_id=interaction.guild.id,
            season_value=season_value,
            season_year=season_year,
            links_vndb_id=payload["vndb_id"],
            links_jiten_deck=payload["jiten_deck"],
        )
        file = discord.File(
            io.BytesIO(payload["buf_bytes"]), filename=payload["filename"],
        )
        await interaction.followup.send(
            content=payload["content"], file=file, view=view,
        )

    async def _build_season_overview_payload(
        self,
        guild_id: int,
        season_value: str,
        season_year: int,
        bot_for_vndb,
    ) -> Optional[dict]:
        """Render the season-overview composite for ``(season_value, season_year)``.

        Returns a payload dict ``{buf_bytes, filename, content, vndb_id,
        jiten_deck}`` — callers wrap a fresh ``discord.File`` at send
        time so the same render can be reused across multiple sends.
        Returns None when the seasonal VNDB lookup fails so the caller can
        bail with a clear error. (Missing seasonal pick is fine — we still
        render the per-month strip.)
        """
        season_months = season_to_months(season_value, season_year)
        season_label = await format_season_label(
            self.bot, season_year, season_value,
        )
        start_month, end_month = season_months[0], season_months[-1]

        seasonal_rows = await self.bot.GET(
            DatabaseQueries.GET_CURRENT_SEASONAL_VNS_FOR_GUILD,
            (start_month, start_month, guild_id),
        )

        seasonal_period_label = (
            f"{month_label_for(start_month)} – {month_label_for(end_month)}"
        )

        monthly_picks_by_month: list[tuple[str, list[dict]]] = []
        s_id: Optional[int] = None
        s_vndb_id: Optional[str] = None
        seasonal_jiten = None
        seasonal_buf = None
        async with JitenClient() as jiten, MonthlyBannerGenerator() as banner_gen:
            if seasonal_rows:
                s_id, s_vndb_id, _s_guild, s_start, s_end, s_points, _s_status, _s_ca = seasonal_rows[0]

                seasonal_vn = await from_vndb_id(bot_for_vndb, s_vndb_id)
                if not seasonal_vn:
                    return None

                try:
                    seasonal_jiten = await jiten.get_by_vndb_id(s_vndb_id)
                except Exception as e:  # noqa: BLE001
                    _log.warning("jiten lookup failed for seasonal %s: %s", s_vndb_id, e)

                seasonal_extras = await fetch_vndb_extras(s_vndb_id)
                seasonal_buf = await render_banner_for_vn_entry(
                    banner_gen, seasonal_vn, seasonal_jiten, s_start,
                    vndb_extras=seasonal_extras,
                    target_end_month=s_end,
                    eyebrow_label="VN OF THE SEASON",
                    period_label_override=season_label,
                )

            for month in season_months:
                rows = await self.bot.GET(
                    DatabaseQueries.GET_CURRENT_MONTHLY_VNS_FOR_GUILD,
                    (month, month, guild_id),
                )
                picks: list[dict] = []
                for row in rows or []:
                    _mid, m_vndb_id, _mg, _ms, _me, _mp, _mst, _mca = row
                    m_vn = await from_vndb_id(bot_for_vndb, m_vndb_id)
                    if not m_vn:
                        _log.warning("season_overview: VNDB miss for monthly %s", m_vndb_id)
                        continue
                    picks.append({
                        "cover_url": m_vn.thumbnail_url,
                        "title": m_vn.title_ja or m_vn.title_en or m_vndb_id,
                        "is_nsfw": bool(m_vn.thumbnail_is_nsfw),
                    })
                monthly_picks_by_month.append((month_label_for(month), picks))

            buf = await render_season_overview(
                banner_gen, seasonal_buf, monthly_picks_by_month,
                season_label=season_label,
                season_period_label=seasonal_period_label,
            )

        if seasonal_rows and s_vndb_id is not None:
            content = f"Seasonal pool entry **#{s_id}**"
            seasonal_jiten_deck = seasonal_jiten.deck_id if seasonal_jiten else None
        else:
            content = (
                f"No seasonal VN set for **{season_label}** — "
                "showing the monthly picks anyway."
            )
            seasonal_jiten_deck = None
        # Return the rendered bytes (not a discord.File) so the cache layer
        # can reuse the same render across multiple sends — discord.File
        # consumes the underlying buffer position-wise and isn't safely
        # reusable. Each caller wraps a fresh BytesIO + discord.File at
        # send time.
        return {
            "buf_bytes": buf.getvalue(),
            "filename": f"season-overview-{season_value}-{season_year}.png",
            "content": content,
            "vndb_id": s_vndb_id,
            "jiten_deck": seasonal_jiten_deck,
        }

    async def _get_or_build_season_overview_payload(
        self,
        guild_id: int,
        season_value: str,
        season_year: int,
        bot_for_vndb,
    ) -> Optional[dict]:
        """Cache-aware wrapper around ``_build_season_overview_payload``.

        Renders are pure functions of ``(guild_id, season_value,
        season_year)`` plus the pool state for that key — pool state
        can technically change while a nav view is open, but the view's
        own lifetime is bounded to 5 minutes by the discord.py timeout,
        and the initial /season_overview embed was already a snapshot
        from when the user invoked it. So a stale render here is the
        same staleness the user already accepted at invocation time.
        """
        key = (guild_id, season_value, season_year)
        cached = self._season_overview_cache.get(key)
        if cached is not None:
            # LRU touch — keep recently-viewed seasons resident.
            self._season_overview_cache.move_to_end(key)
            return cached
        payload = await self._build_season_overview_payload(
            guild_id, season_value, season_year, bot_for_vndb,
        )
        if payload is None:
            return None
        self._season_overview_cache[key] = payload
        while len(self._season_overview_cache) > self._SEASON_OVERVIEW_CACHE_CAP:
            self._season_overview_cache.popitem(last=False)
        return payload


async def setup(bot: VNClubBot):
    await bot.add_cog(VNTitleManagement(bot))
