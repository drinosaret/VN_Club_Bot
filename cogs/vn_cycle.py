"""
Monthly + seasonal VN nomination / voting cycle cog.

Admin entry point:
    `/manage_voting` (no parameters) opens an ephemeral dashboard panel
    showing both monthly + seasonal cycle state with state-aware action
    buttons. All admin operations (Open voting, Post vote message, Close
    voting, Cancel voting, settings) live on that panel. Replaces the
    older parameter-bag form of `/manage_voting` and the separate
    `/manage_settings` command — admins no longer have to memorize an
    action keyword plus the right subset of params for each case.

Decoupled-from-cycle nomination model:
    Nominations are persistent vn_titles rows with status='nominated' and
    a [start_month, end_month] window. Users `/nominate` at any time —
    no nominating phase, no active cycle required. Each row sits with
    cycle_id=NULL until an admin opens voting.

Cycle phases (only two are produced by the current code):
    voting              — `/vote` and the VoteView buttons accept votes.
                          Created by panel "Open voting", which also
                          sweeps every status='nominated' row for the
                          target month onto the new cycle.
    closed              — final, winner(s) promoted into vn_titles
                          (status='monthly' / 'seasonal'). Loser rows
                          stay status='nominated' and get re-swept by
                          the next Open voting for that month.

The legacy `nominating` and `closed_nominating` phase values still exist
on the CHECK constraint for back-compat with old DB rows, but no new
INSERT produces them.

A cycle is keyed by `id`. Multiple closed cycles for the same month are
allowed (admin can re-run voting), so there is no UNIQUE on
`(guild_id, target_month, kind)`. App-level `_active_cycle` check still
prevents two ACTIVE cycles per (guild, kind).
"""

import asyncio
import logging
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from lib.bot import VNClubBot
from lib.embeds import EmbedBuilder, build_vn_links_view
from lib.jiten_client import JitenClient
from lib.monthly_banner import (
    days_in_month as _days_in_month_shared,
    month_label_for as _month_label_shared,
)
from lib.utils import (
    ANIME_SEASONS,
    DEFAULT_MONTHLY_POINTS,
    DatabaseQueries,
    ValidationError,
    current_anime_season,
    format_season_label,
    format_season_label_from_yyyy_mm,
    get_current_month,
    handle_command_error,
    month_to_season_name,
    next_season,
    resolve_vn_from_input,
    season_to_months,
    validate_month_format,
    validate_user_permission,
)
from lib.vndb_api import from_vndb_id
from lib.autocomplete import vn_autocomplete, month_picker_future_autocomplete
from cogs.username_fetcher import cache_user

_log = logging.getLogger(__name__)


# ==================== HELPERS ====================


def _next_month(yyyy_mm: str) -> str:
    """Return the YYYY-MM that follows the given one."""
    y, m = yyyy_mm.split("-")
    yi, mi = int(y), int(m)
    if mi == 12:
        return f"{yi + 1:04d}-01"
    return f"{yi:04d}-{mi + 1:02d}"


def _month_label(yyyy_mm: str) -> str:
    """Re-export of the shared helper for in-cog use."""
    return _month_label_shared(yyyy_mm)


def _days_in_month(yyyy_mm: str) -> int:
    return _days_in_month_shared(yyyy_mm)


async def _active_cycle(bot: VNClubBot, guild_id: int, kind: str = "monthly"):
    """Active cycle of the given kind (monthly|seasonal) for the guild, or None."""
    return await bot.GET_ONE(DatabaseQueries.GET_ACTIVE_CYCLE, (guild_id, kind))


async def _cycle_by_id(bot: VNClubBot, cycle_id: int):
    return await bot.GET_ONE(DatabaseQueries.GET_CYCLE_BY_ID, (cycle_id,))


# Cycle row column order (matches GET_ACTIVE_CYCLE / GET_CYCLE_BY_ID SELECT).
CYCLE_ID = 0
CYCLE_GUILD_ID = 1
CYCLE_PHASE = 2
CYCLE_CHOICE_MODE = 3
CYCLE_WINNER_COUNT = 4
CYCLE_TARGET_MONTH = 5
CYCLE_CHANNEL_ID = 6
CYCLE_MESSAGE_ID = 7
CYCLE_OPENED_AT = 8
CYCLE_CLOSED_AT = 9
CYCLE_KIND = 10
CYCLE_TARGET_END_MONTH = 11
CYCLE_CLOSES_AT = 12
CYCLE_VOTE_UI = 13
CYCLE_ALLOWED_ROLE_ID = 14


def _cycle_period_label(cycle_row) -> str:
    """Sync label like 'June 2026' (monthly) or 'Spring 2026' (seasonal).

    Does NOT include the "Season N" suffix because that requires a DB
    lookup against reading_logs to compute. Use
    ``cycle_period_label_with_season(bot, cycle_row)`` from async contexts
    when you want the full label.
    """
    kind = cycle_row[CYCLE_KIND] or "monthly"
    target_month = cycle_row[CYCLE_TARGET_MONTH]
    if kind == "monthly":
        return _month_label(target_month)
    # Seasonal — find the season name from the start month.
    year_str, month_str = target_month.split("-")
    month = int(month_str)
    season_name = next(
        (name.capitalize() for name, months in ANIME_SEASONS.items() if month == months[0]),
        target_month,
    )
    return f"{season_name} {year_str}"


async def cycle_period_label_with_season(bot, cycle_row) -> str:
    """Async version of ``_cycle_period_label`` that adds ``· Season N``
    for seasonal cycles. For monthly cycles (which don't carry a season
    number), it just returns the month label as-is.
    """
    kind = cycle_row[CYCLE_KIND] or "monthly"
    target_month = cycle_row[CYCLE_TARGET_MONTH]
    if kind == "monthly":
        return _month_label(target_month)
    return await format_season_label_from_yyyy_mm(bot, target_month)


# Nominee row column order (matches GET_CYCLE_NOMINEES SELECT).
NOM_ID = 0
NOM_CYCLE_ID = 1
NOM_VNDB_ID = 2
NOM_USER_ID = 3
NOM_GUILD_ID = 4
NOM_TITLE = 5
NOM_CREATED_AT = 6


# ==================== VOTE VIEW ====================


VOTE_BUTTON_PREFIX = "vncycle:vote"
VOTE_PARTICIPANTS_PREFIX = "vncycle:participants"
VOTE_MANAGE_PREFIX = "vncycle:manage"
VOTE_REMOVE_PREFIX = "vncycle:remove"


# Letter labels used in the live tally (A, B, C, …, Y) — paired with each
# nominee in display order. Caps the displayable nominees at 25 which already
# matches the open_voting refusal threshold (Discord component limits).
_VOTE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXY"

# Discord caps embed description at 4096 chars. Leave headroom for the
# variable header lines (closes_at, allowed_role) we don't always render.
_VOTE_DESC_BUDGET = 3900


class VoteButton(discord.ui.Button):
    def __init__(self, cycle_id: int, nomination_id: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"{VOTE_BUTTON_PREFIX}:{cycle_id}:{nomination_id}",
        )
        self.cycle_id = cycle_id
        self.nomination_id = nomination_id

    async def callback(self, interaction: discord.Interaction):
        await _safe_invoke(
            interaction,
            "_handle_vote",
            _handle_vote(interaction, self.cycle_id, self.nomination_id),
            cycle=self.cycle_id,
            nomination=self.nomination_id,
            user=interaction.user.id,
            via="button",
        )


class VoteSelect(discord.ui.Select):
    def __init__(self, cycle_id: int, nominees):
        options = [
            discord.SelectOption(
                label=_truncate_label(n[NOM_TITLE], 100),
                value=str(n[NOM_ID]),
                description=n[NOM_VNDB_ID],
            )
            for n in nominees[:25]
        ]
        super().__init__(
            placeholder="Pick a nominee…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"{VOTE_BUTTON_PREFIX}:select:{cycle_id}",
        )
        self.cycle_id = cycle_id

    async def callback(self, interaction: discord.Interaction):
        nomination_id = int(self.values[0])
        await _safe_invoke(
            interaction,
            "_handle_vote",
            _handle_vote(interaction, self.cycle_id, nomination_id),
            cycle=self.cycle_id,
            nomination=nomination_id,
            user=interaction.user.id,
            via="select",
        )


class ParticipantsButton(discord.ui.Button):
    """Opens the paginated voter panel for this vote."""

    def __init__(self, cycle_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Participants",
            emoji="👥",
            custom_id=f"{VOTE_PARTICIPANTS_PREFIX}:{cycle_id}",
            row=4,
        )
        self.cycle_id = cycle_id

    async def callback(self, interaction: discord.Interaction):
        await _safe_invoke(
            interaction,
            "_handle_participants",
            _handle_participants(interaction, self.cycle_id),
            cycle=self.cycle_id,
            user=interaction.user.id,
        )


# 10 rows/page keeps the embed description well under Discord's 4096-char cap.
PARTICIPANTS_PAGE_SIZE = 10


