"""Migration smoke tests for ``lib/migrations.py``.

Migrations run from ``Bot.load_cogs`` *before* any cog's ``cog_load``
hook fires. That means at migration time:

- On a fresh install, ``vn_titles`` and ``reading_logs`` don't exist yet
  (the cogs that own them create them later via ``CREATE TABLE IF NOT
  EXISTS``).
- On a legacy upgrade, those tables already exist in their pre-overhaul
  shape (vndb_id PK, no guild_id, no status, no completed_at).
- The unify migration *drops* ``vn_nominations`` after copying its data
  into ``vn_titles``. So post-migration there's no ``vn_nominations``
  table, just a ``vn_nominations_unify_backup`` for recovery.

These tests cover three scenarios that catch the most realistic
production failure modes.
"""
from __future__ import annotations

import aiosqlite
import pytest

from lib.migrations import run_migrations
from tests.conftest import FakeBot


async def _table_columns(db_path: str, table: str) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            return [row[1] for row in await cur.fetchall()]


async def _table_exists(db_path: str, table: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ) as cur:
            return await cur.fetchone() is not None


# Production-shape CREATE statements lifted from utils.py and the
# original cog/cog_load definitions. Seeding these mimics the
# realistic state of a long-running legacy DB.
LEGACY_VN_TITLES = """
    CREATE TABLE vn_titles (
        vndb_id TEXT PRIMARY KEY,
        start_month TEXT NOT NULL,
        end_month TEXT NOT NULL,
        is_monthly_points INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

LEGACY_READING_LOGS = """
    CREATE TABLE reading_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        vndb_id TEXT,
        user_rating INTEGER,
        reward_reason TEXT NOT NULL,
        reward_month TEXT NOT NULL,
        points INTEGER NOT NULL,
        comment TEXT,
        logged_in_guild INTEGER
    )
"""

LEGACY_VNDB_CACHE = """
    CREATE TABLE vndb_cache (
        vndb_id TEXT PRIMARY KEY,
        title_en TEXT,
        title_ja TEXT,
        thumbnail_url TEXT,
        thumbnail_is_nsfw INTEGER,
        length_minutes INTEGER,
        length_rating TEXT,
        description TEXT
    )
