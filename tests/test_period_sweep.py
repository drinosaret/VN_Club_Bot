"""Period-based sweep + demote queries (the reopen-voting machinery).

When a cycle is reopened, the bot needs to find every nomination that
*originally* belonged to that period, including ones whose ``cycle_id``
got overwritten by later cycles' sweeps. Matching by ``(start_month,
end_month)`` instead of ``cycle_id`` is what makes this work.

Bugs in these queries either:
- Re-attach the wrong period's nominations to the reopened cycle, or
- Leave promoted picks promoted (status='monthly'/'seasonal') so the
  reopened vote presents the OLD winner as a regular nominee on the
  ballot, which reads as a duplicate winner.
"""
from __future__ import annotations

import sqlite3

import pytest

from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from tests.conftest import FakeBot


@pytest.fixture
async def schema_bot(fake_bot: FakeBot) -> FakeBot:
    """Same harness as test_vote_sql.py: migrations + cog CREATEs."""
    await run_migrations(fake_bot)
    await fake_bot.RUN(DatabaseQueries.CREATE_VN_TITLES_TABLE)
    await fake_bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    return fake_bot


async def _insert_nom(
    bot: FakeBot, *, vndb_id: str, guild_id: int, cycle_id: int | None,
    start_month: str, end_month: str, status: str = "nominated",
    nominator: int = 999,
) -> int:
    return await bot.RUN_RETURNING_ID(
        "INSERT INTO vn_titles "
        "(vndb_id, guild_id, start_month, end_month, is_monthly_points, "
        " status, cycle_id, nominator_user_id, title_cache) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vndb_id, guild_id, start_month, end_month, 10, status,
         cycle_id, nominator, f"Title {vndb_id}"),
    )


class TestSweepNominationsToCycle:
    """When voting opens, every existing nomination matching the cycle's
    exact period gets its ``cycle_id`` set to the new cycle. This is how
    nominations accumulated across days (or via the recovery sweep from
    a reopen) all end up on the same ballot.
    """

    @pytest.mark.asyncio
    async def test_attaches_matching_period_nominations(
        self, schema_bot: FakeBot
    ) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )
        await _insert_nom(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )

        await schema_bot.RUN(
            DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
            (42, "2026-06", "2026-06", 100),
        )

        rows = await schema_bot.GET(
            "SELECT cycle_id FROM vn_titles WHERE status='nominated'"
        )
        assert all(r[0] == 42 for r in rows), (
            "all matching-period nominations should be on the new cycle"
        )

    @pytest.mark.asyncio
    async def test_skips_different_period(self, schema_bot: FakeBot) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )
        await _insert_nom(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07",
        )

        # Sweep for June only.
        await schema_bot.RUN(
            DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
            (42, "2026-06", "2026-06", 100),
        )

        row = await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v2'"
        )
        assert row[0] is None, (
            "July nomination should not be attached to a June cycle"
        )

    @pytest.mark.asyncio
    async def test_separates_monthly_from_seasonal(
        self, schema_bot: FakeBot
    ) -> None:
        # A monthly nomination for June: start=2026-06, end=2026-06.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )
        # A seasonal nomination spanning April-June: start=2026-04, end=2026-06.
        await _insert_nom(
            schema_bot, vndb_id="v2", guild_id=100, cycle_id=None,
            start_month="2026-04", end_month="2026-06",
        )

        # Open a MONTHLY cycle targeting June: should only sweep v1.
        await schema_bot.RUN(
            DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
            (42, "2026-06", "2026-06", 100),
        )

        monthly_cycle_id = (await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v1'"
        ))[0]
        seasonal_cycle_id = (await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v2'"
        ))[0]
        assert monthly_cycle_id == 42
        assert seasonal_cycle_id is None, (
            "seasonal nomination's wider window must NOT match the "
            "monthly cycle's exact-period filter"
        )

    @pytest.mark.asyncio
    async def test_skips_other_guilds(self, schema_bot: FakeBot) -> None:
        # Different-guild nominations for the same period stay untouched.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )
        await _insert_nom(
            schema_bot, vndb_id="v2", guild_id=200, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )

        await schema_bot.RUN(
            DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
            (42, "2026-06", "2026-06", 100),
        )

        other_guild_cycle = (await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v2'"
        ))[0]
        assert other_guild_cycle is None, (
            "guild 200's nominations must not be swept onto guild 100's cycle"
        )

    @pytest.mark.asyncio
    async def test_legacy_null_guild_is_inclusive(
        self, schema_bot: FakeBot
    ) -> None:
        # Legacy rows with guild_id=NULL match any guild's sweep; they
        # represent "applies globally" semantics from the pre-overhaul DB.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=None, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )

        await schema_bot.RUN(
            DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
            (42, "2026-06", "2026-06", 100),
        )

        cycle = (await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v1'"
        ))[0]
        assert cycle == 42, "legacy NULL-guild rows should match any guild's sweep"

    @pytest.mark.asyncio
    async def test_doesnt_touch_promoted_picks(
        self, schema_bot: FakeBot
    ) -> None:
        # Status filter is 'nominated' only: a previous winner promoted
        # to 'monthly' must NOT be swept back into the new cycle.
        await _insert_nom(
            schema_bot, vndb_id="v_winner", guild_id=100, cycle_id=1,
            start_month="2026-06", end_month="2026-06", status="monthly",
        )
        await _insert_nom(
            schema_bot, vndb_id="v_nominee", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06",
        )

        await schema_bot.RUN(
            DatabaseQueries.SWEEP_NOMINATIONS_TO_CYCLE,
            (42, "2026-06", "2026-06", 100),
        )

        winner_cycle = (await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v_winner'"
        ))[0]
        nominee_cycle = (await schema_bot.GET_ONE(
            "SELECT cycle_id FROM vn_titles WHERE vndb_id='v_nominee'"
        ))[0]
        assert winner_cycle == 1, "promoted pick's cycle_id should NOT change"
        assert nominee_cycle == 42, "pending nominee should attach to new cycle"


