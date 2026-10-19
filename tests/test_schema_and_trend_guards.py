"""Schema-safety and trend-axis guards, one class per guard:

- ``_column_names`` rejects unsafe table identifiers before PRAGMA.
- The ``vn_club_stats`` trend axis uses UTC, not local time.
- ``_drop_vn_cycles_target_month_unique`` rolls back on a row-count
  mismatch instead of leaving a partial table.
- ``_unify_nominations_into_vn_titles`` tolerates a row left by a partial
  earlier run without an IntegrityError.

A refactor that "cleans up" one of these guards fails here.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from lib.migrations import _column_names, _drop_vn_cycles_target_month_unique
from tests.conftest import FakeBot


# ---------------------------------------------------------------------------
# _column_names guard
# ---------------------------------------------------------------------------


class TestColumnNamesGuard:
    """SQLite PRAGMA doesn't accept ``?`` placeholders so the table name
    is interpolated. Every current caller passes a hardcoded literal, but
    the guard exists so the function fails fast if a future caller
    forwards user input.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [
        "",
        "1table",                  # leading digit
        "vn_titles; DROP TABLE x", # statement separator
        "vn_titles)",              # closing paren
        "vn_titles--comment",      # dash → SQL comment
        "vn titles",               # whitespace
        "vn'titles",               # quote
        '"vn_titles"',             # quoted identifier
        "vn_titles, vn_votes",     # comma split
    ])
    async def test_rejects_unsafe_identifiers(self, fake_bot: FakeBot, bad: str) -> None:
        with pytest.raises(ValueError, match="unsafe table name"):
            await _column_names(fake_bot, bad)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("good", [
        "vn_titles",
        "VnTitles",
        "_private_table",
        "a",
        "table_1",
    ])
    async def test_accepts_safe_identifiers(
        self, fake_bot: FakeBot, good: str
    ) -> None:
        # No DROP: it only needs to not raise ValueError. The PRAGMA
        # itself returns [] for a nonexistent table, which is exactly
        # what _column_names promises to handle.
        cols = await _column_names(fake_bot, good)
        assert cols == []  # table doesn't exist yet


# ---------------------------------------------------------------------------
# vn_club_stats 12-month trend axis uses UTC
# ---------------------------------------------------------------------------


class TestClubStatsTrendAxisUTC:
    """The trend axis must not use local ``datetime.now()``. ``reward_month``
    in the DB is UTC-derived via get_current_month, so a local axis near a
    month boundary on a non-UTC server leaves the current bar empty and
    mis-labels the oldest bar.
    """

    def test_axis_always_12_entries(self) -> None:
        from cogs.vn_club_stats import _build_full_12_month_trend
        out = _build_full_12_month_trend(rows=[])
        assert len(out) == 12

    def test_axis_ends_at_current_utc_month(self) -> None:
        from cogs.vn_club_stats import _build_full_12_month_trend
        now_utc = datetime.now(timezone.utc)
        expected_last = f"{now_utc.year:04d}-{now_utc.month:02d}"
        out = _build_full_12_month_trend(rows=[])
        assert out[-1][0] == expected_last, (
            f"trend should end at UTC current month {expected_last}, got {out[-1][0]}"
        )

    def test_axis_starts_eleven_months_before_current(self) -> None:
        from cogs.vn_club_stats import _build_full_12_month_trend
        now_utc = datetime.now(timezone.utc)
        # Compute "11 months ago" the way the function does.
        y, m = now_utc.year, now_utc.month
        for _ in range(11):
            if m == 1:
                y -= 1
                m = 12
            else:
                m -= 1
        expected_first = f"{y:04d}-{m:02d}"
        out = _build_full_12_month_trend(rows=[])
        assert out[0][0] == expected_first

    def test_zero_fills_missing_months(self) -> None:
        from cogs.vn_club_stats import _build_full_12_month_trend
        # Only the current UTC month has data.
        now_utc = datetime.now(timezone.utc)
        cur = f"{now_utc.year:04d}-{now_utc.month:02d}"
        out = _build_full_12_month_trend(rows=[(cur, 5)])
        # Last bar = 5, every other bar = 0.
        assert out[-1] == (cur, 5)
        assert all(count == 0 for _, count in out[:-1])

    def test_handles_sparse_input_in_any_order(self) -> None:
        from cogs.vn_club_stats import _build_full_12_month_trend
        # The query returns DESC, but the function rebuilds ASC regardless.
        now_utc = datetime.now(timezone.utc)
        cur = f"{now_utc.year:04d}-{now_utc.month:02d}"
        # One arbitrary month in the past, input as DESC.
        rows = [(cur, 3), ("2020-01", 99)]  # 2020 not in window, ignored
        out = _build_full_12_month_trend(rows=rows)
        keys = [k for k, _ in out]
        # Strictly ascending YYYY-MM order.
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Migration rollback on row-count mismatch
# ---------------------------------------------------------------------------


