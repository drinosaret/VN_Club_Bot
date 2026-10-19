"""Schema tests for `reading_logs` indexes + partial unique constraint.

The migration's `_add_reading_logs_indexes` early-returns when the
table doesn't exist (fresh install). The cog's `cog_load` is supposed
to pick up the slack by running `CREATE_READING_LOGS_INDEXES`. These
tests assert that the resulting schema after applying both statements
matches what every query plan relies on.

Also covers the partial unique index that makes the
`ADD_READING_LOG_OR_IGNORE` race-safety hold up.
"""
from __future__ import annotations

import aiosqlite
import pytest

from lib.utils import DatabaseQueries
from tests.conftest import FakeBot


EXPECTED_INDEXES = {
    "idx_reading_logs_user_id",
    "idx_reading_logs_reward_month",
    "idx_reading_logs_logged_in_guild",
    "idx_reading_logs_user_month",
    "idx_reading_logs_vndb_id",
    "idx_reading_logs_user_vn_month",
}


@pytest.fixture
async def reading_logs_bot(fake_bot: FakeBot) -> FakeBot:
    """Simulate the cog's cog_load path: create the table, then run
    every CREATE_READING_LOGS_INDEXES statement. No migrations needed;
    these tests pin the cog-side schema specifically.
    """
    await fake_bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    for stmt in DatabaseQueries.CREATE_READING_LOGS_INDEXES:
        await fake_bot.RUN(stmt)
    return fake_bot


class TestReadingLogsIndexes:
    @pytest.mark.asyncio
    async def test_all_expected_indexes_present(
        self, reading_logs_bot: FakeBot,
    ) -> None:
        rows = await reading_logs_bot.GET(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='reading_logs'"
        )
        actual = {r[0] for r in rows}
        missing = EXPECTED_INDEXES - actual
        assert not missing, f"missing reading_logs indexes: {missing}"

    @pytest.mark.asyncio
    async def test_user_id_query_uses_index(
        self, reading_logs_bot: FakeBot,
    ) -> None:
        # EXPLAIN QUERY PLAN should show the index usage for a typical
        # /logs lookup. A fallback to SCAN reading_logs means a fresh
        # install is left without the index.
        async with aiosqlite.connect(reading_logs_bot.path_to_db) as db:
            async with db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM reading_logs WHERE user_id = ?",
                (1,),
            ) as cur:
                plan = await cur.fetchall()
        plan_text = " ".join(row[3] for row in plan)
        assert "idx_reading_logs_user_id" in plan_text or "idx_reading_logs_user_month" in plan_text, (
            f"user_id query falls back to table scan: {plan_text}"
        )


class TestReadingLogPartialUniqueIndex:
    @pytest.mark.asyncio
    async def test_duplicate_user_vn_month_blocked(
        self, reading_logs_bot: FakeBot,
    ) -> None:
        # Two inserts for the same (user_id, vndb_id, reward_month). The
        # second one should be silently ignored by INSERT OR IGNORE
        # because of the partial unique index.
        bind = (42, "v17", 5, 5, "As Monthly VN", "2026-06", 10, "first", 100)
        await reading_logs_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE, bind,
        )
        bind2 = (42, "v17", 4, 5, "As Monthly VN", "2026-06", 10, "second", 100)
        await reading_logs_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE, bind2,
        )

        (count,) = await reading_logs_bot.GET_ONE(
            "SELECT COUNT(*) FROM reading_logs "
            "WHERE user_id = ? AND vndb_id = ? AND reward_month = ?",
            (42, "v17", "2026-06"),
        )
        assert count == 1, "second insert should have been silently ignored"
        (comment,) = await reading_logs_bot.GET_ONE(
            "SELECT comment FROM reading_logs WHERE user_id = ? AND vndb_id = ?",
            (42, "v17"),
        )
        assert comment == "first", "OR IGNORE should preserve the original row"

    @pytest.mark.asyncio
    async def test_different_month_allowed(
        self, reading_logs_bot: FakeBot,
    ) -> None:
        # Same (user, vn), different reward_month → both inserts succeed.
        await reading_logs_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
            (42, "v17", 5, 5, "As Monthly VN", "2026-05", 10, "may", 100),
        )
        await reading_logs_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
            (42, "v17", 5, 5, "As Monthly VN", "2026-06", 10, "june", 100),
        )
        (count,) = await reading_logs_bot.GET_ONE(
            "SELECT COUNT(*) FROM reading_logs WHERE user_id = ? AND vndb_id = ?",
            (42, "v17"),
        )
        assert count == 2

    @pytest.mark.asyncio
    async def test_null_vndb_id_allows_duplicates(
        self, reading_logs_bot: FakeBot,
    ) -> None:
        # The unique index is partial (WHERE vndb_id IS NOT NULL) so
        # admin reward_points rows with NULL vndb_id can stack in a
        # single month without colliding.
        bind = (42, None, None, 5, "Admin reward", "2026-06", 50, "first reward", 100)
        await reading_logs_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE, bind,
        )
        await reading_logs_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE, bind,
        )
        (count,) = await reading_logs_bot.GET_ONE(
            "SELECT COUNT(*) FROM reading_logs WHERE user_id = ? AND reward_month = ?",
            (42, "2026-06"),
        )
        assert count == 2, "NULL-vndb_id rows shouldn't collide on the partial index"

    @pytest.mark.asyncio
    async def test_returning_id_zero_signals_ignored(
        self, reading_logs_bot: FakeBot,
    ) -> None:
        # The /finish callback distinguishes "inserted" from "race
        # detected" by checking if RUN_RETURNING_ID came back as 0
        # (no row inserted on a fresh connection → lastrowid stays 0).
        bind = (42, "v17", 5, 5, "As Monthly VN", "2026-06", 10, "first", 100)
        first_id = await reading_logs_bot.RUN_RETURNING_ID(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE, bind,
        )
        assert first_id > 0, "first insert should return a real log_id"
        second_id = await reading_logs_bot.RUN_RETURNING_ID(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE, bind,
        )
        assert second_id == 0, (
            "second insert should be ignored; the caller relies on this "
            "to surface an 'already logged' message"
        )
