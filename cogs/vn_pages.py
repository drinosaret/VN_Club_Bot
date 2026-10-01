"""/vndb, a VN's page, and /ratings, what members scored and wrote.

Public replies stay short. The long views (a VN's full details, members' full
reviews) open privately for whoever presses the button, so a channel only
ever receives the short message. Those buttons carry their target in the
custom id and are routed through one registered dynamic item, so they keep
working on old messages and across restarts.

Names are only ever shown for members of the server the command runs in:
members who logged a read there or linked their VNDB account there. Counts
across all servers are shown as numbers only.
"""

import logging
from datetime import datetime, timezone
import re
from typing import Optional

import discord
import discord.app_commands as app_commands
from discord.ext import commands

from cogs.vndb_links import NAME_FETCH_LIMIT, pack_pages, resolve_names
from lib import style
from lib import vndb_links as vl
from lib.autocomplete import vn_autocomplete
from lib.bot import VNClubBot
from lib.desciption_processing import replace_bbcode, to_plain_text
from lib.jiten_client import JitenClient, JitenInfo, resolve_display_cover
from lib.monthly_banner import format_length_tier
from lib.pagination import BasePaginationView, PAGE_BLOCKS, PAGE_ROWS, PAGE_TEXT_BUDGET
from lib.ratings import format_average, format_rating, normalize
from lib.utils import (
    link_label,
    BotError,
    ValidationError,
    canonical_vndb_id,
    close_cut,
    handle_command_error,
    inert_text,
    resolve_vn_from_input,
    send_error,
    smart_truncate,
    sql_canonical_vndb_id,
    vndb_id_forms,
)
from lib.vndb_api import from_vndb_id
from lib.visibility import PUBLIC_OPTION_HELP, defer_reply
from lib.vn_details import (
    DEVSTATUS_LABELS,
    RELATION_LABELS,
    VNDetails,
    fetch_vn_details,
    platform_label,
    release_year,
    safe_tags,
)

_log = logging.getLogger(__name__)


SOURCE_CHOICES = [
    app_commands.Choice(name="Both (default)", value="both"),
    app_commands.Choice(name="VN Club logs only", value="hikaru"),
    app_commands.Choice(name="VNDB lists only", value="vndb"),
]
SOURCES = {c.value for c in SOURCE_CHOICES}

FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
# Room kept under the embed total for the footer and rounding.
EMBED_TOTAL_MARGIN = 200
# Source characters of a comment or note inside a full review; escaping can
# lengthen them, and the field itself caps at FIELD_VALUE_LIMIT.
REVIEW_TEXT_LIMIT = 700
# Source characters of the one-line preview on a ratings row.
PREVIEW_LIMIT = 45
CARD_DESCRIPTION_LIMIT = 300
FULL_DESCRIPTION_LIMIT = 2500
FULL_TAG_LIMIT = 20
FULL_RELATION_LIMIT = 10
FULL_LINK_LIMIT = 12

# Members counted as part of a server: logged a read there, or linked their
# VNDB account there. Bind (guild, guild, guild); NULL means every server.
_IN_SCOPE = (
    "(? IS NULL OR user_id IN (SELECT user_id FROM reading_logs WHERE logged_in_guild = ?) "
    "OR user_id IN (SELECT user_id FROM vndb_links WHERE linked_in_guild = ?))"
)

# Every log of a VN by members in scope, most recent reading month first.
# Bind both id forms from vndb_id_forms: some logs store the bare number.
VN_LOGS = f"""
SELECT user_id, user_rating, rating_scale, comment, reward_month,
       COALESCE(completed_at, reward_month)
FROM reading_logs
WHERE vndb_id IN (?, ?) AND {_IN_SCOPE}
ORDER BY reward_month DESC, log_id DESC;
"""

# Linked members' VNDB entries for a VN, in scope.
VN_VNDB = f"""
SELECT l.user_id, l.vndb_username, l.vndb_uid, u.vote, u.notes, u.is_finished, u.lastmod
FROM vndb_ulist u JOIN vndb_links l ON l.user_id = u.user_id
WHERE u.vndb_id = ? AND {vl.scope_clause("l")};
"""

# Ids come back canonical ('v184') even where a log stores the bare number,
# so the log matches its cached title and the member's VNDB list entry.
USER_LOGS = f"""
SELECT {sql_canonical_vndb_id("rl.vndb_id")}, COALESCE(vc.title_ja, vc.title_en), rl.user_rating,
       rl.rating_scale, rl.comment, rl.reward_month,
       COALESCE(rl.completed_at, rl.reward_month)
FROM reading_logs rl
LEFT JOIN vndb_cache vc ON vc.vndb_id = {sql_canonical_vndb_id("rl.vndb_id")}
WHERE rl.user_id = ? AND rl.vndb_id IS NOT NULL
ORDER BY rl.reward_month DESC, rl.log_id DESC;
"""

