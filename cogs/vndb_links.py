"""VNDB account links, the profile link and the VNDB leaderboards.

Links are global: a linked account shows up in every server the bot is in,
the same way reading logs follow a user. The optional ``server`` filters use
where a member linked or logged reads, so none of this needs the Server
Members intent.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import discord
import discord.app_commands as app_commands
from discord.ext import commands, tasks

from lib.autocomplete import server_autocomplete
from lib.bot import VNClubBot
from lib.pagination import BasePaginationView, PAGE_ROWS, PAGE_TEXT_BUDGET
from cogs.username_fetcher import get_username_db
from lib.utils import (
    link_label,
    BotError,
    EMBED_DESCRIPTION_BUFFER,
    MAX_EMBED_DESCRIPTION,
    ValidationError,
    add_pagination_footer,
    create_base_embed,
    handle_command_error,
    inert_text,
    send_error,
    validate_user_permission,
)
from lib import style
from lib import vndb_links as vl
from lib.visibility import PUBLIC_OPTION_HELP, defer_reply

_log = logging.getLogger(__name__)

SYNC_TICK_MINUTES = 15
SYNC_USERS_PER_TICK = 10
# A re-run of /vndb_link on an already linked account refreshes the cache,
# but not more often than this.
MANUAL_RESYNC_MIN_AGE_MINUTES = 10
# Members missing from every name cache that one reply may look up on Discord
# per call; each lookup is paced, so more would delay the reply.
NAME_FETCH_LIMIT = 10

VN_CATEGORY_CHOICES = [
    app_commands.Choice(name=label, value=key) for key, label in vl.VN_CATEGORIES.items()
]
USER_CATEGORY_CHOICES = [
    app_commands.Choice(name=label, value=key) for key, label in vl.USER_CATEGORIES.items()
]
TIMEFRAME_CHOICES = [
    app_commands.Choice(name="All-time (default)", value="all_time"),
    app_commands.Choice(name="This year", value="this_year"),
]
LINK_ACTION_CHOICES = [
    app_commands.Choice(name="Link (default)", value="link"),
    app_commands.Choice(name="Unlink", value="unlink"),
]
# Seconds between link attempts per user; each attempt is a VNDB lookup.
LINK_COOLDOWN_SECONDS = 30

def _utcnow() -> datetime:
    """Naive UTC now, comparable with SQLite CURRENT_TIMESTAMP strings."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pack_pages(blocks: list[str], budget: int, max_per_page: int, separator: str) -> list[str]:
    """Group blocks into page bodies that each fit ``budget`` characters, at
    most ``max_per_page`` blocks per page. A single block longer than the
    budget gets a page to itself and is cut, so no block is ever skipped."""
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if len(block) > budget:
            block = block[: budget - 1] + "…"
        added = len(block) + (len(separator) if current else 0)
        if current and (size + added > budget or len(current) >= max_per_page):
            pages.append(separator.join(current))
            current, size = [], 0
            added = len(block)
        current.append(block)
        size += added
    if current:
        pages.append(separator.join(current))
    return pages