class _ParticipantsNomineeSelect(discord.ui.Select):
    """Dropdown that picks which nominee's voters to display."""

    def __init__(self, nominees, votes_by_nom):
        options = [
            discord.SelectOption(
                label=_truncate_label(
                    f"{_VOTE_LETTERS[i] if i < len(_VOTE_LETTERS) else '?'} - "
                    f"{n[NOM_TITLE]}",
                    100,
                ),
                value=str(n[NOM_ID]),
                description=f"{votes_by_nom.get(n[NOM_ID], 0)} voter(s)",
                default=(i == 0),
            )
            for i, n in enumerate(nominees[:25])
        ]
        super().__init__(
            placeholder="Pick a nominee to see voters…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        # discord.py auto-populates `self.view` when this item is added
        # to a View. Don't shadow `_parent`; that's the lib's internal
        # back-reference and clobbering it breaks the check chain.
        view: "ParticipantsView" = self.view  # type: ignore[assignment]
        await view._on_nominee_change(interaction, int(self.values[0]))


class _ParticipantsPrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀ Prev",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "ParticipantsView" = self.view  # type: ignore[assignment]
        await view._on_page_change(interaction, -1)


class _ParticipantsNextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Next ▶",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "ParticipantsView" = self.view  # type: ignore[assignment]
        await view._on_page_change(interaction, +1)


class ParticipantsView(discord.ui.View):
    """Ephemeral paginated voter panel. Dropdown picks a nominee,
    prev/next buttons page through that nominee's voters. Voter rows
    are lazy-fetched and cached on the view for the panel's lifetime."""

    def __init__(self, bot, cycle_id: int, nominees, votes_by_nom):
        super().__init__(timeout=600)  # 10 minutes idle
        self.bot = bot
        self.cycle_id = cycle_id
        self.nominees = nominees
        self.votes_by_nom = votes_by_nom
        self.selected_nom_id = nominees[0][NOM_ID]
        self.voter_page = 0
        self.voters_cache: dict = {}  # nom_id -> list of (user_id, created_at)
        self.message = None  # WebhookMessage set by _handle_participants after send
        # Serialises update callbacks so a rapid double-click on Prev/Next
        # can't trigger `InteractionResponded` from the second handler racing
        # the first's edit_message.
        self._lock = asyncio.Lock()

        self._select = _ParticipantsNomineeSelect(nominees, votes_by_nom)
        self._prev_btn = _ParticipantsPrevButton()
        self._next_btn = _ParticipantsNextButton()
        self.add_item(self._select)
        self.add_item(self._prev_btn)
        self.add_item(self._next_btn)

    async def _voters_for(self, nom_id: int) -> list:
        if nom_id not in self.voters_cache:
            rows = await self.bot.GET(
                DatabaseQueries.GET_VOTERS_FOR_NOMINATION,
                (self.cycle_id, nom_id),
            )
            self.voters_cache[nom_id] = list(rows)
            # Persist all of this nominee's resolvable voters once per panel
            # session. Page flips reuse the cache, so this never re-fires
            # for the same nominee. Bonus: covers ALL voters for the
            # nominee, not just the currently-visible page.
            await _persist_resolved_users(
                self.bot, [uid for uid, _ in self.voters_cache[nom_id]],
            )
        return self.voters_cache[nom_id]

    def _selected_nominee(self):
        for n in self.nominees:
            if n[NOM_ID] == self.selected_nom_id:
                return n
        return self.nominees[0]

    def _selected_letter(self) -> str:
        for i, n in enumerate(self.nominees):
            if n[NOM_ID] == self.selected_nom_id:
                return _VOTE_LETTERS[i] if i < len(_VOTE_LETTERS) else "?"
        return "?"

    @staticmethod
    def _max_page(voters: list) -> int:
        if not voters:
            return 0
        return (len(voters) - 1) // PARTICIPANTS_PAGE_SIZE

    async def render_embed(self) -> discord.Embed:
        nominee = self._selected_nominee()
        voters = await self._voters_for(self.selected_nom_id)
        max_page = self._max_page(voters)
        # Clamp in case voters shrank between renders.
        if self.voter_page > max_page:
            self.voter_page = max_page

        # Sync button enabled state for the new render.
        self._prev_btn.disabled = self.voter_page <= 0 or not voters
        self._next_btn.disabled = self.voter_page >= max_page or not voters

        letter = self._selected_letter()
        title_display = _truncate_label(nominee[NOM_TITLE], 200)
        header = f"**{len(voters)} voter(s)** · Vote ID `{self.cycle_id}`"

        if not voters:
            body = "_No voters yet._"
        else:
            start = self.voter_page * PARTICIPANTS_PAGE_SIZE
            end = start + PARTICIPANTS_PAGE_SIZE
            page_slice = voters[start:end]
            # Batch the cache lookup for voters not in the in-process member
            # cache. One SQLite round-trip instead of N. `bot.get_user` is a
            # sync dict lookup so we can resolve cache hits inline. Note
            # that `_voters_for` already persisted resolvable voters for
            # this nominee on first fetch, so no persist call here.
            missing = [uid for uid, _ in page_slice if self.bot.get_user(uid) is None]
            tag_map: dict = {}
            if missing:
                ph = ",".join("?" * len(missing))
                rows = await self.bot.GET(
                    f"SELECT discord_user_id, user_tag, user_name FROM users "
                    f"WHERE discord_user_id IN ({ph})",
                    tuple(missing),
                )
                # Prefer the unique handle; legacy rows pre-migration only
                # have user_name (display) which we fall back to so the
                # line still identifies the voter.
                tag_map = {r[0]: (r[1] or r[2]) for r in rows if r[1] or r[2]}
            lines = []
            for user_id, created_at in page_slice:
                user = self.bot.get_user(user_id)
                if user is not None:
                    tag = user.name
                else:
                    tag = tag_map.get(user_id) or "unknown-user"
                ts = _format_closes_at_relative(created_at)
                line = f"• @{tag} (<@{user_id}>)"
                if ts:
                    line += f" · {ts}"
                lines.append(line)
            body = "\n".join(lines)

        embed = discord.Embed(
            title=f"👥 Participants · {letter} {title_display}",
            description=f"{header}\n\n{body}",
            color=discord.Color.blurple(),
        )
        if max_page > 0:
            embed.set_footer(
                text=f"Page {self.voter_page + 1}/{max_page + 1} · "
                     "use the dropdown below to switch nominee",
            )
        else:
            embed.set_footer(text="Use the dropdown above to switch nominee")
        return embed

    async def _on_nominee_change(self, interaction, new_nom_id: int):
        async with self._lock:
            self.selected_nom_id = new_nom_id
            self.voter_page = 0
            # Reflect the new choice as the dropdown's default so it stays
            # visually selected when Discord re-renders the view.
            for opt in self._select.options:
                opt.default = (opt.value == str(new_nom_id))
            embed = await self.render_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def _on_page_change(self, interaction, delta: int):
        async with self._lock:
            voters = await self._voters_for(self.selected_nom_id)
            new_page = max(0, min(self._max_page(voters), self.voter_page + delta))
            self.voter_page = new_page
            embed = await self.render_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        # Disable components so the ephemeral can't be re-poked after timeout.
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


class ManageVotesButton(discord.ui.Button):
    """Opens an ephemeral panel where the user can remove their current
    votes for this cycle. Mirrors EasyPoll's 'Manage your votes' panel.
    """

    def __init__(self, cycle_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Manage your votes",
            emoji="🗑",
            custom_id=f"{VOTE_MANAGE_PREFIX}:{cycle_id}",
            row=4,
        )
        self.cycle_id = cycle_id

    async def callback(self, interaction: discord.Interaction):
        await _safe_invoke(
            interaction,
            "_handle_manage_votes",
            _handle_manage_votes(interaction, self.cycle_id),
            cycle=self.cycle_id,
            user=interaction.user.id,
        )


class RemoveVoteButton(discord.ui.Button):
    """Per-vote remove button rendered inside the ephemeral manage panel.

    Not persistent — its custom_id is unique per vote row so it's only
    valid for one click before the row is gone, but since the view is
    ephemeral and not registered globally, that's fine.
    """

    def __init__(self, vote_id: int, cycle_id: int, nominee_label: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Remove {nominee_label}",
            custom_id=f"{VOTE_REMOVE_PREFIX}:{cycle_id}:{vote_id}",
        )
        self.vote_id = vote_id
        self.cycle_id = cycle_id

    async def callback(self, interaction: discord.Interaction):
        await _safe_invoke(
            interaction,
            "_handle_remove_vote",
            _handle_remove_vote(interaction, self.cycle_id, self.vote_id),
            cycle=self.cycle_id,
            vote=self.vote_id,
            user=interaction.user.id,
        )


def _truncate_label(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _votes_phrase(n: int) -> str:
    """Singular/plural-aware vote count, e.g. '1 vote' / '5 votes'."""
    return "1 vote" if n == 1 else f"{n} votes"


async def _persist_resolved_users(bot, user_ids) -> None:
    """Upsert display_names into the local `users` table for any user_id
    in `user_ids` that bot.get_user can resolve. One batched write,
    deduped. Lets vote-menu surfaces survive guild departures: even if
    a member later leaves, their name stays in the table for future
    renders. Failures swallowed; this is best-effort caching."""
    to_persist: list = []
    seen: set = set()
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        user = bot.get_user(uid)
        if user is not None:
            to_persist.append((user.id, user.display_name, user.name))
    if not to_persist:
        return
    placeholders = ",".join("(?, ?, ?)" for _ in to_persist)
    flat: list = []
    for uid, name, tag in to_persist:
        flat.extend([uid, name, tag])
    try:
        await bot.RUN(
            f"INSERT INTO users (discord_user_id, user_name, user_tag) VALUES {placeholders} "
            "ON CONFLICT(discord_user_id) DO UPDATE SET "
            "user_name = excluded.user_name, user_tag = excluded.user_tag",
            tuple(flat),
        )
    except Exception:  # noqa: BLE001
        _log.exception("_persist_resolved_users failed for %d users", len(to_persist))


def _winners_after_tiebreak(tally: list, winner_count: int) -> list:
    """Top `winner_count` rows by vote count. Ties between rows with the
    same vote count are broken by the SQL caller's secondary sort
    (vn_titles.id ASC) so the earliest nomination wins. 0-vote rows
    never claim a seat.

    Examples (winner_count=1):
      [10, 5]    -> [10]
      [5, 5]     -> [first-by-id]
      [0, 0]     -> []
      [10]       -> [10]

    Examples (winner_count=2):
      [10, 5, 3] -> [10, 5]
      [10, 5, 5] -> [10, first-of-the-tied-5s]
      [5, 5, 3]  -> [both-tied-5s, lower-id first]
    """
    winners: list = []
    cap = min(winner_count, len(tally))
    for i in range(cap):
        if tally[i][6] <= 0:
            break
        winners.append(tally[i])
    return winners


def _build_close_voting_summary(
    cycle_kind: str,
    period_label: str,
    winners,
    promoted_pool_ids: dict,
    *,
    tally=(),
) -> str:
    """Compose the admin's ephemeral followup after Close voting.

    Ties are resolved by lowest-id ('nominated first'); winners whose
    seat was contested at the same vote count are flagged inline so the
    admin can see when a tie was broken.

    Empty-`winners` cases:
      - No nominees in the cycle (empty tally): nothing to promote.
      - Tally exists but every row has zero votes: voting happened but
        no one actually voted. Same outcome (nothing promoted) but
        worth distinguishing in the message.
    """
    if not winners:
        if tally:
            return (
                f"✅ {cycle_kind.capitalize()} voting closed for "
                f"**{period_label}** (no votes cast, nothing to promote)."
            )
        return (
            f"✅ {cycle_kind.capitalize()} voting closed for **{period_label}** "
            "(no nominees, nothing to promote)."
        )

    # A winner's seat was contested if the next tally row has the same
    # vote count. The SQL secondary sort guarantees the lower-id row of
    # the tied set is the one we picked, so "tied" here means "we won by
    # the id-tiebreak rule".
    tied_winners: set = set()
    for i, w in enumerate(winners):
        if i + 1 < len(tally) and tally[i + 1][6] == w[6]:
            tied_winners.add(w[0])

    if len(winners) == 1:
        w = winners[0]
        votes_str = _votes_phrase(w[6])
        tied = w[0] in tied_winners
        if w[1] in promoted_pool_ids:
            pid = promoted_pool_ids[w[1]]
            if tied:
                return (
                    f"⚖️ {cycle_kind.capitalize()} voting closed for "
                    f"**{period_label}**. Tied at {votes_str}; "
                    f"**{w[2]}** wins as the earliest nomination "
                    f"(pool **#{pid}**)."
                )
            return (
                f"✅ **{w[2]}** wins the {cycle_kind} vote for "
                f"**{period_label}** ({votes_str}, pool **#{pid}**)."
            )
        return (
            f"⚠️ **{w[2]}** ({votes_str}) won the {cycle_kind} vote for "
            f"**{period_label}**, but its pool entry was removed before "
            f"voting closed — its status couldn't be set to {cycle_kind}. "
            "Re-add it via `/manage_pool action:Add` if you still want it "
            f"as the {cycle_kind} pick."
        )
    parts = []
    for w in winners:
        votes_str = _votes_phrase(w[6])
        tie_note = ", earliest of a tie" if w[0] in tied_winners else ""
        if w[1] in promoted_pool_ids:
            parts.append(
                f"**{w[2]}** ({votes_str}, pool "
                f"**#{promoted_pool_ids[w[1]]}**{tie_note})"
            )
        else:
            parts.append(
                f"**{w[2]}** ({votes_str}, pool entry removed mid-vote)"
            )
    return (
        f"✅ Winners of the {cycle_kind} vote for **{period_label}**: "
        + ", ".join(parts)
    )


class VoteView(discord.ui.View):
    """
    Persistent vote view. Re-registered on bot boot for each phase='voting' cycle.

    ``vote_ui`` selects the input shape:
    - ``"dropdown"`` (new default) — single Select with up to 25 options.
    - ``"buttons"`` — one button per nominee, capped at 20 (5 buttons × 4
      rows; row 4 reserved for the utility buttons).
    - ``None`` (legacy / unset) — auto-pick: ≤5 nominees → buttons, else
      dropdown. Cycles created before the vote_ui setting was added land
      here on re-registration; new cycles always get an explicit value.

    Two utility buttons live on the bottom row: Participants (who voted what)
    and Manage your votes (per-user vote removal panel). Both are persistent
    via stable custom_ids so they survive bot restarts.
    """

    def __init__(self, cycle_id: int, nominees=None, vote_ui: Optional[str] = None):
        super().__init__(timeout=None)
        self.cycle_id = cycle_id
        if nominees is None:
            return  # placeholder for re-registration; children rebuilt below
        # Resolve effective UI mode.
        if vote_ui == "buttons":
            use_buttons = True
        elif vote_ui == "dropdown":
            use_buttons = False
        else:
            # Legacy auto-pick when vote_ui is NULL on the row.
            use_buttons = len(nominees) <= 5
        if use_buttons:
            for n in nominees:
                self.add_item(VoteButton(
                    cycle_id=cycle_id,
                    nomination_id=n[NOM_ID],
                    label=_truncate_label(n[NOM_TITLE], 80),
                ))
        else:
            self.add_item(VoteSelect(cycle_id=cycle_id, nominees=nominees))
        # Bottom-row utility buttons (row=4 keeps them visually separated
        # from the vote inputs, which auto-flow into rows 0-3).
        self.add_item(ParticipantsButton(cycle_id=cycle_id))
        self.add_item(ManageVotesButton(cycle_id=cycle_id))


class ClosedVoteView(discord.ui.View):
    """Persistent view left on a closed vote message: just the Participants
    button, so people can still see who voted for what after voting ends.
    The vote inputs and "Manage your votes" button are dropped since voting
    is over.

    Shares the Participants custom_id (``VOTE_PARTICIPANTS_PREFIX:{cycle_id}``)
    with VoteView. In-session that custom_id is already registered by the live
    VoteView, so the button keeps working immediately after close; across a
    restart it is re-registered for closed cycles by
    ``_register_persistent_vote_views`` (the full VoteView is only re-attached
    for cycles still in voting).
    """

    def __init__(self, cycle_id: int):
        super().__init__(timeout=None)
        self.cycle_id = cycle_id
        self.add_item(ParticipantsButton(cycle_id=cycle_id))


async def _send_handler_error(interaction: discord.Interaction) -> None:
    """Ephemeral 'something went wrong, logged' fallback that tolerates
    either response state (already-deferred vs. fresh)."""
    msg = "❌ Something went wrong. This has been logged."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:  # noqa: BLE001
        _log.exception(
            "failed to deliver handler-error fallback to user=%s",
            getattr(interaction.user, "id", "?"),
        )


async def _safe_invoke(
    interaction: discord.Interaction,
    handler_name: str,
    coro,
    **ctx,
) -> None:
    """Run an interaction handler with a top-level safety net so DB / Discord
    failures land in ``hikaru_bot.log`` (with the cycle/user/nomination
    context that the framework's generic error handler doesn't have) instead
    of vanishing into an opaque "interaction failed" toast for the user.
    """
    try:
        await coro
    except Exception:  # noqa: BLE001
        ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items())
        _log.exception("%s failed: %s", handler_name, ctx_str)
        await _send_handler_error(interaction)


# Per-(cycle_id, user_id) locks for the vote read-modify-write block.
# The UNIQUE constraint on vn_votes is (cycle_id, user_id,
# nomination_id) — different nominees from the same user are NOT
# blocked, so a double-click for two different options in single-choice
# mode (or one over the limit in multi-choice) races past the
# application-level check between GET_USER_VOTES_IN_CYCLE and INSERT.
# A per-user lock serializes that block so the GET+INSERT pair is
# effectively atomic for the same voter.
#
# Lock dict grows with each unique (cycle, user) pair the bot has
# served; entries are tiny (just asyncio.Lock objects), and a typical
# server processes a few hundred per year — negligible.
_VOTE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _get_vote_lock(cycle_id: int, user_id: int) -> asyncio.Lock:
    """Lazy lock creation. Dict ops are race-free in single-threaded
    asyncio because no await happens between get/set.
    """
    key = (cycle_id, user_id)
    lock = _VOTE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _VOTE_LOCKS[key] = lock
    return lock


async def _handle_vote(interaction: discord.Interaction, cycle_id: int, nomination_id: int):
    """Vote-button / Select callback shared by all interactions on a VoteView."""
    bot: VNClubBot = interaction.client  # type: ignore
    _log.debug(
        "_handle_vote: entry cycle=%d user=%d nomination=%d",
        cycle_id, interaction.user.id, nomination_id,
    )
    cycle = await _cycle_by_id(bot, cycle_id)
    if not cycle or cycle[CYCLE_PHASE] != "voting":
        await interaction.response.send_message(
            "❌ That voting is not open right now.", ephemeral=True
        )
        return

    # Allowed-role gate. Checked here so both button clicks and /vote
    # slash-command go through the same validation. Server-side: a
    # role removed mid-cycle blocks new votes immediately, even on
    # already-rendered menus.
    allowed_role_id = cycle[CYCLE_ALLOWED_ROLE_ID]
    if allowed_role_id is not None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            await interaction.response.send_message(
                "❌ This voting can only be cast from inside the guild.",
                ephemeral=True,
            )
            return
        if not any(r.id == allowed_role_id for r in member.roles):
            await interaction.response.send_message(
                f"❌ You need the <@&{allowed_role_id}> role to vote in this voting.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

    nomination = await bot.GET_ONE(DatabaseQueries.GET_NOMINATION_BY_ID, (nomination_id,))
    if not nomination:
        await interaction.response.send_message(
            "❌ Couldn't find that nominee — it may have been removed.", ephemeral=True
        )
        return
    # Reject votes whose nomination_id belongs to a different cycle. In normal
    # use the button's cycle_id and nomination_id are consistent, but stale
    # custom_ids (e.g. from before a unify migration) could otherwise let a
    # vote land in vn_votes pointing across cycles.
    if nomination[NOM_CYCLE_ID] != cycle_id:
        _log.warning(
            "Vote rejected: nomination %d belongs to cycle %d, not %d",
            nomination_id, nomination[NOM_CYCLE_ID], cycle_id,
        )
        await interaction.response.send_message(
            "❌ That nominee isn't part of this voting.", ephemeral=True
        )
        return
    nominee_title = nomination[NOM_TITLE]

    choice_mode = cycle[CYCLE_CHOICE_MODE] or "single"
    winner_count = cycle[CYCLE_WINNER_COUNT] or 1
    user_id = interaction.user.id
    guild_id = interaction.guild.id if interaction.guild else cycle[CYCLE_GUILD_ID]

    async def _phase_still_voting() -> bool:
        # Re-fetch the cycle right before any vote-write so a click that
        # raced /close_voting lands a "sorry, just closed" message instead
        # of polluting vn_votes with a vote against a now-closed cycle.
        fresh = await _cycle_by_id(bot, cycle_id)
        return bool(fresh) and fresh[CYCLE_PHASE] == "voting"

    # Per-user lock — the entire read-modify-write block below races
    # against a concurrent click from the same user for a different
    # nominee. Without the lock, two GETs both observe pre-state, both
    # pass the check, and both INSERT (different nomination_ids so the
    # UNIQUE constraint doesn't block them). Acquiring per-(cycle,user)
    # serializes the check+write for the same voter while still letting
    # different voters proceed in parallel.
    async with _get_vote_lock(cycle_id, user_id):
        if choice_mode == "single":
            existing = await bot.GET(
                DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, user_id)
            )
            already_picked = any(row[1] == nomination_id for row in existing)
            if already_picked:
                await interaction.response.send_message(
                    f"You've already voted for **{nominee_title}**.", ephemeral=True
                )
                return
            if not await _phase_still_voting():
                await interaction.response.send_message(
                    "❌ Voting just closed — your vote wasn't recorded.", ephemeral=True
                )
                return
            # Single transaction so a failed INSERT doesn't leave the user
            # with their old vote deleted but no new one recorded.
            await bot.RUN_TRANSACTION([
                (DatabaseQueries.DELETE_USER_VOTES_IN_CYCLE, (cycle_id, user_id)),
                (DatabaseQueries.INSERT_VOTE, (cycle_id, user_id, guild_id, nomination_id)),
            ])
            await cache_user(bot, interaction.user)
            action = "replaced" if existing else "cast"
            _log.info(
                "vote %s: cycle=%d user=%d nomination=%d mode=single",
                action, cycle_id, user_id, nomination_id,
            )
            msg = (
                f"✅ Replaced your earlier vote with **{nominee_title}**."
                if existing
                else f"✅ Voted for **{nominee_title}**."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            await _refresh_vote_message(bot, cycle_id)
            return

        # multi-choice — toggle
        existing = await bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, user_id)
        )
        already_row = next((row for row in existing if row[1] == nomination_id), None)
        if already_row:
            await bot.RUN(DatabaseQueries.DELETE_VOTE_BY_ID, (already_row[0],))
            _log.info(
                "vote removed: cycle=%d user=%d nomination=%d mode=multi",
                cycle_id, user_id, nomination_id,
            )
            await interaction.response.send_message(
                f"➖ Removed your vote for **{nominee_title}**.", ephemeral=True
            )
            await _refresh_vote_message(bot, cycle_id)
            return

        if len(existing) >= winner_count:
            await interaction.response.send_message(
                f"❌ You can pick at most {winner_count} nominees in this voting.",
                ephemeral=True,
            )
            return

        if not await _phase_still_voting():
            await interaction.response.send_message(
                "❌ Voting just closed — your vote wasn't recorded.", ephemeral=True
            )
            return
        await bot.RUN(DatabaseQueries.INSERT_VOTE, (cycle_id, user_id, guild_id, nomination_id))
        await cache_user(bot, interaction.user)
        _log.info(
            "vote cast: cycle=%d user=%d nomination=%d mode=multi total=%d/%d",
            cycle_id, user_id, nomination_id, len(existing) + 1, winner_count,
        )
        await interaction.response.send_message(
            f"✅ Voted for **{nominee_title}** ({len(existing) + 1}/{winner_count}).",
            ephemeral=True,
        )
        await _refresh_vote_message(bot, cycle_id)


# ==================== LIVE TALLY + UTILITY HANDLERS ====================


def _format_closes_at_relative(closes_at) -> str:
    """Discord relative timestamp for the closes_at field. Returns "" when
    closes_at is NULL (no auto-close timer). closes_at is stored as the
    SQLite ISO TIMESTAMP string from `datetime` strftime; treat as UTC.
    """
    if not closes_at:
        return ""
    from datetime import datetime, timezone
    try:
        # Accept both "YYYY-MM-DD HH:MM:SS" (SQLite default) and ISO 8601.
        s = str(closes_at).replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except (ValueError, TypeError):
        return ""


def _seconds_until(closes_at) -> Optional[float]:
    """Seconds from now (UTC) until ``closes_at`` fires. Returns None when
    the value is NULL / unparseable so callers can skip scheduling. A
    negative result means the time has already passed — caller decides
    whether to fire immediately (max(0, …)) or skip.
    """
    if not closes_at:
        return None
    from datetime import datetime, timezone
    try:
        s = str(closes_at).replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    now = datetime.now(timezone.utc)
    return (dt - now).total_seconds()


