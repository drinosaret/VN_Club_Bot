"""Badge aggregation + unlock logic.

``lib/badges.py:aggregate_user_stats`` runs the SQL queries behind the
unlock predicates; ``compute_user_badges`` is the thin wrapper that
applies the thresholds. These queries determine what every user sees in
``/badges``: wrong joins or NULL-handling silently mis-award badges.

These tests seed reading_logs / vn_titles / vn_votes / vn_cycles
directly, then assert on the aggregates each user should see.
"""
from __future__ import annotations

import pytest

from lib.badges import (
    BADGE_BY_ID,
    BADGE_DEFS,
    aggregate_user_stats,
    compute_user_badges,
)
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from tests.conftest import FakeBot


@pytest.fixture
async def schema_bot(fake_bot: FakeBot) -> FakeBot:
    await run_migrations(fake_bot)
    await fake_bot.RUN(DatabaseQueries.CREATE_VN_TITLES_TABLE)
    await fake_bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    return fake_bot


async def _log(
    bot: FakeBot, *,
    user_id: int, vndb_id: str | None, reward_reason: str = "Casual VN",
    reward_month: str = "2026-06", points: int = 1,
    guild: int | None = 100,
) -> int:
    return await bot.RUN_RETURNING_ID(
        "INSERT INTO reading_logs "
        "(user_id, vndb_id, user_rating, reward_reason, reward_month, "
        " points, comment, logged_in_guild) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, vndb_id, 5, reward_reason, reward_month,
         points, "", guild),
    )


async def _seed_cycle(
    bot: FakeBot, *,
    cycle_id: int, guild_id: int, phase: str = "closed",
    target_month: str = "2026-06", target_end_month: str = "2026-06",
) -> None:
    await bot.RUN(
        "INSERT INTO vn_cycles "
        "(id, guild_id, phase, kind, target_month, target_end_month) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cycle_id, guild_id, phase, "monthly", target_month, target_end_month),
    )


async def _seed_nom(
    bot: FakeBot, *,
    vndb_id: str, guild_id: int, cycle_id: int,
    nominator_user_id: int, title: str = "Title",
    status: str = "nominated",
) -> int:
    return await bot.RUN_RETURNING_ID(
        "INSERT INTO vn_titles "
        "(vndb_id, guild_id, start_month, end_month, is_monthly_points, "
        " status, cycle_id, nominator_user_id, title_cache) "
        "VALUES (?, ?, '2026-06', '2026-06', 10, ?, ?, ?, ?)",
        (vndb_id, guild_id, status, cycle_id, nominator_user_id, title),
    )


async def _cast_vote(
    bot: FakeBot, *, cycle_id: int, user_id: int, guild_id: int, nomination_id: int,
) -> None:
    await bot.RUN(
        DatabaseQueries.INSERT_VOTE,
        (cycle_id, user_id, guild_id, nomination_id),
    )


# ============================================================================
# aggregate_user_stats: volume + pool counts
# ============================================================================


class TestVolumeAggregates:
    @pytest.mark.asyncio
    async def test_distinct_vndb_counts_so_rereads_dont_inflate(
        self, schema_bot: FakeBot
    ) -> None:
        # Re-reading the same VN in a different month shouldn't push
        # total_completions to 2. Distinct-by-vndb_id keeps the count honest.
        await _log(schema_bot, user_id=1, vndb_id="v1", reward_month="2026-06")
        await _log(schema_bot, user_id=1, vndb_id="v1", reward_month="2026-07")
        await _log(schema_bot, user_id=1, vndb_id="v2", reward_month="2026-06")

        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["total_completions"] == 2

    @pytest.mark.asyncio
    async def test_distinct_months_does_count_rereads(
        self, schema_bot: FakeBot
    ) -> None:
        # `distinct_months` is the only metric that genuinely WANTS to
        # count cross-month re-reads: logging in 3 different months IS
        # the consistency badge signal.
        await _log(schema_bot, user_id=1, vndb_id="v1", reward_month="2026-06")
        await _log(schema_bot, user_id=1, vndb_id="v1", reward_month="2026-07")
        await _log(schema_bot, user_id=1, vndb_id="v1", reward_month="2026-08")
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["distinct_months"] == 3

    @pytest.mark.asyncio
    async def test_null_vndb_rows_excluded_from_vn_counts(
        self, schema_bot: FakeBot
    ) -> None:
        # /manage_reward_points writes rows with vndb_id NULL. Those must
        # not bump total_completions or pool counts. NULL also shouldn't
        # contribute a "distinct VN" via DISTINCT (which already ignores NULL).
        await _log(schema_bot, user_id=1, vndb_id=None,
                   reward_reason="As Monthly VN", reward_month="2026-06")
        await _log(schema_bot, user_id=1, vndb_id="v1",
                   reward_reason="Casual VN", reward_month="2026-06")
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["total_completions"] == 1
        assert aggs["monthly_pool_count"] == 0

    @pytest.mark.asyncio
    async def test_pool_count_filters_by_reward_reason(
        self, schema_bot: FakeBot
    ) -> None:
        await _log(schema_bot, user_id=1, vndb_id="v1",
                   reward_reason="As Monthly VN")
        await _log(schema_bot, user_id=1, vndb_id="v2",
                   reward_reason="As Seasonal VN")
        await _log(schema_bot, user_id=1, vndb_id="v3",
                   reward_reason="Casual VN")
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["monthly_pool_count"] == 1
        assert aggs["seasonal_pool_count"] == 1
        # v3 is neither, which proves the WHEN clause is selective.

    @pytest.mark.asyncio
    async def test_pool_count_distinct_by_vndb(
        self, schema_bot: FakeBot
    ) -> None:
        # If the same monthly pick gets logged twice (legitimate per
        # docstring), monthly_pool_count stays 1: distinct VNs only.
        await _log(schema_bot, user_id=1, vndb_id="v_pick",
                   reward_reason="As Monthly VN", reward_month="2026-06")
        await _log(schema_bot, user_id=1, vndb_id="v_pick",
                   reward_reason="As Monthly VN", reward_month="2026-07")
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["monthly_pool_count"] == 1


