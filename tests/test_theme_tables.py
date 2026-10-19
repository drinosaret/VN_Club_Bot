# tests/test_theme_tables.py
from __future__ import annotations
import json
import aiosqlite
import pytest
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from tests.conftest import FakeBot


async def _tables(path):
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            return {r[0] for r in await cur.fetchall()}


async def test_migration_creates_theme_tables(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    names = await _tables(fake_bot.path_to_db)
    assert "theme_templates" in names
    assert "theme_assignments" in names


async def test_template_insert_and_list(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    rules = json.dumps({"schema_version": 1})
    tid = await fake_bot.RUN_RETURNING_ID(
        DatabaseQueries.INSERT_THEME_TEMPLATE, (123, "Mystery Month", rules, 5))
    assert tid > 0
    rows = await fake_bot.GET(DatabaseQueries.LIST_THEME_TEMPLATES, (123,))
    assert len(rows) == 1 and rows[0][1] == "Mystery Month"
    # Per-guild isolation: another guild sees nothing.
    assert await fake_bot.GET(DatabaseQueries.LIST_THEME_TEMPLATES, (999,)) == []


async def test_template_name_unique_per_guild(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    rules = json.dumps({"schema_version": 1})
    await fake_bot.RUN_RETURNING_ID(DatabaseQueries.INSERT_THEME_TEMPLATE, (1, "A", rules, 5))
    with pytest.raises(aiosqlite.IntegrityError):
        await fake_bot.RUN_RETURNING_ID(DatabaseQueries.INSERT_THEME_TEMPLATE, (1, "A", rules, 5))


async def test_assignment_upsert_and_get(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    rules = json.dumps({"schema_version": 1, "nsfw_cover": "disallow"})
    await fake_bot.RUN(DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
                       (7, "monthly", "2026-08", "2026-08", "KEY Month", rules, None, 5))
    row = await fake_bot.GET_ONE(DatabaseQueries.GET_THEME_ASSIGNMENT_FOR_PERIOD,
                                 (7, "monthly", "2026-08", "2026-08"))
    assert row[0] == "KEY Month"
    assert json.loads(row[1])["nsfw_cover"] == "disallow"
    # Upsert replaces the snapshot for the same period key.
    rules2 = json.dumps({"schema_version": 1, "nsfw_cover": "allow"})
    await fake_bot.RUN(DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
                       (7, "monthly", "2026-08", "2026-08", "KEY Month v2", rules2, None, 5))
    row2 = await fake_bot.GET_ONE(DatabaseQueries.GET_THEME_ASSIGNMENT_FOR_PERIOD,
                                  (7, "monthly", "2026-08", "2026-08"))
    assert row2[0] == "KEY Month v2"


async def test_assignment_isolated_by_guild_and_kind(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    rules = json.dumps({"schema_version": 1})
    await fake_bot.RUN(DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
                       (7, "monthly", "2026-08", "2026-08", "M", rules, None, 5))
    # Different guild -> nothing.
    assert await fake_bot.GET_ONE(DatabaseQueries.GET_THEME_ASSIGNMENT_FOR_PERIOD,
                                  (8, "monthly", "2026-08", "2026-08")) is None
    # Different kind, same window -> nothing (lanes independent).
    assert await fake_bot.GET_ONE(DatabaseQueries.GET_THEME_ASSIGNMENT_FOR_PERIOD,
                                  (7, "seasonal", "2026-08", "2026-08")) is None