async def resolve_names(bot, user_ids: list[int], fetch_limit: int = 0) -> dict[int, str]:
    """Display names from the client cache, then the users table. Up to
    ``fetch_limit`` ids still unknown are fetched from Discord, which also
    fills the users table for next time; the fetch is paced, so the limit
    keeps a long list from stalling the reply. Ids left unresolved are
    omitted."""
    out: dict[int, str] = {}
    missing = []
    for uid in user_ids:
        u = bot.get_user(uid)
        if u is not None:
            out[uid] = u.display_name
        else:
            missing.append(uid)
    for i in range(0, len(missing), 500):
        chunk = missing[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = await bot.GET(
            f"SELECT discord_user_id, user_name FROM users "
            f"WHERE discord_user_id IN ({placeholders})",
            tuple(chunk),
        )
        for uid, name in rows:
            if name:
                out[uid] = name
    unresolved = [uid for uid in missing if uid not in out][:fetch_limit]
    for uid in unresolved:
        name = await get_username_db(bot, uid)
        if name and name != "Unknown User":
            out[uid] = name
    return out


class PagedBlocksView(BasePaginationView):
    """Pages of free-form text blocks under a fixed header, packed so every
    page fits the embed description limit."""

    def __init__(
        self,
        blocks: list[str],
        title: str,
        header: str = "",
        per_page: int = 5,
        url: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        separator: str = "\n\n",
        item_count: Optional[int] = None,
        noun: str = "entry",
        plural_form: Optional[str] = None,
    ):
        self.header = header
        self.noun = noun
        self.plural_form = plural_form or noun + "s"
        self.url = url
        self.thumbnail_url = thumbnail_url
        self.item_count = len(blocks) if item_count is None else item_count
        cap = min(PAGE_TEXT_BUDGET, MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER)
        budget = cap - (len(header) + 2 if header else 0)
        pages = pack_pages(blocks, budget, per_page, separator)
        super().__init__(pages, title, per_page=1)

    def create_embed(self) -> discord.Embed:
        embed = create_base_embed(title=self.title, color=style.LEADERBOARD)
        if self.url:
            embed.url = self.url
        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)
        page = self.get_page_data()
        body = page[0] if page else "Nothing on this page."
        embed.description = f"{self.header}\n\n{body}" if self.header else body
        add_pagination_footer(
            embed, self.current_page, self.max_pages, self.item_count,
            self.noun, self.plural_form,
        )
        return embed

    async def send(
        self,
        interaction: discord.Interaction,
        links: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        """Follow up with the first page. Page buttons appear only when there
        is more than one page; ``links`` (label, url) always get a row of link
        buttons below them."""
        link_buttons = [
            discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url, row=1)
            for label, url in (links or [])
        ]
        if self.max_pages > 1:
            for button in link_buttons:
                self.add_item(button)
            self.message = await interaction.followup.send(embed=self.create_embed(), view=self)
            return
        self.stop()
        if link_buttons:
            view = discord.ui.View(timeout=None)
            for button in link_buttons:
                view.add_item(button)
            await interaction.followup.send(embed=self.create_embed(), view=view)
        else:
            await interaction.followup.send(embed=self.create_embed())


def _vn_display(vndb_id: str, title: Optional[str], alttitle: Optional[str] = None) -> str:
    """Markdown link to a VN with its title neutralised (titles come from VNDB)."""
    name = link_label(alttitle or title or vndb_id, 80)
    return f"[{name}](https://vndb.org/{vndb_id})"