# ============================================================================
# Scope filtering: global vs per-guild
# ============================================================================


class TestScopeFiltering:
    @pytest.mark.asyncio
    async def test_global_scope_aggregates_across_guilds(
        self, schema_bot: FakeBot
    ) -> None:
        # Hikaru is multi-server. Without scope, a user's
        # badges span every server they've logged in.
        await _log(schema_bot, user_id=1, vndb_id="v1", guild=100)
        await _log(schema_bot, user_id=1, vndb_id="v2", guild=200)
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["total_completions"] == 2

    @pytest.mark.asyncio
    async def test_guild_scope_only_counts_target_guild(
        self, schema_bot: FakeBot
    ) -> None:
        await _log(schema_bot, user_id=1, vndb_id="v1", guild=100)
        await _log(schema_bot, user_id=1, vndb_id="v2", guild=200)
        aggs = await aggregate_user_stats(schema_bot, user_id=1, scope_guild_id=100)
        assert aggs["total_completions"] == 1


# ============================================================================
# Cycle engagement aggregates
# ============================================================================


class TestEngagementAggregates:
    @pytest.mark.asyncio
    async def test_nominations_made_counts_only_attached_to_cycle(
        self, schema_bot: FakeBot
    ) -> None:
        # Legacy nomination rows can have cycle_id IS NULL. Those should
        # NOT count as "made a nomination": the filter is cycle_id IS NOT NULL.
        await _seed_cycle(schema_bot, cycle_id=1, guild_id=100)
        await _seed_nom(schema_bot, vndb_id="v1", guild_id=100,
                        cycle_id=1, nominator_user_id=42)
        # Orphaned-pre-unify row.
        await schema_bot.RUN(
            "INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, "
            " is_monthly_points, status, cycle_id, nominator_user_id) "
            "VALUES ('v_orphan', 100, '2026-06', '2026-06', 10, 'nominated', NULL, 42)"
        )
        aggs = await aggregate_user_stats(schema_bot, user_id=42)
        assert aggs["nominations_made"] == 1

    @pytest.mark.asyncio
    async def test_votes_cast_counts_user_votes(
        self, schema_bot: FakeBot
    ) -> None:
        await _seed_cycle(schema_bot, cycle_id=1, guild_id=100)
        nom = await _seed_nom(
            schema_bot, vndb_id="v1", guild_id=100,
            cycle_id=1, nominator_user_id=1,
        )
        await _cast_vote(schema_bot, cycle_id=1, user_id=42,
                         guild_id=100, nomination_id=nom)
        aggs = await aggregate_user_stats(schema_bot, user_id=42)
        assert aggs["votes_cast"] == 1

    @pytest.mark.asyncio
    async def test_tastemaker_only_counts_closed_cycle_wins(
        self, schema_bot: FakeBot
    ) -> None:
        # Two cycles. Cycle 1 closed, user 42's nom won it. Cycle 2 is
        # still voting and user 42's nom is currently winning; that should
        # NOT count.
        await _seed_cycle(schema_bot, cycle_id=1, guild_id=100, phase="closed")
        await _seed_cycle(schema_bot, cycle_id=2, guild_id=100, phase="voting")
        n1 = await _seed_nom(schema_bot, vndb_id="v1", guild_id=100,
                             cycle_id=1, nominator_user_id=42)
        n2 = await _seed_nom(schema_bot, vndb_id="v2", guild_id=100,
                             cycle_id=2, nominator_user_id=42)
        # Vote for n1 in the closed cycle.
        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=n1)
        # Vote for n2 in the still-open cycle.
        await _cast_vote(schema_bot, cycle_id=2, user_id=10,
                         guild_id=100, nomination_id=n2)

        aggs = await aggregate_user_stats(schema_bot, user_id=42)
        assert aggs["tastemaker_wins"] == 1

    @pytest.mark.asyncio
    async def test_tastemaker_skips_loser_nominations(
        self, schema_bot: FakeBot
    ) -> None:
        # In a closed cycle where user 42's nom lost, tastemaker stays 0.
        await _seed_cycle(schema_bot, cycle_id=1, guild_id=100, phase="closed")
        nom_user = await _seed_nom(schema_bot, vndb_id="v_user", guild_id=100,
                                   cycle_id=1, nominator_user_id=42)
        nom_winner = await _seed_nom(schema_bot, vndb_id="v_winner", guild_id=100,
                                     cycle_id=1, nominator_user_id=99)
        # Winner gets 2 votes, user's nom gets 1.
        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom_winner)
        await _cast_vote(schema_bot, cycle_id=1, user_id=11,
                         guild_id=100, nomination_id=nom_winner)
        await _cast_vote(schema_bot, cycle_id=1, user_id=12,
                         guild_id=100, nomination_id=nom_user)
        aggs = await aggregate_user_stats(schema_bot, user_id=42)
        assert aggs["tastemaker_wins"] == 0


