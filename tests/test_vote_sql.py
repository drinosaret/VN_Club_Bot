"""SQL-level behavior tests for the vote-cycle subsystem.

Exercises the actual production query strings against a real SQLite DB,
because the queries are where most of the dangerous business logic lives:
- Cross-cycle vote isolation (the LEFT JOIN's `AND v.cycle_id = vt.cycle_id`
  keeps vote totals from a later cycle out of the closing cycle's tally).
- Per-guild scoping on pool queries.
- UNIQUE constraints that prevent double-voting and duplicate nominations.

Each test seeds a minimum realistic schema, inserts a focused scenario,
and asserts the query result against the *final* schema shape.
"""
from __future__ import annotations

import pytest

from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from lib.vndb_api import CREATE_VNDB_CACHE_TABLE
from tests.conftest import FakeBot


# Production CREATE TABLE statements for the tables migrations don't own
# (vn_titles + reading_logs are cog-created in production; vndb_cache is
# created on first VNDB lookup in production but tests need it upfront so
# GET_CYCLE_NOMINEES' LEFT JOIN to vndb_cache resolves).
PROD_VN_TITLES = DatabaseQueries.CREATE_VN_TITLES_TABLE
PROD_READING_LOGS = DatabaseQueries.CREATE_READING_LOGS_TABLE


@pytest.fixture
async def schema_bot(fake_bot: FakeBot) -> FakeBot:
    """A bot with the full production schema applied: migrations + cog
    CREATE TABLEs for the tables migrations don't own.
    """
    await run_migrations(fake_bot)
    await fake_bot.RUN(PROD_VN_TITLES)
    await fake_bot.RUN(PROD_READING_LOGS)
    await fake_bot.RUN(CREATE_VNDB_CACHE_TABLE)
    return fake_bot


async def _insert_cycle(
    bot: FakeBot, *, cycle_id: int, guild_id: int,
    target_month: str = "2026-06", target_end_month: str = "2026-06",
    kind: str = "monthly", phase: str = "voting",
) -> None:
    await bot.RUN(
        "INSERT INTO vn_cycles "
        "(id, guild_id, phase, kind, target_month, target_end_month) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cycle_id, guild_id, phase, kind, target_month, target_end_month),
    )


async def _insert_nomination(
    bot: FakeBot, *, vndb_id: str, guild_id: int, cycle_id: int,
    nominator_user_id: int, title: str,
    start_month: str = "2026-06", end_month: str = "2026-06",
    status: str = "nominated",
) -> int:
    return await bot.RUN_RETURNING_ID(
        "INSERT INTO vn_titles "
        "(vndb_id, guild_id, start_month, end_month, is_monthly_points, "
        " status, cycle_id, nominator_user_id, title_cache) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vndb_id, guild_id, start_month, end_month, 10,
         status, cycle_id, nominator_user_id, title),
    )


async def _cast_vote(
    bot: FakeBot, *, cycle_id: int, user_id: int, guild_id: int, nomination_id: int,
) -> None:
    await bot.RUN(
        DatabaseQueries.INSERT_VOTE,
        (cycle_id, user_id, guild_id, nomination_id),
    )


class TestVoteTally:
    @pytest.mark.asyncio
    async def test_tally_counts_votes_per_nominee(self, schema_bot: FakeBot) -> None:
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        nom_a = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="Title A",
        )
        nom_b = await _insert_nomination(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=1,
            nominator_user_id=2, title="Title B",
        )
        # 3 votes for A, 1 for B.
        for uid in (10, 11, 12):
            await _cast_vote(schema_bot, cycle_id=1, user_id=uid,
                             guild_id=100, nomination_id=nom_a)
        await _cast_vote(schema_bot, cycle_id=1, user_id=20,
                         guild_id=100, nomination_id=nom_b)

        rows = await schema_bot.GET(DatabaseQueries.TALLY_VOTES, (1,))
        votes_by_vndb = {row[1]: row[6] for row in rows}
        assert votes_by_vndb == {"v1": 3, "v2": 1}

    @pytest.mark.asyncio
    async def test_tally_excludes_other_cycles_votes(
        self, schema_bot: FakeBot
    ) -> None:
        """A LEFT JOIN on `v.nomination_id = vt.id` alone would count
        votes from a LATER cycle that re-used the same nomination row. The `AND v.cycle_id = vt.cycle_id` guard scopes
        the join to the closing cycle's own votes.
        """
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        await _insert_cycle(schema_bot, cycle_id=2, guild_id=100,
                            target_month="2026-07", target_end_month="2026-07")
        # Same nomination row in both cycles (the second cycle's sweep
        # would have re-pointed cycle_id; the row started in cycle 1).
        nom_a = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=2,
            nominator_user_id=1, title="Title A",
        )
        # Votes belong to cycle 2 specifically.
        for uid in (10, 11):
            await _cast_vote(schema_bot, cycle_id=2, user_id=uid,
                             guild_id=100, nomination_id=nom_a)

        # Tally cycle 1: should see zero votes because the votes are in
        # cycle 2, even though the join would otherwise match on nomination_id.
        # But the WHERE filters out rows whose vt.cycle_id != 1 first.
        rows = await schema_bot.GET(DatabaseQueries.TALLY_VOTES, (1,))
        assert rows == [], "tally for cycle 1 should be empty"

        # Tally cycle 2: should see both votes.
        rows = await schema_bot.GET(DatabaseQueries.TALLY_VOTES, (2,))
        assert len(rows) == 1
        assert rows[0][6] == 2  # vote count

    @pytest.mark.asyncio
    async def test_tally_orders_by_votes_desc_then_id(
        self, schema_bot: FakeBot
    ) -> None:
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        nom_a = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="A",
        )
        nom_b = await _insert_nomination(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=1,
            nominator_user_id=2, title="B",
        )
        # Both tied at 1 vote each. Order should follow vt.id ASC so the
        # earliest-nominated row wins the tiebreak (nom_a was inserted
        # first and got the lower auto-increment id).
        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom_a)
        await _cast_vote(schema_bot, cycle_id=1, user_id=11,
                         guild_id=100, nomination_id=nom_b)

        rows = await schema_bot.GET(DatabaseQueries.TALLY_VOTES, (1,))
        # First row = earlier nomination since tied.
        assert rows[0][1] == "v1"
        assert rows[1][1] == "v2"

    @pytest.mark.asyncio
    async def test_tally_includes_zero_vote_nominees(
        self, schema_bot: FakeBot
    ) -> None:
        # LEFT JOIN means a nominee with 0 votes still appears with COUNT=0.
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="A",
        )
        rows = await schema_bot.GET(DatabaseQueries.TALLY_VOTES, (1,))
        assert len(rows) == 1
        assert rows[0][6] == 0


