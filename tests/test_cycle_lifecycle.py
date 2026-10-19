"""End-to-end SQL-level walkthrough of the cycle lifecycle.

This file plays out the production queries in the same order
``cogs/vn_cycle.py`` does, against a real SQLite DB:

    open voting → sweep existing noms → cast votes → tally → close →
    promote winner → re-open → second cycle for the SAME period.

Each test isolates one phase or one cross-guild scoping invariant. They
share the ``schema_bot`` fixture (migrations + cog CREATE TABLEs) to mirror
the production schema exactly.
"""
from __future__ import annotations

import pytest

from cogs.vn_cycle import _winners_after_tiebreak
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from lib.vndb_api import CREATE_VNDB_CACHE_TABLE
from tests.conftest import FakeBot


PROD_VN_TITLES = DatabaseQueries.CREATE_VN_TITLES_TABLE
PROD_READING_LOGS = DatabaseQueries.CREATE_READING_LOGS_TABLE


@pytest.fixture
async def schema_bot(fake_bot: FakeBot) -> FakeBot:
    """Migrations + the cog-owned CREATE TABLEs the bot expects to have
    run by the time any cycle query fires. vndb_cache is included so
    GET_CYCLE_NOMINEES' LEFT JOIN to it resolves.
    """
    await run_migrations(fake_bot)
    await fake_bot.RUN(PROD_VN_TITLES)
    await fake_bot.RUN(PROD_READING_LOGS)
    await fake_bot.RUN(CREATE_VNDB_CACHE_TABLE)
    return fake_bot


# ---------------------------------------------------------------------------
# Helpers: keep test bodies focused on the scenario, not row plumbing.
# ---------------------------------------------------------------------------


async def _open_voting(
    bot: FakeBot,
    *,
    guild_id: int,
    target_month: str = "2026-06",
    target_end_month: str = "2026-06",
    kind: str = "monthly",
    channel_id: int | None = 9_999,
) -> int:
    """Mimic _open_voting: INSERT_CYCLE → SET_VOTING_SETTINGS → SWEEP.
    Returns the new cycle id.
    """
    cycle_id = await bot.RUN_RETURNING_ID(
        DatabaseQueries.INSERT_CYCLE,
        (guild_id, target_month, channel_id, kind, target_end_month),
    )
    await bot.RUN(
        DatabaseQueries.SET_VOTING_SETTINGS,
        ("single", 1, channel_id, None, None, "buttons", None, cycle_id),
    )
    await bot.RUN(
        DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
        (cycle_id, target_month, target_end_month, guild_id),
    )
    return cycle_id


async def _seed_nomination(
    bot: FakeBot,
    *,
    vndb_id: str,
    guild_id: int,
    nominator_user_id: int,
    title: str,
    start_month: str = "2026-06",
    end_month: str = "2026-06",
    cycle_id: int | None = None,
    status: str = "nominated",
) -> int:
    """Seed a pending nomination (cycle_id NULL by default, so SWEEP
    picks it up on the next /open_voting). Pass ``cycle_id`` to attach
    directly without going through SWEEP.
    """
    return await bot.RUN_RETURNING_ID(
        "INSERT INTO vn_titles "
        "(vndb_id, guild_id, start_month, end_month, is_monthly_points, "
        " status, cycle_id, nominator_user_id, title_cache) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vndb_id, guild_id, start_month, end_month, 10,
         status, cycle_id, nominator_user_id, title),
    )


async def _vote(
    bot: FakeBot, *, cycle_id: int, user_id: int, guild_id: int, nomination_id: int,
) -> None:
    await bot.RUN(DatabaseQueries.INSERT_VOTE,
                  (cycle_id, user_id, guild_id, nomination_id))


async def _tally(bot: FakeBot, cycle_id: int) -> list:
    return await bot.GET(DatabaseQueries.TALLY_VOTES, (cycle_id,))


# ---------------------------------------------------------------------------
# Phase 1: Open voting + sweep
# ---------------------------------------------------------------------------