class VndbLinks(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot
        # At most one pending link-triggered sync per user.
        self._tasks: dict[int, asyncio.Task] = {}
        self._last_link_attempt: dict[int, float] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        # Started here rather than in cog_load: cogs load before login, when
        # before_loop's wait_until_ready cannot run yet. on_ready repeats on
        # reconnects, hence the guard.
        if not self._sync_loop.is_running():
            self._sync_loop.start()

    async def cog_unload(self):
        self._sync_loop.cancel()
        for t in self._tasks.values():
            t.cancel()

    # ------------------------------------------------------------ helpers --

    @tasks.loop(minutes=SYNC_TICK_MINUTES)
    async def _sync_loop(self):
        try:
            await vl.sync_due(self.bot, SYNC_USERS_PER_TICK)
        except Exception:  # noqa: BLE001
            _log.exception("vndb sync pass failed")

    @_sync_loop.before_loop
    async def _before_sync(self):
        await self.bot.wait_until_ready()

    def _spawn_sync(self, user_id: int, vndb_uid: str) -> None:
        async def run():
            try:
                await vl.sync_user(self.bot, user_id, vndb_uid)
            except vl.VndbThrottled:
                _log.warning("vndb sync for new link throttled; loop will retry")
            except Exception:  # noqa: BLE001
                _log.exception("vndb sync for new link failed user=%s", user_id)

        # A newer link supersedes a sync still queued for the previous one.
        previous = self._tasks.get(user_id)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(run(), name=f"vndb-sync-{user_id}")
        self._tasks[user_id] = task

        def _forget(t: asyncio.Task, uid: int = user_id) -> None:
            if self._tasks.get(uid) is t:
                del self._tasks[uid]

        task.add_done_callback(_forget)

    async def _names(self, user_ids: list[int], fetch_limit: int = 0) -> dict[int, str]:
        return await resolve_names(self.bot, user_ids, fetch_limit)

    @staticmethod
    def _parse_server(server: Optional[str]) -> Optional[int]:
        if not server:
            return None
        try:
            return int(server)
        except ValueError:
            raise ValidationError(
                f"bad server value {server!r}",
                "Pick a server from the autocomplete list.",
            )

    # ------------------------------------------------------- /vndb_link ---

    @app_commands.command(name="vndb_link", description="Link or unlink your VNDB account.")
    @app_commands.describe(
        account="Your VNDB username, user id (u followed by digits) or profile URL.",
        action="Link (default) or Unlink. Unlinking deletes the bot's copy of your list.",
    )
    @app_commands.choices(action=LINK_ACTION_CHOICES)
    async def vndb_link(
        self,
        interaction: discord.Interaction,
        account: Optional[app_commands.Range[str, 2, 100]] = None,
        action: Optional[app_commands.Choice[str]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            if action is not None and action.value == "unlink":
                await self._unlink(interaction)
                return
            if not account:
                raise ValidationError(
                    "vndb_link without account",
                    "Pass `account:` with your VNDB username or profile URL, "
                    "or `action: Unlink` to remove your link.",
                )
            # Only linking reaches VNDB, so only linking is rate limited.
            now = time.monotonic()
            last = self._last_link_attempt.get(interaction.user.id)
            if last is not None and now - last < LINK_COOLDOWN_SECONDS:
                wait = int(LINK_COOLDOWN_SECONDS - (now - last)) + 1
                raise ValidationError(
                    "vndb_link cooldown",
                    f"Please wait {wait}s before linking again.",
                )
            self._last_link_attempt[interaction.user.id] = now
            await self._link(interaction, account)
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/vndb_link failed")
            await handle_command_error(interaction, e, "Couldn't update your VNDB link.")

    async def _link(self, interaction: discord.Interaction, account: str) -> None:
        query = vl.parse_account_input(account)
        if not query:
            raise ValidationError(
                "unparseable vndb account input",
                "That doesn't look like a VNDB username, user id or profile URL.",
            )
        try:
            found = await vl.lookup_account(query)
        except vl.VndbUnavailable as e:
            _log.warning("vndb lookup failed: %s", e)
            raise BotError("vndb unavailable", "Couldn't reach VNDB right now. Try again in a minute.")
        if found is None:
            raise ValidationError(
                "vndb account not found",
                "VNDB has no user by that name. Check the spelling, or paste your profile URL.",
            )

        owner = await self.bot.GET_ONE(vl.GET_LINK_OWNER, (found.uid,))
        if owner and owner[0] != interaction.user.id:
            raise ValidationError(
                f"vndb {found.uid} already linked to another user",
                "That VNDB account is already linked to another member. If it's yours, "
                "ask a server manager to remove the other link.",
            )

        current = await self.bot.GET_ONE(vl.GET_LINK_DETAIL, (interaction.user.id,))
        if current and current[0] == found.uid:
            synced_at, sync_error = current[3], current[4]
            stale = True
            # After a failed attempt nothing was refreshed, so the age of the
            # attempt is no reason to wait.
            if synced_at and not sync_error:
                try:
                    age = _utcnow() - datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S")
                    stale = age.total_seconds() > MANUAL_RESYNC_MIN_AGE_MINUTES * 60
                except ValueError:
                    pass
            if stale:
                self._spawn_sync(interaction.user.id, found.uid)
                note = "Refreshing your list now."
            else:
                note = "Your list was refreshed a few minutes ago."
            await interaction.followup.send(
                style.info(f"You're already linked to **{inert_text(found.username, 40)}**. {note}")
            )
            return

        statements = [
            (vl.DELETE_ULIST, (interaction.user.id,)),
            (vl.UPSERT_LINK, (interaction.user.id, found.uid, found.username, interaction.guild_id)),
        ]
        try:
            await self.bot.RUN_TRANSACTION(statements)
        except aiosqlite.IntegrityError:
            # Lost a race with another user linking the same account.
            raise ValidationError(
                f"vndb {found.uid} linked concurrently",
                "That VNDB account is already linked to another member.",
            )
        _log.info(
            "vndb link: user=%s uid=%s replaced=%s",
            interaction.user.id, found.uid, bool(current),
        )
        self._spawn_sync(interaction.user.id, found.uid)
        await interaction.followup.send(style.ok(
            f"Linked to **{inert_text(found.username, 40)}**. Your public VNDB list "
            "shows up in `/ratings` and the VNDB leaderboards within a few minutes."
        ))

    async def _unlink(self, interaction: discord.Interaction) -> None:
        link = await vl.get_link(self.bot, interaction.user.id)
        if not link:
            await interaction.followup.send(style.info("You don't have a VNDB account linked."))
            return
        await self.bot.RUN_TRANSACTION([
            (vl.DELETE_ULIST, (interaction.user.id,)),
            (vl.DELETE_LINK, (interaction.user.id,)),
        ])
        _log.info("vndb unlink: user=%s", interaction.user.id)
        await interaction.followup.send(
            style.ok("Unlinked. The bot's copy of your VNDB list has been deleted.")
        )

    # ---------------------------------------------------- /vndb_profile ---

    @app_commands.command(name="vndb_profile", description="Quick link to a member's VNDB profile.")
    @app_commands.describe(user="Whose profile to link. Defaults to you.", public=PUBLIC_OPTION_HELP)
    async def profile(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User] = None,
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)
        target = user or interaction.user
        link = await vl.get_link(self.bot, target.id)
        if not link:
            if target.id == interaction.user.id:
                msg = "You haven't linked a VNDB account yet. Use `/vndb_link`."
            else:
                msg = f"{inert_text(target.display_name, 40)} hasn't linked a VNDB account."
            await send_error(interaction, style.info(msg))
            return
        vndb_uid, vndb_username = link
        await interaction.followup.send(
            f"🔗 **{inert_text(target.display_name, 40)}** on VNDB: "
            f"[{link_label(vndb_username, 40)}](<{vl.profile_url(vndb_uid)}>)"
        )

    # ------------------------------------------------ /vndb_leaderboard ---

    @app_commands.command(name="vndb_leaderboard", description="VN rankings from linked members' VNDB lists.")
    @app_commands.describe(
        category="What to rank.",
        timeframe="All-time, or only this year (by finish or vote date).",
        server="Optional: only members who linked or logged reads in this server.",
        public=PUBLIC_OPTION_HELP,
    )
    @app_commands.choices(category=VN_CATEGORY_CHOICES, timeframe=TIMEFRAME_CHOICES)
    @app_commands.autocomplete(server=server_autocomplete)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        timeframe: Optional[app_commands.Choice[str]] = None,
        server: Optional[str] = None,
        public: Optional[bool] = None,
    ):
        await self._leaderboard(interaction, category.value, category.name, timeframe, server, public=public)

    # ------------------------------------------- /vndb_user_leaderboard ---

    @app_commands.command(
        name="vndb_user_leaderboard",
        description="Member rankings from linked VNDB lists, with links to each profile.",
    )
    @app_commands.describe(
        category="What to rank members by. Defaults to most finished.",
        timeframe="All-time (lists every linked member), or only this year.",
        server="Optional: only members who linked or logged reads in this server.",
        public=PUBLIC_OPTION_HELP,
    )
    @app_commands.choices(category=USER_CATEGORY_CHOICES, timeframe=TIMEFRAME_CHOICES)
    @app_commands.autocomplete(server=server_autocomplete)
    async def user_leaderboard(
        self,
        interaction: discord.Interaction,
        category: Optional[app_commands.Choice[str]] = None,
        timeframe: Optional[app_commands.Choice[str]] = None,
        server: Optional[str] = None,
        public: Optional[bool] = None,
    ):
        key = category.value if category else "most_finished"
        await self._leaderboard(interaction, key, vl.USER_CATEGORIES[key], timeframe, server, public=public)

    async def _leaderboard(
        self,
        interaction: discord.Interaction,
        key: str,
        label: str,
        timeframe: Optional[app_commands.Choice[str]],
        server: Optional[str],
        public: Optional[bool] = None,
    ) -> None:
        await defer_reply(interaction, public)
        try:
            guild_id = self._parse_server(server)
            this_year = timeframe is not None and timeframe.value == "this_year"
            since = f"{_utcnow().year}-01-01" if this_year else None
            linked, pending = await self.bot.GET_ONE(
                vl.LINKED_IN_SCOPE, (guild_id, guild_id, guild_id)
            )
            linked, pending = linked or 0, pending or 0
            # A fixed floor, so one or two members' votes cannot top the list.
            min_votes = vl.TOP_RATED_MIN_VOTES
            rows = await self.bot.GET(
                vl.leaderboard_query(key, since, min_votes),
                (since, since, guild_id, guild_id, guild_id),
            )
            is_user_board = key in vl.USER_CATEGORIES
            if is_user_board and this_year:
                # All-time keeps zero rows as the list of who is linked; for
                # one year only members with activity are ranked.
                rows = [r for r in rows if r[2] > 0]
            scope_bits = [_utcnow().strftime("%Y") if this_year else "All-time"]
            if guild_id is not None:
                g = self.bot.get_guild(guild_id)
                scope_bits.append(g.name if g else "Unknown server")
            title = style.title("🏆", label, *scope_bits)
            if not rows:
                # Say what is actually missing rather than a generic "no data".
                if linked == 0:
                    reason = "Nobody here has linked a VNDB account yet. Use `/vndb_link`."
                elif pending == linked:
                    reason = "The linked VNDB lists are still syncing; try again later."
                elif key in vl.RATED_CATEGORIES:
                    reason = (
                        f"No VN has votes from {min_votes} or more linked members"
                        f"{' this year' if this_year else ''} yet."
                    )
                else:
                    what = "finished VNs" if key in ("most_finished", "most_read") else "votes"
                    reason = f"Linked members' public lists have no {what}{' this year' if this_year else ''} yet."
                if pending and pending != linked:
                    reason += f" ({style.plural(pending, 'list')} still syncing.)"
                await interaction.followup.send(style.info(reason))
                return

            lines = []
            if is_user_board:
                names = await self._names([r[0] for r in rows])
                for i, (uid, vndb_username, n, vndb_uid, synced_at) in enumerate(rows, 1):
                    # Discord name first, then the VNDB account it belongs to,
                    # so each entry reads as both identities.
                    who = inert_text(names.get(uid) or "Unknown member", 40)
                    account = inert_text(vndb_username, 40)
                    if re.fullmatch(r"u\d+", vndb_uid or ""):
                        account = f"[{link_label(vndb_username, 40)}]({vl.profile_url(vndb_uid)})"
                    if synced_at is None:
                        # Nothing fetched yet; a zero here would be wrong.
                        count = "*list syncing*"
                    else:
                        count = f"{n:,} finished" if key == "most_finished" else style.plural(n, "vote")
                    lines.append(f"**{i}.** {who} · {account} · {count}")
            else:
                for i, (vid, title_, alttitle, n, avg_vote) in enumerate(rows, 1):
                    vote_txt = vl.format_vndb_vote(round(avg_vote)) if avg_vote else None
                    if key == "most_read":
                        tail = style.plural(n, "reader") + (f" · avg {vote_txt}" if vote_txt else "")
                    else:
                        tail = f"{vote_txt} · {style.plural(n, 'vote')}"
                    lines.append(f"**{i}.** {_vn_display(vid, title_, alttitle)} · {tail}")

            header = ""
            if key in vl.RATED_CATEGORIES:
                header = f"VNs with at least {style.plural(min_votes, 'vote')} from linked members."
            elif is_user_board and not this_year:
                header = f"{style.plural(linked, 'linked member')}. Link yours with `/vndb_link`."
            view = PagedBlocksView(
                lines, title, header=header, per_page=PAGE_ROWS, separator="\n",
                noun="member" if is_user_board else "VN",
            )
            await view.send(interaction)
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("vndb leaderboard failed")
            await handle_command_error(interaction, e, "Couldn't build that leaderboard.")

    # ------------------------------------------------ /manage_vndb_link ---

    @app_commands.command(
        name="manage_vndb_link",
        description="[MANAGER] Remove a member's VNDB link.",
    )
    @app_commands.describe(user="The member whose VNDB link to remove.")
    @app_commands.guild_only()
    async def manage_vndb_link(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        try:
            # Links are global and a disputed link may have been made from
            # another server, so any server's managers can remove one. Removal
            # is not destructive: the owner can link again at once.
            await validate_user_permission(interaction)
            link = await vl.get_link(self.bot, user.id)
            if not link:
                await interaction.followup.send(style.info(f"{inert_text(user.display_name, 40)} has no VNDB link."))
                return
            await self.bot.RUN_TRANSACTION([
                (vl.DELETE_ULIST, (user.id,)),
                (vl.DELETE_LINK, (user.id,)),
            ])
            _log.info(
                "vndb link removed by manager: manager=%s guild=%s user=%s uid=%s",
                interaction.user.id, interaction.guild_id, user.id, link[0],
            )
            await interaction.followup.send(style.ok(
                f"Removed {inert_text(user.display_name, 40)}'s link to "
                f"**{inert_text(link[1], 40)}** and the cached list."
            ))
        except BotError as e:
            await handle_command_error(interaction, e)


async def setup(bot: VNClubBot):
    await bot.add_cog(VndbLinks(bot))