class TestDemotePeriodPicksToNominations:
    """Reopening a closed vote needs to revert the winners back to
    nominations. The reopen path uses period match instead of cycle_id
    because later cycles can have overwritten cycle_id via the sweep.
    """

    @pytest.mark.asyncio
    async def test_demotes_monthly_winner(self, schema_bot: FakeBot) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            start_month="2026-06", end_month="2026-06", status="monthly",
        )

        await schema_bot.RUN(
            DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
            ("2026-06", "2026-06", 100),
        )

        status = (await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE vndb_id='v1'"
        ))[0]
        assert status == "nominated"

    @pytest.mark.asyncio
    async def test_works_even_when_cycle_id_was_overwritten(
        self, schema_bot: FakeBot
    ) -> None:
        """The reason DEMOTE matches by period not cycle_id.

        Scenario: June cycle ran and promoted v1 to 'monthly'. Then the
        JULY cycle's sweep overwrote v1.cycle_id (the sweep doesn't
        filter by status). When the admin reopens the June cycle, the
        demote needs to find v1 by its original period, not by
        cycle_id (which now points at the July cycle).
        """
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=2,  # overwritten
            start_month="2026-06", end_month="2026-06", status="monthly",
        )

        await schema_bot.RUN(
            DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
            ("2026-06", "2026-06", 100),
        )

        status = (await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE vndb_id='v1'"
        ))[0]
        assert status == "nominated", (
            "demote must find the winner by period even if cycle_id was "
            "overwritten by a later cycle's sweep"
        )

    @pytest.mark.asyncio
    async def test_skips_already_nominated(self, schema_bot: FakeBot) -> None:
        # Status filter is 'monthly'/'seasonal' only: pending nominations
        # for the same period must NOT be re-marked.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-06", end_month="2026-06", status="nominated",
        )

        await schema_bot.RUN(
            DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
            ("2026-06", "2026-06", 100),
        )

        # No-op: still 'nominated', and the (already-NULL) cycle_id is
        # left alone.
        row = await schema_bot.GET_ONE(
            "SELECT status, cycle_id FROM vn_titles WHERE vndb_id='v1'"
        )
        assert row == ("nominated", None)

    @pytest.mark.asyncio
    async def test_skips_other_periods(self, schema_bot: FakeBot) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v_june", guild_id=100, cycle_id=1,
            start_month="2026-06", end_month="2026-06", status="monthly",
        )
        await _insert_nom(
            schema_bot, vndb_id="v_july", guild_id=100, cycle_id=2,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )

        # Reopen only the June cycle.
        await schema_bot.RUN(
            DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
            ("2026-06", "2026-06", 100),
        )

        june_status = (await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE vndb_id='v_june'"
        ))[0]
        july_status = (await schema_bot.GET_ONE(
            "SELECT status FROM vn_titles WHERE vndb_id='v_july'"
        ))[0]
        assert june_status == "nominated"
        assert july_status == "monthly", "July winner must not be demoted"