_SAFE_URL = re.compile(r"https?://[^\s()<>\[\]]+")
_VN_ID = re.compile(r"v\d+")
_VNDB_UID = re.compile(r"u\d+")


# ------------------------------------------------------------ formatting --

def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else close_cut(text[: limit - 1])


def _vndb_vote(vote: int) -> str:
    return f"{vote / 10:.1f}"


def _vn_link(vndb_id: str, title: Optional[str]) -> str:
    return f"[{link_label(title or vndb_id, 60)}](https://vndb.org/{vndb_id})"


def _length_text(details: VNDetails) -> Optional[str]:
    tier = format_length_tier(details.length)
    if details.length_minutes:
        hours = max(1, round(details.length_minutes / 60))
        return f"{tier} (~{hours} h)" if tier else f"~{hours} h"
    return tier


def _cover(details: VNDetails, jiten: Optional[JitenInfo]) -> Optional[str]:
    url, is_nsfw = resolve_display_cover(details, jiten)
    return url if url and not is_nsfw else None


def _link_buttons(vndb_id: str, jiten: Optional[JitenInfo]) -> list[discord.ui.Button]:
    buttons = [discord.ui.Button(label="VNDB", style=discord.ButtonStyle.link, url=f"https://vndb.org/{vndb_id}")]
    if jiten is not None and getattr(jiten, "deck_id", None) is not None:
        buttons.append(discord.ui.Button(
            label="jiten.moe", style=discord.ButtonStyle.link,
            url=f"https://jiten.moe/decks/media/{jiten.deck_id}/detail",
        ))
    return buttons


def _fit_embed(embed: discord.Embed) -> discord.Embed:
    """Drop trailing fields until the embed fits Discord's total size."""
    while len(embed) > EMBED_TOTAL_LIMIT - EMBED_TOTAL_MARGIN and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    return embed


# ---------------------------------------------------------- page buttons --

class PageAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"hikaru:(?:(?P<action>vninfo|vnrat):(?P<vid>v\d+)"
        r"|(?:vnrev:(?P<rvid>v\d+)|urev:(?P<uid>\d+)):(?P<src>both|hikaru|vndb))"
    ),
):
    """A button that opens a private view: full VN info, a VN's ratings, or
    full reviews. Registered once at load; message views carry plain buttons
    with the same custom id, which this item answers."""

    def __init__(self, action: str, key: str, src: Optional[str] = None):
        super().__init__(page_button(action, key, src))
        self.action, self.key, self.src = action, key, src or "both"

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        if match["action"]:
            return cls(match["action"], match["vid"])
        if match["rvid"]:
            return cls("vnrev", match["rvid"], match["src"])
        return cls("urev", match["uid"], match["src"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("VNPages")
        if cog is None:
            await send_error(interaction, style.error("This button isn't available right now."))
            return
        await cog.open_private(interaction, self.action, self.key, self.src)


_BUTTON_LOOK = {
    "vninfo": ("Full info", "ℹ️"),
    "vnrat": ("Ratings", "⭐"),
    "vnrev": ("Full reviews", "📖"),
    "urev": ("Full reviews", "📖"),
}


def page_button(action: str, key: str, src: Optional[str] = None, row: int = 1) -> discord.ui.Button:
    label, emoji = _BUTTON_LOOK[action]
    custom_id = f"hikaru:{action}:{key}" + (f":{src}" if src and action in ("vnrev", "urev") else "")
    return discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=custom_id, row=row)


class PagesView(BasePaginationView):
    """Pages under a fixed summary. A page is either a block of text lines or
    a list of (name, value) fields."""

    def __init__(self, pages: list, title: str, summary: str, footer: str,
                 url: Optional[str] = None, thumbnail_url: Optional[str] = None,
                 color: discord.Color = style.ACCENT):
        self.summary = summary
        self.footer = footer
        self.url = url
        self.thumbnail_url = thumbnail_url
        self.color = color
        super().__init__(pages or [""], title, per_page=1)

    def create_embed(self) -> discord.Embed:
        embed = discord.Embed(title=_cap(self.title, 256), color=self.color)
        if self.url:
            embed.url = self.url
        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)
        page = (self.get_page_data() or [""])[0]
        if isinstance(page, list):
            embed.description = self.summary or None
            for name, value in page:
                embed.add_field(name=name, value=value, inline=False)
        else:
            embed.description = "\n\n".join(p for p in (self.summary, page) if p) or None
        embed.set_footer(text=style.footer(self.current_page, self.max_pages, self.footer))
        return embed