class TestOpenVoting:
    @pytest.mark.asyncio
    async def test_sweep_attaches_existing_pending_noms_to_new_cycle(
        self, schema_bot: FakeBot
    ) -> None:
        # Two pending noms (cycle_id NULL) exist for June 2026 in guild 100.
        n1 = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                    nominator_user_id=1, title="A")
        n2 = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=100,
                                    nominator_user_id=2, title="B")
        cycle_id = await _open_voting(schema_bot, guild_id=100)

        # Both got swept onto the new cycle.
        rows = await schema_bot.GET(DatabaseQueries.GET_CYCLE_NOMINEES, (cycle_id,))
        ids = {row[0] for row in rows}
        assert ids == {n1, n2}

    @pytest.mark.asyncio
    async def test_sweep_does_not_touch_other_guilds(
        self, schema_bot: FakeBot
    ) -> None:
        # Pending nom in guild A, plus a pending nom in guild B for the
        # same period. Guild A opens voting; guild B's nom stays free.
        await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                               nominator_user_id=1, title="A")
        b_nom = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=200,
                                       nominator_user_id=2, title="B")
        await _open_voting(schema_bot, guild_id=100)

        # B's row still has cycle_id NULL: multi-server isolation holds.
        (b_cycle,) = await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE id = ?", (b_nom,)
        )
        assert b_cycle is None

    @pytest.mark.asyncio
    async def test_sweep_period_isolation_monthly_vs_seasonal(
        self, schema_bot: FakeBot
    ) -> None:
        # SWEEP requires exact period match: a seasonal nom (Apr-Jun)
        # must NOT be pulled into a monthly Jun cycle, and vice versa.
        seasonal_nom = await _seed_nomination(
            schema_bot, vndb_id="v_season", guild_id=100,
            nominator_user_id=1, title="Seasonal",
            start_month="2026-04", end_month="2026-06",
        )
        monthly_nom = await _seed_nomination(
            schema_bot, vndb_id="v_month", guild_id=100,
            nominator_user_id=2, title="Monthly",
            start_month="2026-06", end_month="2026-06",
        )
        # Open monthly cycle for June only.
        await _open_voting(schema_bot, guild_id=100,
                           target_month="2026-06", target_end_month="2026-06")

        (s_cycle,) = await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE id = ?", (seasonal_nom,)
        )
        (m_cycle,) = await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE id = ?", (monthly_nom,)
        )
        assert s_cycle is None, "seasonal nom must NOT be swept into monthly cycle"
        assert m_cycle is not None, "monthly nom must be swept into monthly cycle"

    @pytest.mark.asyncio
    async def test_sweep_does_not_pull_promoted_winners(
        self, schema_bot: FakeBot
    ) -> None:
        # A previously-won pick (status='monthly') in the same period
        # must stay out of the new cycle; only status='nominated' is
        # swept. Otherwise reopening a cycle would re-vote on last
        # month's winner.
        winner = await _seed_nomination(
            schema_bot, vndb_id="v_win", guild_id=100,
            nominator_user_id=1, title="Winner",
            status="monthly",
        )
        pending = await _seed_nomination(
            schema_bot, vndb_id="v_pending", guild_id=100,
            nominator_user_id=2, title="Pending",
        )
        cycle_id = await _open_voting(schema_bot, guild_id=100)

        rows = await schema_bot.GET(DatabaseQueries.GET_CYCLE_NOMINEES, (cycle_id,))
        ids = {row[0] for row in rows}
        assert ids == {pending}, "promoted winner should not be re-voted"
        # And the winner's status is unchanged.
        (status,) = await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE id = ?", (winner,)
        )
        assert status == "monthly"


# ---------------------------------------------------------------------------
# Phase 2: Voting (single-choice + multi-choice + replace)
# ---------------------------------------------------------------------------


