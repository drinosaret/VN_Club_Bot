"""
Achievements / badges for hikaru.

Badges are computed *live* from the existing tables (reading_logs,
vn_titles, vn_votes, vn_cycles, vndb_cache) — no badge unlock events
are persisted, so any historical reading retroactively earns the matching
badges. This keeps the system simple and means deployment requires no
backfill script.

Public surface:

    BADGE_DEFS                                        — ordered list of Badge
    compute_user_badges(bot, user_id, scope_guild_id) — set[badge_id] unlocked
    aggregate_user_stats(bot, user_id, scope_guild_id) — raw counts (for tests / UI)

The renderer (`render_badges_grid`) lives in ``lib/badges_grid.py`` to keep
this module dependency-free of Pillow — call sites that only need the data
(profile-card strip, /finish unlock detection) don't pull image deps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


# ---------- Badge spec ----------

@dataclass(frozen=True)
class Badge:
    """Static metadata for a single badge.

    `key` is the aggregate name in the dict produced by `aggregate_user_stats`.
    `threshold` is the minimum value of `key` needed to unlock. Sorting
    badges by (category, threshold) gives a stable display order across
    locked/unlocked states.
    """
    id: str
    name: str
    description: str
    emoji: str
    category: str
    key: str
    threshold: int


BADGE_DEFS: Sequence[Badge] = (
    # Volume — number of completed VNs.
    Badge("first_steps",  "First Steps", "Finish your first VN.", "🌱", "volume", "total_completions",   1),
    Badge("reader",       "Reader",      "Finish 10 VNs.",        "📚", "volume", "total_completions",  10),
    Badge("enthusiast",   "Enthusiast",  "Finish 25 VNs.",        "✨", "volume", "total_completions",  25),
    Badge("scholar",      "Scholar",     "Finish 50 VNs.",        "🏛️", "volume", "total_completions",  50),
    Badge("legend",       "Legend",      "Finish 100 VNs.",       "🌟", "volume", "total_completions", 100),

    # Pool picks — finished pool entries during their active window. Tiered
    # so dedicated readers earn higher ranks for sustained participation.
    # Replaces the old `total_chars` family because jiten doesn't index every
    # VN, which made character-count badges unfair.
    Badge("monthly_apprentice", "Monthly Apprentice", "Finish 1 monthly pick in its window.",   "🌙", "pool", "monthly_pool_count",  1),
    Badge("monthly_devotee",    "Monthly Devotee",    "Finish 5 monthly picks in their windows.","🌘", "pool", "monthly_pool_count",  5),
    Badge("monthly_veteran",    "Monthly Veteran",    "Finish 12 monthly picks (a full year).",  "🌑", "pool", "monthly_pool_count", 12),
    Badge("seasonal_apprentice","Seasonal Apprentice","Finish 1 seasonal pick in its window.",   "🌸", "pool", "seasonal_pool_count", 1),
    Badge("seasonal_veteran",   "Seasonal Veteran",   "Finish 4 seasonal picks (a full year).",  "🌺", "pool", "seasonal_pool_count", 4),

    # Engagement — cycle participation. (A "Special Reader" badge for the
    # 'As Special VN' pool kind was removed because the special-pick status
    # isn't guaranteed to be used by every server, which made the badge
    # unfair / dead weight in the grid.)
    Badge("voter",           "Voter",           "Vote in a nomination cycle.",                       "🗳️", "engagement", "votes_cast",          1),
    Badge("nominator",       "Nominator",       "Nominate a VN for a cycle.",                        "🎯", "engagement", "nominations_made",    1),
    Badge("tastemaker",      "Tastemaker",      "Have one of your nominations win its vote.",        "🏆", "engagement", "tastemaker_wins",     1),

    # Season leaderboard placement — ranked in a *completed* anime season's
    # leaderboard. Current/in-progress seasons don't count until they end so
    # placements are stable.
    Badge("season_top10",   "Season Top 10",   "Rank in the top 10 of a completed season's leaderboard.", "🌟", "leaderboard", "season_top10_count", 1),
    Badge("season_podium",  "Season Podium",   "Rank in the top 3 of a completed season's leaderboard.",  "🥈", "leaderboard", "season_top3_count",  1),
    Badge("season_champion","Season Champion", "Rank #1 in a completed season's leaderboard.",            "🥇", "leaderboard", "season_top1_count",  1),

    # Long-term consistency — distinct reward_months in the user's logs.
    Badge("quarter_year","Quarter Year", "Log VNs in 3 different months.",  "📆", "consistency", "distinct_months",  3),
    Badge("half_year",   "Half Year",    "Log VNs in 6 different months.",  "📅", "consistency", "distinct_months",  6),
    Badge("year_rounder","Year-Rounder", "Log VNs in 12 different months.", "🗓️", "consistency", "distinct_months", 12),
)


def _badge_index() -> dict[str, Badge]:
    return {b.id: b for b in BADGE_DEFS}


BADGE_BY_ID: dict[str, Badge] = _badge_index()


# ---------- Aggregates ----------

async def aggregate_user_stats(
    bot,
    user_id: int,
    scope_guild_id: Optional[int] = None,
) -> dict[str, int]:
    """Compute every aggregate the badges need in one batched pass.

    `scope_guild_id` filters reading_logs by `logged_in_guild` (and cycle-
    derived stats by `guild_id`). Pass `None` for global (a user's
    achievements span every server they've logged in by default).

    Returns a dict with all keys referenced by ``Badge.key`` even if zero,
    so predicates can read them unconditionally.
    """
    # Build the optional guild predicate string + param list once. We add the
    # appropriate clause to each query rather than relying on coalesced
    # parameters because the joined queries (vn_votes etc.) reference
    # different columns for the guild.
    g = scope_guild_id

    # 1. Reading-log-derived counts (most aggregates).
    base_where = "WHERE user_id = ?"
    base_params: list = [user_id]
    if g is not None:
        base_where += " AND logged_in_guild = ?"
        base_params.append(g)

    # All vndb-keyed badge counts are DISTINCT-by-vndb_id so re-reads (now
    # allowed once per (user, vn, reward_month) by /finish) don't inflate
    # "Finish N VNs"-style achievements. distinct_months stays a raw
    # DISTINCT(reward_month) since consistency badges genuinely care about
    # how many months the user logged in — re-reads in new months are
    # legitimate signals there.
    # NULL vndb_id rows (e.g. /manage_reward_points entries with no VN)
    # are excluded from VN-counting metrics; DISTINCT ignores NULL anyway,
    # but the explicit filter on the pool-kind aggregates makes it visible.
    row = await bot.GET_ONE(
        f"""
        SELECT
            COUNT(DISTINCT vndb_id) AS total_completions,
            COUNT(DISTINCT CASE WHEN reward_reason = 'As Monthly VN'  THEN vndb_id END) AS monthly_pool_count,
            COUNT(DISTINCT CASE WHEN reward_reason = 'As Seasonal VN' THEN vndb_id END) AS seasonal_pool_count,
            COUNT(DISTINCT reward_month) AS distinct_months
        FROM reading_logs
        {base_where}
        """,
        tuple(base_params),
    )
    total_completions   = row[0] if row else 0
    monthly_pool_count  = row[1] if row and row[1] is not None else 0
    seasonal_pool_count = row[2] if row and row[2] is not None else 0
    distinct_months     = row[3] if row and row[3] is not None else 0

    # 2. Cycle engagement.
    # (numbered comment kept stable for future readers; the chars query was
    #  removed when character-count badges were dropped — too many VNs
    #  aren't indexed on jiten for that to be a fair achievement gate.)
    # Post-unify: nominations live in vn_titles with cycle_id set. We count
    # any nomination-derived row (status may be 'nominated' for losers/in-flight
    # OR 'monthly'/'seasonal' for promoted winners), so the gate is just
    # "did the user nominate", filtered via cycle_id IS NOT NULL.
    nom_row = await bot.GET_ONE(
        f"""
        SELECT COUNT(*) FROM vn_titles
        WHERE nominator_user_id = ? AND cycle_id IS NOT NULL
          {"AND guild_id = ?" if g is not None else ""}
        """,
        tuple([user_id] + ([g] if g is not None else [])),
    )
    nominations_made = nom_row[0] if nom_row else 0

    vote_row = await bot.GET_ONE(
        f"""
        SELECT COUNT(*) FROM vn_votes
        WHERE user_id = ?
          {"AND guild_id = ?" if g is not None else ""}
        """,
        tuple([user_id] + ([g] if g is not None else [])),
    )
    votes_cast = vote_row[0] if vote_row else 0

    # 4. Tastemaker — nominations by this user that won (highest vote count
    #    in their closed cycle). Single query joining nominations + cycles
    #    + a per-cycle top-nomination subquery.
    # Post-unify: nominations live in vn_titles. The user-nominated row
    # is identified by cycle_id IS NOT NULL + nominator_user_id; "won"
    # means it's the top-voted nomination in a closed cycle (regardless of
    # whether its status was already flipped to monthly/seasonal — the
    # subquery joins on cycle_id, not on status).
    win_row = await bot.GET_ONE(
        f"""
        SELECT COUNT(*) FROM vn_titles n
        JOIN vn_cycles c ON c.id = n.cycle_id
        WHERE n.nominator_user_id = ?
          AND n.cycle_id IS NOT NULL
          {"AND n.guild_id = ?" if g is not None else ""}
          AND c.phase = 'closed'
          AND n.id = (
              SELECT v.nomination_id FROM vn_votes v
              WHERE v.cycle_id = n.cycle_id
              GROUP BY v.nomination_id
              ORDER BY COUNT(*) DESC LIMIT 1
          )
        """,
        tuple([user_id] + ([g] if g is not None else [])),
    )
    tastemaker_wins = win_row[0] if win_row else 0

    # 5. Season-leaderboard placements. Buckets logs by anime season,
    # ranks users by SUM(points) per (year, season), and counts how many
    # *completed* seasons (last month strictly before current month) the
    # user landed in the top 10 / top 3 / top 1. Window functions need
    # SQLite ≥ 3.25; that's the project's minimum.
    #
    # UTC: ``reward_month`` is UTC-derived elsewhere; use the same anchor
    # here so a user near a season boundary on a non-UTC server doesn't
    # see a season suddenly count as "completed" or "pending" depending
    # on which side of midnight local time falls.
    from datetime import datetime, timezone
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    placement_row = await bot.GET_ONE(
        f"""
        WITH season_buckets AS (
            SELECT
                user_id,
                points,
                CAST(substr(reward_month, 1, 4) AS INTEGER) AS yr,
                CASE
                    WHEN substr(reward_month, 6, 2) IN ('01','02','03') THEN 1
                    WHEN substr(reward_month, 6, 2) IN ('04','05','06') THEN 2
                    WHEN substr(reward_month, 6, 2) IN ('07','08','09') THEN 3
                    ELSE 4
                END AS season_idx
            FROM reading_logs
            WHERE 1=1
              {"AND logged_in_guild = ?" if g is not None else ""}
        ),
        season_totals AS (
            SELECT yr, season_idx, user_id, SUM(points) AS pts
            FROM season_buckets
            GROUP BY yr, season_idx, user_id
        ),
        season_ranks AS (
            SELECT yr, season_idx, user_id,
                   RANK() OVER (PARTITION BY yr, season_idx ORDER BY pts DESC) AS rnk
            FROM season_totals
        )
        SELECT
            COALESCE(SUM(CASE WHEN rnk = 1  THEN 1 ELSE 0 END), 0) AS top1,
            COALESCE(SUM(CASE WHEN rnk <= 3 THEN 1 ELSE 0 END), 0) AS top3,
            COALESCE(SUM(CASE WHEN rnk <= 10 THEN 1 ELSE 0 END), 0) AS top10
        FROM season_ranks
        WHERE user_id = ?
          AND (CAST(yr AS TEXT) || '-' ||
               CASE season_idx
                   WHEN 1 THEN '03' WHEN 2 THEN '06'
                   WHEN 3 THEN '09' ELSE '12'
               END) < ?
        """,
        tuple(
            ([g] if g is not None else [])
            + [user_id, current_month]
        ),
    )
    season_top1_count  = placement_row[0] if placement_row else 0
    season_top3_count  = placement_row[1] if placement_row else 0
    season_top10_count = placement_row[2] if placement_row else 0

    return {
        "total_completions":   int(total_completions or 0),
        "monthly_pool_count":  int(monthly_pool_count or 0),
        "seasonal_pool_count": int(seasonal_pool_count or 0),
        "distinct_months":     int(distinct_months or 0),
        "votes_cast":          int(votes_cast or 0),
        "nominations_made":    int(nominations_made or 0),
        "tastemaker_wins":     int(tastemaker_wins or 0),
        "season_top1_count":   int(season_top1_count or 0),
        "season_top3_count":   int(season_top3_count or 0),
        "season_top10_count":  int(season_top10_count or 0),
    }


async def compute_user_badges(
    bot,
    user_id: int,
    scope_guild_id: Optional[int] = None,
) -> set[str]:
    """Return the set of badge IDs this user currently has unlocked.

    Live derivation from `aggregate_user_stats` — calling twice in a row
    (e.g., before/after a /finish insert) is the supported way to detect
    new unlocks.
    """
    aggs = await aggregate_user_stats(bot, user_id, scope_guild_id=scope_guild_id)
    return {b.id for b in BADGE_DEFS if aggs.get(b.key, 0) >= b.threshold}