class ReviewsView(PagesView):
    """Full reviews, with a button that switches between highest score first
    and newest first."""

    ORDERS = {"score": "highest score first", "recent": "newest first"}

    def __init__(self, by_score: list, by_recent: list, title: str, count: int,
                 url: Optional[str] = None):
        self._orders = {"score": by_score, "recent": by_recent}
        self._count = count
        self.order = "score"
        super().__init__(by_score, title, summary="", footer=self._footer_text(), url=url)
        self.toggle = discord.ui.Button(style=discord.ButtonStyle.secondary, row=1)
        self.toggle.callback = self._on_toggle
        self._label_toggle()
        self.add_item(self.toggle)

    def _footer_text(self) -> str:
        return style.SEP.join((style.plural(self._count, "review"), self.ORDERS[self.order], SCORE_KEY))

    def _label_toggle(self) -> None:
        if self.order == "score":
            self.toggle.label, self.toggle.emoji = "Newest first", "🕒"
        else:
            self.toggle.label, self.toggle.emoji = "Highest score first", "⭐"

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        self.order = "recent" if self.order == "score" else "score"
        self.footer = self._footer_text()
        self._label_toggle()
        self.set_data(self._orders[self.order])
        await self._navigate(interaction, "sort")

    async def on_timeout(self):
        if self.toggle in self.children:
            self.remove_item(self.toggle)
        await super().on_timeout()

    async def send(self, interaction: discord.Interaction) -> None:
        self.message = await interaction.followup.send(
            embed=self.create_embed(), view=self, ephemeral=True,
        )


async def send_pages(interaction: discord.Interaction, view: PagesView,
                     extras: list[discord.ui.Item], ephemeral: bool = False) -> None:
    """Follow up with the first page. Page buttons appear only when there is
    more than one page; ``extras`` always sit on the row below."""
    if view.max_pages > 1:
        for item in extras:
            view.add_item(item)
        view.message = await interaction.followup.send(
            embed=view.create_embed(), view=view, ephemeral=ephemeral,
        )
        return
    view.stop()
    kwargs = {}
    if extras:
        plain = discord.ui.View(timeout=None)
        for item in extras:
            plain.add_item(item)
        # Stopped so the message is not tracked; its page-action buttons are
        # answered by the registered dynamic item.
        plain.stop()
        kwargs["view"] = plain
    await interaction.followup.send(embed=view.create_embed(), ephemeral=ephemeral, **kwargs)


def pack_field_pages(fields: list[tuple[str, str]], per_page: int, budget: int) -> list[list[tuple[str, str]]]:
    """Group (name, value) fields into pages within Discord's embed limits."""
    pages: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for name, value in fields:
        name, value = _cap(name, FIELD_NAME_LIMIT), _cap(value, FIELD_VALUE_LIMIT)
        cost = len(name) + len(value)
        if current and (size + cost > budget or len(current) >= per_page):
            pages.append(current)
            current, size = [], 0
        current.append((name, value))
        size += cost
    if current:
        pages.append(current)
    return pages


# ------------------------------------------------------ ratings helpers --
#
# An entry is one member's take on one VN: their logs (newest first, as
# (rating, scale, comment, month)) plus their VNDB vote and note.

def _new_entry() -> dict:
    return {"logs": [], "vote": None, "notes": None, "finished": False,
            "vndb_name": None, "vndb_uid": None, "written_at": "",
            "log_at": "", "vndb_at": ""}