async def _render_vote_prompt(bot, cycle_row, nominees, tally) -> discord.Embed:
    """Build the live vote embed with two sections: Choices (alphabetical
    A-Z list of nominees with their nominator) and Standings (ranked by
    votes DESC, ties share rank, zero-vote entries collapsed into a
    tail line).

    ``nominees`` is the ordered list (display order = letter labels A, B, …).
    ``tally`` is the row list from TALLY_VOTES — keyed by nomination_id;
    Choices renders in display order, Standings re-sorts by votes.

    Returns an Embed because the two-section layout would overflow
    Discord's 2000-char message-content cap; the 4096-char embed
    description fits with headroom.

    Async because seasonal cycles get a "· Season N" suffix on their
    period label that needs a reading_logs lookup.
    """
    period_label = await cycle_period_label_with_season(bot, cycle_row)
    kind = cycle_row[CYCLE_KIND] or "monthly"
    title_lead = "VN of the Season Vote" if kind == "seasonal" else "VN of the Month Vote"
    choice_mode = cycle_row[CYCLE_CHOICE_MODE] or "single"
    winner_count = cycle_row[CYCLE_WINNER_COUNT] or 1
    closes_at = cycle_row[CYCLE_CLOSES_AT]

    # tally shape: (nomination_id, vndb_id, title, user_id, guild_id,
    # created_at, votes). Re-key by nomination_id for fast lookup.
    votes_by_nom = {row[0]: row[6] for row in tally}
    total_votes = sum(votes_by_nom.values())
    total_nominees = min(len(nominees), 25)

    meta_lines = [
        f"Mode: `{choice_mode}` · Winners: `{winner_count}` · "
        f"Vote ID: `{cycle_row[CYCLE_ID]}`",
    ]
    rel = _format_closes_at_relative(closes_at)
    if rel:
        meta_lines.append(f"⏱ Closes {rel}")
    allowed_role_id = cycle_row[CYCLE_ALLOWED_ROLE_ID]
    if allowed_role_id:
        meta_lines.append(f"🔒 Allowed role: <@&{allowed_role_id}>")

    # Persist every visible nominator's name so the cache stays warm for
    # future renders even if a nominator later leaves the guild.
    nom_user_ids = [n[NOM_USER_ID] for n in nominees[:total_nominees]]
    await _persist_resolved_users(bot, nom_user_ids)
    # Batch the cache fallback lookup for nominators not in the in-process
    # member cache. One SQLite round-trip instead of N inside the loop.
    missing_nominators = [uid for uid in nom_user_ids if bot.get_user(uid) is None]
    nom_tag_map: dict = {}
    if missing_nominators:
        ph = ",".join("?" * len(missing_nominators))
        rows = await bot.GET(
            f"SELECT discord_user_id, user_tag, user_name FROM users "
            f"WHERE discord_user_id IN ({ph})",
            tuple(missing_nominators),
        )
        # Prefer the unique handle; legacy rows without a tag fall back to
        # display name so the line still identifies the nominator.
        nom_tag_map = {r[0]: (r[1] or r[2]) for r in rows if r[1] or r[2]}

    # ---- Choices section: alphabetical, title link + nominator ----
    choices_lines = ["📋 **Choices**"]
    for idx, n in enumerate(nominees[:total_nominees]):
        letter = _VOTE_LETTERS[idx]
        title = _truncate_label(n[NOM_TITLE], 60)
        # Escape `[` `]` in titles so the markdown link parser doesn't
        # choke on unusual VN names.
        safe_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        title_link = f"[{safe_title}](https://vndb.org/{n[NOM_VNDB_ID]})"
        nom_user = bot.get_user(n[NOM_USER_ID])
        if nom_user is not None:
            nom_tag = nom_user.name
        else:
            nom_tag = nom_tag_map.get(n[NOM_USER_ID]) or "unknown-user"
        choices_lines.append(f"`{letter}` · {title_link} · @{nom_tag}")

    # ---- Standings section: ranked DESC, ties share rank ----
    # Secondary sort by nomination id ASC mirrors TALLY_VOTES' tie-break,
    # so the standings ordering matches the rule used to pick the winner.
    ranked = sorted(
        enumerate(nominees[:total_nominees]),
        key=lambda iv: (-votes_by_nom.get(iv[1][NOM_ID], 0), iv[1][NOM_ID]),
    )
    standings_header = f"📊 **Standings** · {_votes_phrase(total_votes)}"
    standings_body: list[str] = []
    zero_vote_letters: list[str] = []
    prev_votes: Optional[int] = None
    rank = 0
    for display_pos, (idx, n) in enumerate(ranked):
        votes = votes_by_nom.get(n[NOM_ID], 0)
        letter = _VOTE_LETTERS[idx]
        if votes <= 0:
            zero_vote_letters.append(letter)
            continue
        # Standard competition ranking: ties share the same rank, the
        # next distinct count skips past the tied positions
        # (e.g. 1, 2, 3, 3, 5).
        if votes != prev_votes:
            rank = display_pos + 1
            prev_votes = votes
        pct = (votes / total_votes * 100.0) if total_votes else 0.0
        title_short = _truncate_label(n[NOM_TITLE], 40)
        standings_body.append(
            f"`{rank:>2}.` `{letter}` · {title_short} · **{pct:.1f}%** ({votes})"
        )
    if not standings_body and not zero_vote_letters:
        zero_tail: Optional[str] = "_No votes yet._"
    elif zero_vote_letters:
        zero_tail = f"_No votes: {', '.join(zero_vote_letters)}_"
    else:
        zero_tail = None

    footer_text = "Tap a button below or use `/vote` for a personal voting menu."

    # Per-section sizing: Choices is never truncated (knowing who nominated
    # each title is the whole point of that section). Standings shrinks
    # from the bottom if we'd overflow — drop the zero-vote tail first
    # since it's the lowest-information line, then pop ranked entries.
    def _assemble(
        body: list[str], tail: Optional[str], note: Optional[str]
    ) -> str:
        standings = [standings_header, *body]
        if tail is not None:
            standings.append(tail)
        if note is not None:
            standings.append(note)
        return "\n".join(
            meta_lines + [""] + choices_lines + [""] + standings + ["", footer_text]
        )

    fit_body = list(standings_body)
    fit_tail = zero_tail
    overflow_note: Optional[str] = None
    while len(_assemble(fit_body, fit_tail, overflow_note)) > _VOTE_DESC_BUDGET:
        if fit_tail is not None:
            fit_tail = None
        elif fit_body:
            fit_body.pop()
            dropped = len(standings_body) - len(fit_body)
            overflow_note = f"_…{dropped} more in standings._"
        else:
            # Choices + meta + footer alone exceed the budget. Only
            # reachable at the 25-nom cap with pathologically long
            # titles and nominator names. Fall through to the byte-cut.
            break

    description = _assemble(fit_body, fit_tail, overflow_note)
    if len(description) > _VOTE_DESC_BUDGET:
        _log.warning(
            "vote prompt overflowed after Standings trim: %d chars (cap %d)",
            len(description), _VOTE_DESC_BUDGET,
        )
        description = description[:_VOTE_DESC_BUDGET].rstrip() + "\n_…truncated._"
    embed = discord.Embed(
        title=f"🗳️ {title_lead} - {period_label}",
        description=description,
        color=discord.Color.blurple(),
    )
    return embed


async def _refresh_vote_message(bot, cycle_id: int) -> None:
    """Re-render the vote message with the current tally. No-op when the
    cycle is no longer in voting phase or its message can't be fetched.

    Failures here are logged but never raise — vote insertion has already
    succeeded by the time we hit this, and a stale prompt is a far better
    failure mode than a 500 to the voter.
    """
    try:
        cycle = await _cycle_by_id(bot, cycle_id)
        if not cycle or cycle[CYCLE_PHASE] != "voting":
            return
        channel_id = cycle[CYCLE_CHANNEL_ID]
        message_id = cycle[CYCLE_MESSAGE_ID]
        if not channel_id or not message_id:
            return
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        nominees = await bot.GET(DatabaseQueries.GET_CYCLE_NOMINEES, (cycle_id,))
        tally = await bot.GET(DatabaseQueries.TALLY_VOTES, (cycle_id,))
        embed = await _render_vote_prompt(bot, cycle, nominees, tally)
        # Suppress pings — embed has nominator mentions in its description
        # and we'd otherwise notify them on every vote. content=None clears
        # any legacy plain-text content from messages posted before the
        # embed migration.
        await message.edit(
            content=None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as e:
        # Discord rate limits / transient HTTP errors — swallow, the next
        # vote will retry the edit.
        _log.debug("Vote-message refresh hit HTTP error: %s", e)
    except Exception as e:  # noqa: BLE001
        _log.exception("Unexpected error refreshing vote message: %s", e)


async def _handle_participants(interaction: discord.Interaction, cycle_id: int):
    """Spawn the paginated participants panel for this vote. Voters
    are lazy-loaded per nominee on the view; this handler only does the
    initial render for the default-selected first nominee."""
    _log.debug(
        "_handle_participants: entry cycle=%d user=%d",
        cycle_id, interaction.user.id,
    )
    await interaction.response.defer(ephemeral=True)
    bot: VNClubBot = interaction.client  # type: ignore
    cycle = await _cycle_by_id(bot, cycle_id)
    if not cycle:
        await interaction.followup.send(
            "❌ This voting no longer exists.", ephemeral=True,
        )
        return
    # All-status fetch so a closed vote's panel still includes the promoted
    # winner (its row is no longer status='nominated'). Identical to
    # GET_CYCLE_NOMINEES during voting (nothing is promoted yet).
    nominees = await bot.GET(DatabaseQueries.GET_CYCLE_NOMINEES_ALL, (cycle_id,))
    if not nominees:
        await interaction.followup.send(
            f"👥 **Participants — Vote ID `{cycle_id}`**\n\n"
            "_No nominees in this voting._",
            ephemeral=True,
        )
        return
    tally = await bot.GET(DatabaseQueries.TALLY_VOTES, (cycle_id,))
    votes_by_nom = {row[0]: row[6] for row in tally}

    view = ParticipantsView(bot, cycle_id, list(nominees[:25]), votes_by_nom)
    embed = await view.render_embed()
    msg = await interaction.followup.send(
        embed=embed, view=view, ephemeral=True, wait=True,
    )
    view.message = msg


async def _handle_manage_votes(interaction: discord.Interaction, cycle_id: int):
    """Render an ephemeral panel with one Remove button per current vote."""
    _log.debug(
        "_handle_manage_votes: entry cycle=%d user=%d",
        cycle_id, interaction.user.id,
    )
    bot: VNClubBot = interaction.client  # type: ignore
    cycle = await _cycle_by_id(bot, cycle_id)
    if not cycle or cycle[CYCLE_PHASE] != "voting":
        await interaction.response.send_message(
            "❌ This voting is no longer open.", ephemeral=True
        )
        return
    user_id = interaction.user.id
    existing = await bot.GET(
        DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, user_id),
    )
    if not existing:
        await interaction.response.send_message(
            "You haven't voted in this cycle yet.", ephemeral=True
        )
        return

    # Build a nominee-id → letter+title lookup so the Remove buttons render
    # readable labels.
    nominees = await bot.GET(DatabaseQueries.GET_CYCLE_NOMINEES, (cycle_id,))
    label_by_nom: dict[int, str] = {}
    for idx, n in enumerate(nominees[:25]):
        label_by_nom[n[NOM_ID]] = (
            f"{_VOTE_LETTERS[idx]} · {_truncate_label(n[NOM_TITLE], 40)}"
        )

    view = discord.ui.View(timeout=300)
    for vote_row in existing[:25]:
        vote_id, nomination_id = vote_row[0], vote_row[1]
        label = label_by_nom.get(nomination_id, f"Nominee #{nomination_id}")
        # Discord button labels max 80 chars.
        view.add_item(RemoveVoteButton(
            vote_id=vote_id,
            cycle_id=cycle_id,
            nominee_label=_truncate_label(label, 70),
        ))

    lines = ["**Your current votes for this cycle:**"]
    for vote_row in existing:
        nomination_id = vote_row[1]
        lines.append(f"- {label_by_nom.get(nomination_id, f'#{nomination_id}')}")
    lines.append("")
    lines.append("Click a button below to remove a vote.")
    await interaction.response.send_message(
        "\n".join(lines), view=view, ephemeral=True,
    )


async def _handle_remove_vote(interaction: discord.Interaction,
                              cycle_id: int, vote_id: int):
    """Delete a single vote, edit the ephemeral confirmation, refresh the
    main vote message tally.
    """
    _log.debug(
        "_handle_remove_vote: entry cycle=%d user=%d vote=%d",
        cycle_id, interaction.user.id, vote_id,
    )
    bot: VNClubBot = interaction.client  # type: ignore
    cycle = await _cycle_by_id(bot, cycle_id)
    if not cycle or cycle[CYCLE_PHASE] != "voting":
        await interaction.response.send_message(
            "❌ Voting is no longer open — can't remove votes.", ephemeral=True
        )
        return
    # Defensive: ensure the vote actually belongs to the clicker. A clever
    # user can craft custom_ids, so we re-check ownership server-side.
    user_id = interaction.user.id
    existing = await bot.GET(
        DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, user_id),
    )
    if not any(row[0] == vote_id for row in existing):
        await interaction.response.send_message(
            "❌ That vote doesn't belong to you.", ephemeral=True
        )
        return
    await bot.RUN(DatabaseQueries.DELETE_VOTE_BY_ID, (vote_id,))
    _log.info(
        "vote removed via manage panel: cycle=%d user=%d vote=%d",
        cycle_id, user_id, vote_id,
    )
    await interaction.response.send_message(
        "🗑 Vote removed.", ephemeral=True,
    )
    await _refresh_vote_message(bot, cycle_id)


# ==================== CHOICE CONSTANTS ====================


# Surfaced on /nominate and /vote — those still use Discord choice
# dropdowns since they're plain user commands, not the admin dashboard.
CYCLE_KIND_CHOICES = [
    app_commands.Choice(name="Monthly voting", value="monthly"),
    app_commands.Choice(name="Seasonal voting (3-month range)", value="seasonal"),
]

# Buttons-mode caps at 20 nominees because Discord allows 5 buttons × 4 rows
# (row 4 reserved for Participants/Manage-votes utility buttons).
_VOTE_UI_BUTTONS_MAX = 20


def _parse_duration_to_seconds(value: str) -> Optional[int]:
    """Parse a free-form duration token into total seconds.

    Accepted forms:
        - empty / 'none' / '0'              → None (no timer)
        - integer + unit suffix             → e.g. '30s', '10m', '2h', '1d', '1w'
        - bare integer (no suffix)          → treated as hours for back-compat
                                              with the old hours-only modal field

    Seconds and minutes are supported primarily for testing — the
    auto-close background task polls at a 60s cadence, so a sub-minute
    timer fires within ~60s of expiry rather than instantly. Returns
    None for unparseable / non-positive input.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in ("none", "0"):
        return None
    # Last char is a unit suffix; otherwise treat the whole string as hours.
    if s[-1].isalpha():
        unit = s[-1]
        num_part = s[:-1]
    else:
        unit = "h"
        num_part = s
    try:
        n = int(num_part)
    except ValueError:
        return None
    if n <= 0:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    if unit not in multipliers:
        return None
    return n * multipliers[unit]


# ==================== ADMIN DASHBOARD ====================
#
# `/manage_voting` opens an ephemeral panel that shows current cycle state
# and surfaces only the actions valid in that state. Replaces the older
# action-dispatch form of /manage_voting + the separate /manage_settings
# command, which forced admins to recall an action keyword + a parameter
# bag where most params only mattered for one action.
#
# Architecture:
#   _fetch_panel_state — snapshot both kinds' active cycles + guild defaults
#   _build_panel_text  — render the embed-text for the main panel
#   VotingAdminPanelView — main panel; conditionally renders action buttons
#   OpenVotingModal       — 4-input modal; calls _open_voting on submit
#   VotingSettingsPanelView — sub-panel for guild-wide defaults
#
# The action helpers (`_open_voting`, `_post_vote_message`, `_close_voting`,
# `_cancel`) on the cog are reused unchanged — only their callers move.


async def _fetch_panel_state(bot, guild_id: int) -> dict:
    """Snapshot the state needed to render the admin dashboard.

    Pulls both monthly + seasonal active cycles (one of each is allowed
    per guild) and guild-level defaults in parallel-ish form. The
    settings row may be NULL on first invocation; fall back to None.
    """
    monthly = await _active_cycle(bot, guild_id, "monthly")
    seasonal = await _active_cycle(bot, guild_id, "seasonal")
    settings = await bot.GET_ONE(DatabaseQueries.GET_GUILD_SETTINGS, (guild_id,))
    return {
        "guild_id": guild_id,
        "monthly": monthly,
        "seasonal": seasonal,
        "default_voting_role_id": settings[1] if settings else None,
        "default_vote_ui": settings[2] if settings else None,
    }


async def _build_panel_text(bot, state: dict) -> str:
    """Render the main panel's status block from a state dict."""
    lines = ["🛠️ **Voting admin dashboard**", ""]
    for kind, kind_label in (("monthly", "Monthly"), ("seasonal", "Seasonal")):
        cycle = state[kind]
        if cycle is None:
            lines.append(f"• **{kind_label}** — no active cycle.")
            continue
        if cycle[CYCLE_PHASE] != "voting":
            lines.append(
                f"• **{kind_label}** — phase `{cycle[CYCLE_PHASE]}` "
                f"(cycle `{cycle[CYCLE_ID]}`)."
            )
            continue
        period_label = await cycle_period_label_with_season(bot, cycle)
        nominees = await bot.GET(
            DatabaseQueries.GET_CYCLE_NOMINEES, (cycle[CYCLE_ID],),
        )
        n_count = len(nominees)
        if cycle[CYCLE_MESSAGE_ID] is None:
            lines.append(
                f"• **{kind_label}** — voting open for **{period_label}** · "
                f"{n_count} nominee(s) · vote message **not posted yet**"
            )
        else:
            close_str = _format_closes_at_relative(cycle[CYCLE_CLOSES_AT])
            close_part = f" · closes {close_str}" if close_str else ""
            lines.append(
                f"• **{kind_label}** — voting open for **{period_label}** · "
                f"{n_count} nominee(s) · message posted{close_part}"
            )

    role_id = state["default_voting_role_id"]
    role_part = f"<@&{role_id}>" if role_id else "_not set_"
    ui_part = state["default_vote_ui"] or "dropdown"
    lines.append("")
    lines.append(f"**Defaults** — voting role: {role_part} · vote UI: `{ui_part}`")
    return "\n".join(lines)


async def _build_settings_text(state: dict) -> str:
    role_id = state["default_voting_role_id"]
    role_part = f"<@&{role_id}>" if role_id else "_not set_"
    ui_part = state["default_vote_ui"] or "dropdown"
    return (
        "⚙️ **Voting settings**\n\n"
        f"**Default voting role**: {role_part}\n"
        "_Open voting falls back to this role when set._\n\n"
        f"**Default vote UI**: `{ui_part}`\n"
        "_Used as the default when Open voting fires from the dashboard._\n\n"
        "Use the controls below or click **Back** to return to the dashboard."
    )