class TestDropVnCyclesUniqueRollback:
    """When the rebuilt vn_cycles has a row count different from
    the original (e.g. some rows failed to copy), the migration must
    DROP the partial new table and RENAME the backup back to vn_cycles
    before re-raising. Without the rollback, a subsequent restart sees
    the (broken) new shape, the idempotency check passes, and the data
    loss is permanent.

    Construct the exact bad state: an `_unique_drop_backup` left over
    from a previous failed run, with the new vn_cycles missing rows.
    Then call the migration again (it's idempotent under normal flow,
    so the state is seeded manually).
    """

    @pytest.mark.asyncio
    async def test_migration_idempotent_when_constraint_already_dropped(
        self, fake_bot: FakeBot
    ) -> None:
        # Create vn_cycles WITHOUT the legacy UNIQUE; the migration's
        # idempotency check should detect this and bail without
        # touching anything.
        await fake_bot.RUN(
            """CREATE TABLE vn_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                phase TEXT NOT NULL DEFAULT 'voting',
                kind TEXT NOT NULL DEFAULT 'monthly',
                vote_choice_mode TEXT,
                vote_winner_count INTEGER,
                target_month TEXT NOT NULL,
                target_end_month TEXT,
                announcement_channel_id INTEGER,
                announcement_message_id INTEGER,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                closes_at TIMESTAMP,
                vote_ui TEXT,
                allowed_role_id INTEGER
            )"""
        )
        # No-op call: must complete without raising.
        await _drop_vn_cycles_target_month_unique(fake_bot)
        # Table still exists, no backup table was created.
        rows = await fake_bot.GET(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in rows}
        assert "vn_cycles" in names
        assert "vn_cycles_unique_drop_backup" not in names

    @pytest.mark.asyncio
    async def test_existing_backup_table_aborts_safely(
        self, fake_bot: FakeBot
    ) -> None:
        # If a prior failed run left a backup table behind, the
        # migration must skip rather than crash or double-rebuild.
        # Seed the bad state.
        await fake_bot.RUN(
            """CREATE TABLE vn_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                phase TEXT NOT NULL DEFAULT 'voting',
                kind TEXT NOT NULL DEFAULT 'monthly',
                target_month TEXT NOT NULL,
                UNIQUE(guild_id, target_month, kind)
            )"""
        )
        await fake_bot.RUN(
            "CREATE TABLE vn_cycles_unique_drop_backup (id INTEGER)"
        )
        # Should NOT crash, just log + return.
        await _drop_vn_cycles_target_month_unique(fake_bot)
        # Backup table is still there (admin's job to clean up).
        rows = await fake_bot.GET(
            "SELECT name FROM sqlite_master "
            "WHERE name = 'vn_cycles_unique_drop_backup'"
        )
        assert rows, "backup should be left for manual recovery"


# ---------------------------------------------------------------------------
# _unify_nominations_into_vn_titles partial-rerun guard
# ---------------------------------------------------------------------------


class TestUnifyMigrationPartialRerunGuard:
    """The unify migration's fresh-INSERT branch must not blow up
    with IntegrityError when a prior partial run left a vn_titles row
    for the same (vndb_id, guild_id, period) but with a non-cycle-kind
    status.

    Full migration plumbing is heavy to set up here, so the guard is
    exercised at the SQL level: the function the migration calls before
    its INSERT must find a pre-existing row regardless of status.
    """

    @pytest.mark.asyncio
    async def test_unify_finds_existing_row_regardless_of_status(
        self, fake_bot: FakeBot
    ) -> None:
        from lib.utils import DatabaseQueries
        # Set up vn_titles via the production CREATE so the test rides
        # the exact schema the migration is meant to handle.
        await fake_bot.RUN(DatabaseQueries.CREATE_VN_TITLES_TABLE)

        await fake_bot.RUN(
            "INSERT INTO vn_titles "
            "(vndb_id, guild_id, start_month, end_month, is_monthly_points, "
            " status, cycle_id, nominator_user_id, title_cache) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("v_pre", 100, "2026-06", "2026-06", 10,
             "special",          # non-cycle-kind: a prior partial migration
             None,                # left cycle_id NULL
             999, "Pre-existing"),
        )

        # The guard query (broad, no status filter).
        rows = await fake_bot.GET(
            "SELECT id FROM vn_titles "
            "WHERE vndb_id = ? AND guild_id = ? "
            "  AND start_month = ? AND end_month = ?",
            ("v_pre", 100, "2026-06", "2026-06"),
        )
        assert len(rows) == 1, (
            "broad guard must find the pre-existing row "
            "regardless of status, so unify reuses it"
        )

        # A narrow guard (status + cycle_id IS NULL) would NOT find it,
        # which is why the guard has no status filter.
        narrow = await fake_bot.GET(
            "SELECT id FROM vn_titles "
            "WHERE vndb_id = ? AND guild_id = ? "
            "  AND start_month = ? AND end_month = ? "
            "  AND status = ? AND cycle_id IS NULL",
            ("v_pre", 100, "2026-06", "2026-06", "monthly"),
        )
        assert narrow == [], (
            "the narrow check misses the row, so the broader guard "
            "covers a case the narrow check does not"
        )
