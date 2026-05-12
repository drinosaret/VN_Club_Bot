"""
``/badges`` and ``/club_stats`` slash commands.

Lives in its own cog so the existing user/title/cycle cogs stay focused.
Both commands share scope-handling helpers (server vs. global aggregates).
"""

import asyncio
import logging
from typing import Optional

import discord
import discord.app_commands as app_commands
from discord.ext import commands

from lib.badges import BADGE_BY_ID, BADGE_DEFS, compute_user_badges
from lib.badges_grid import render_badges_grid
from lib.bot import VNClubBot
from lib.club_stats_card import render_club_stats
from lib.utils import DatabaseQueries

_log = logging.getLogger(__name__)


CLUB_STATS_SCOPE_CHOICES = [
    app_commands.Choice(name="This server", value="server"),
    app_commands.Choice(name="Global (all servers)", value="global"),
]


def _build_full_12_month_trend(rows) -> list[tuple[str, int]]:
    """Take whatever the trend query returned (sparse, DESC) and rebuild a
    contiguous oldest→newest 12-month series ending at the current month.
    Missing months become zero entries so the chart's x-axis is always 12
    bars wide — the dashboard's eyebrow promises "LAST 12 MONTHS"."""
    from datetime import datetime, timezone
    counts = {str(r[0]): int(r[1]) for r in rows if r and r[0]}
    # ``reward_month`` is derived from ``get_current_month()`` (UTC). Using
    # ``datetime.now()`` here would diverge from the stored data near
    # month boundaries on any non-UTC server, making the current bar empty
    # and the oldest bar labelled one month too early.
    today = datetime.now(timezone.utc)
    out: list[tuple[str, int]] = []
    # Walk back 11 months from current to build the [oldest, ..., newest] axis.
    year = today.year
    month = today.month
    months_back: list[tuple[int, int]] = []
    for _ in range(12):
        months_back.append((year, month))
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
    for y, m in reversed(months_back):
        key = f"{y:04d}-{m:02d}"
        out.append((key, counts.get(key, 0)))
    return out