def _stamp(value) -> str:
    """A sortable text timestamp. Logs carry text dates (a completion time,
    or just the reading month); VNDB entries carry unix seconds."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return value or ""


def _touch(entry: dict, value, source: str) -> None:
    """Record a timestamp from one source ("log_at" or "vndb_at"). Each is
    kept apart so a single-source view can order by that source alone;
    written_at is the latest of both."""
    stamp = _stamp(value)
    entry[source] = max(entry[source], stamp)
    entry["written_at"] = max(entry["written_at"], stamp)


def _latest_rating(logs: list) -> Optional[tuple[int, int]]:
    for rating, scale, _c, _m in logs:
        if rating:
            return rating, scale or 5
    return None


def sort_score(entry: dict) -> float:
    """Order key on 1-100: the VNDB vote when there is one, since that is the
    score most members keep current, else the latest VN Club rating."""
    if entry["vote"]:
        return float(entry["vote"])
    rating = _latest_rating(entry["logs"])
    return normalize(*rating) if rating else -1.0


def has_score_or_text(entry: dict) -> bool:
    return bool(entry["vote"] or entry["notes"] or any(r or c for r, _s, c, _m in entry["logs"]))


def is_written(entry: dict) -> bool:
    return bool(entry["notes"] or any(c for _r, _s, c, _m in entry["logs"]))


def _filter_source(entry: dict, src: str) -> dict:
    if src == "hikaru":
        return {**entry, "vote": None, "notes": None, "written_at": entry["log_at"]}
    if src == "vndb":
        return {**entry, "logs": [], "written_at": entry["vndb_at"]}
    return entry


# A ratings row leads with the score it is sorted by: the VNDB vote when
# there is one, with the VN Club rating in brackets, else the club rating
# alone. The two formats ("9.5" against "4/5") tell the sources apart, and
# SCORE_KEY spells that out once per list.
SCORE_KEY = "Score is the VNDB vote, with the VN Club rating in brackets; x/y alone is a club rating"


def lead_score(entry: dict, bold: bool = True) -> str:
    rating = _latest_rating(entry["logs"])
    club = format_rating(*rating) if rating else None
    lead = _vndb_vote(entry["vote"]) if entry["vote"] else club
    if lead is None:
        return "**-**" if bold else ""
    text = f"**{lead}**" if bold else lead
    return text + (f" ({club})" if entry["vote"] and club else "")


def rating_row(label: str, entry: dict) -> str:
    """'**9.3** (4/5) · label · *preview*' on one line."""
    row = f"{lead_score(entry)} · {label}"
    comment = next((c for _r, _s, c, _m in entry["logs"] if c), None)
    note = comment or entry["notes"]
    if note:
        row += f" · *{inert_text(note, PREVIEW_LIMIT)}*"
    return row


def review_field(label: str, entry: dict) -> tuple[str, str]:
    """A full review: the heading leads with the score in the same form as
    the ratings list, the comments quoted beneath."""
    score = lead_score(entry, bold=False)
    heading = f"{score}{style.SEP}{label}" if score else label
    lines = []
    written = [(c, m) for _r, _s, c, m in entry["logs"] if c]
    for comment, month in written:
        head = f"**{style.month_short(month)}**\n" if len(written) > 1 else ""
        lines.append(head + "> " + inert_text(comment, REVIEW_TEXT_LIMIT))
    if entry["notes"]:
        lines.append("📝 *VNDB note*\n> " + inert_text(entry["notes"], REVIEW_TEXT_LIMIT))
    return heading, "\n".join(lines) or "*No written review.*"


def _averages(entries: list[dict]) -> tuple[list[int], list[float]]:
    votes = [e["vote"] for e in entries if e["vote"]]
    club = [normalize(*r) for r in (_latest_rating(e["logs"]) for e in entries) if r]
    return votes, club


def averages_line(entries: list[dict]) -> str:
    votes, club = _averages(entries)
    bits = []
    if votes:
        bits.append(f"VNDB **{sum(votes) / len(votes) / 10:.1f}/10** from {len(votes):,}")
    if club:
        bits.append(f"VN Club **{format_average(sum(club) / len(club))}** from {len(club):,}")
    return " · ".join(bits)


# ------------------------------------------------------------------ cog --

class VNPages(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(PageAction)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(PageAction)

    # ---------------------------------------------------------- data ---

    async def vn_entries(self, vndb_id: str, guild_id: Optional[int]) -> dict[int, dict]:
        """Every member in scope with a log or a VNDB entry for the VN."""
        people: dict[int, dict] = {}
        for uid, rating, scale, comment, month, written in await self.bot.GET(
            VN_LOGS, (*vndb_id_forms(vndb_id), guild_id, guild_id, guild_id)
        ):
            e = people.setdefault(uid, _new_entry())
            e["logs"].append((rating, scale, comment, month))
            _touch(e, written, "log_at")
        for uid, name, vndb_uid, vote, notes, finished, lastmod in await self.bot.GET(
            VN_VNDB, (vndb_id, guild_id, guild_id, guild_id)
        ):
            e = people.setdefault(uid, _new_entry())
            e.update(vote=vote, notes=notes, finished=bool(finished), vndb_name=name, vndb_uid=vndb_uid)
            _touch(e, lastmod, "vndb_at")
        return people

    async def user_entries(self, user_id: int) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        titles: dict[str, Optional[str]] = {}
        for vid, title, rating, scale, comment, month, written in await self.bot.GET(USER_LOGS, (user_id,)):
            vid = canonical_vndb_id(vid)
            e = entries.setdefault(vid, _new_entry())
            e["logs"].append((rating, scale, comment, month))
            _touch(e, written, "log_at")
            titles.setdefault(vid, title)
        for vid, title, alttitle, vote, notes, lastmod in await self.bot.GET(
            vl.USER_IMPRESSIONS, (user_id,)
        ):
            vid = canonical_vndb_id(vid)
            e = entries.setdefault(vid, _new_entry())
            e.update(vote=vote, notes=notes)
            _touch(e, lastmod, "vndb_at")
            titles[vid] = alttitle or title or titles.get(vid)
        for vid, e in entries.items():
            e["title"] = titles.get(vid)
        return entries

    async def _jiten(self, vndb_id: str) -> Optional[JitenInfo]:
        try:
            async with JitenClient() as jiten:
                return await jiten.get_by_vndb_id(vndb_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("jiten lookup failed for %s: %s", vndb_id, e)
            return None

    async def _details(self, vndb_id: str) -> VNDetails:
        details = await fetch_vn_details(vndb_id)
        if details is None:
            raise BotError(
                f"vn details unavailable for {vndb_id}",
                "Couldn't load that VN from VNDB right now. Try again in a minute.",
            )
        return details

    async def _resolve(self, title: str) -> str:
        vndb_id = await resolve_vn_from_input(title)
        if not vndb_id:
            raise ValidationError(
                "unresolved vn",
                "Could not determine the VN. Pick one from the autocomplete list.",
            )
        return vndb_id

    async def club_stats(self, vndb_id: str, guild_id: Optional[int]) -> dict:
        """Reader counts and averages across every server, and for this one
        with the member who read it most recently."""
        everywhere = await self.vn_entries(vndb_id, None)
        stats = {"all": self._summarise(everywhere), "here": None}
        if guild_id is not None:
            here = await self.vn_entries(vndb_id, guild_id)
            summary = self._summarise(here)
            latest = max(
                ((m, uid) for uid, e in here.items() for _r, _s, _c, m in e["logs"][:1]),
                default=None,
            )
            if latest:
                names = await resolve_names(self.bot, [latest[1]], fetch_limit=1)
                summary["last"] = (names.get(latest[1]), latest[0])
            stats["here"] = summary
        return stats

    @staticmethod
    def _summarise(people: dict[int, dict]) -> dict:
        entries = list(people.values())
        votes, club = _averages(entries)
        return {
            "readers": sum(1 for e in entries if e["logs"]),
            "club_avg": sum(club) / len(club) if club else None,
            "club_n": len(club),
            "vndb_finished": sum(1 for e in entries if e["finished"]),
            "vndb_avg": sum(votes) / len(votes) if votes else None,
            "vndb_n": len(votes),
            "last": None,
        }

    # --------------------------------------------------------- /vndb ---

    @app_commands.command(name="vndb", description="A VN's page: details, scores, and who read it in this server.")
    @app_commands.describe(title="The VN to look up.", public=PUBLIC_OPTION_HELP)
    @app_commands.autocomplete(title=vn_autocomplete)
    async def vndb(
        self, interaction: discord.Interaction, title: str, public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)
        try:
            vndb_id = await self._resolve(title)
            details = await self._details(vndb_id)
            jiten = await self._jiten(vndb_id)
            stats = await self.club_stats(vndb_id, interaction.guild_id)
            embed = self.card_embed(details, jiten, stats)
            view = discord.ui.View(timeout=None)
            for button in _link_buttons(vndb_id, jiten):
                view.add_item(button)
            view.add_item(page_button("vnrat", vndb_id, row=0))
            view.add_item(page_button("vninfo", vndb_id, row=0))
            view.stop()
            await interaction.followup.send(embed=embed, view=view)
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/vndb failed")
            await handle_command_error(interaction, e, "Couldn't load that VN.")

    @staticmethod
    def _club_value(s: dict) -> str:
        lines = [style.plural(s["readers"], "reader")]
        if s["club_avg"] is not None:
            lines.append(f"{format_average(s['club_avg'])} from {s['club_n']:,}")
        return "\n".join(lines)

    @staticmethod
    def _here_value(s: dict) -> str:
        lines = [style.plural(s["readers"], "reader")]
        if s["club_avg"] is not None:
            lines[0] += f" · {format_average(s['club_avg'])}"
        if s["last"] and s["last"][0]:
            lines.append(f"Last: {inert_text(s['last'][0], 32)}, {style.month_short(s['last'][1])}")
        if s["vndb_finished"] or s["vndb_n"]:
            vndb = [f"{s['vndb_finished']:,} finished"] if s["vndb_finished"] else []
            if s["vndb_avg"] is not None:
                vndb.append(f"{s['vndb_avg'] / 10:.1f}/10")
            lines.append("VNDB: " + " · ".join(vndb))
        return "\n".join(lines)

    def card_embed(self, details: VNDetails, jiten: Optional[JitenInfo], stats: dict) -> discord.Embed:
        embed = discord.Embed(
            title=_cap(details.display_title, 256),
            url=f"https://vndb.org/{details.vndb_id}",
            color=style.ACCENT,
        )
        lines = []
        if details.romanized_title:
            lines.append(f"*{inert_text(details.romanized_title, 120)}*")
        facts = [inert_text(d, 40) for d in details.developers[:2]]
        facts += [x for x in (release_year(details.released), _length_text(details)) if x]
        if facts:
            lines.append(" · ".join(facts))
        blurb = to_plain_text(details.description or "").strip()
        if blurb:
            lines.append("")
            lines.append(inert_text(smart_truncate(blurb, CARD_DESCRIPTION_LIMIT)))
        embed.description = "\n".join(lines) or None

        if details.rating:
            embed.add_field(
                name="VNDB",
                value=f"{details.rating / 10:.1f}/10\n{style.plural(details.votecount, 'vote')}",
                inline=True,
            )
        s_all = stats["all"]
        if s_all["readers"]:
            label = "VN Club, all servers" if stats["here"] is not None else "VN Club"
            embed.add_field(name=label, value=self._club_value(s_all), inline=True)
        s_here = stats["here"]
        if s_here and (s_here["readers"] or s_here["vndb_finished"] or s_here["vndb_n"]):
            embed.add_field(name="This server", value=self._here_value(s_here), inline=True)
        cover = _cover(details, jiten)
        if cover:
            embed.set_thumbnail(url=cover)
        return _fit_embed(embed)

    def full_info_embed(self, details: VNDetails, jiten: Optional[JitenInfo], stats: dict) -> discord.Embed:
        embed = discord.Embed(
            title=_cap(details.display_title, 256),
            url=f"https://vndb.org/{details.vndb_id}",
            color=style.ACCENT,
        )
        desc = replace_bbcode(details.description or "").strip()
        head = f"*{inert_text(details.romanized_title, 120)}*\n\n" if details.romanized_title else ""
        embed.description = (head + (smart_truncate(desc, FULL_DESCRIPTION_LIMIT) if desc else "No description on VNDB.")).strip()

        def add(name: str, value: Optional[str], inline: bool = True) -> None:
            if value:
                embed.add_field(name=name, value=_cap(value, FIELD_VALUE_LIMIT), inline=inline)

        add("Developer" if len(details.developers) == 1 else "Developers",
            ", ".join(inert_text(d, 60) for d in details.developers))
        add("Released", details.released)
        if details.devstatus:
            add("Status", DEVSTATUS_LABELS.get(details.devstatus))
        length = _length_text(details)
        if length and details.length_votes:
            length += f"\nfrom {style.plural(details.length_votes, 'vote')}"
        add("Length", length)
        if details.rating:
            score = f"{details.rating / 10:.1f}/10 from {style.plural(details.votecount, 'vote')}"
            if details.average and round(details.average) != round(details.rating):
                score += f"\nmean {details.average / 10:.1f}"
            add("VNDB score", score)
        add("Platforms", ", ".join(platform_label(p) for p in details.platforms), inline=False)
        tags = safe_tags(details, FULL_TAG_LIMIT)
        add("Tags", " · ".join(inert_text(t, 40) for t in tags), inline=False)

        if jiten is not None:
            bits = []
            if jiten.character_count:
                bits.append(f"{jiten.character_count:,} characters")
            if jiten.difficulty_raw and jiten.difficulty_raw > 0:
                bits.append(f"difficulty {jiten.difficulty_raw:.2f}/5")
            if jiten.unique_kanji_count:
                bits.append(f"{jiten.unique_kanji_count:,} unique kanji")
            if jiten.dialogue_percentage and jiten.dialogue_percentage > 0:
                bits.append(f"{jiten.dialogue_percentage:.0f}% dialogue")
            add("jiten.moe", " · ".join(bits), inline=False)

        s_all = stats["all"]
        if s_all["readers"] or s_all["vndb_n"]:
            bits = [style.plural(s_all["readers"], "reader")]
            if s_all["club_avg"] is not None:
                bits.append(f"{format_average(s_all['club_avg'])} from {style.plural(s_all['club_n'], 'rating')}")
            if s_all["vndb_n"]:
                bits.append(f"linked VNDB votes {s_all['vndb_avg'] / 10:.1f}/10 from {s_all['vndb_n']:,}")
            add("VN Club, all servers", " · ".join(bits), inline=False)
        s_here = stats["here"]
        if s_here and (s_here["readers"] or s_here["vndb_finished"] or s_here["vndb_n"]):
            add("This server", self._here_value(s_here).replace("\n", " · "), inline=False)


        related = []
        for r in details.relations[:FULL_RELATION_LIMIT]:
            rid = r.get("id") or ""
            if not _VN_ID.fullmatch(rid):
                continue
            label = RELATION_LABELS.get(r.get("relation"), "Related")
            if r.get("relation_official") is False:
                label += " (unofficial)"
            related.append(f"{label}: {_vn_link(rid, r.get('alttitle') or r.get('title'))}")
        add("Related", "\n".join(related), inline=False)

        links = [
            f"[{link_label(x.get('label') or 'Link', 40)}]({x['url']})"
            for x in details.extlinks[:FULL_LINK_LIMIT]
            if isinstance(x.get("url"), str) and _SAFE_URL.fullmatch(x["url"])
        ]
        add("Links", " · ".join(links), inline=False)

        cover = _cover(details, jiten)
        if cover:
            embed.set_thumbnail(url=cover)
        return _fit_embed(embed)

    # ------------------------------------------------------ /ratings ---

    @app_commands.command(
        name="ratings",
        description="Members' scores for a VN, or one member's scores, with their notes.",
    )
    @app_commands.describe(
        title="The VN to show ratings for.",
        user="Show this member's ratings across VNs instead.",
        source="Where scores come from. Defaults to both.",
        public=PUBLIC_OPTION_HELP,
    )
    @app_commands.autocomplete(title=vn_autocomplete)
    @app_commands.choices(source=SOURCE_CHOICES)
    @app_commands.guild_only()
    async def ratings(
        self,
        interaction: discord.Interaction,
        title: Optional[str] = None,
        user: Optional[discord.User] = None,
        source: Optional[app_commands.Choice[str]] = None,
        public: Optional[bool] = None,
    ):
        await defer_reply(interaction, public)
        try:
            src = source.value if source else "both"
            if bool(title) == bool(user):
                raise ValidationError(
                    "ratings needs exactly one of title/user",
                    "Pass either `title:` or `user:`.",
                )
            if title:
                await self.send_vn_ratings(interaction, await self._resolve(title), src)
            else:
                await self.send_user_ratings(interaction, user, src)
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("/ratings failed")
            await handle_command_error(interaction, e, "Couldn't load ratings.")

    async def _vn_title_and_cover(self, vndb_id: str) -> tuple[str, Optional[str], Optional[JitenInfo]]:
        details = await fetch_vn_details(vndb_id)
        jiten = await self._jiten(vndb_id)
        if details is not None:
            return details.display_title, _cover(details, jiten), jiten
        cached = await from_vndb_id(self.bot, vndb_id)
        if cached is None:
            return vndb_id, None, jiten
        return cached.title_ja or cached.title_en or vndb_id, _cover(cached, jiten), jiten

    async def send_vn_ratings(self, interaction: discord.Interaction, vndb_id: str,
                              src: str, ephemeral: bool = False) -> None:
        people = {
            uid: e for uid, e in (
                (uid, _filter_source(e, src)) for uid, e in
                (await self.vn_entries(vndb_id, interaction.guild_id)).items()
            ) if has_score_or_text(e)
        }
        display_title, _cover_url, jiten = await self._vn_title_and_cover(vndb_id)
        if not people:
            await interaction.followup.send(
                style.info(f"Nobody in this server has rated **{inert_text(display_title, 80)}** yet."),
                ephemeral=ephemeral,
            )
            return
        names = await resolve_names(self.bot, list(people), fetch_limit=NAME_FETCH_LIMIT)

        def label(uid: int) -> str:
            return inert_text(names.get(uid) or people[uid]["vndb_name"] or "Unknown member", 32)

        def row_label(uid: int) -> str:
            # A row showing VNDB data links the name to that VNDB profile.
            e = people[uid]
            vndb_uid = e.get("vndb_uid") or ""
            if (e["vote"] or e["notes"]) and _VNDB_UID.fullmatch(vndb_uid):
                raw = names.get(uid) or e["vndb_name"] or "Unknown member"
                return f"[{link_label(raw, 32)}]({vl.profile_url(vndb_uid)})"
            return label(uid)

        ordered = sorted(people, key=lambda uid: (-sort_score(people[uid]), label(uid).lower()))
        rows = [rating_row(row_label(uid), people[uid]) for uid in ordered]
        pages = pack_pages(rows, PAGE_TEXT_BUDGET, PAGE_ROWS, "\n")
        view = PagesView(
            pages,
            f"⭐ Ratings · {display_title}",
            summary=averages_line(list(people.values())),
            footer=style.SEP.join((f"{style.plural(len(rows), 'member')} in this server", SCORE_KEY)),
            url=f"https://vndb.org/{vndb_id}",
        )
        extras: list[discord.ui.Item] = []
        if any(is_written(e) for e in people.values()):
            extras.append(page_button("vnrev", vndb_id, src))
        extras += [b for b in _link_buttons(vndb_id, jiten) if not ephemeral]
        for item in extras:
            item.row = 1
        await send_pages(interaction, view, extras, ephemeral=ephemeral)

    async def send_user_ratings(self, interaction: discord.Interaction, user: discord.abc.User,
                                src: str, ephemeral: bool = False) -> None:
        entries = {
            vid: e for vid, e in (
                (vid, _filter_source(e, src)) for vid, e in (await self.user_entries(user.id)).items()
            ) if has_score_or_text(e)
        }
        link = await self.bot.GET_ONE(vl.GET_LINK_DETAIL, (user.id,))
        who = inert_text(user.display_name, 40)
        if not entries:
            hint = ""
            if src in ("both", "vndb"):
                if not link:
                    hint = " They haven't linked a VNDB account."
                elif link[3] is None:
                    hint = " Their VNDB list is still syncing; try again later."
            await interaction.followup.send(style.info(f"**{who}** hasn't rated anything yet.{hint}"), ephemeral=ephemeral)
            return
        ordered = sorted(entries, key=lambda vid: (-sort_score(entries[vid]), (entries[vid]["title"] or vid).lower()))
        rows = [rating_row(_vn_link(vid, entries[vid]["title"]), entries[vid]) for vid in ordered]
        summary = []
        if link:
            summary.append(f"🔗 VNDB [{link_label(link[1], 40)}]({vl.profile_url(link[0])})")
        averages = averages_line(list(entries.values()))
        if averages:
            summary.append(averages)
        view = PagesView(
            pack_pages(rows, PAGE_TEXT_BUDGET, PAGE_ROWS, "\n"),
            f"⭐ Ratings · {user.display_name}",
            summary="\n".join(summary),
            footer=style.SEP.join((style.plural(len(rows), "VN"), SCORE_KEY)),
        )
        extras: list[discord.ui.Item] = []
        if any(is_written(e) for e in entries.values()):
            extras.append(page_button("urev", str(user.id), src))
        await send_pages(interaction, view, extras, ephemeral=ephemeral)

    async def send_vn_reviews(self, interaction: discord.Interaction, vndb_id: str, src: str) -> None:
        people = {
            uid: e for uid, e in (
                (uid, _filter_source(e, src)) for uid, e in
                (await self.vn_entries(vndb_id, interaction.guild_id)).items()
            ) if is_written(e)
        }
        display_title, _cover_url, _jiten = await self._vn_title_and_cover(vndb_id)
        if not people:
            await interaction.followup.send(
                style.info(f"No written reviews of **{inert_text(display_title, 80)}** in this server yet."),
                ephemeral=True,
            )
            return
        names = await resolve_names(self.bot, list(people), fetch_limit=NAME_FETCH_LIMIT)

        def label(uid: int) -> str:
            return inert_text(names.get(uid) or people[uid]["vndb_name"] or "Unknown member", 40)

        by_score = sorted(people, key=lambda uid: (-sort_score(people[uid]), label(uid).lower()))
        by_recent = sorted(people, key=lambda uid: people[uid]["written_at"], reverse=True)

        def pages(order: list) -> list:
            return pack_field_pages([review_field(label(uid), people[uid]) for uid in order],
                                    PAGE_BLOCKS, PAGE_TEXT_BUDGET)

        view = ReviewsView(
            pages(by_score), pages(by_recent), f"📖 Reviews · {display_title}",
            count=len(people), url=f"https://vndb.org/{vndb_id}",
        )
        await view.send(interaction)

    async def send_user_reviews(self, interaction: discord.Interaction, user_id: int, src: str) -> None:
        entries = {
            vid: e for vid, e in (
                (vid, _filter_source(e, src)) for vid, e in (await self.user_entries(user_id)).items()
            ) if is_written(e)
        }
        names = await resolve_names(self.bot, [user_id], fetch_limit=1)
        who = names.get(user_id) or "This member"
        if not entries:
            await interaction.followup.send(style.info(f"No written reviews from **{inert_text(who, 40)}** yet."), ephemeral=True)
            return
        by_score = sorted(entries, key=lambda vid: (-sort_score(entries[vid]), (entries[vid]["title"] or vid).lower()))
        by_recent = sorted(entries, key=lambda vid: entries[vid]["written_at"], reverse=True)

        def pages(order: list) -> list:
            return pack_field_pages(
                [review_field(inert_text(entries[vid]["title"] or vid, 80), entries[vid]) for vid in order],
                PAGE_BLOCKS, PAGE_TEXT_BUDGET,
            )

        view = ReviewsView(pages(by_score), pages(by_recent), f"📖 Reviews · {who}", count=len(entries))
        await view.send(interaction)

    # ------------------------------------------------ private views ---

    async def open_private(self, interaction: discord.Interaction, action: str, key: str, src: str) -> None:
        """Answer a page-action button with a view only the presser sees."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if action == "vninfo":
                details = await self._details(key)
                jiten = await self._jiten(key)
                stats = await self.club_stats(key, interaction.guild_id)
                await interaction.followup.send(
                    embed=self.full_info_embed(details, jiten, stats), ephemeral=True,
                )
            elif interaction.guild_id is None:
                await send_error(interaction, style.error("Ratings are only available inside a server."))
            elif action == "vnrat":
                await self.send_vn_ratings(interaction, key, "both", ephemeral=True)
            elif action == "vnrev":
                await self.send_vn_reviews(interaction, key, src if src in SOURCES else "both")
            elif action == "urev":
                await self.send_user_reviews(interaction, int(key), src if src in SOURCES else "both")
        except BotError as e:
            await handle_command_error(interaction, e)
        except Exception as e:
            _log.exception("page action %s failed", action)
            await handle_command_error(interaction, e, "Couldn't open that.")


async def setup(bot: VNClubBot):
    await bot.add_cog(VNPages(bot))