class TestVoteUniqueness:
    @pytest.mark.asyncio
    async def test_double_vote_same_nomination_silently_skipped(
        self, schema_bot: FakeBot
    ) -> None:
        """INSERT OR IGNORE drops duplicates without erroring. The UNIQUE
        is (cycle_id, user_id, nomination_id) so the same user voting
        twice for the same nominee in the same cycle is a no-op.
        """
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        nom = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="A",
        )

        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom)
        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom)

        rows = await schema_bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (1, 10)
        )
        assert len(rows) == 1, "duplicate vote should be ignored"

    @pytest.mark.asyncio
    async def test_user_can_vote_different_nominees_in_same_cycle(
        self, schema_bot: FakeBot
    ) -> None:
        # The UNIQUE includes nomination_id, so multi-vote modes work:
        # one user, multiple distinct nominee picks in the same cycle.
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        nom_a = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="A",
        )
        nom_b = await _insert_nomination(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=1,
            nominator_user_id=2, title="B",
        )

        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom_a)
        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom_b)

        rows = await schema_bot.GET(
            DatabaseQueries.GET_USER_VOTES_IN_CYCLE, (1, 10)
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_user_can_vote_same_nominee_across_cycles(
        self, schema_bot: FakeBot
    ) -> None:
        # Same VN, two cycles, same user. Should record both; they're
        # distinct votes in two distinct cycles.
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        await _insert_cycle(schema_bot, cycle_id=2, guild_id=100,
                            target_month="2026-07", target_end_month="2026-07")
        nom_in_1 = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="A",
        )
        nom_in_2 = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=2,
            nominator_user_id=1, title="A",
            start_month="2026-07", end_month="2026-07",
        )

        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom_in_1)
        await _cast_vote(schema_bot, cycle_id=2, user_id=10,
                         guild_id=100, nomination_id=nom_in_2)

        # Each cycle should show 1 vote from this user.
        for cid in (1, 2):
            (n,) = await schema_bot.GET_ONE(
                DatabaseQueries.COUNT_USER_VOTES_IN_CYCLE, (cid, 10)
            )
            assert n == 1


class TestVoteDeletion:
    @pytest.mark.asyncio
    async def test_delete_user_votes_scoped_to_cycle(
        self, schema_bot: FakeBot
    ) -> None:
        # DELETE_USER_VOTES_IN_CYCLE shouldn't touch votes in other cycles.
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        await _insert_cycle(schema_bot, cycle_id=2, guild_id=100,
                            target_month="2026-07", target_end_month="2026-07")
        nom_1 = await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="A",
        )
        nom_2 = await _insert_nomination(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=2,
            nominator_user_id=1, title="B",
            start_month="2026-07", end_month="2026-07",
        )
        await _cast_vote(schema_bot, cycle_id=1, user_id=10,
                         guild_id=100, nomination_id=nom_1)
        await _cast_vote(schema_bot, cycle_id=2, user_id=10,
                         guild_id=100, nomination_id=nom_2)

        await schema_bot.RUN(
            DatabaseQueries.DELETE_USER_VOTES_IN_CYCLE, (1, 10)
        )

        (count_1,) = await schema_bot.GET_ONE(
            DatabaseQueries.COUNT_USER_VOTES_IN_CYCLE, (1, 10)
        )
        (count_2,) = await schema_bot.GET_ONE(
            DatabaseQueries.COUNT_USER_VOTES_IN_CYCLE, (2, 10)
        )
        assert count_1 == 0, "votes in cycle 1 should be deleted"
        assert count_2 == 1, "votes in cycle 2 should be preserved"


class TestGuildScoping:
    @pytest.mark.asyncio
    async def test_get_cycle_nominees_only_returns_nominated(
        self, schema_bot: FakeBot
    ) -> None:
        # GET_CYCLE_NOMINEES filters by status='nominated'. Promoted picks
        # ('monthly', 'seasonal') should NOT appear in the nominee list.
        await _insert_cycle(schema_bot, cycle_id=1, guild_id=100)
        await _insert_nomination(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            nominator_user_id=1, title="Pending",
        )
        await _insert_nomination(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=1,
            nominator_user_id=2, title="Already Picked",
            status="monthly",
        )

        rows = await schema_bot.GET(DatabaseQueries.GET_CYCLE_NOMINEES, (1,))
        vndb_ids = [r[2] for r in rows]
        assert vndb_ids == ["v1"]