class VotingAdminPanelView(discord.ui.View):
    """Main admin dashboard. State-aware: button set rebuilt each time
    state changes so admins only see actions valid for the current
    phase. Lives only inside the ephemeral interaction response — not
    persisted, not registered globally; user re-runs `/manage_voting`
    to refresh after the view's 600s timeout.
    """

    def __init__(self, cog, state: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.state = state
        self._build_buttons()

    def _build_buttons(self) -> None:
        """Populate the view's children based on ``self.state``."""
        self.clear_items()
        # Disambiguate kind in button labels only when BOTH kinds are
        # active. Single-kind setups (almost everyone) get terse labels.
        both_active = (
            self.state["monthly"] is not None
            and self.state["seasonal"] is not None
        )
        for kind in ("monthly", "seasonal"):
            cycle = self.state[kind]
            kind_suffix = f" {kind}" if both_active else ""
            if cycle is None:
                self.add_item(_OpenVotingButton(self, kind))
                continue
            if cycle[CYCLE_PHASE] != "voting":
                # Closed/legacy phase — nothing actionable from the panel.
                continue
            # Post is always available during voting phase. First click
            # publishes the menu; subsequent clicks repost it in the current
            # channel (the helper edits the old message to redirect users).
            posted = cycle[CYCLE_MESSAGE_ID] is not None
            self.add_item(_PostMessageButton(self, kind, kind_suffix, posted))
            if posted:
                self.add_item(_CloseVotingButton(self, kind, kind_suffix))
            self.add_item(_CancelVotingButton(self, kind, kind_suffix))
            # Per-vote moderation surface (admin troll-vote removal).
            self.add_item(_ManageVotesAdminButton(self, kind, kind_suffix))
        self.add_item(_RefreshButton(self))
        self.add_item(_ReopenVotingButton(self))
        self.add_item(_SettingsButton(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Re-fetch state and edit the panel message in place. Tolerates
        both the "response not yet sent" path (used by sub-views) and
        the "post-defer" path (used by buttons that defer first)."""
        self.state = await _fetch_panel_state(
            self.cog.bot, interaction.guild.id,
        )
        self._build_buttons()
        content = await _build_panel_text(self.cog.bot, self.state)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, view=self)
        else:
            await interaction.response.edit_message(content=content, view=self)


class _OpenVotingButton(discord.ui.Button):
    """Opens the modal collecting target_month / mode / winners / duration."""

    def __init__(self, panel: VotingAdminPanelView, kind: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=f"Open {kind} voting",
            emoji="🗳️",
        )
        self.panel = panel
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(OpenVotingModal(self.panel, self.kind))


class _PostMessageButton(discord.ui.Button):
    def __init__(self, panel: VotingAdminPanelView, kind: str, kind_suffix: str, is_repost: bool):
        verb = "Repost" if is_repost else "Post"
        super().__init__(
            style=discord.ButtonStyle.success,
            label=f"{verb}{kind_suffix} vote message",
            emoji="📢",
        )
        self.panel = panel
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.panel.cog._post_vote_message(interaction, kind=self.kind)
        except ValidationError as e:
            await interaction.followup.send(f"❌ {e.user_message}", ephemeral=True)
        except Exception:  # noqa: BLE001
            _log.exception("panel post_vote_message failed")
            await interaction.followup.send(
                "❌ Could not post vote message. Check the bot logs for details.",
                ephemeral=True,
            )
        await self.panel.refresh(interaction)


class _CloseVotingButton(discord.ui.Button):
    def __init__(self, panel: VotingAdminPanelView, kind: str, kind_suffix: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Close{kind_suffix} voting",
            emoji="🔒",
        )
        self.panel = panel
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.panel.cog._close_voting(interaction, kind=self.kind)
        except ValidationError as e:
            await interaction.followup.send(f"❌ {e.user_message}", ephemeral=True)
        except Exception:  # noqa: BLE001
            _log.exception("panel close_voting failed")
            await interaction.followup.send(
                "❌ Close failed. Check the bot logs for details.",
                ephemeral=True,
            )
        await self.panel.refresh(interaction)


class _CancelVotingButton(discord.ui.Button):
    def __init__(self, panel: VotingAdminPanelView, kind: str, kind_suffix: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"Cancel{kind_suffix} voting",
            emoji="🛑",
        )
        self.panel = panel
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.panel.cog._cancel(interaction, kind=self.kind)
        except ValidationError as e:
            await interaction.followup.send(f"❌ {e.user_message}", ephemeral=True)
        except Exception:  # noqa: BLE001
            _log.exception("panel cancel failed")
            await interaction.followup.send(
                "❌ Cancel failed. Check the bot logs for details.",
                ephemeral=True,
            )
        await self.panel.refresh(interaction)


class _RefreshButton(discord.ui.Button):
    def __init__(self, panel: VotingAdminPanelView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Refresh",
            emoji="🔄",
            row=4,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await self.panel.refresh(interaction)


class _SettingsButton(discord.ui.Button):
    def __init__(self, panel: VotingAdminPanelView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Settings…",
            emoji="⚙️",
            row=4,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        settings_view = VotingSettingsPanelView(self.panel)
        content = await _build_settings_text(self.panel.state)
        await interaction.response.edit_message(content=content, view=settings_view)


class _ReopenVotingButton(discord.ui.Button):
    """Reopen a previously-closed cycle by its Vote ID.

    Always visible on the panel — closed cycles aren't surfaced in the
    main status block, so without this button there's no in-bot way to
    recover from a premature close. Validation lives in the cog helper
    (`_reopen_voting`): rejects the ID if it doesn't belong to this
    guild, isn't actually closed, or another cycle of the same kind is
    already running.
    """

    def __init__(self, panel: VotingAdminPanelView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Reopen voting…",
            emoji="♻️",
            row=4,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReopenVotingModal(self.panel))


class OpenVotingModal(discord.ui.Modal):
    """Modal collecting the four inputs needed to open a voting cycle.

    Cycle ``kind`` is fixed by the panel button (one button per kind),
    so the modal doesn't ask. ``vote_ui`` and ``allowed_role_id`` come
    from the guild's defaults rather than being per-cycle prompts —
    those settings live in the Settings sub-panel instead.

    For seasonal cycles, the modal accepts any month within the season;
    the on_submit handler derives ``(season, year)`` from it before
    calling the helper.
    """

    def __init__(self, panel: VotingAdminPanelView, kind: str):
        kind_label = "monthly" if kind == "monthly" else "seasonal"
        super().__init__(title=f"Open {kind_label} voting", timeout=600)
        self.panel = panel
        self.kind = kind

        next_default = _next_month(get_current_month())
        self.target_month_input = discord.ui.TextInput(
            label="Target month (YYYY-MM)",
            placeholder=f"e.g. {next_default}",
            default=next_default,
            required=True,
            max_length=7,
        )
        self.choice_mode_input = discord.ui.TextInput(
            label="Choice mode (single or multi)",
            placeholder="single = one vote per user · multi = pick several",
            default="single",
            required=True,
            max_length=10,
        )
        self.winner_count_input = discord.ui.TextInput(
            label="Winner count (1-10)",
            placeholder="1",
            default="1",
            required=True,
            max_length=2,
        )
        self.duration_input = discord.ui.TextInput(
            label="Auto-close after (e.g. 30s, 10m, 2h, 1d, 1w)",
            placeholder="0 (no timer) · 30s · 10m · 2h · 1d · 1w",
            default="0",
            required=True,
            max_length=8,
        )
        self.add_item(self.target_month_input)
        self.add_item(self.choice_mode_input)
        self.add_item(self.winner_count_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate input fields BEFORE deferring so a validation error can
        # respond with an ephemeral send_message that doesn't disturb the
        # parent panel. Once we defer (DeferredUpdateMessage path), the
        # interaction's "original response" becomes the panel itself.
        try:
            target = (self.target_month_input.value or "").strip()
            if not validate_month_format(target):
                raise ValidationError(
                    "bad month",
                    "Target month must be `YYYY-MM` (e.g. `2026-06`).",
                )

            mode_raw = (self.choice_mode_input.value or "").strip().lower()
            if mode_raw in ("single", "s", "one"):
                choice_mode = "single"
            elif mode_raw in ("multi", "multiple", "m"):
                choice_mode = "multi"
            else:
                raise ValidationError(
                    "bad choice_mode",
                    "Choice mode must be `single` or `multi`.",
                )

            try:
                winner_count = int(
                    (self.winner_count_input.value or "1").strip()
                )
            except ValueError:
                raise ValidationError(
                    "bad winner_count", "Winner count must be a number.",
                )
            if winner_count < 1 or winner_count > 10:
                raise ValidationError(
                    "bad winner_count",
                    "Winner count must be between 1 and 10.",
                )

            # Free-form duration: integer + optional unit suffix.
            # Bare integers fall back to hours for back-compat; explicit
            # 's' / 'm' / 'h' / 'd' / 'w' all supported (s+m mainly for
            # testing — auto-close polls every 60s).
            raw_duration = (self.duration_input.value or "").strip()
            if raw_duration.lower() in ("", "0", "none"):
                duration_token: Optional[str] = None
            else:
                duration_secs = _parse_duration_to_seconds(raw_duration)
                if duration_secs is None or duration_secs <= 0:
                    raise ValidationError(
                        "bad duration",
                        "Duration must be a positive number with an "
                        "optional unit suffix — `30s`, `10m`, `2h`, `1d`, "
                        "`1w` (or a bare integer for hours). Use `0` for "
                        "no timer.",
                    )
                if duration_secs > 30 * 86400:
                    raise ValidationError(
                        "bad duration",
                        "Duration cannot exceed 30 days.",
                    )
                duration_token = raw_duration

            # For seasonal cycles, derive (season, year) from target_month
            # — _open_voting requires those args explicitly and ignores
            # target_month for kind='seasonal'.
            if self.kind == "seasonal":
                year_int = int(target[:4])
                month_int = int(target[5:7])
                try:
                    season_name = month_to_season_name(month_int)
                except ValueError:
                    raise ValidationError(
                        "bad month",
                        "Target month must be a valid calendar month.",
                    )
                season_arg: Optional[str] = season_name
                year_arg: Optional[int] = year_int
                target_month_arg: Optional[str] = None
            else:
                season_arg = None
                year_arg = None
                target_month_arg = target
        except ValidationError as e:
            await interaction.response.send_message(
                f"❌ {e.user_message}", ephemeral=True,
            )
            return

        # Inputs valid — defer with thinking=False so the response is a
        # DeferredUpdateMessage tied to the parent panel; followups can
        # still send ephemeral confirmations independently.
        await interaction.response.defer()

        try:
            await self.panel.cog._open_voting(
                interaction,
                kind=self.kind,
                choice_mode=choice_mode,
                winner_count=winner_count,
                target_month=target_month_arg,
                season=season_arg,
                year=year_arg,
                duration=duration_token,
                vote_ui=None,         # sourced from guild defaults inside helper
                allowed_role_id=None,  # ditto
            )
        except ValidationError as e:
            await interaction.followup.send(
                f"❌ {e.user_message}", ephemeral=True,
            )
        except Exception:  # noqa: BLE001
            _log.exception("OpenVotingModal submit failed")
            await interaction.followup.send(
                "❌ Could not open voting. Check the bot logs for details.",
                ephemeral=True,
            )
        # Always refresh the panel — _open_voting may have created a
        # cycle-then-rolled-back (no nominees) or partially mutated state.
        await self.panel.refresh(interaction)


class ReopenVotingModal(discord.ui.Modal):
    """Reopen a closed cycle in place by its Vote ID.

    The cycle id is the user-facing "Vote ID" shown on every vote
    message header. The other three fields mirror OpenVotingModal so an
    admin can adjust voting settings on the way back in (e.g. flip from
    single→multi, raise winner_count, attach a timer). Each is optional
    — leaving a field blank keeps the cycle's existing value.
    Submitting flips the cycle back to voting phase, demotes any
    promoted winner(s) for the period back to nominations, re-sweeps
    those nominations onto this cycle, and clears the message pointers
    so the admin can publish a fresh menu via Repost.
    """

    def __init__(self, panel: VotingAdminPanelView):
        super().__init__(title="Reopen a closed vote", timeout=600)
        self.panel = panel
        # Discord caps modal input labels at 45 chars — any value above
        # that and the modal fails to open ("interaction failed" with no
        # body). All four labels below stay under 45 deliberately.
        self.cycle_id_input = discord.ui.TextInput(
            label="Vote ID (from the closed vote message)",
            placeholder="e.g. 6",
            required=True,
            max_length=10,
        )
        self.choice_mode_input = discord.ui.TextInput(
            label="Choice mode — blank = keep",
            placeholder="single or multi",
            required=False,
            max_length=10,
        )
        self.winner_count_input = discord.ui.TextInput(
            label="Winner count — blank = keep",
            placeholder="1-10",
            required=False,
            max_length=2,
        )
        self.duration_input = discord.ui.TextInput(
            label="Auto-close — blank = keep, 0 = none",
            placeholder="30s · 10m · 2h · 1d · 1w · 0",
            required=False,
            max_length=8,
        )
        self.add_item(self.cycle_id_input)
        self.add_item(self.choice_mode_input)
        self.add_item(self.winner_count_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Parse all inputs BEFORE deferring so validation errors can
        # respond with send_message ephemerally (won't disturb the
        # parent panel). Vote ID is the only required field; the rest
        # default to "keep current" when blank.
        raw_id = (self.cycle_id_input.value or "").strip()
        try:
            target_cycle_id = int(raw_id)
        except ValueError:
            await interaction.response.send_message(
                "❌ Vote ID must be a number.", ephemeral=True,
            )
            return
        if target_cycle_id <= 0:
            await interaction.response.send_message(
                "❌ Vote ID must be positive.", ephemeral=True,
            )
            return

        choice_mode_override: Optional[str] = None
        winner_count_override: Optional[int] = None
        duration_override: Optional[str] = None
        try:
            mode_raw = (self.choice_mode_input.value or "").strip().lower()
            if mode_raw:
                if mode_raw in ("single", "s", "one"):
                    choice_mode_override = "single"
                elif mode_raw in ("multi", "multiple", "m"):
                    choice_mode_override = "multi"
                else:
                    raise ValidationError(
                        "bad choice_mode",
                        "Choice mode must be `single` or `multi` (or blank).",
                    )

            wc_raw = (self.winner_count_input.value or "").strip()
            if wc_raw:
                try:
                    winner_count_override = int(wc_raw)
                except ValueError:
                    raise ValidationError(
                        "bad winner_count",
                        "Winner count must be a number (or blank).",
                    )
                if winner_count_override < 1 or winner_count_override > 10:
                    raise ValidationError(
                        "bad winner_count",
                        "Winner count must be between 1 and 10.",
                    )

            dur_raw = (self.duration_input.value or "").strip()
            if dur_raw:
                if dur_raw.lower() in ("0", "none"):
                    # Explicit "no timer" override — distinguish from blank
                    # ("keep current"). Sentinel handled in cog helper.
                    duration_override = "0"
                else:
                    secs = _parse_duration_to_seconds(dur_raw)
                    if secs is None or secs <= 0:
                        raise ValidationError(
                            "bad duration",
                            "Duration must be a positive number with an "
                            "optional unit suffix — `30s`, `10m`, `2h`, "
                            "`1d`, `1w` (or `0` for no timer; blank to "
                            "keep current).",
                        )
                    if secs > 30 * 86400:
                        raise ValidationError(
                            "bad duration",
                            "Duration cannot exceed 30 days.",
                        )
                    duration_override = dur_raw
        except ValidationError as e:
            await interaction.response.send_message(
                f"❌ {e.user_message}", ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            await self.panel.cog._reopen_voting(
                interaction, target_cycle_id,
                choice_mode=choice_mode_override,
                winner_count=winner_count_override,
                duration=duration_override,
            )
        except ValidationError as e:
            await interaction.followup.send(
                f"❌ {e.user_message}", ephemeral=True,
            )
        except Exception:  # noqa: BLE001
            _log.exception("ReopenVotingModal submit failed")
            await interaction.followup.send(
                "❌ Could not reopen voting. Check the bot logs for details.",
                ephemeral=True,
            )
        await self.panel.refresh(interaction)


class VotingSettingsPanelView(discord.ui.View):
    """Sub-panel for the guild-wide defaults: voting role + vote UI."""

    def __init__(self, parent_panel: VotingAdminPanelView):
        super().__init__(timeout=600)
        self.parent_panel = parent_panel
        self.cog = parent_panel.cog
        self.state = parent_panel.state
        self.add_item(_DefaultRoleSelect(self))
        self.add_item(_ClearRoleButton(self))
        self.add_item(_DefaultVoteUiSelect(self))
        self.add_item(_BackToPanelButton(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.state = await _fetch_panel_state(
            self.cog.bot, interaction.guild.id,
        )
        self.parent_panel.state = self.state
        # Rebuild the children so the vote-UI Select reflects the new
        # default value (the discord.SelectOption.default flag is set
        # at construction time and isn't mutable on the live component).
        self.clear_items()
        self.add_item(_DefaultRoleSelect(self))
        self.add_item(_ClearRoleButton(self))
        self.add_item(_DefaultVoteUiSelect(self))
        self.add_item(_BackToPanelButton(self))
        content = await _build_settings_text(self.state)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, view=self)
        else:
            await interaction.response.edit_message(content=content, view=self)


class _DefaultRoleSelect(discord.ui.RoleSelect):
    def __init__(self, settings_view: VotingSettingsPanelView):
        super().__init__(
            placeholder="Set the default voting role…",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.settings_view = settings_view

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        await self.settings_view.cog.bot.RUN(
            DatabaseQueries.UPSERT_DEFAULT_VOTING_ROLE,
            (interaction.guild.id, role.id),
        )
        await self.settings_view.refresh(interaction)


class _ClearRoleButton(discord.ui.Button):
    def __init__(self, settings_view: VotingSettingsPanelView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Clear default role",
            emoji="🧹",
            row=1,
        )
        self.settings_view = settings_view

    async def callback(self, interaction: discord.Interaction):
        await self.settings_view.cog.bot.RUN(
            DatabaseQueries.CLEAR_DEFAULT_VOTING_ROLE,
            (interaction.guild.id,),
        )
        await self.settings_view.refresh(interaction)


class _DefaultVoteUiSelect(discord.ui.Select):
    def __init__(self, settings_view: VotingSettingsPanelView):
        current = settings_view.state["default_vote_ui"] or "dropdown"
        options = [
            discord.SelectOption(
                label="Dropdown (default)",
                value="dropdown",
                description="Single Select with up to 25 options.",
                default=(current == "dropdown"),
            ),
            discord.SelectOption(
                label="Buttons",
                value="buttons",
                description="One button per nominee, max 20 nominees.",
                default=(current == "buttons"),
            ),
        ]
        super().__init__(
            placeholder="Default vote UI…",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )
        self.settings_view = settings_view

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        await self.settings_view.cog.bot.RUN(
            DatabaseQueries.UPSERT_DEFAULT_VOTE_UI,
            (interaction.guild.id, choice),
        )
        await self.settings_view.refresh(interaction)


class _BackToPanelButton(discord.ui.Button):
    def __init__(self, settings_view: VotingSettingsPanelView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Back",
            emoji="↩",
            row=3,
        )
        self.settings_view = settings_view

    async def callback(self, interaction: discord.Interaction):
        # Re-fetch state in case settings changed while the sub-panel was open.
        new_state = await _fetch_panel_state(
            self.settings_view.cog.bot, interaction.guild.id,
        )
        new_panel = VotingAdminPanelView(self.settings_view.cog, new_state)
        content = await _build_panel_text(self.settings_view.cog.bot, new_state)
        await interaction.response.edit_message(content=content, view=new_panel)


# ==================== VOTE MODERATION SUB-PANEL ====================
#
# Surgical per-vote removal for admins (troll votes, duplicates, etc.).
# Cancel voting nukes a whole cycle; this lets a mod yank one row.
# Reached via the Manage votes button on the main dashboard.


def _format_relative_age(created_at) -> str:
    """Short relative timestamp for the moderation Select description.

    Falls back to an empty string when created_at is missing or
    unparseable; the Select option just omits the description in that
    case (Discord allows None descriptions).
    """
    if not created_at:
        return ""
    from datetime import datetime, timezone
    try:
        s = str(created_at).replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
    except (ValueError, TypeError):
        return ""
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


class VoteModerationPanelView(discord.ui.View):
    """Sub-panel for individual-vote removal. State refreshed on every
    interaction to reflect concurrent admin actions / vote churn.
    """

    def __init__(self, parent_panel: VotingAdminPanelView, kind: str,
                 cycle_row, votes: list):
        super().__init__(timeout=600)
        self.parent_panel = parent_panel
        self.cog = parent_panel.cog
        self.kind = kind
        self.cycle_row = cycle_row
        self.votes = votes  # rows: (vote_id, user_id, nomination_id, title, created_at)
        self.selected_vote_id: Optional[int] = None
        # Page index for the Select. Each page shows up to 25 votes
        # ordered most-recent-first. Pagination buttons appear only
        # when there are more than 25 votes total.
        self.page: int = 0
        self._build_children()

    def _build_children(self) -> None:
        self.clear_items()
        self.add_item(_VoteModerationSelect(self))
        self.add_item(_RemoveVoteAdminButton(self))
        # Only show pagination controls when they're actually needed.
        # Keeps the panel uncluttered for the common <=25 case.
        if len(self.votes) > 25:
            self.add_item(_NewerPageButton(self))
            self.add_item(_OlderPageButton(self))
        self.add_item(_BackToPanelFromModerationButton(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Re-fetch the cycle's vote list and re-render the sub-panel."""
        cycle = await _cycle_by_id(self.cog.bot, self.cycle_row[CYCLE_ID])
        if cycle is None or cycle[CYCLE_PHASE] != "voting":
            # Cycle moved on (closed / cancelled by another admin). Pop
            # back to the main panel so the user sees current state.
            await self._pop_to_main(interaction)
            return
        self.cycle_row = cycle
        self.votes = await self.cog.bot.GET(
            DatabaseQueries.GET_ALL_VOTES_IN_CYCLE, (cycle[CYCLE_ID],),
        )
        # Clamp page so the admin stays on the highest still-valid page
        # rather than snapping back to 0 after every remove. Useful when
        # they're paging through old votes to clean up: they keep their
        # place until the page itself becomes empty.
        max_page = max(0, (len(self.votes) - 1) // 25)
        self.page = min(self.page, max_page)
        self.selected_vote_id = None
        self._build_children()
        content = _build_moderation_text(
            self.kind, self.cycle_row, self.votes, page=self.page,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, view=self)
        else:
            await interaction.response.edit_message(content=content, view=self)

    async def _pop_to_main(self, interaction: discord.Interaction) -> None:
        new_state = await _fetch_panel_state(self.cog.bot, interaction.guild.id)
        new_panel = VotingAdminPanelView(self.cog, new_state)
        content = await _build_panel_text(self.cog.bot, new_state)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=content, view=new_panel)
        else:
            await interaction.response.edit_message(content=content, view=new_panel)


def _build_moderation_text(kind: str, cycle_row, votes: list, page: int = 0) -> str:
    """Body text for the moderation sub-panel.

    Surfaces total count and, when more than 25 votes exist, the current
    page's range. Discord caps a Select at 25 options, so longer vote
    histories paginate via the Newer/Older buttons.
    """
    cycle_id = cycle_row[CYCLE_ID]
    total = len(votes)
    lines = [
        f"🛠 **Vote moderation — {kind} vote ID `{cycle_id}`**",
        "",
        f"Total votes: **{total}**",
    ]
    if total == 0:
        lines.append("_No votes cast yet._")
    elif total > 25:
        start = page * 25 + 1
        end = min((page + 1) * 25, total)
        lines.append(
            f"_Showing votes {start}–{end} of {total}. "
            f"Use **◀ Newer** / **Older ▶** to navigate._"
        )
    lines.append("")
    lines.append("Pick a vote and click **Remove selected vote** to delete it.")
    return "\n".join(lines)


class _VoteModerationSelect(discord.ui.Select):
    def __init__(self, mod_view: VoteModerationPanelView):
        # Resolve member display names lazily — the guild from the click
        # interaction context is what we want; here we only have access
        # to the bot. Fall back to "User {id}" for users who left.
        guild = mod_view.cog.bot.get_guild(mod_view.cycle_row[CYCLE_GUILD_ID])
        options: list[discord.SelectOption] = []
        start = mod_view.page * 25
        end = start + 25
        for row in mod_view.votes[start:end]:
            vote_id, user_id, _, title, created_at = row
            member = guild.get_member(user_id) if guild else None
            uname = member.display_name if member else f"User {user_id}"
            label = _truncate_label(f"@{uname} → {title}", 100)
            desc = _format_relative_age(created_at)
            options.append(discord.SelectOption(
                label=label,
                value=str(vote_id),
                description=f"voted {desc}" if desc else None,
                default=(vote_id == mod_view.selected_vote_id),
            ))
        if not options:
            # Discord requires at least one option per Select. Use a
            # disabled placeholder when there's nothing to show.
            options = [discord.SelectOption(
                label="(no votes to remove)", value="__none__",
            )]
            super().__init__(
                placeholder="No votes to remove",
                min_values=1, max_values=1,
                options=options,
                disabled=True,
                row=0,
            )
        else:
            super().__init__(
                placeholder="Pick a vote to remove…",
                min_values=1, max_values=1,
                options=options,
                row=0,
            )
        self.mod_view = mod_view

    async def callback(self, interaction: discord.Interaction):
        try:
            self.mod_view.selected_vote_id = int(self.values[0])
        except (TypeError, ValueError):
            self.mod_view.selected_vote_id = None
        # Just re-render so the Remove button enables and the Select
        # remembers the highlighted choice. No DB write yet.
        self.mod_view._build_children()
        content = _build_moderation_text(
            self.mod_view.kind, self.mod_view.cycle_row, self.mod_view.votes,
            page=self.mod_view.page,
        )
        await interaction.response.edit_message(content=content, view=self.mod_view)


class _NewerPageButton(discord.ui.Button):
    """Step toward more recent votes (page 0 = newest). Disabled at page 0."""

    def __init__(self, mod_view: VoteModerationPanelView):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Newer",
            emoji="◀",
            row=2,
            disabled=mod_view.page <= 0,
        )
        self.mod_view = mod_view

    async def callback(self, interaction: discord.Interaction):
        self.mod_view.page = max(0, self.mod_view.page - 1)
        # Clear selection — the highlighted vote may not be on the new page.
        self.mod_view.selected_vote_id = None
        self.mod_view._build_children()
        content = _build_moderation_text(
            self.mod_view.kind, self.mod_view.cycle_row, self.mod_view.votes,
            page=self.mod_view.page,
        )
        await interaction.response.edit_message(content=content, view=self.mod_view)


class _OlderPageButton(discord.ui.Button):
    """Step toward older votes. Disabled when already on the last page."""

    def __init__(self, mod_view: VoteModerationPanelView):
        max_page = max(0, (len(mod_view.votes) - 1) // 25)
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Older",
            emoji="▶",
            row=2,
            disabled=mod_view.page >= max_page,
        )
        self.mod_view = mod_view

    async def callback(self, interaction: discord.Interaction):
        max_page = max(0, (len(self.mod_view.votes) - 1) // 25)
        self.mod_view.page = min(max_page, self.mod_view.page + 1)
        self.mod_view.selected_vote_id = None
        self.mod_view._build_children()
        content = _build_moderation_text(
            self.mod_view.kind, self.mod_view.cycle_row, self.mod_view.votes,
            page=self.mod_view.page,
        )
        await interaction.response.edit_message(content=content, view=self.mod_view)


class _RemoveVoteAdminButton(discord.ui.Button):
    def __init__(self, mod_view: VoteModerationPanelView):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Remove selected vote",
            emoji="🗑",
            row=1,
            disabled=mod_view.selected_vote_id is None or not mod_view.votes,
        )
        self.mod_view = mod_view

    async def callback(self, interaction: discord.Interaction):
        if self.mod_view.selected_vote_id is None:
            await interaction.response.send_message(
                "❌ Pick a vote from the dropdown first.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        # Capture the chosen row's metadata for the confirmation message
        # before we delete it (otherwise the refresh loses the title).
        target = next(
            (r for r in self.mod_view.votes
             if r[0] == self.mod_view.selected_vote_id),
            None,
        )
        await self.mod_view.cog.bot.RUN(
            DatabaseQueries.DELETE_VOTE_BY_ID,
            (self.mod_view.selected_vote_id,),
        )
        # Refresh the public vote message tally (no-op when the cycle
        # is no longer in voting phase or its message is gone).
        await _refresh_vote_message(
            self.mod_view.cog.bot, self.mod_view.cycle_row[CYCLE_ID],
        )
        if target:
            _, user_id, _, title, _ = target
            await interaction.followup.send(
                f"🗑 Removed <@{user_id}>'s vote for **{title}**.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await self.mod_view.refresh(interaction)


class _BackToPanelFromModerationButton(discord.ui.Button):
    def __init__(self, mod_view: VoteModerationPanelView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Back",
            emoji="↩",
            row=2,
        )
        self.mod_view = mod_view

    async def callback(self, interaction: discord.Interaction):
        await self.mod_view._pop_to_main(interaction)


class _ManageVotesAdminButton(discord.ui.Button):
    """Main-panel button that opens VoteModerationPanelView for one cycle."""

    def __init__(self, panel: VotingAdminPanelView, kind: str, kind_suffix: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"Manage{kind_suffix} votes",
            emoji="🛠",
        )
        self.panel = panel
        self.kind = kind

    async def callback(self, interaction: discord.Interaction):
        cycle = self.panel.state[self.kind]
        if cycle is None or cycle[CYCLE_PHASE] != "voting":
            await interaction.response.send_message(
                "❌ This cycle is no longer in the voting phase.",
                ephemeral=True,
            )
            return
        votes = await self.panel.cog.bot.GET(
            DatabaseQueries.GET_ALL_VOTES_IN_CYCLE, (cycle[CYCLE_ID],),
        )
        mod_view = VoteModerationPanelView(self.panel, self.kind, cycle, votes)
        content = _build_moderation_text(self.kind, cycle, votes)
        await interaction.response.edit_message(content=content, view=mod_view)


# ==================== COG ====================


class VNCycleCog(commands.Cog):
    """Nomination/vote cycle commands."""

    def __init__(self, bot: VNClubBot):
        self.bot = bot
        # Per-cycle scheduled close tasks (asyncio.create_task wrappers
        # that sleep until closes_at and then fire _close_voting). Keyed
        # by cycle_id so reopens / settings edits can cancel and reschedule
        # cleanly. The 60s polling loop stays as a recovery safety net.
        self._close_tasks: dict[int, asyncio.Task] = {}
        # Per-cycle exclusion locks for `_close_voting`. The scheduled
        # close task and the 60s polling loop can both observe
        # phase='voting' for the same cycle in the same instant; the DB
        # promote+close transaction is idempotent but the post-commit
        # winner-banner channel.send loop is not — without this lock,
        # a racing pair would post the winner banner twice. Acquired
        # right after we resolve cycle_id and held through the entire
        # close path including the banner posts.
        self._close_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        await self._register_persistent_vote_views()

    async def cog_unload(self):
        self._auto_close_loop.cancel()
        # Cancel every scheduled close task on shutdown so we don't leak
        # warnings about pending asyncio tasks.
        for task in list(self._close_tasks.values()):
            if not task.done():
                task.cancel()
        self._close_tasks.clear()

    @commands.Cog.listener()
    async def on_ready(self):
        # Start the auto-close poller from on_ready (not cog_load).
        # cog_load runs during setup_hook before the gateway is connected
        # — a tasks.loop scheduled then can silently fail to begin
        # ticking (observed: role_rewards which uses the on_ready pattern
        # runs reliably, while this cog's loop didn't fire at all in
        # multi-hour testing). on_ready can fire multiple times across
        # gateway RESUMEs; is_running() guards against double-start.
        if not self._auto_close_loop.is_running():
            self._auto_close_loop.start()
        # Re-attach exact-time close tasks for cycles already in flight.
        # On gateway RESUME this re-creates tasks (the dict-replacement
        # in _schedule_close cancels the previous one first), which is
        # cheap enough to not bother debouncing.
        try:
            rows = await self.bot.GET(
                "SELECT id, closes_at FROM vn_cycles "
                "WHERE phase='voting' AND closes_at IS NOT NULL"
            )
        except Exception:  # noqa: BLE001
            _log.exception("scheduled_close: failed to re-attach on on_ready")
            return
        for cid, closes_at in rows:
            if closes_at:
                self._schedule_close(int(cid), str(closes_at))

    def _schedule_close(self, cycle_id: int, closes_at_iso: str) -> None:
        """Schedule (or reschedule) a one-shot task that closes
        ``cycle_id`` at ``closes_at_iso`` (UTC). Cancels any existing
        scheduled task for the same cycle first so reopens / settings
        edits don't pile up duplicate firings.
        """
        self._cancel_scheduled_close(cycle_id)
        secs = _seconds_until(closes_at_iso)
        if secs is None:
            return
        delay = max(0.0, secs)

        async def _runner():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                # Let cancellation propagate (shutdown / explicit reschedule).
                # Returning would skip the finally cleanup in
                # ``_close_cycle_by_id_safely`` and leave a stale entry in
                # ``_close_tasks`` after bot shutdown.
                self._close_tasks.pop(cycle_id, None)
                raise
            await self._close_cycle_by_id_safely(cycle_id)

        self._close_tasks[cycle_id] = asyncio.create_task(_runner())

    def _cancel_scheduled_close(self, cycle_id: int) -> None:
        """Cancel a per-cycle close task (if any) — used by manual
        Close / Cancel paths so the scheduled fire-time doesn't race
        the manual action.

        Self-cancellation guard: when ``_close_voting`` is reached via
        the scheduled-close path itself, the running task IS the one
        recorded in ``self._close_tasks[cycle_id]``. Cancelling it
        would inject ``CancelledError`` at the next await inside
        ``_close_voting``, aborting the close mid-flight (we hit this
        IRL — the polling loop had to mop up 46s later). Detect
        running-task identity and just drop the dict entry without
        cancelling — the task is already doing the close anyway, and
        will exit normally after.
        """
        task = self._close_tasks.get(cycle_id)
        if task is None:
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        self._close_tasks.pop(cycle_id, None)
        if current is task:
            return
        if not task.done():
            task.cancel()

    async def _close_cycle_by_id_safely(self, cycle_id: int) -> None:
        """Re-fetch the cycle and route through ``_close_voting`` if
        it's still in voting phase. No-op when the cycle has already
        moved on (manually closed, cancelled, or guild gone)."""
        try:
            cycle = await _cycle_by_id(self.bot, cycle_id)
            if cycle is None or cycle[CYCLE_PHASE] != "voting":
                return
            guild = self.bot.get_guild(cycle[CYCLE_GUILD_ID])
            if guild is None:
                _log.warning(
                    "scheduled_close: guild %s no longer accessible; "
                    "skipping cycle %s", cycle[CYCLE_GUILD_ID], cycle_id,
                )
                return
            _log.info(
                "scheduled_close: firing close for cycle %s (kind=%s)",
                cycle_id, cycle[CYCLE_KIND],
            )
            await self._close_voting(
                interaction=None,
                kind=cycle[CYCLE_KIND] or "monthly",
                guild=guild,
                expected_cycle_id=cycle_id,
            )
        except Exception:  # noqa: BLE001
            _log.exception("scheduled_close: cycle %s close failed", cycle_id)
        finally:
            self._close_tasks.pop(cycle_id, None)

    async def _register_persistent_vote_views(self):
        """On boot, reattach a VoteView to every phase='voting' cycle so
        button presses on existing announcement messages keep working.

        ``LIST_VOTING_CYCLES`` SELECT order:
            (id, guild_id, channel_id, message_id, choice_mode,
             winner_count, kind, target_end_month, closes_at, vote_ui)
        """
        rows = await self.bot.GET(DatabaseQueries.LIST_VOTING_CYCLES)
        attached = 0
        skipped_no_nominees = 0
        failed = 0
        for row in rows:
            cycle_id = row[0]
            # Per-cycle try/except — one bad cycle (deleted channel,
            # add_view rejection, transient DB hiccup) must not break
            # the loop and leave subsequent cycles' vote buttons
            # silently dead. Log and continue; the 60s auto_close
            # poller is the eventual cleanup mechanism for cycles
            # whose state has drifted.
            try:
                vote_ui = row[9]
                nominees = await self.bot.GET(
                    DatabaseQueries.GET_CYCLE_NOMINEES, (cycle_id,)
                )
                # Discord rejects a Select with min_values=1 and zero
                # options, so skip re-registering vote views for cycles
                # that have lost all their nominees (admin removed them
                # while voting was open). The cycle is effectively
                # unrecoverable for voting — closing it would have
                # nothing to tally — so silently skipping the view
                # rebind is fine; the auto_close loop will eventually
                # tidy it up.
                if not nominees:
                    _log.warning(
                        "register_vote_views: skipping cycle %s (no nominees)",
                        cycle_id,
                    )
                    skipped_no_nominees += 1
                    continue
                self.bot.add_view(VoteView(
                    cycle_id=cycle_id, nominees=nominees, vote_ui=vote_ui,
                ))
                attached += 1
            except Exception:  # noqa: BLE001
                _log.exception(
                    "register_vote_views: failed to re-register cycle %s; "
                    "vote buttons on its announcement message will not work "
                    "until the cycle closes",
                    cycle_id,
                )
                failed += 1
                continue
        # Closed cycles keep a participants-only view so the Participants
        # button on a closed vote survives restarts. The voting loop above
        # only re-attaches full VoteViews for cycles still in voting; a closed
        # cycle has no live VoteView, so without this its participants
        # custom_id would be unregistered and the button would go dead.
        closed_attached = 0
        try:
            closed_rows = await self.bot.GET(DatabaseQueries.LIST_CLOSED_CYCLES)
        except Exception:  # noqa: BLE001
            _log.exception("register_vote_views: failed to list closed cycles")
            closed_rows = []
        for crow in closed_rows:
            cid = crow[0]
            try:
                self.bot.add_view(ClosedVoteView(cid))
                closed_attached += 1
            except Exception:  # noqa: BLE001
                _log.exception(
                    "register_vote_views: failed to re-register closed cycle "
                    "%s participants view",
                    cid,
                )
        _log.info(
            "register_vote_views: attached=%d skipped_no_nominees=%d failed=%d "
            "closed_participants=%d (of %d voting cycles)",
            attached, skipped_no_nominees, failed, closed_attached, len(rows),
        )

    # ---------------- background auto-close ----------------

    @tasks.loop(seconds=60.0)
    async def _auto_close_loop(self):
        """Poll every 60s for cycles whose closes_at has passed. For each,
        synthesize an admin-style close by calling _close_voting with a
        Bot/guild surrogate — there's no Interaction available here, so the
        helper has to tolerate ``interaction=None`` (see _close_voting).

        The 60s cadence trades close-time precision for low overhead. Worst
        case: a vote lands within the 60s window and gets included in the
        tally (which is fine — the closes_at is a soft deadline, not a hard
        cutoff against votes).
        """
        try:
            rows = await self.bot.GET(DatabaseQueries.LIST_EXPIRED_VOTING_CYCLES)
        except Exception as e:  # noqa: BLE001
            _log.exception("auto_close: failed to query expired cycles: %s", e)
            return
        # Heartbeat: INFO when there's actual work, DEBUG otherwise.
        # Lets us grep for "auto_close" to confirm the loop is alive
        # without spamming logs every minute under normal operation.
        if rows:
            _log.info("auto_close: tick — %d expired cycle(s) to close", len(rows))
        else:
            _log.debug("auto_close: tick — no expired cycles")
        for row in rows:
            cycle_id, guild_id, kind = row[0], row[1], row[2]
            try:
                # Re-fetch so the cycle row matches the column shape _close_voting
                # expects, and so we re-check the phase right before closing.
                cycle = await _cycle_by_id(self.bot, cycle_id)
                if not cycle or cycle[CYCLE_PHASE] != "voting":
                    continue
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    _log.warning(
                        "auto_close: guild %s no longer accessible; skipping cycle %s",
                        guild_id, cycle_id,
                    )
                    continue
                _log.info("auto_close: closing cycle %s (guild=%s, kind=%s)",
                          cycle_id, guild_id, kind)
                await self._close_voting(
                    interaction=None,
                    kind=kind,
                    guild=guild,
                    expected_cycle_id=cycle_id,
                )
            except Exception as e:  # noqa: BLE001
                _log.exception("auto_close: cycle %s close failed: %s", cycle_id, e)

    @_auto_close_loop.before_loop
    async def _auto_close_before(self):
        await self.bot.wait_until_ready()
        # Single startup log so it's grep-able from the bot's hikaru_bot.log
        # to confirm the poller actually began ticking after on_ready.
        _log.info("auto_close: poll loop started (60s cadence)")

    # ---------------- /manage_voting ----------------

    @app_commands.command(
        name="manage_voting",
        description="Open the voting admin dashboard (admin).",
    )
    @app_commands.guild_only()
    async def manage_voting(self, interaction: discord.Interaction):
        """Mod-only entry point for managing voting cycles + settings.

        Opens an ephemeral dashboard panel — no parameters. The panel
        surfaces only the actions valid for the current cycle state and
        collects per-action inputs through a modal. Replaces the older
        older `/manage_voting action:…` parameter-bag command and the
        separate `/manage_settings` command — admins now have a single
        click-driven surface instead of having to memorize action +
        parameter combinations.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            await validate_user_permission(
                interaction, "Only admins can use the voting dashboard.",
            )
            state = await _fetch_panel_state(self.bot, interaction.guild.id)
            view = VotingAdminPanelView(self, state)
            content = await _build_panel_text(self.bot, state)
            await interaction.followup.send(
                content=content, view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ValidationError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/manage_voting failed")
            await handle_command_error(
                interaction, e, "Could not open the voting dashboard.",
            )

    # ---------------- action handlers ----------------

    async def _open_voting(self, interaction, *, kind, choice_mode, winner_count,
                           target_month=None, season=None, year=None,
                           duration=None, vote_ui=None, allowed_role_id=None, **_):
        """Create a fresh voting cycle and sweep the existing
        status='nominated' VNs for the target month into it as the
        candidate pool.

        Nominations are not cycle-scoped — users `/nominate` any time,
        rows accumulate with status='nominated', and Open voting is what
        actually creates a cycle and gathers the candidates.
        """
        guild_id = interaction.guild.id
        existing = await _active_cycle(self.bot, guild_id, kind)
        if existing:
            raise ValidationError(
                "active cycle already exists",
                f"A {kind} vote is already in progress in this server "
                f"(target: `{existing[CYCLE_TARGET_MONTH]}`). "
                "Close or cancel it before starting a new one. (Monthly and "
                "seasonal votes are tracked separately and may run in parallel.)",
            )

        # Resolve target_month + target_end_month from the kind-specific inputs.
        if kind == "seasonal":
            if not season:
                raise ValidationError(
                    "season required",
                    "Open a seasonal vote with `season:<Winter|Spring|Summer|Fall>`.",
                )
            effective_year = year if year is not None else discord.utils.utcnow().year
            months = season_to_months(season, effective_year)
            target = months[0]
            target_end = months[-1]
            period_label = await format_season_label(
                self.bot, effective_year, season,
            )
        else:
            target = target_month or _next_month(get_current_month())
            if not validate_month_format(target):
                raise ValidationError(
                    f"bad month {target!r}",
                    "target_month must be in YYYY-MM format.",
                )
            target_end = target
            period_label = _month_label(target)

        if not choice_mode:
            raise ValidationError(
                "missing choice_mode", "`choice_mode` is required for open_voting."
            )
        if winner_count is None:
            winner_count = 1
        if winner_count < 1 or winner_count > 10:
            raise ValidationError("bad winner_count", "winner_count must be 1-10.")

        # Compute closes_at from the duration choice. None means "no timer".
        closes_at_iso: Optional[str] = None
        duration_secs = _parse_duration_to_seconds(duration) if duration else None
        if duration_secs:
            from datetime import datetime, timedelta, timezone
            close_dt = datetime.now(timezone.utc) + timedelta(seconds=duration_secs)
            closes_at_iso = close_dt.strftime("%Y-%m-%d %H:%M:%S")

        channel = interaction.channel

        # Validate everything BEFORE we touch the DB. The previous
        # implementation inserted the cycle, swept nominations, then
        # raised on bad counts — leaving a closed phantom cycle row
        # behind on every failed open. Now: pre-count, resolve config,
        # then write atomically. The COUNT predicate matches
        # SWEEP_NOMINATIONS_TO_CYCLE exactly so the count is what the
        # sweep will actually move.
        (pending_count,) = await self.bot.GET_ONE(
            DatabaseQueries.COUNT_PENDING_NOMINATIONS_FOR_PERIOD,
            (target, target_end, guild_id),
        )
        if pending_count == 0:
            raise ValidationError(
                "no nominees",
                f"No nominations found for **{period_label}**. Have users run "
                f"`/nominate status:{kind} title:<title>` first, then open voting again.",
            )
        if pending_count > 25:
            raise ValidationError(
                "too many nominees",
                f"More than 25 nominations exist for **{period_label}** "
                "— voting UI doesn't support that. Reduce nominations "
                "(via `/manage_pool action:Remove`) before opening voting.",
            )

        # vote_ui resolution: explicit arg wins; otherwise pull the
        # guild's default_vote_ui setting; otherwise default to dropdown.
        # Buttons-mode caps at 20 since Discord allows 5 buttons × 4 rows
        # + the row reserved for Participants/Manage-votes utility buttons.
        vote_ui_value = vote_ui
        if vote_ui_value is None:
            settings_ui_row = await self.bot.GET_ONE(
                DatabaseQueries.GET_GUILD_SETTINGS, (interaction.guild.id,),
            )
            if settings_ui_row and settings_ui_row[2]:
                vote_ui_value = settings_ui_row[2]
        if vote_ui_value is None:
            vote_ui_value = "dropdown"
        if vote_ui_value not in ("dropdown", "buttons"):
            raise ValidationError(
                "bad vote_ui",
                f"Unknown vote_ui `{vote_ui_value}`. Expected `dropdown` or `buttons`.",
            )
        if vote_ui_value == "buttons" and pending_count > _VOTE_UI_BUTTONS_MAX:
            raise ValidationError(
                "too many nominees for buttons",
                f"Buttons-mode supports at most {_VOTE_UI_BUTTONS_MAX} nominees "
                f"(have {pending_count}). Use `vote_ui:Dropdown` instead.",
            )

        # Allowed-role resolution: explicit `allowed_role` arg wins; if
        # omitted, fall back to the guild's default_voting_role_id setting.
        # NULL on both sides means no restriction (default behavior).
        if allowed_role_id is None:
            settings_row = await self.bot.GET_ONE(
                DatabaseQueries.GET_GUILD_SETTINGS, (interaction.guild.id,),
            )
            if settings_row and settings_row[1]:
                allowed_role_id = settings_row[1]

        # Atomic write: INSERT_CYCLE + SWEEP + SET_SETTINGS as one
        # transaction. Previously these were three separate connections;
        # a crash between the insert and SET_VOTING_SETTINGS would leave
        # the cycle in phase='voting' with NULL closes_at, which the
        # auto-close polling loop never picks up (it only looks at rows
        # whose closes_at has passed). Single commit guarantees that
        # either all three writes land or none do.
        async with aiosqlite.connect(self.bot.path_to_db) as db:
            cur = await db.execute(
                DatabaseQueries.INSERT_CYCLE,
                (guild_id, target, channel.id, kind, target_end),
            )
            cycle_id = cur.lastrowid
            # Exact period match — sweep only picks up noms whose
            # [start,end] span matches the cycle's, keeping monthly +
            # seasonal lanes separate (monthly cycle: target==target_end;
            # seasonal cycle: 3-month range).
            await db.execute(
                DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
                (cycle_id, target, target_end, guild_id),
            )
            await db.execute(
                DatabaseQueries.SET_VOTING_SETTINGS,
                (choice_mode, winner_count,
                 channel.id, None,
                 closes_at_iso, vote_ui_value, allowed_role_id,
                 cycle_id),
            )
            await db.commit()

        # Schedule the exact-time close. Polling loop is the recovery
        # net; this gives sub-second precision on the happy path.
        if closes_at_iso:
            self._schedule_close(cycle_id, closes_at_iso)

        timer_note = (
            f", auto-closes in `{duration}`" if closes_at_iso else ""
        )
        role_note = (
            f", restricted to <@&{allowed_role_id}>" if allowed_role_id else ""
        )
        kind_label = "Seasonal" if kind == "seasonal" else "Monthly"
        await interaction.followup.send(
            f"🗳️ {kind_label} vote `{cycle_id}` is now open for **{period_label}** "
            f"with **{pending_count}** nominee(s) "
            f"(mode `{choice_mode}`, {winner_count} winner(s){timer_note}{role_note}). "
            "Click **Post vote message** on the dashboard from the channel "
            "where you want the voting menu to appear. "
            "Users can also run `/vote` for a personal voting menu "
            "without waiting for the public message.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _post_vote_message(
        self, interaction, *, kind, allowed_role_id=None, **_,
    ):
        """Publish (or re-publish) the live-tally vote message as the
        interaction's followup — the menu itself becomes the visible
        response, matching EasyPoll's UX of "command marker → poll
        message" with no separate confirmation.

        Decoupled from `Open voting` so the admin can:
        - Open voting first, then announce later in a different channel
        - Repost the menu if it scrolled off, was deleted, or moved
        - Update the ``allowed_role`` gate mid-cycle (passing the
          ``allowed_role`` slash arg here overwrites the cycle's stored
          role; existing votes are kept, but new vote attempts are
          checked against the new role immediately)

        Reposts edit the previous announcement (if any) to point at the
        new message, then update the cycle row's
        announcement_channel_id / announcement_message_id so refreshes /
        close edits hit the new message instead of the stale one.
        """
        cycle = await _active_cycle(self.bot, interaction.guild.id, kind)
        if not cycle:
            raise ValidationError(
                "no active cycle", f"No active {kind} voting found.",
            )
        if cycle[CYCLE_PHASE] != "voting":
            raise ValidationError(
                "wrong phase",
                f"Voting is in phase `{cycle[CYCLE_PHASE]}`; "
                "click **Open voting** on the dashboard first.",
            )

        # If admin passed allowed_role, persist the change *before*
        # rendering so _render_vote_prompt picks up the new role on the
        # message header. Existing votes stay; new vote attempts get
        # gated against the new role immediately.
        if allowed_role_id is not None and allowed_role_id != cycle[CYCLE_ALLOWED_ROLE_ID]:
            await self.bot.RUN(
                DatabaseQueries.SET_VOTING_SETTINGS,
                (cycle[CYCLE_CHOICE_MODE], cycle[CYCLE_WINNER_COUNT],
                 cycle[CYCLE_CHANNEL_ID], cycle[CYCLE_MESSAGE_ID],
                 cycle[CYCLE_CLOSES_AT], cycle[CYCLE_VOTE_UI],
                 allowed_role_id, cycle[CYCLE_ID]),
            )
            cycle = await _cycle_by_id(self.bot, cycle[CYCLE_ID])

        nominees = await self.bot.GET(
            DatabaseQueries.GET_CYCLE_NOMINEES, (cycle[CYCLE_ID],),
        )
        if not nominees:
            raise ValidationError(
                "no nominees", "Voting has no nominees — nothing to render.",
            )

        tally = await self.bot.GET(DatabaseQueries.TALLY_VOTES, (cycle[CYCLE_ID],))
        embed = await _render_vote_prompt(self.bot, cycle, nominees, tally)
        view = VoteView(
            cycle_id=cycle[CYCLE_ID],
            nominees=list(nominees),
            vote_ui=cycle[CYCLE_VOTE_UI],
        )

        # Send the menu as the interaction's followup so the only thing the
        # user sees is the "X used /manage_voting" marker + the menu itself
        # (no separate ephemeral "posted" confirmation cluttering the UX).
        # ``wait=True`` returns the WebhookMessage so we can persist its id.
        # ``allowed_mentions=none`` suppresses the @-pings on the nominator
        # mentions in the embed — they render as clickable user names but
        # don't notify (which would otherwise spam every refresh).
        new_message = await interaction.followup.send(
            embed=embed, view=view, wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        new_channel_id = interaction.channel_id

        # Best-effort: edit the old vote message (if there was one and it's
        # in a different location) to redirect users at the new menu.
        # Failure here is fine — the new message is what matters.
        old_channel_id = cycle[CYCLE_CHANNEL_ID]
        old_message_id = cycle[CYCLE_MESSAGE_ID]
        if old_message_id and old_channel_id and (
            old_channel_id != new_channel_id or old_message_id != new_message.id
        ):
            old_channel = self.bot.get_channel(old_channel_id)
            if old_channel is not None:
                try:
                    old_msg = await old_channel.fetch_message(old_message_id)
                    # embed=None clears the stale vote tally; the new
                    # message owns the rendering from here on. (Pre-embed
                    # vote messages had no embed, so this was a no-op
                    # before the embed migration.)
                    await old_msg.edit(
                        content=(
                            f"🔁 Voting menu moved — see "
                            f"{new_message.jump_url}"
                        ),
                        embed=None,
                        view=None,
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    _log.debug("post_vote_message: old-message edit skipped: %s", e)

        # Update cycle row so subsequent refreshes / auto-close hit the
        # new message + channel. vote_ui + allowed_role_id are preserved.
        await self.bot.RUN(
            DatabaseQueries.SET_VOTING_SETTINGS,
            (cycle[CYCLE_CHOICE_MODE], cycle[CYCLE_WINNER_COUNT],
             new_channel_id, new_message.id,
             cycle[CYCLE_CLOSES_AT], cycle[CYCLE_VOTE_UI],
             cycle[CYCLE_ALLOWED_ROLE_ID], cycle[CYCLE_ID]),
        )

    def _get_close_lock(self, cycle_id: int) -> asyncio.Lock:
        """Lazy lock creation. Same pattern as _VOTE_LOCKS module-level
        — dict ops are race-free in single-threaded asyncio because no
        await happens between get and set.
        """
        lock = self._close_locks.get(cycle_id)
        if lock is None:
            lock = asyncio.Lock()
            self._close_locks[cycle_id] = lock
        return lock

    async def _close_voting(
        self, interaction, *, kind, guild=None,
        expected_cycle_id: Optional[int] = None, **_,
    ):
        # ``interaction`` may be None when called from the auto-close
        # background task; in that case the caller passes ``guild`` directly.
        # All ``interaction.followup.send`` / ``interaction.channel`` accesses
        # below are guarded with ``if interaction`` so the auto-close path
        # produces no admin-facing followup (the channel-side winner banner
        # + vote-message edit are still performed identically).
        #
        # ``expected_cycle_id`` is passed by the scheduled-close path so we
        # can detect a cancel-then-reopen race: the original close timer
        # fires after the admin cancelled cycle N and opened cycle N+1.
        # Without this check we'd happily close N+1 instead. The auto-close
        # polling loop also passes it for the same reason — even though the
        # 60s poll is unlikely to race itself, it's cheap insurance.
        guild_for_lookup = guild if guild is not None else interaction.guild
        cycle = await _active_cycle(self.bot, guild_for_lookup.id, kind)
        if not cycle:
            raise ValidationError("no active cycle", f"No active {kind} voting to close.")
        if cycle[CYCLE_PHASE] != "voting":
            raise ValidationError(
                "wrong phase",
                f"Voting is in phase `{cycle[CYCLE_PHASE]}`; expected `voting`.",
            )
        if expected_cycle_id is not None and cycle[CYCLE_ID] != expected_cycle_id:
            # The active cycle changed between the scheduler's queue time
            # and now (cancel + reopen, most likely). Bail out — the new
            # cycle will close on its own schedule.
            _log.warning(
                "_close_voting: expected cycle %s but active %s cycle is %s; skipping",
                expected_cycle_id, kind, cycle[CYCLE_ID],
            )
            return

        # Acquire the per-cycle exclusion lock and re-check the phase
        # under it. The scheduled-close task and the 60s polling loop
        # can both observe phase='voting' at the same instant; without
        # this lock the second runner would re-promote (idempotent on
        # the DB) and re-post the winner banner (NOT idempotent — the
        # channel would see the announcement twice). The re-check
        # catches the case where the first runner finished while we
        # were waiting on the lock.
        async with self._get_close_lock(cycle[CYCLE_ID]):
            cycle = await _cycle_by_id(self.bot, cycle[CYCLE_ID])
            if not cycle or cycle[CYCLE_PHASE] != "voting":
                _log.info(
                    "_close_voting: cycle already closed under lock (phase=%s); skipping",
                    cycle[CYCLE_PHASE] if cycle else "missing",
                )
                return
            await self._close_voting_locked(
                interaction=interaction, cycle=cycle, kind=kind,
            )

    async def _close_voting_locked(self, *, interaction, cycle, kind):
        """Body of close-voting that runs inside the per-cycle exclusion
        lock. Split from `_close_voting` so the lookup + race-detection
        live above the lock and the actual write/render side stays one
        logical unit.
        """
        # Cancel any per-cycle scheduled close so the manual path doesn't
        # race the timer firing. No-op when called from the scheduler
        # itself (entry already pop'd the task in
        # `_close_cycle_by_id_safely`'s finally block) or when there
        # was no timer set.
        self._cancel_scheduled_close(cycle[CYCLE_ID])

        winner_count = cycle[CYCLE_WINNER_COUNT] or 1
        tally = await self.bot.GET(DatabaseQueries.TALLY_VOTES, (cycle[CYCLE_ID],))
        if not tally:
            # Empty tally: all nominees got removed mid-vote (likely via
            # /manage_pool) or the cycle opened with stale state. The
            # behavior split here matters because the auto-close polling
            # loop catches whatever we raise and keeps the cycle in
            # phase='voting' — so a ValidationError on the auto-path
            # would wedge the loop, retrying every minute forever.
            #
            # Manual close (admin invoked): raise so they see the error
            # and decide whether to reopen / clean up.
            # Auto close (interaction is None): close gracefully with no
            # winners. The cycle moves to 'closed' phase, polling stops
            # retrying it, and an admin can reopen later if desired.
            if interaction is None:
                _log.warning(
                    "_close_voting auto-path: cycle %s has no tallyable "
                    "nominees; closing with no winners to break the "
                    "polling loop",
                    cycle[CYCLE_ID],
                )
                await self.bot.RUN(
                    DatabaseQueries.CLOSE_CYCLE, (cycle[CYCLE_ID],),
                )
                return
            raise ValidationError("no nominees", "No nominees to tally.")
        # Tie-aware winner resolution: contested seats (where votes ==
        # next-place) are forfeit, and 0-vote "winners" never claim a
        # seat. The cycle still closes; admin can promote manually via
        # /manage_pool action:Edit if they want a different outcome.
        winners = _winners_after_tiebreak(tally, winner_count)
        guild_id = cycle[CYCLE_GUILD_ID]
        cycle_kind = cycle[CYCLE_KIND] or "monthly"
        period_label = await cycle_period_label_with_season(self.bot, cycle)

        # All cycle-state writes happen up front in a single transaction so
        # promotion and cycle-close land together — a crash mid-render after
        # a partial promote no longer leaves the cycle stuck open with some
        # nominations already flipped to 'monthly'/'seasonal'.
        # winner row shape from TALLY_VOTES: (nomination_id, vndb_id, title, ...).
        # The first column is the nomination_id which is the vn_titles.id.
        statements: list[tuple[str, tuple]] = [
            (DatabaseQueries.PROMOTE_NOMINATION_TO_PICK, (cycle_kind, w[0]))
            for w in winners
        ]
        statements.append((DatabaseQueries.CLOSE_CYCLE, (cycle[CYCLE_ID],)))
        _log.info(
            "close_voting: cycle=%s kind=%s guild=%s promoting nominations=%s",
            cycle[CYCLE_ID], cycle_kind, guild_id, [w[0] for w in winners],
        )
        try:
            await self.bot.RUN_TRANSACTION(statements)
            promoted_pool_ids: dict[str, int] = {w[1]: w[0] for w in winners}
        except Exception as e:  # noqa: BLE001
            _log.error(
                "Failed to commit cycle %s close transaction: %s",
                cycle[CYCLE_ID], e,
            )
            raise

        # Verify each winner row actually flipped status. PROMOTE_NOMINATION_TO_PICK
        # is an UPDATE; SQLite UPDATE with no matching row succeeds silently —
        # so an admin /manage_pool action:remove that ran during voting could
        # have deleted the row and we'd close "successfully" without promotion.
        # We don't reverse the close, but we surface the gap loudly.
        for w in winners:
            row = await self.bot.GET_ONE(
                "SELECT status FROM vn_titles WHERE id = ?", (w[0],)
            )
            if not row or row[0] != cycle_kind:
                _log.warning(
                    "Winner row id=%s did not flip to status=%s (got %s) — "
                    "row may have been removed mid-vote.",
                    w[0], cycle_kind, row[0] if row else "missing",
                )
                promoted_pool_ids.pop(w[1], None)
        _log.info(
            "close_voting: cycle=%s promoted=%d expected=%d",
            cycle[CYCLE_ID], len(promoted_pool_ids), len(winners),
        )

        # Cross-cog cache invalidation. The /season_overview cache lives on
        # VNTitleManagement; vote close mutates vn_titles.status without going
        # through /manage_pool, so we need to call the invalidator directly.
        vn_mgmt = self.bot.get_cog("VNTitleManagement")
        if vn_mgmt is not None:
            vn_mgmt._invalidate_season_overview_cache()

        # Winner banners are NOT auto-posted on close. The close promotes the
        # winner(s) and (below) edits the vote message to the final standings
        # with the winner marked; an admin announces the banner deliberately via
        # /monthly or /seasonal, where they can also drop the cover for a
        # title. Resolve the channel for that vote-message edit; when
        # auto-closing, ``interaction`` is None so rely on the stored channel id.
        channel = self.bot.get_channel(cycle[CYCLE_CHANNEL_ID])
        if channel is None and interaction is not None:
            channel = interaction.channel
        if channel is None:
            _log.error(
                "_close_voting: no channel available for cycle %s (channel_id=%s); "
                "skipping vote-message edit.",
                cycle[CYCLE_ID], cycle[CYCLE_CHANNEL_ID],
            )
        if channel is not None:
            # Edit the vote-control message: keep the final tally
            # visible (people want to see what they voted for after the
            # fact, like EasyPoll's "Final Result" section), but swap
            # the active-poll header for a "Voting closed" banner and
            # drop the "tap a button" hint since the buttons are gone.
            #
            # We render from `tally` (not GET_CYCLE_NOMINEES): the close
            # transaction has already flipped winner rows to
            # status='monthly'/'seasonal', and GET_CYCLE_NOMINEES filters
            # by status='nominated' — so re-fetching there would silently
            # drop winners from the final tally body.
            #
            # Tally row shape: (nomination_id, vndb_id, title, user_id,
            # guild_id, created_at, votes). Sorted by votes DESC,
            # created_at ASC. We re-sort by created_at ASC for the body
            # so letter labels (A, B, C…) match the live message order.
            if cycle[CYCLE_MESSAGE_ID]:
                try:
                    vote_msg = await channel.fetch_message(cycle[CYCLE_MESSAGE_ID])
                    total_votes = sum(t[6] for t in tally)
                    closed_lines = [
                        f"🔒 **Voting closed — {period_label}**",
                    ]
                    if winners:
                        winners_label = ", ".join(f"**{w[2]}**" for w in winners)
                        closed_lines.append(
                            f"{'Winners' if len(winners) > 1 else 'Winner'}: "
                            f"{winners_label}"
                        )
                    else:
                        closed_lines.append(
                            "No votes cast. No winner."
                        )
                    closed_lines.append(
                        f"Mode: `{cycle[CYCLE_CHOICE_MODE] or 'single'}` · "
                        f"Vote ID: `{cycle[CYCLE_ID]}`"
                    )
                    closed_lines.append("")
                    closed_lines.append(
                        f"📊 **Standings** · {_votes_phrase(total_votes)}"
                    )
                    # Letters stay in nomination order so they match what
                    # voters saw on the live message; the standings then
                    # re-sort by votes. `tally` already arrives votes DESC,
                    # id ASC, so iterating it is standings order and
                    # zero-vote entries (sorted last) collapse into a tail
                    # line, mirroring the live embed's Standings section.
                    capped = tally[:25]
                    # created_at, then id, as the tie-break so equal-second
                    # nominations get the same letters the live message did.
                    letter_by_nom = {
                        t[0]: _VOTE_LETTERS[i]
                        for i, t in enumerate(
                            sorted(capped, key=lambda t: (t[5] or "", t[0]))
                        )
                    }
                    winner_ids = {w[0] for w in winners}
                    # Resolve nominator handles as plain text, the way the live
                    # embed does (bot-cache username, else the cached
                    # users-table tag/name, else a placeholder), so the
                    # standings read `@handle` rather than a clickable mention.
                    voted_user_ids = [t[3] for t in capped if t[6] > 0 and t[3]]
                    missing_noms = list({
                        uid for uid in voted_user_ids
                        if self.bot.get_user(uid) is None
                    })
                    nom_tag_map: dict = {}
                    if missing_noms:
                        ph = ",".join("?" * len(missing_noms))
                        rows = await self.bot.GET(
                            "SELECT discord_user_id, user_tag, user_name "
                            f"FROM users WHERE discord_user_id IN ({ph})",
                            tuple(missing_noms),
                        )
                        nom_tag_map = {
                            r[0]: (r[1] or r[2]) for r in rows if r[1] or r[2]
                        }
                    zero_vote_letters: list[str] = []
                    standings_rows: list[str] = []
                    prev_votes: Optional[int] = None
                    rank = 0
                    for pos, t in enumerate(capped):
                        nom_id, vndb_id, title, user_id, _gid, _ca, votes = t
                        letter = letter_by_nom[nom_id]
                        if votes <= 0:
                            zero_vote_letters.append(letter)
                            continue
                        # Standard competition ranking: ties share a rank,
                        # the next distinct count skips past them (1,2,3,3,5).
                        if votes != prev_votes:
                            rank = pos + 1
                            prev_votes = votes
                        pct = (votes / total_votes * 100.0) if total_votes else 0.0
                        truncated = _truncate_label(str(title), 60)
                        safe_title = (
                            truncated.replace("\\", "\\\\")
                            .replace("[", "\\[")
                            .replace("]", "\\]")
                        )
                        title_link = (
                            f"[{safe_title}](<https://vndb.org/{vndb_id}>)"
                        )
                        nom_user = self.bot.get_user(user_id) if user_id else None
                        if nom_user is not None:
                            nom_tag = nom_user.name
                        elif user_id:
                            nom_tag = nom_tag_map.get(user_id) or "unknown-user"
                        else:
                            nom_tag = "unknown-user"
                        nominator = f"@{nom_tag}"
                        winner_marker = " 🏆" if nom_id in winner_ids else ""
                        standings_rows.append(
                            f"`{rank:>2}.` `{letter}` · {title_link} · "
                            f"{nominator} · `{pct:5.1f}%` ({votes})"
                            f"{winner_marker}"
                        )
                    zero_tail = (
                        f"_No votes: {', '.join(zero_vote_letters)}_"
                        if zero_vote_letters else None
                    )
                    # Fit Discord's 2000-char message-content cap (the live
                    # prompt uses an embed, which has more room; this closed
                    # message is plain content). Drop the lowest-information
                    # lines first: the zero-vote tail, then ranked rows from the
                    # bottom, leaving a "N more" note. Without this a >2000-char
                    # render makes vote_msg.edit raise, stranding the message on
                    # the live (now dead) voting view.
                    def _fit(
                        body: list[str], tail: Optional[str], note: Optional[str]
                    ) -> str:
                        lines = [*closed_lines, *body]
                        if tail is not None:
                            lines.append(tail)
                        if note is not None:
                            lines.append(note)
                        return "\n".join(lines)

                    fit_body = list(standings_rows)
                    fit_tail = zero_tail
                    overflow_note: Optional[str] = None
                    while len(_fit(fit_body, fit_tail, overflow_note)) > 1990:
                        if fit_tail is not None:
                            fit_tail = None
                        elif fit_body:
                            fit_body.pop()
                            overflow_note = (
                                f"_{len(standings_rows) - len(fit_body)} more "
                                "in standings._"
                            )
                        else:
                            break
                    content = _fit(fit_body, fit_tail, overflow_note)
                    if len(content) > 2000:
                        content = content[:1990].rstrip() + "\n_truncated._"
                    # Keep a participants-only view so people can still see who
                    # voted for what after close; the vote inputs are gone. The
                    # participants custom_id is already registered in-session by
                    # the live VoteView, so we do NOT add_view here (that would
                    # collide); boot re-registration handles it across restarts.
                    await vote_msg.edit(
                        content=content,
                        embed=None,
                        view=ClosedVoteView(cycle[CYCLE_ID]),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("Could not edit vote message: %s", e)

        if interaction is not None:
            await interaction.followup.send(
                _build_close_voting_summary(
                    cycle_kind, period_label, winners, promoted_pool_ids,
                    tally=tally,
                ),
                ephemeral=True,
            )
        else:
            _log.info(
                "Auto-close: cycle %s closed. Winners: %s",
                cycle[CYCLE_ID],
                ", ".join(f"{w[2]} ({_votes_phrase(w[6])})" for w in winners),
            )

    async def _reopen_voting(
        self, interaction: discord.Interaction, cycle_id: int,
        *,
        choice_mode: Optional[str] = None,
        winner_count: Optional[int] = None,
        duration: Optional[str] = None,
    ) -> None:
        """Reopen a previously-closed cycle by id.

        Validates that the target cycle belongs to this guild and is
        currently in `phase='closed'`, and that no other cycle of the
        same kind is already running. Demotes any winners (period-based
        match) back to `status='nominated'` and re-sweeps the period's
        nominations onto this cycle so they're picked up by the tally.
        Re-registers a persistent VoteView so button clicks on a future
        Repost message land correctly.

        Optional ``choice_mode`` / ``winner_count`` / ``duration`` let
        the admin tune voting settings on the way back in. ``None``
        means "keep current cycle value". For ``duration``, the literal
        string ``"0"`` is the explicit "clear the timer" sentinel (vs.
        ``None`` = "keep whatever closes_at was on the closed cycle",
        which is moot since REOPEN_CYCLE always nulls it; in practice
        any non-None duration here SETS a fresh closes_at).

        Admin still needs to click **Repost vote message** on the
        dashboard to publish a fresh live-tally menu —
        `announcement_message_id` is cleared so the panel surfaces
        that step.
        """
        target = await _cycle_by_id(self.bot, cycle_id)
        if target is None:
            raise ValidationError(
                "no such cycle",
                f"No vote with id `{cycle_id}` exists.",
            )
        if target[CYCLE_GUILD_ID] != interaction.guild.id:
            raise ValidationError(
                "wrong guild",
                f"Vote `{cycle_id}` doesn't belong to this server.",
            )
        if target[CYCLE_PHASE] != "closed":
            raise ValidationError(
                "not closed",
                f"Vote `{cycle_id}` is in phase `{target[CYCLE_PHASE]}`, "
                "not `closed` — only closed votes can be reopened.",
            )
        kind = target[CYCLE_KIND] or "monthly"
        # Same-kind active-cycle conflict: one active per (guild, kind)
        # is the long-standing invariant. Reopening would violate it.
        active = await _active_cycle(self.bot, interaction.guild.id, kind)
        if active:
            raise ValidationError(
                "active cycle exists",
                f"A {kind} vote (id `{active[CYCLE_ID]}`) is already "
                "running in this server. Close or cancel it before "
                "reopening another one of the same kind.",
            )

        # Atomic: reopen + demote winners + re-sweep nominations.
        # The demote and sweep are PERIOD-based (not cycle_id based)
        # because a later cycle for the same target month may have
        # already swept these rows' cycle_id away from `cycle_id`.
        # Filtering by (start_month, end_month, guild_id) catches the
        # rows regardless of which cycle currently owns them, and the
        # sweep then re-attaches them to this reopened cycle so the
        # tally / nominees-list queries (filtered by cycle_id) find
        # them again.
        target_month = target[CYCLE_TARGET_MONTH]
        target_end_month = target[CYCLE_TARGET_END_MONTH] or target_month
        guild_id = target[CYCLE_GUILD_ID]
        _log.info(
            "reopen_voting: cycle=%s kind=%s guild=%s period=%s..%s — demoting picks + re-sweeping nominations",
            cycle_id, kind, guild_id, target_month, target_end_month,
        )
        await self.bot.RUN_TRANSACTION([
            (DatabaseQueries.REOPEN_CYCLE, (cycle_id,)),
            (
                DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
                (target_month, target_end_month, guild_id),
            ),
            (
                DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
                (cycle_id, target_month, target_end_month, guild_id),
            ),
        ])

        # Cross-cog cache invalidation: demoting picks back to nominations
        # removes them from /season_overview's source set. Same reasoning as
        # the close-voting site above.
        vn_mgmt = self.bot.get_cog("VNTitleManagement")
        if vn_mgmt is not None:
            vn_mgmt._invalidate_season_overview_cache()

        # Apply optional setting overrides. Only fields the admin
        # explicitly provided are touched — everything else inherits
        # from the closed cycle's stored values. Done as a separate
        # UPDATE because REOPEN_CYCLE already nulled some columns we
        # may want to set here (closes_at).
        applied_changes: list[str] = []
        sets: list[str] = []
        params: list = []
        if choice_mode is not None:
            sets.append("vote_choice_mode = ?")
            params.append(choice_mode)
            applied_changes.append(f"mode `{choice_mode}`")
        if winner_count is not None:
            sets.append("vote_winner_count = ?")
            params.append(int(winner_count))
            applied_changes.append(f"{int(winner_count)} winner(s)")
        if duration is not None:
            if duration in ("0", "none", "0s"):
                sets.append("closes_at = NULL")
                applied_changes.append("no timer")
            else:
                duration_secs = _parse_duration_to_seconds(duration)
                if duration_secs and duration_secs > 0:
                    from datetime import datetime, timedelta, timezone
                    close_dt = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=duration_secs)
                    )
                    sets.append("closes_at = ?")
                    params.append(close_dt.strftime("%Y-%m-%d %H:%M:%S"))
                    applied_changes.append(f"auto-closes in `{duration}`")
        if sets:
            params.append(cycle_id)
            await self.bot.RUN(
                f"UPDATE vn_cycles SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )

        # Re-register the persistent VoteView so any future Repost'd
        # menu's buttons route correctly without needing a bot restart.
        nominees = await self.bot.GET(
            DatabaseQueries.GET_CYCLE_NOMINEES, (cycle_id,),
        )
        # Re-fetch the cycle so vote_ui is the post-reopen value.
        reopened = await _cycle_by_id(self.bot, cycle_id)
        # Schedule (or re-schedule) the exact-time close. REOPEN_CYCLE
        # nulls closes_at first, so this is a no-op unless the admin
        # supplied a duration override above.
        if reopened and reopened[CYCLE_CLOSES_AT]:
            self._schedule_close(cycle_id, str(reopened[CYCLE_CLOSES_AT]))
        if reopened and nominees:
            self.bot.add_view(VoteView(
                cycle_id=cycle_id,
                nominees=list(nominees),
                vote_ui=reopened[CYCLE_VOTE_UI],
            ))

        period_label = await cycle_period_label_with_season(self.bot, target)
        kind_label = "Seasonal" if kind == "seasonal" else "Monthly"
        overrides_note = (
            f" Settings updated: {', '.join(applied_changes)}."
            if applied_changes else ""
        )
        await interaction.followup.send(
            f"♻️ {kind_label} vote `{cycle_id}` reopened for "
            f"**{period_label}**. Any previous winner is back as a "
            f"nominee.{overrides_note} Click **Repost vote message** "
            "on the dashboard from the channel where you want the "
            "live tally menu.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _cancel(self, interaction, *, kind, **_):
        cycle = await _active_cycle(self.bot, interaction.guild.id, kind)
        if not cycle:
            raise ValidationError("no active cycle", f"No active {kind} voting to cancel.")
        # Drop any scheduled close so the timer doesn't fire after the
        # cycle has been moved to phase='closed'.
        self._cancel_scheduled_close(cycle[CYCLE_ID])
        _log.info(
            "cancel_voting: cycle=%s kind=%s guild=%s actor=%s — closing without promoting",
            cycle[CYCLE_ID], kind, interaction.guild.id, interaction.user.id,
        )
        # Just close the cycle — don't delete the swept nominations. In the
        # decoupled model, nominations are persistent: cancelling a vote
        # shouldn't wipe the candidate pool, since users typically want to
        # re-run voting for the same month afterward (e.g. wrong choice_mode
        # was picked). The noms stay status='nominated' and get re-swept by
        # the next Open voting for that month.
        await self.bot.RUN(DatabaseQueries.CLOSE_CYCLE, (cycle[CYCLE_ID],))
        await interaction.followup.send(
            f"🛑 {kind.capitalize()} vote `{cycle[CYCLE_ID]}` cancelled. "
            "Nominations are preserved — click **Open voting** on the "
            "dashboard to vote on them again.",
            ephemeral=True,
        )

    # ---------------- /nominate ----------------

    @app_commands.command(name="nominate", description="Nominate a VN for an upcoming monthly/seasonal vote.")
    @app_commands.describe(
        title="Search for a VN by title (type at least 2 characters).",
        status="Which vote status to nominate for (default: monthly).",
        target_month="YYYY-MM target month (default: next month for monthly / next season for seasonal).",
    )
    @app_commands.choices(status=CYCLE_KIND_CHOICES)
    @app_commands.autocomplete(
        title=vn_autocomplete,
        target_month=month_picker_future_autocomplete,
    )
    @app_commands.guild_only()
    async def nominate(
        self,
        interaction: discord.Interaction,
        title: str,
        status: Optional[app_commands.Choice[str]] = None,
        target_month: Optional[str] = None,
    ):
        """Decoupled-from-cycle /nominate. Creates a status='nominated'
        vn_titles row for the chosen target month with cycle_id=NULL —
        Open voting later sweeps it (and any siblings) onto the cycle.
        """
        await interaction.response.defer(ephemeral=True)

        # Nominations follow the same "default voting role" gate as
        # casting votes — a guild restricting voting to a specific role
        # implicitly restricts who can put VNs in front of that role
        # for a vote. Per-cycle role overrides only exist at cycle-open
        # time and don't apply to nominations (which are submitted
        # pre-cycle), so we read straight from guild_settings. NULL or
        # missing row means "no gate" (matches voting's zero-config
        # behaviour).
        settings_row = await self.bot.GET_ONE(
            DatabaseQueries.GET_GUILD_SETTINGS, (interaction.guild.id,),
        )
        default_role_id = settings_row[1] if settings_row else None
        if default_role_id is not None:
            member = interaction.user  # Member in a guild-only context
            has_role = any(
                getattr(r, "id", None) == default_role_id
                for r in getattr(member, "roles", [])
            )
            if not has_role:
                await interaction.followup.send(
                    f"❌ You need the <@&{default_role_id}> role to "
                    "nominate VNs in this server.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        # `kind_value` is the underlying cycle 'kind' (monthly|seasonal) —
        # the slash param is exposed as `status` for user-facing
        # consistency, but the column on vn_cycles is still called `kind`.
        kind_value = status.value if status else "monthly"
        # Back-compat for the kind→status rename: if the user's Discord
        # client still has the pre-rename slash schema cached, their
        # selection lands in interaction.data under the legacy `kind`
        # option name and our `status` arg is None. Recover the value
        # so a transition-period invocation isn't silently downgraded
        # to monthly. Drops to dead code once Discord propagates the
        # new schema everywhere.
        if status is None and interaction.data:
            for opt in (interaction.data.get('options') or []):
                if opt.get('name') == 'kind' and opt.get('value'):
                    legacy_value = str(opt['value']).strip().lower()
                    if legacy_value in ('monthly', 'seasonal'):
                        kind_value = legacy_value
                    break

        try:
            # Resolve target window. Monthly = single month, defaults to
            # next calendar month. Seasonal = 3-month range; default is
            # the *next* season (fall-through via _next_month would land
            # mid-current-season for most calendar months, which is the
            # opposite of what users expect — they want the upcoming vote).
            if kind_value == "seasonal":
                if target_month:
                    if not validate_month_format(target_month):
                        raise ValidationError(
                            f"bad month {target_month!r}",
                            "target_month must be in YYYY-MM format.",
                        )
                    year_int = int(target_month[:4])
                    month_int = int(target_month[5:7])
                    try:
                        season_name = month_to_season_name(month_int)
                    except ValueError:
                        raise ValidationError(
                            "bad month",
                            "target_month must be a real calendar month for seasonal noms.",
                        )
                else:
                    cur_season, cur_year = current_anime_season()
                    year_int, season_name = next_season(cur_year, cur_season)
                months = season_to_months(season_name, year_int)
                start_month = months[0]
                end_month = months[-1]
            else:
                target = target_month or _next_month(get_current_month())
                if not validate_month_format(target):
                    raise ValidationError(
                        f"bad month {target!r}",
                        "target_month must be in YYYY-MM format.",
                    )
                start_month = target
                end_month = target

            if kind_value == "seasonal":
                period_label = await format_season_label(
                    self.bot, year_int, season_name,
                )
            else:
                period_label = _month_label(start_month)

            # Reject when an active voting cycle of the SAME KIND as the
            # user's nomination overlaps the target period. Without this
            # guard, /nominate silently accepts a nom during an in-progress
            # vote of the same series and the user gets a "✅ nominated"
            # confirmation even though their pick won't be in the running
            # (the sweep already ran at cycle-open time and doesn't re-run
            # on /nominate). Cross-kind overlap is fine — monthly + seasonal
            # voting tracks are independent.
            active_overlap = await self.bot.GET_ONE(
                DatabaseQueries.GET_ACTIVE_VOTING_OVERLAPPING_MONTH,
                (interaction.guild.id, kind_value, end_month, start_month),
            )
            if active_overlap:
                raise ValidationError(
                    "vote in progress",
                    f"A {kind_value} vote is currently running for "
                    f"**{period_label}** — wait for it to close before "
                    "nominating new VNs for that period. (New nominations "
                    "wouldn't be in the active vote anyway since the "
                    "candidate pool is set when voting opens.)",
                )

            # Look up any existing nomination for this user + EXACT
            # period (start, end). Exact match instead of "any nom
            # containing start_month" so monthly and seasonal lanes
            # stay independent. The active-vote guard above already
            # rejected anything tied to an in-progress cycle, so
            # anything we find here is safe to update in place
            # (or no-op if it's the same VN).
            existing = await self.bot.GET_ONE(
                DatabaseQueries.GET_USER_NOM_IN_MONTH_WITH_CYCLE,
                (interaction.user.id, start_month, end_month, interaction.guild.id),
            )

            vndb_id = await resolve_vn_from_input(title)
            if not vndb_id:
                raise ValidationError(
                    "couldn't resolve VN",
                    "Could not determine the VN from your input. Use the autocomplete dropdown.",
                )

            vn_info = await from_vndb_id(self.bot, vndb_id)
            if not vn_info:
                raise ValidationError(
                    "vndb fetch failed",
                    "Couldn't fetch that VN from VNDB. VNDB is most likely "
                    "temporarily unreachable — try again in a moment. If "
                    "the error persists, double-check the ID.",
                )

            display_title = vn_info.title_ja or vn_info.title_en or vndb_id

            if existing:
                existing_id, existing_vndb_id, _, _ = existing
                pool_id = existing_id
                if existing_vndb_id == vndb_id:
                    # Same VN as before — friendly no-op so re-running the
                    # exact same command isn't an error.
                    await interaction.followup.send(
                        f"ℹ️ You've already nominated **{display_title}** for "
                        f"**{period_label}** (pool entry **#{pool_id}**). "
                        "Re-run with a different VN to swap your pick.",
                    )
                    return
                # Re-point the existing row at the new VN. cycle_id stays
                # whatever it was (NULL, or pointing at a closed cycle that
                # the next Open voting will overwrite via SWEEP).
                await self.bot.RUN(
                    DatabaseQueries.UPDATE_NOMINATION_VN,
                    (vndb_id, display_title, existing_id),
                )
                await cache_user(self.bot, interaction.user)
                update_mode = True
            else:
                # cycle_id stays NULL — nominations are unattached until
                # Open voting sweeps them onto a cycle. Points default to
                # DEFAULT_MONTHLY_POINTS; admin can edit via /manage_pool.
                pool_id = await self.bot.RUN_RETURNING_ID(
                    DatabaseQueries.INSERT_NOMINATION_AS_PICK,
                    (vndb_id, interaction.guild.id, start_month, end_month,
                     DEFAULT_MONTHLY_POINTS, None,
                     interaction.user.id, display_title),
                )
                if not pool_id:
                    # Race: a concurrent /nominate from the same user for
                    # the same period landed first and the partial unique
                    # index made this INSERT a no-op. Same UX as the
                    # SELECT-then-decide "already nominated" path.
                    await interaction.followup.send(
                        f"ℹ️ You've already nominated a VN for "
                        f"**{period_label}**. Re-run with a different VN "
                        "to swap your pick."
                    )
                    return
                await cache_user(self.bot, interaction.user)
                update_mode = False

            jiten_data = None
            try:
                async with JitenClient() as jiten:
                    jiten_data = await jiten.get_by_vndb_id(vndb_id)
            except Exception as e:  # noqa: BLE001
                _log.warning("jiten lookup failed for nominee %s: %s", vndb_id, e)

            embed = await EmbedBuilder.create_nominee_card_embed(
                vn_info,
                jiten_data=jiten_data,
                footer_phase="nominations",
                nominator=interaction.user,
            )
            view = build_vn_links_view(
                vndb_id, jiten_data.deck_id if jiten_data else None,
            )
            if update_mode:
                content = (
                    f"🔄 Updated your nomination for the {kind_value} vote "
                    f"({period_label}) to **{display_title}** "
                    f"— pool entry **#{pool_id}**."
                )
            else:
                content = (
                    f"✅ **{display_title}** nominated for the {kind_value} vote "
                    f"({period_label}) as pool entry **#{pool_id}** "
                    f"— `/pool_entry id:{pool_id}` for full detail."
                )
            await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
            )
        except ValidationError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/nominate failed")
            await handle_command_error(interaction, e, "An error occurred recording your nomination.")

    # ---------------- /vote ----------------

    @app_commands.command(
        name="vote",
        description="Open a personal voting menu for the active vote(s) in this server.",
    )
    @app_commands.guild_only()
    async def vote(self, interaction: discord.Interaction):
        """Parameterless. Sends an ephemeral copy of the public vote
        message (same VoteView, same controls) — one per active cycle.

        Useful when the public vote message has scrolled off, or when
        the bot is in multiple servers and the user wants to be sure
        which guild's vote they're casting against. The view's
        custom_ids are guild-scoped via the cycle id, so clicks always
        land on the correct cycle even if the user is in many guilds.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = interaction.guild.id
            actives = []
            for kind in ("monthly", "seasonal"):
                cycle = await _active_cycle(self.bot, guild_id, kind)
                if cycle and cycle[CYCLE_PHASE] == "voting":
                    actives.append(cycle)
            if not actives:
                await interaction.followup.send(
                    "❌ No active voting in this server.", ephemeral=True,
                )
                return
            for cycle in actives:
                nominees = await self.bot.GET(
                    DatabaseQueries.GET_CYCLE_NOMINEES, (cycle[CYCLE_ID],),
                )
                if not nominees:
                    continue
                tally = await self.bot.GET(
                    DatabaseQueries.TALLY_VOTES, (cycle[CYCLE_ID],),
                )
                embed = await _render_vote_prompt(
                    self.bot, cycle, nominees, tally,
                )
                view = VoteView(
                    cycle_id=cycle[CYCLE_ID],
                    nominees=list(nominees),
                    vote_ui=cycle[CYCLE_VOTE_UI],
                )
                await interaction.followup.send(
                    embed=embed, view=view, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:  # noqa: BLE001
            _log.exception("/vote failed")
            await interaction.followup.send(
                "❌ Could not open the voting menu. Check the bot logs for details.",
                ephemeral=True,
            )


async def setup(bot: VNClubBot):
    await bot.add_cog(VNCycleCog(bot))