# ============================================================================
# Season-placement aggregates
# ============================================================================


class TestSeasonPlacement:
    @pytest.mark.asyncio
    async def test_top1_in_completed_past_season(
        self, schema_bot: FakeBot
    ) -> None:
        # Spring 2020 (Apr-Jun) is definitely completed. User 1 logs 10
        # points; nobody else logs. They're rank 1 → season_top1_count = 1.
        await _log(schema_bot, user_id=1, vndb_id="v1",
                   reward_month="2020-05", points=10)
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["season_top1_count"] == 1
        assert aggs["season_top3_count"] == 1
        assert aggs["season_top10_count"] == 1

    @pytest.mark.asyncio
    async def test_current_season_does_not_count(
        self, schema_bot: FakeBot
    ) -> None:
        # Use current UTC month: the WHERE clause requires the season's
        # end month to be strictly LESS than current month, so a still-in-
        # progress season must not contribute.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Log in the current month. Current season's end can't be < current_month.
        cur_month = f"{now.year:04d}-{now.month:02d}"
        await _log(schema_bot, user_id=1, vndb_id="v1",
                   reward_month=cur_month, points=10)
        aggs = await aggregate_user_stats(schema_bot, user_id=1)
        assert aggs["season_top1_count"] == 0
        assert aggs["season_top3_count"] == 0
        assert aggs["season_top10_count"] == 0


# ============================================================================
# compute_user_badges: threshold mapping
# ============================================================================


class TestComputeUserBadges:
    @pytest.mark.asyncio
    async def test_below_first_threshold_returns_empty(
        self, schema_bot: FakeBot
    ) -> None:
        # User has logged nothing. No badges.
        badges = await compute_user_badges(schema_bot, user_id=1)
        assert badges == set()

    @pytest.mark.asyncio
    async def test_first_steps_unlocks_at_one(
        self, schema_bot: FakeBot
    ) -> None:
        # Match the lowest-threshold badge in BADGE_DEFS: first_steps =
        # 1 completion.
        await _log(schema_bot, user_id=1, vndb_id="v1")
        badges = await compute_user_badges(schema_bot, user_id=1)
        assert "first_steps" in badges

    @pytest.mark.asyncio
    async def test_higher_threshold_skipped(
        self, schema_bot: FakeBot
    ) -> None:
        # 1 completion is well below the "Reader" threshold. The
        # Reader badge should NOT be in the returned set.
        await _log(schema_bot, user_id=1, vndb_id="v1")
        badges = await compute_user_badges(schema_bot, user_id=1)
        assert "reader" not in badges

    @pytest.mark.asyncio
    async def test_every_badge_id_in_index(self) -> None:
        # BADGE_BY_ID must map every BADGE_DEFS id. Tests catch a typo
        # where a Badge() was added but the index helper wasn't rerun.
        for badge in BADGE_DEFS:
            assert BADGE_BY_ID.get(badge.id) is badge

    @pytest.mark.asyncio
    async def test_every_badge_key_is_returned_by_aggregator(
        self, schema_bot: FakeBot
    ) -> None:
        # aggregate_user_stats must produce every key the badges look at;
        # otherwise a badge with a misnamed `key` silently never unlocks.
        aggs = await aggregate_user_stats(schema_bot, user_id=999)
        missing = {b.key for b in BADGE_DEFS} - set(aggs)
        assert not missing, f"aggregator missing keys: {missing}"