class TestVotingSingleChoice:
    @pytest.mark.asyncio
    async def test_replace_path_is_atomic(self, schema_bot: FakeBot) -> None:
        """The DELETE+INSERT for single-choice replace is wrapped in
        RUN_TRANSACTION. Simulate the production call shape to
        prove that a user re-voting ends up with exactly one vote, and
        for the right nominee.
        """
        n1 = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                    nominator_user_id=1, title="A")
        n2 = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=100,
                                    nominator_user_id=2, title="B")
        cycle_id = await _open_voting(schema_bot, guild_id=100)

        # First vote: user 10 → A
        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n1)

        # Replace via RUN_TRANSACTION (exactly what vn_cycle does).
        await schema_bot.RUN_TRANSACTION([
            (DatabaseQueries.DELETE_USER_VOTES_IN_CYCLE, (cycle_id, 10)),
            (DatabaseQueries.INSERT_VOTE, (cycle_id, 10, 100, n2)),
        ])

        rows = await schema_bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, 10)
        )
        assert len(rows) == 1, "exactly one vote after replace"
        assert rows[0][1] == n2, "vote should be for n2 (the new pick)"

    @pytest.mark.asyncio
    async def test_replace_transaction_rolls_back_on_failure(
        self, schema_bot: FakeBot
    ) -> None:
        """The converse: if the INSERT inside the transaction fails for
        any reason, the DELETE must roll back too, so the user's prior
        vote survives.
        """
        n1 = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                    nominator_user_id=1, title="A")
        cycle_id = await _open_voting(schema_bot, guild_id=100)
        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n1)

        # Replace with a transaction whose second statement is invalid
        # SQL (table doesn't exist). The first statement is the
        # production DELETE; the second forces RUN_TRANSACTION's
        # except branch, which must roll back the DELETE. The actual
        # INSERT_VOTE uses INSERT OR IGNORE so NOT NULL violations
        # would be silently dropped; a syntax error is the only
        # reliable way to exercise the rollback path.
        with pytest.raises(Exception):
            await schema_bot.RUN_TRANSACTION([
                (DatabaseQueries.DELETE_USER_VOTES_IN_CYCLE, (cycle_id, 10)),
                ("INSERT INTO _nonexistent_table_for_test VALUES (1)", ()),
            ])

        # Prior vote survives the failed replace.
        rows = await schema_bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, 10)
        )
        assert len(rows) == 1
        assert rows[0][1] == n1


class TestVotingMultiChoice:
    @pytest.mark.asyncio
    async def test_multi_pick_records_each(self, schema_bot: FakeBot) -> None:
        n1 = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                    nominator_user_id=1, title="A")
        n2 = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=100,
                                    nominator_user_id=2, title="B")
        cycle_id = await _open_voting(schema_bot, guild_id=100)

        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n1)
        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n2)

        (count,) = await schema_bot.GET_ONE(
            DatabaseQueries.COUNT_USER_VOTES_IN_CYCLE, (cycle_id, 10)
        )
        assert count == 2

    @pytest.mark.asyncio
    async def test_toggle_off_specific_pick(self, schema_bot: FakeBot) -> None:
        # The multi-choice toggle uses DELETE_VOTE_BY_ID (single row),
        # not DELETE_USER_VOTES_IN_CYCLE; toggling off one pick must
        # not nuke the user's other picks.
        n1 = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                    nominator_user_id=1, title="A")
        n2 = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=100,
                                    nominator_user_id=2, title="B")
        cycle_id = await _open_voting(schema_bot, guild_id=100)
        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n1)
        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n2)

        # Find the vote row for n2 and delete it (production path
        # via DELETE_VOTE_BY_ID).
        rows = await schema_bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, 10)
        )
        v_id_for_n2 = next(row[0] for row in rows if row[1] == n2)
        await schema_bot.RUN(DatabaseQueries.DELETE_VOTE_BY_ID, (v_id_for_n2,))

        # User now has only n1.
        rows = await schema_bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (cycle_id, 10)
        )
        assert len(rows) == 1
        assert rows[0][1] == n1


# ---------------------------------------------------------------------------
# Phase 3: Tally + close + promote
# ---------------------------------------------------------------------------


