"""SQL behavior tests for the ``guild_managers`` table.

`guild_managers` is the per-(guild, principal) bridge table that powers
the per-server manager model. The slash command `/manage_managers` is
the only writer; `validate_user_permission` is the only reader. Tests
here cover the SQL constants directly so the contract is locked in
independent of the cog wiring.
"""
from __future__ import annotations

import aiosqlite
import pytest

from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from tests.conftest import FakeBot


@pytest.fixture
async def schema_bot(fake_bot: FakeBot) -> FakeBot:
    """Run migrations so guild_managers exists with its full shape."""
    await run_migrations(fake_bot)
    return fake_bot


class TestInsertGuildManager:
    @pytest.mark.asyncio
    async def test_insert_user(self, schema_bot: FakeBot) -> None:
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER,
            (100, "user", 42, 999),
        )
        row = await schema_bot.GET_ONE(
            "SELECT principal_type, principal_id, added_by_user_id "
            "FROM guild_managers WHERE guild_id = ?",
            (100,),
        )
        assert row == ("user", 42, 999)

    @pytest.mark.asyncio
    async def test_insert_role(self, schema_bot: FakeBot) -> None:
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER,
            (100, "role", 7, 999),
        )
        row = await schema_bot.GET_ONE(
            "SELECT principal_type, principal_id FROM guild_managers WHERE guild_id = ?",
            (100,),
        )
        assert row == ("role", 7)

    @pytest.mark.asyncio
    async def test_insert_or_ignore_duplicate(self, schema_bot: FakeBot) -> None:
        """INSERT OR IGNORE makes re-adding a friendly no-op rather than
        an error, so the slash command can safely re-add without checking.
        """
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 42, 999),
        )
        # A second insert with a different added_by_user_id should be ignored.
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 42, 1234),
        )
        rows = await schema_bot.GET(
            "SELECT added_by_user_id FROM guild_managers "
            "WHERE guild_id = ? AND principal_type = 'user' AND principal_id = ?",
            (100, 42),
        )
        assert len(rows) == 1
        assert rows[0][0] == 999  # original added_by preserved

    @pytest.mark.asyncio
    async def test_invalid_principal_type_silently_ignored(
        self, schema_bot: FakeBot,
    ) -> None:
        """The CHECK constraint exists as a defense-in-depth declaration,
        but INSERT OR IGNORE swallows constraint violations by design
        (its primary job is making duplicate inserts no-op). So a bad
        principal_type doesn't raise here; it just doesn't insert.
        The slash command validates inputs upstream so this path
        shouldn't fire in practice.
        """
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "channel", 42, 999),
        )
        # Row count for the invalid principal_type stays zero.
        row = await schema_bot.GET_ONE(
            "SELECT COUNT(*) FROM guild_managers WHERE principal_type = 'channel'"
        )
        assert row[0] == 0


class TestDeleteGuildManager:
    @pytest.mark.asyncio
    async def test_delete_existing(self, schema_bot: FakeBot) -> None:
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 42, 999),
        )
        await schema_bot.RUN(
            DatabaseQueries.DELETE_GUILD_MANAGER, (100, "user", 42),
        )
        row = await schema_bot.GET_ONE(
            "SELECT 1 FROM guild_managers WHERE guild_id = ? AND principal_id = ?",
            (100, 42),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_no_error(self, schema_bot: FakeBot) -> None:
        # Deleting a row that was never there is fine; the slash command
        # handles "wasn't a manager" UX by pre-checking, not by catching.
        await schema_bot.RUN(
            DatabaseQueries.DELETE_GUILD_MANAGER, (100, "user", 42),
        )

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_other_guilds(
        self, schema_bot: FakeBot,
    ) -> None:
        # Same user added in two guilds; deleting from one preserves the other.
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 42, 999),
        )
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (200, "user", 42, 999),
        )
        await schema_bot.RUN(
            DatabaseQueries.DELETE_GUILD_MANAGER, (100, "user", 42),
        )
        # Guild 100 entry gone, guild 200 entry preserved.
        assert await schema_bot.GET_ONE(
            "SELECT 1 FROM guild_managers WHERE guild_id = ? AND principal_id = ?",
            (100, 42),
        ) is None
        assert await schema_bot.GET_ONE(
            "SELECT 1 FROM guild_managers WHERE guild_id = ? AND principal_id = ?",
            (200, 42),
        ) is not None


class TestListGuildManagers:
    @pytest.mark.asyncio
    async def test_list_orders_by_added_at(self, schema_bot: FakeBot) -> None:
        # Inserts ordered by time; LIST should return them in insertion order.
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 1, 999),
        )
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 2, 999),
        )
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "role", 7, 999),
        )
        rows = await schema_bot.GET(
            DatabaseQueries.LIST_GUILD_MANAGERS, (100,),
        )
        assert len(rows) == 3
        # First two are users, third is the role.
        assert (rows[0][0], rows[0][1]) == ("user", 1)
        assert (rows[1][0], rows[1][1]) == ("user", 2)
        assert (rows[2][0], rows[2][1]) == ("role", 7)

    @pytest.mark.asyncio
    async def test_list_empty_guild(self, schema_bot: FakeBot) -> None:
        rows = await schema_bot.GET(
            DatabaseQueries.LIST_GUILD_MANAGERS, (100,),
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_list_scoped_to_guild(self, schema_bot: FakeBot) -> None:
        # Inserts in guild A shouldn't appear when listing guild B.
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 1, 999),
        )
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (200, "user", 2, 999),
        )
        rows_a = await schema_bot.GET(
            DatabaseQueries.LIST_GUILD_MANAGERS, (100,),
        )
        rows_b = await schema_bot.GET(
            DatabaseQueries.LIST_GUILD_MANAGERS, (200,),
        )
        assert [r[1] for r in rows_a] == [1]
        assert [r[1] for r in rows_b] == [2]


class TestSchema:
    @pytest.mark.asyncio
    async def test_table_exists_after_migrations(self, schema_bot: FakeBot) -> None:
        # Fresh-install run_migrations should create the table.
        row = await schema_bot.GET_ONE(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_managers'"
        )
        assert row is not None

    @pytest.mark.asyncio
    async def test_index_exists(self, schema_bot: FakeBot) -> None:
        # The (guild_id) index speeds up validate_user_permission's lookup.
        row = await schema_bot.GET_ONE(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_guild_managers_guild'"
        )
        assert row is not None

    @pytest.mark.asyncio
    async def test_columns_present(self, schema_bot: FakeBot) -> None:
        async with aiosqlite.connect(schema_bot.path_to_db) as db:
            async with db.execute("PRAGMA table_info(guild_managers)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
        for expected in (
            "guild_id", "principal_type", "principal_id",
            "added_by_user_id", "added_at",
        ):
            assert expected in cols, f"guild_managers missing column {expected}"