class VNClubStats(commands.Cog):
    """Read-only image commands powered by aggregate stats."""

    def __init__(self, bot: VNClubBot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /badges
    # ------------------------------------------------------------------
    @app_commands.command(
        name="badges",
        description="Show a user's earned achievements as an image grid.",
    )
    @app_commands.describe(
        user="Optional: whose badges to show (defaults to yourself).",
    )
    async def badges(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User] = None,
    ):
        """Render the badges grid for ``user`` (or the caller).

        Badges are computed live from existing logs/cycles — no backfill or
        unlock-event table is needed. Scope is intentionally global per
        user: a user's achievements span every server they've logged in,
        which is the same way `/profile` already works.
        """
        await interaction.response.defer()

        target = user or interaction.user
        try:
            unlocked = await compute_user_badges(self.bot, target.id, scope_guild_id=None)
        except Exception as e:  # noqa: BLE001
            _log.exception("compute_user_badges failed for %s: %s", target.id, e)
            await interaction.followup.send(
                "❌ Couldn't load badges right now. Try again in a moment."
            )
            return

        display_name = getattr(target, "display_name", None) or target.name
        try:
            buf = await asyncio.to_thread(render_badges_grid, unlocked, display_name)
        except Exception as e:  # noqa: BLE001
            _log.exception("render_badges_grid failed: %s", e)
            await interaction.followup.send(
                "❌ Couldn't render the badges image."
            )
            return

        # Attach a short text summary alongside the image so unlocked badge
        # names are searchable in chat (the grid uses tier-label discs, not
        # emoji glyphs — the emoji belongs to Discord text, not the canvas).
        unlocked_names = [
            f"{BADGE_BY_ID[b].emoji} {BADGE_BY_ID[b].name}"
            for b in unlocked
            if b in BADGE_BY_ID
        ]
        summary_lines = [
            f"**{display_name}** — {len(unlocked)}/{len(BADGE_DEFS)} achievements earned.",
        ]
        if unlocked_names:
            summary_lines.append("Unlocked: " + ", ".join(unlocked_names))
        else:
            summary_lines.append("No badges yet — use `/finish` to log a VN!")

        file = discord.File(buf, filename=f"badges-{target.id}.png")
        await interaction.followup.send(content="\n".join(summary_lines), file=file)


    # ------------------------------------------------------------------
    # /club_stats
    # ------------------------------------------------------------------
    @app_commands.command(
        name="club_stats",
        description="Server-wide stats dashboard (or global across all servers).",
    )
    @app_commands.describe(
        scope="`server` (default) — this server only · `global` — every server combined.",
    )
    @app_commands.choices(scope=CLUB_STATS_SCOPE_CHOICES)
    @app_commands.guild_only()
    async def club_stats(
        self,
        interaction: discord.Interaction,
        scope: Optional[app_commands.Choice[str]] = None,
    ):
        """Dashboard image. Server scope shows the current guild only;
        global aggregates every server hikaru is in."""
        await interaction.response.defer()

        scope_value = (scope.value if scope else "server").lower()
        if scope_value == "global":
            scope_label = "GLOBAL"
            guild_filter: Optional[int] = None
        else:
            scope_label = (
                interaction.guild.name if interaction.guild else "—"
            )
            guild_filter = interaction.guild.id if interaction.guild else None

        # ---- Aggregates ----
        try:
            totals_row = await self.bot.GET_ONE(
                DatabaseQueries.CLUB_STATS_TOTALS,
                (guild_filter, guild_filter),
            )
            top_rows = await self.bot.GET(
                DatabaseQueries.CLUB_STATS_TOP_CONTRIBUTORS,
                (guild_filter, guild_filter, 5),
            )
            rating_rows = await self.bot.GET(
                DatabaseQueries.CLUB_STATS_RATING_DIST,
                (guild_filter, guild_filter),
            )
            trend_rows = await self.bot.GET(
                DatabaseQueries.CLUB_STATS_MONTHLY_TREND,
                (guild_filter, guild_filter),
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("club_stats query failure: %s", e)
            await interaction.followup.send("❌ Couldn't load club stats right now.")
            return

        if not totals_row:
            await interaction.followup.send("No data yet — try logging some VNs first!")
            return

        total_completions, unique_vns, active_members, total_points = totals_row

        # Resolve top-contributor user IDs to display names. Bot's user cache
        # covers most active users; for ones that aren't cached we fall back
        # to "User <id>" so the renderer never blocks on Discord lookups.
        top_contributors: list[tuple[str, int, int]] = []
        for row in top_rows or []:
            uid, pts, completions = row[0], row[1] or 0, row[2] or 0
            display = f"User {uid}"
            try:
                u = self.bot.get_user(int(uid))
                if u is not None:
                    display = getattr(u, "display_name", None) or u.name
            except Exception:
                pass
            top_contributors.append((display, int(pts), int(completions)))

        rating_distribution = [
            (int(r[0]), int(r[1])) for r in (rating_rows or [])
        ]
        # Pad the trend to a full 12-month window ending at the current month
        # so the eyebrow's "LAST 12 MONTHS" matches what's drawn. Months with
        # no logs render as a zero-height bar — the strip stays a stable
        # length regardless of how sparse the underlying data is.
        monthly_trend = _build_full_12_month_trend(trend_rows or [])

        try:
            buf = await asyncio.to_thread(
                render_club_stats,
                scope_label=scope_label,
                total_points=int(total_points or 0),
                total_completions=int(total_completions or 0),
                unique_vns=int(unique_vns or 0),
                active_members=int(active_members or 0),
                top_contributors=top_contributors,
                rating_distribution=rating_distribution,
                monthly_trend=monthly_trend,
            )
        except Exception as e:  # noqa: BLE001
            _log.exception("render_club_stats failed: %s", e)
            await interaction.followup.send("❌ Couldn't render the dashboard image.")
            return

        file = discord.File(
            buf,
            filename=f"club-stats-{scope_value}.png",
        )
        await interaction.followup.send(file=file)

async def setup(bot: VNClubBot):
    await bot.add_cog(VNClubStats(bot))