class TestCloseAndPromote:
    @pytest.mark.asyncio
    async def test_full_close_promotes_clean_winner(
        self, schema_bot: FakeBot
    ) -> None:
        """End-to-end: open → noms → votes → tally → tiebreak → promote.
        The promoted row's status flips from 'nominated' to the cycle kind.
        """
        n_a = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                     nominator_user_id=1, title="A")
        n_b = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=100,
                                     nominator_user_id=2, title="B")
        cycle_id = await _open_voting(schema_bot, guild_id=100, kind="monthly")
        # 3-1 in favour of A.
        for uid in (10, 11, 12):
            await _vote(schema_bot, cycle_id=cycle_id, user_id=uid,
                        guild_id=100, nomination_id=n_a)
        await _vote(schema_bot, cycle_id=cycle_id, user_id=20,
                    guild_id=100, nomination_id=n_b)

        tally = await _tally(schema_bot, cycle_id)
        winners = _winners_after_tiebreak(tally, winner_count=1)
        assert len(winners) == 1
        winner_row = winners[0]
        # nomination_id is column 0 of the TALLY row.
        winner_nom_id = winner_row[0]
        assert winner_nom_id == n_a

        # Promote + close the cycle (mirrors _close_voting).
        await schema_bot.RUN(
            DatabaseQueries.PROMOTE_NOMINATION_TO_PICK,
            ("monthly", winner_nom_id),
        )
        await schema_bot.RUN(DatabaseQueries.CLOSE_CYCLE, (cycle_id,))

        # Status flipped on A; B stayed 'nominated'.
        (a_status,) = await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE id = ?", (n_a,)
        )
        (b_status,) = await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE id = ?", (n_b,)
        )
        assert a_status == "monthly"
        assert b_status == "nominated"

        # Cycle moved to closed.
        cycle_row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_CYCLE_BY_ID, (cycle_id,)
        )
        assert cycle_row[2] == "closed"  # phase
        assert cycle_row[9] is not None  # closed_at set

    @pytest.mark.asyncio
    async def test_close_with_tie_promotes_lower_id(
        self, schema_bot: FakeBot
    ) -> None:
        n_a = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                     nominator_user_id=1, title="A")
        n_b = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=100,
                                     nominator_user_id=2, title="B")
        cycle_id = await _open_voting(schema_bot, guild_id=100)
        # 1-1 tie. The earlier-nominated row (n_a, lower vn_titles.id)
        # wins by the SQL ORDER BY vt.id ASC tiebreak.
        await _vote(schema_bot, cycle_id=cycle_id, user_id=10,
                    guild_id=100, nomination_id=n_a)
        await _vote(schema_bot, cycle_id=cycle_id, user_id=11,
                    guild_id=100, nomination_id=n_b)

        tally = await _tally(schema_bot, cycle_id)
        winners = _winners_after_tiebreak(tally, winner_count=1)
        assert [w[1] for w in winners] == ["v1"]

        # Cycle still closes cleanly. (This test doesn't invoke
        # PROMOTE_NOMINATION_TO_PICK so rows stay 'nominated' here; the
        # promotion side is covered by test_full_close_promotes_clean_winner
        # above.)
        await schema_bot.RUN(DatabaseQueries.CLOSE_CYCLE, (cycle_id,))
        rows = await schema_bot.GET(
            "SELECT status FROM vn_titles WHERE cycle_id = ?", (cycle_id,)
        )
        assert all(r[0] == "nominated" for r in rows)


# ---------------------------------------------------------------------------
# Phase 4: Reopen
# ---------------------------------------------------------------------------


class TestReopenCycle:
    @pytest.mark.asyncio
    async def test_reopen_clears_close_metadata(
        self, schema_bot: FakeBot
    ) -> None:
        n = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                   nominator_user_id=1, title="A")
        cycle_id = await _open_voting(schema_bot, guild_id=100,
                                      channel_id=12345)
        await schema_bot.RUN(DatabaseQueries.CLOSE_CYCLE, (cycle_id,))
        # The close path also sets announcement_message_id.
        await schema_bot.RUN(
            "UPDATE vn_cycles SET announcement_message_id = ? WHERE id = ?",
            (98765, cycle_id),
        )

        await schema_bot.RUN(DatabaseQueries.REOPEN_CYCLE, (cycle_id,))

        cycle_row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_CYCLE_BY_ID, (cycle_id,)
        )
        # phase, announcement_channel_id, announcement_message_id,
        # closed_at, closes_at all reset.
        assert cycle_row[2] == "voting"
        assert cycle_row[6] is None   # announcement_channel_id
        assert cycle_row[7] is None   # announcement_message_id
        assert cycle_row[9] is None   # closed_at
        assert cycle_row[12] is None  # closes_at

    @pytest.mark.asyncio
    async def test_second_cycle_for_same_period_is_allowed(
        self, schema_bot: FakeBot
    ) -> None:
        """The migrations drop UNIQUE(guild_id, target_month, kind) on
        vn_cycles so a period can be voted on more than once. Verify two
        cycles for the same month coexist without an IntegrityError.
        """
        first = await _open_voting(schema_bot, guild_id=100,
                                   target_month="2026-06",
                                   target_end_month="2026-06")
        await schema_bot.RUN(DatabaseQueries.CLOSE_CYCLE, (first,))

        # Second cycle, same guild, same period: must succeed.
        second = await _open_voting(schema_bot, guild_id=100,
                                    target_month="2026-06",
                                    target_end_month="2026-06")
        assert second != first

        # Both rows exist.
        rows = await schema_bot.GET(
            "SELECT id, phase FROM vn_cycles "
            "WHERE guild_id = ? AND target_month = ? AND kind = ? "
            "ORDER BY id",
            (100, "2026-06", "monthly"),
        )
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Phase 5: Multi-guild cycle isolation
# ---------------------------------------------------------------------------