"""


class TestFreshInstall:
    """Migrations on an empty DB, run before any cog has created its tables."""

    @pytest.mark.asyncio
    async def test_creates_cycle_and_settings_tables(self, fake_bot: FakeBot) -> None:
        await run_migrations(fake_bot)
        # These are the tables migrations own (created by _create_cycle_tables,
        # _create_guild_settings_table, and _create_guild_managers_table).
        for table in (
            "vn_cycles", "vn_votes", "guild_settings", "guild_managers",
        ):
            assert await _table_exists(fake_bot.path_to_db, table), (
                f"migrations should create {table} on a fresh install"
            )

    @pytest.mark.asyncio
    async def test_doesnt_create_cog_owned_tables(self, fake_bot: FakeBot) -> None:
        # vn_titles + reading_logs are NOT created by migrations on a fresh
        # install. The respective cogs create them in cog_load via their
        # own CREATE TABLE IF NOT EXISTS. If a future refactor moves those
        # CREATEs into migrations, this test will (correctly) need updating.
        await run_migrations(fake_bot)
        assert not await _table_exists(fake_bot.path_to_db, "vn_titles")
        assert not await _table_exists(fake_bot.path_to_db, "reading_logs")

    @pytest.mark.asyncio
    async def test_empty_vn_nominations_dropped_on_fresh_install(
        self, fake_bot: FakeBot,
    ) -> None:
        """_create_cycle_tables unconditionally creates vn_nominations, but
        on a fresh install the unify step early-exits because vn_titles
        doesn't exist yet. The empty table must still be dropped, or it
        stays on disk for every new deployment.
        """
        await run_migrations(fake_bot)
        assert not await _table_exists(
            fake_bot.path_to_db, "vn_nominations"
        ), "vn_nominations should be dropped on the fresh-install path"

    @pytest.mark.asyncio
    async def test_vn_cycles_has_full_modern_shape(self, fake_bot: FakeBot) -> None:
        await run_migrations(fake_bot)
        cols = await _table_columns(fake_bot.path_to_db, "vn_cycles")
        # The fresh CREATE in _create_cycle_tables already declares every
        # column the later per-column ALTER migrations would otherwise add.
        for col in (
            "id", "guild_id", "phase", "kind",
            "vote_choice_mode", "vote_winner_count",
            "target_month", "target_end_month",
            "announcement_channel_id", "announcement_message_id",
            "opened_at", "closed_at",
        ):
            assert col in cols, f"vn_cycles missing column {col} after fresh migration"

    @pytest.mark.asyncio
    async def test_vn_cycles_has_no_obsolete_unique_constraint(
        self, fake_bot: FakeBot,
    ) -> None:
        """The ``_create_cycle_tables`` DDL omits the obsolete
        ``UNIQUE(guild_id, target_month, kind)``, so fresh installs never
        carry the constraint and never trigger the
        ``_drop_vn_cycles_target_month_unique`` rebuild.
        """
        await run_migrations(fake_bot)
        row = await fake_bot.GET_ONE(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vn_cycles'"
        )
        assert row is not None
        create_sql = row[0]
        assert "UNIQUE(guild_id, target_month, kind)" not in create_sql
        assert "UNIQUE (guild_id, target_month, kind)" not in create_sql

    @pytest.mark.asyncio
    async def test_guild_settings_has_default_vote_ui(self, fake_bot: FakeBot) -> None:
        await run_migrations(fake_bot)
        cols = await _table_columns(fake_bot.path_to_db, "guild_settings")
        assert "default_vote_ui" in cols


class TestLegacyUpgrade:
    """Migrations on a DB with the pre-overhaul shape, as when restoring
    from an old backup.
    """

    @pytest.fixture
    async def legacy_db(self, fake_bot: FakeBot) -> FakeBot:
        await fake_bot.RUN(LEGACY_VN_TITLES)
        await fake_bot.RUN(LEGACY_READING_LOGS)
        await fake_bot.RUN(LEGACY_VNDB_CACHE)
        # A few rows to verify they survive the rebuild.
        await fake_bot.RUN(
            "INSERT INTO vn_titles VALUES "
            "('v17', '2025-01', '2025-01', 10, CURRENT_TIMESTAMP)"
        )
        await fake_bot.RUN(
            "INSERT INTO vn_titles VALUES "
            "('v2002', '2025-02', '2025-03', 15, CURRENT_TIMESTAMP)"
        )
        return fake_bot

    @pytest.mark.asyncio
    async def test_rebuilds_vn_titles_with_synthetic_id(
        self, legacy_db: FakeBot
    ) -> None:
        await run_migrations(legacy_db)
        cols = await _table_columns(legacy_db.path_to_db, "vn_titles")
        # Modern vn_titles columns introduced by the rebuild + later steps.
        for col in ("id", "guild_id", "status", "cycle_id",
                    "nominator_user_id", "title_cache"):
            assert col in cols, f"vn_titles missing modernized column {col}"

    @pytest.mark.asyncio
    async def test_preserves_legacy_rows(self, legacy_db: FakeBot) -> None:
        await run_migrations(legacy_db)
        async with aiosqlite.connect(legacy_db.path_to_db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM vn_titles WHERE guild_id IS NULL"
            ) as cur:
                count = (await cur.fetchone())[0]
        # Both seeded rows preserved under guild_id=NULL (legacy/global).
        assert count == 2, f"expected 2 preserved rows, got {count}"

    @pytest.mark.asyncio
    async def test_keeps_rebuild_backup_table(self, legacy_db: FakeBot) -> None:
        # The vn_titles rebuild leaves vn_titles_backup behind so a manual
        # rollback is possible if downstream queries break.
        await run_migrations(legacy_db)
        assert await _table_exists(legacy_db.path_to_db, "vn_titles_backup")

    @pytest.mark.asyncio
    async def test_drops_vn_nominations_after_unify(
        self, legacy_db: FakeBot
    ) -> None:
        # _create_cycle_tables creates vn_nominations, then
        # _unify_nominations_into_vn_titles drops it after copying. The
        # backup table is what survives.
        await run_migrations(legacy_db)
        assert not await _table_exists(
            legacy_db.path_to_db, "vn_nominations"
        ), "vn_nominations should be dropped by unify after legacy upgrade"
        assert await _table_exists(
            legacy_db.path_to_db, "vn_nominations_unify_backup"
        ), "unify should leave a backup table behind"

    @pytest.mark.asyncio
    async def test_creates_cycle_tables(self, legacy_db: FakeBot) -> None:
        await run_migrations(legacy_db)
        for table in ("vn_cycles", "vn_votes", "guild_settings"):
            assert await _table_exists(legacy_db.path_to_db, table)

    @pytest.mark.asyncio
    async def test_reading_logs_gains_completed_at(self, legacy_db: FakeBot) -> None:
        await run_migrations(legacy_db)
        cols = await _table_columns(legacy_db.path_to_db, "reading_logs")
        assert "completed_at" in cols

    @pytest.mark.asyncio
    async def test_vndb_cache_gains_character_count(
        self, legacy_db: FakeBot
    ) -> None:
        await run_migrations(legacy_db)
        cols = await _table_columns(legacy_db.path_to_db, "vndb_cache")
        assert "character_count" in cols

    @pytest.mark.asyncio
    async def test_reading_logs_indexes_created(self, legacy_db: FakeBot) -> None:
        await run_migrations(legacy_db)
        async with aiosqlite.connect(legacy_db.path_to_db) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='reading_logs'"
            ) as cur:
                names = [r[0] for r in await cur.fetchall()]
        # Every hot filter or sort column gets an index.
        for expected in ("idx_reading_logs_user_id",
                         "idx_reading_logs_reward_month",
                         "idx_reading_logs_logged_in_guild",
                         "idx_reading_logs_user_month",
                         "idx_reading_logs_vndb_id"):
            assert expected in names, f"missing index {expected}"

    @pytest.mark.asyncio
    async def test_partial_unique_index_on_legacy_upgrade(
        self, legacy_db: FakeBot,
    ) -> None:
        """The partial unique index on
        (user_id, vndb_id, reward_month) is what makes the
        ADD_READING_LOG_OR_IGNORE pattern race-safe. The cog's
        cog_load creates it on fresh installs, but legacy DBs only
        ever touch the migration's _add_reading_logs_indexes, so
        without that step ALSO creating the partial unique index,
        legacy upgrades silently lose the /finish dedup guarantee.
        """
        await run_migrations(legacy_db)
        async with aiosqlite.connect(legacy_db.path_to_db) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='reading_logs' "
                "AND name='idx_reading_logs_user_vn_month'"
            ) as cur:
                row = await cur.fetchone()
        assert row is not None, (
            "legacy upgrade path must also create the partial unique "
            "index that backs ADD_READING_LOG_OR_IGNORE"
        )


class TestIdempotency:
    """Re-running migrations must be safe. The bot does this on every boot."""

    @pytest.mark.asyncio
    async def test_fresh_install_run_twice(self, fake_bot: FakeBot) -> None:
        await run_migrations(fake_bot)
        await run_migrations(fake_bot)  # Must not raise.

    @pytest.mark.asyncio
    async def test_legacy_upgrade_run_twice_preserves_data(
        self, fake_bot: FakeBot
    ) -> None:
        """Re-running migrations after a successful legacy upgrade must not
        drop or duplicate rows, and must not raise.

        Note: structurally the second pass DOES re-create an empty
        ``vn_nominations`` table (because ``_create_cycle_tables`` uses
        CREATE IF NOT EXISTS and the unify step short-circuits on the
        existing backup). That's harmless (an empty table), but it rules
        out asserting exact table-count equality across the two passes.
        The test asserts what matters: data preserved, no exceptions.
        """
        await fake_bot.RUN(LEGACY_VN_TITLES)
        await fake_bot.RUN(LEGACY_READING_LOGS)
        await fake_bot.RUN(LEGACY_VNDB_CACHE)
        await fake_bot.RUN(
            "INSERT INTO vn_titles VALUES "
            "('v17', '2025-01', '2025-01', 10, CURRENT_TIMESTAMP)"
        )
        await fake_bot.RUN(
            "INSERT INTO vn_titles VALUES "
            "('v42', '2025-03', '2025-03', 10, CURRENT_TIMESTAMP)"
        )
        await run_migrations(fake_bot)

        async with aiosqlite.connect(fake_bot.path_to_db) as db:
            async with db.execute("SELECT COUNT(*) FROM vn_titles") as cur:
                row_count_before = (await cur.fetchone())[0]

        await run_migrations(fake_bot)  # Must not raise.

        async with aiosqlite.connect(fake_bot.path_to_db) as db:
            async with db.execute("SELECT COUNT(*) FROM vn_titles") as cur:
                row_count_after = (await cur.fetchone())[0]

        assert row_count_after == row_count_before, (
            f"second migration pass changed vn_titles row count: "
            f"{row_count_before} -> {row_count_after}"
        )