class TestGetUserNomDedupe:
    """The `/nominate` dedupe uses GET_USER_NOM_IN_MONTH_WITH_CYCLE to
    detect "this user has already nominated for this period" so the new
    submission can swap the VN instead of adding a duplicate row.

    Behavior: exact-period match (start AND end), per user, per guild.
    Matches SWEEP_NOMINATIONS_TO_CYCLE so a nomination is deduped under
    the same key it will eventually be swept by.
    """

    @pytest.mark.asyncio
    async def test_exact_period_match_finds_existing(
        self, schema_bot: FakeBot,
    ) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-04", end_month="2026-06", status="nominated",
        )
        # Same user, same exact period: should match for dedupe.
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_USER_NOM_IN_MONTH_WITH_CYCLE,
            (999, "2026-04", "2026-06", 100),
        )
        assert row is not None
        assert row[1] == "v1"

    @pytest.mark.asyncio
    async def test_overlapping_but_different_period_no_match(
        self, schema_bot: FakeBot,
    ) -> None:
        """Keying on start_month alone would let a monthly-May nom match
        a seasonal Apr-Jun nom check. Exact match keeps monthly and
        seasonal lanes independent.
        """
        # Existing: monthly May.
        await _insert_nom(
            schema_bot, vndb_id="v_may", guild_id=100, cycle_id=None,
            start_month="2026-05", end_month="2026-05", status="nominated",
        )
        # User now tries seasonal Apr-Jun. Exact period differs.
        # Should NOT match: different cycle kinds, independent noms.
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_USER_NOM_IN_MONTH_WITH_CYCLE,
            (999, "2026-04", "2026-06", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_different_user_no_match(self, schema_bot: FakeBot) -> None:
        # Same period, different user: not a dedupe target.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-04", end_month="2026-06", status="nominated",
        )
        # The _insert_nom helper hardcodes nominator_user_id=999, so
        # querying for a different user yields nothing.
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_USER_NOM_IN_MONTH_WITH_CYCLE,
            (1234, "2026-04", "2026-06", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_different_guild_no_match(self, schema_bot: FakeBot) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=200, cycle_id=None,
            start_month="2026-04", end_month="2026-06", status="nominated",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_USER_NOM_IN_MONTH_WITH_CYCLE,
            (999, "2026-04", "2026-06", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_promoted_pick_doesnt_match(self, schema_bot: FakeBot) -> None:
        # Status filter is 'nominated': a winner already promoted to
        # 'monthly' shouldn't be treated as a pending nom to swap.
        await _insert_nom(
            schema_bot, vndb_id="v_promoted", guild_id=100, cycle_id=1,
            start_month="2026-05", end_month="2026-05", status="monthly",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_USER_NOM_IN_MONTH_WITH_CYCLE,
            (999, "2026-05", "2026-05", 100),
        )
        assert row is None


class TestVnPeriodDedupe:
    """The SAME VN must not be nominated twice for the SAME exact period in
    a guild, regardless of who nominates it. The per-user dedupe only catches
    one user re-nominating; this guards the gap where a *second* user
    nominates an already-nominated VN, which would otherwise stack a duplicate
    onto the same ballot (vote-splitting + 25-cap consumption).
    """

    @pytest.mark.asyncio
    async def test_finds_existing_regardless_of_nominator(
        self, schema_bot: FakeBot,
    ) -> None:
        # _insert_nom hardcodes nominator 999; the lookup is nominator-blind,
        # so a different user nominating the same VN/period still sees it.
        existing_id = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_NOMINATION_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is not None
        assert row[0] == existing_id

    @pytest.mark.asyncio
    async def test_exact_period_keeps_lanes_independent(
        self, schema_bot: FakeBot,
    ) -> None:
        # Monthly July nom; a seasonal Summer (Jul-Sep) nom of the same VN is
        # a different lane and must NOT be treated as a duplicate.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_NOMINATION_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-09", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_different_guild_no_match(self, schema_bot: FakeBot) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=200, cycle_id=None,
            start_month="2026-07", end_month="2026-07",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_NOMINATION_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_promoted_pick_doesnt_match(self, schema_bot: FakeBot) -> None:
        # Only pending nominations count; a promoted winner of the same VN
        # for the same period isn't a duplicate nomination.
        await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=1,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_NOMINATION_BY_VN_AND_PERIOD,
            ("v_win", "2026-07", "2026-07", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_unique_index_blocks_second_nominator(
        self, schema_bot: FakeBot,
    ) -> None:
        # The dedupe indexes are created against an existing vn_titles table;
        # the fixture creates that table *after* its first migrations pass
        # (mirroring a fresh install, where the index lands on the next boot),
        # so re-run migrations now that the table exists.
        await run_migrations(schema_bot)
        # First nominator (user 1) lands v1 for July.
        first = await schema_bot.RUN_RETURNING_ID(
            DatabaseQueries.INSERT_NOMINATION_AS_PICK,
            ("v1", 100, "2026-07", "2026-07", 10, None, 1, "Title v1"),
        )
        assert first
        # A *different* user nominating the same VN/period: the partial unique
        # index makes INSERT OR IGNORE a no-op (lastrowid 0), the same race
        # path the cog already handles for per-user dupes.
        second = await schema_bot.RUN_RETURNING_ID(
            DatabaseQueries.INSERT_NOMINATION_AS_PICK,
            ("v1", 100, "2026-07", "2026-07", 10, None, 2, "Title v1"),
        )
        assert not second, "duplicate-VN nomination must be rejected by the index"
        count = (await schema_bot.GET_ONE(
            "SELECT COUNT(*) FROM vn_titles "
            "WHERE vndb_id='v1' AND status='nominated'"
        ))[0]
        assert count == 1

    @pytest.mark.asyncio
    async def test_unique_index_allows_cross_lane_same_vn(
        self, schema_bot: FakeBot,
    ) -> None:
        await run_migrations(schema_bot)  # ensure the dedupe index exists
        # Same VN, monthly July AND seasonal Summer (Jul-Sep): different
        # exact periods, so both inserts succeed (cross-lane is intended).
        monthly = await schema_bot.RUN_RETURNING_ID(
            DatabaseQueries.INSERT_NOMINATION_AS_PICK,
            ("v1", 100, "2026-07", "2026-07", 10, None, 1, "Title v1"),
        )
        seasonal = await schema_bot.RUN_RETURNING_ID(
            DatabaseQueries.INSERT_NOMINATION_AS_PICK,
            ("v1", 100, "2026-07", "2026-09", 10, None, 1, "Title v1"),
        )
        assert monthly and seasonal and monthly != seasonal

    @pytest.mark.asyncio
    async def test_unique_index_allows_cross_guild_same_vn(
        self, schema_bot: FakeBot,
    ) -> None:
        await run_migrations(schema_bot)  # ensure the dedupe index exists
        # Same VN, same period, same nominator, DIFFERENT guilds. Each
        # server's pool is independent, so both inserts must succeed; a
        # nomination in one server must never block another server. guild_id
        # is part of BOTH dedupe index keys, so this exercises both at once.
        g100 = await schema_bot.RUN_RETURNING_ID(
            DatabaseQueries.INSERT_NOMINATION_AS_PICK,
            ("v1", 100, "2026-07", "2026-07", 10, None, 1, "Title v1"),
        )
        g200 = await schema_bot.RUN_RETURNING_ID(
            DatabaseQueries.INSERT_NOMINATION_AS_PICK,
            ("v1", 200, "2026-07", "2026-07", 10, None, 1, "Title v1"),
        )
        assert g100 and g200 and g100 != g200


class TestReopenDedupeCollision:
    """Reopening a closed cycle demotes the winner back to 'nominated'. If a
    user re-nominated that winning VN during the closed window (the winner was
    status='monthly'/'seasonal', so invisible to the /nominate dup guard), the
    demote recreates a duplicate 'nominated' row and collides with
    idx_vn_titles_vn_period_dedup, rolling the whole reopen back. The reopen
    transaction drops the vote-less re-nom first to avoid that.
    """

    @pytest.mark.asyncio
    async def test_demote_alone_collides_with_renominated_winner(
        self, schema_bot: FakeBot,
    ) -> None:
        await run_migrations(schema_bot)  # ensure the dedupe index exists
        # Winner promoted at close: status='monthly', keeps its cycle_id.
        await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=5,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        # A user re-nominated the same VN during the closed window.
        await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07", status="nominated",
        )
        # Demoting the winner back to 'nominated' without first clearing the
        # re-nom violates the VN-period unique index (documents the hazard the
        # cleanup step prevents).
        with pytest.raises(sqlite3.IntegrityError):
            await schema_bot.RUN_TRANSACTION([
                (DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
                 ("2026-07", "2026-07", 100)),
            ])

    @pytest.mark.asyncio
    async def test_cleanup_then_demote_reopens_cleanly(
        self, schema_bot: FakeBot,
    ) -> None:
        await run_migrations(schema_bot)
        winner_id = await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=5,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        renom_id = await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07", status="nominated",
        )
        # The real reopen order: clear redundant re-noms, then demote.
        await schema_bot.RUN_TRANSACTION([
            (DatabaseQueries.DELETE_REDUNDANT_NOMINATIONS_FOR_REOPEN,
             ("2026-07", "2026-07", 100, "2026-07", "2026-07", 100)),
            (DatabaseQueries.DEMOTE_PERIOD_PICKS_TO_NOMINATIONS,
             ("2026-07", "2026-07", 100)),
        ])
        # The vote-less re-nom is gone; the winner is back as a candidate, so
        # the VN sits on the ballot exactly once.
        rows = await schema_bot.GET(
            "SELECT id, status FROM vn_titles "
            "WHERE vndb_id='v_win' AND start_month='2026-07' "
            "AND end_month='2026-07' AND guild_id=100"
        )
        assert len(rows) == 1
        assert rows[0][0] == winner_id
        assert rows[0][1] == "nominated"
        gone = await schema_bot.GET_ONE(
            "SELECT id FROM vn_titles WHERE id = ?", (renom_id,),
        )
        assert gone is None

    @pytest.mark.asyncio
    async def test_cleanup_spares_voted_rows_and_unrelated_noms(
        self, schema_bot: FakeBot,
    ) -> None:
        await run_migrations(schema_bot)
        # Winner being demoted.
        await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=5,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        # A standalone nomination for a DIFFERENT VN must survive. Distinct
        # nominators keep these clear of the per-user dedupe index.
        other = await _insert_nom(
            schema_bot, vndb_id="v_other", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07", status="nominated",
            nominator=1,
        )
        # A re-nom of the winning VN that carries a cycle_id (swept, may hold
        # votes) must NOT be deleted: leave the index unformed rather than
        # drop a voted row.
        voted = await _insert_nom(
            schema_bot, vndb_id="v_win", guild_id=100, cycle_id=9,
            start_month="2026-07", end_month="2026-07", status="nominated",
            nominator=2,
        )
        await schema_bot.RUN(
            DatabaseQueries.DELETE_REDUNDANT_NOMINATIONS_FOR_REOPEN,
            ("2026-07", "2026-07", 100, "2026-07", "2026-07", 100),
        )
        survived = await schema_bot.GET(
            "SELECT id FROM vn_titles WHERE id IN (?, ?) ORDER BY id",
            (other, voted),
        )
        assert [r[0] for r in survived] == sorted([other, voted])


class TestPoolEntryByVnAndPeriod:
    """GET_POOL_ENTRY_BY_VN_AND_PERIOD is the pool-wide lookup /nominate uses to
    block re-nominating a VN already in the pool for the exact period, whether
    as a pending nomination or an already-decided pick. Unlike the
    nomination-only GET_NOMINATION_BY_VN_AND_PERIOD, it matches every status and
    reports it so the command can phrase the right message.
    """

    @pytest.mark.asyncio
    async def test_finds_monthly_pick(self, schema_bot: FakeBot) -> None:
        pid = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is not None
        assert row[0] == pid and row[1] == "monthly"
        # The nomination-only query must still ignore a pick.
        nom_only = await schema_bot.GET_ONE(
            DatabaseQueries.GET_NOMINATION_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert nom_only is None

    @pytest.mark.asyncio
    async def test_finds_seasonal_pick(self, schema_bot: FakeBot) -> None:
        pid = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            start_month="2026-07", end_month="2026-09", status="seasonal",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-09", 100),
        )
        assert row is not None and row[0] == pid and row[1] == "seasonal"

    @pytest.mark.asyncio
    async def test_finds_special_pick(self, schema_bot: FakeBot) -> None:
        pid = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07", status="special",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is not None and row[0] == pid and row[1] == "special"

    @pytest.mark.asyncio
    async def test_finds_pending_nomination(self, schema_bot: FakeBot) -> None:
        pid = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is not None and row[0] == pid and row[1] == "nominated"

    @pytest.mark.asyncio
    async def test_exact_period_keeps_lanes_independent(
        self, schema_bot: FakeBot,
    ) -> None:
        # Seasonal pick (Jul-Sep); a monthly July (Jul-Jul) nomination is a
        # different lane and must NOT see it.
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            start_month="2026-07", end_month="2026-09", status="seasonal",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_different_guild_no_match(self, schema_bot: FakeBot) -> None:
        await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=200, cycle_id=1,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_legacy_null_guild_matches(self, schema_bot: FakeBot) -> None:
        # A legacy global (guild_id NULL) pick is visible to any guild, the same
        # inclusivity the nomination-only query uses.
        pid = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=None, cycle_id=1,
            start_month="2026-07", end_month="2026-07", status="monthly",
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is not None and row[0] == pid

    @pytest.mark.asyncio
    async def test_prefers_pick_when_pick_and_nomination_coexist(
        self, schema_bot: FakeBot,
    ) -> None:
        # Legacy corrupt state: both a pick and a pending nomination for the
        # same VN/period. The lookup must deterministically report the pick
        # (ORDER BY puts non-nominated rows first), regardless of insert order.
        nom_id = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=None,
            start_month="2026-07", end_month="2026-07", status="nominated",
            nominator=1,
        )
        pick_id = await _insert_nom(
            schema_bot, vndb_id="v1", guild_id=100, cycle_id=1,
            start_month="2026-07", end_month="2026-07", status="monthly",
            nominator=2,
        )
        row = await schema_bot.GET_ONE(
            DatabaseQueries.GET_POOL_ENTRY_BY_VN_AND_PERIOD,
            ("v1", "2026-07", "2026-07", 100),
        )
        assert row is not None
        assert row[0] == pick_id and row[1] == "monthly"
        assert row[0] != nom_id