class TestMultiGuildCycleIsolation:
    @pytest.mark.asyncio
    async def test_two_guilds_run_concurrent_cycles(
        self, schema_bot: FakeBot
    ) -> None:
        # Hikaru is explicitly multi-server. Two guilds
        # running monthly cycles for the same period must not see each
        # other's nominees, votes, or winners.
        a_nom = await _seed_nomination(schema_bot, vndb_id="v1", guild_id=100,
                                       nominator_user_id=1, title="A")
        b_nom = await _seed_nomination(schema_bot, vndb_id="v2", guild_id=200,
                                       nominator_user_id=2, title="B")
        a_cycle = await _open_voting(schema_bot, guild_id=100)
        b_cycle = await _open_voting(schema_bot, guild_id=200)

        # Each guild's cycle sees only its own nominee.
        a_nominees = await schema_bot.GET(
            DatabaseQueries.GET_CYCLE_NOMINEES, (a_cycle,)
        )
        b_nominees = await schema_bot.GET(
            DatabaseQueries.GET_CYCLE_NOMINEES, (b_cycle,)
        )
        assert [r[0] for r in a_nominees] == [a_nom]
        assert [r[0] for r in b_nominees] == [b_nom]

        # Vote in each.
        await _vote(schema_bot, cycle_id=a_cycle, user_id=10,
                    guild_id=100, nomination_id=a_nom)
        await _vote(schema_bot, cycle_id=b_cycle, user_id=20,
                    guild_id=200, nomination_id=b_nom)

        # Tallies stay isolated.
        a_tally = await _tally(schema_bot, a_cycle)
        b_tally = await _tally(schema_bot, b_cycle)
        assert len(a_tally) == 1 and a_tally[0][6] == 1
        assert len(b_tally) == 1 and b_tally[0][6] == 1

    @pytest.mark.asyncio
    async def test_active_cycle_lookup_is_per_guild(
        self, schema_bot: FakeBot
    ) -> None:
        # GET_ACTIVE_CYCLE filters by guild_id + kind. Opening a cycle
        # in guild 100 must not surface as the active cycle in guild 200.
        await _open_voting(schema_bot, guild_id=100)
        b_active = await schema_bot.GET_ONE(
            DatabaseQueries.GET_ACTIVE_CYCLE, (200, "monthly")
        )
        assert b_active is None


# ---------------------------------------------------------------------------
# Auto-close polling
# ---------------------------------------------------------------------------


class TestAutoCloseListing:
    @pytest.mark.asyncio
    async def test_list_expired_picks_up_past_closes_at(
        self, schema_bot: FakeBot
    ) -> None:
        # The auto-close polling loop runs LIST_EXPIRED_VOTING_CYCLES
        # periodically. Set closes_at to a past timestamp and verify the
        # cycle shows up; set it to a future timestamp and verify it
        # doesn't.
        past_cycle = await _open_voting(schema_bot, guild_id=100,
                                        target_month="2026-06")
        future_cycle = await _open_voting(schema_bot, guild_id=200,
                                          target_month="2026-06")
        await schema_bot.RUN(
            "UPDATE vn_cycles SET closes_at = '2000-01-01 00:00:00' "
            "WHERE id = ?", (past_cycle,),
        )
        await schema_bot.RUN(
            "UPDATE vn_cycles SET closes_at = '2999-01-01 00:00:00' "
            "WHERE id = ?", (future_cycle,),
        )

        rows = await schema_bot.GET(
            DatabaseQueries.LIST_EXPIRED_VOTING_CYCLES,
        )
        ids = [row[0] for row in rows]
        assert past_cycle in ids
        assert future_cycle not in ids

    @pytest.mark.asyncio
    async def test_closed_cycle_not_picked_up_even_if_overdue(
        self, schema_bot: FakeBot
    ) -> None:
        # Defensive: a cycle already in phase='closed' must never appear,
        # even with a stale closes_at. Otherwise close logic would fire
        # twice on a single shutdown/restart cycle.
        cid = await _open_voting(schema_bot, guild_id=100)
        await schema_bot.RUN(
            "UPDATE vn_cycles SET closes_at = '2000-01-01 00:00:00' "
            "WHERE id = ?", (cid,),
        )
        await schema_bot.RUN(DatabaseQueries.CLOSE_CYCLE, (cid,))

        rows = await schema_bot.GET(
            DatabaseQueries.LIST_EXPIRED_VOTING_CYCLES,
        )
        assert all(row[0] != cid for row in rows)
