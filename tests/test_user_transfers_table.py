from __future__ import annotations

import aiosqlite

from lib.migrations import run_migrations
from tests.conftest import FakeBot


async def _columns(path, table):
    async with aiosqlite.connect(path) as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            return [r[1] for r in await cur.fetchall()]


async def test_migration_creates_user_transfers(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    assert await _columns(fake_bot.path_to_db, "user_transfers") == [
        "id", "from_user_id", "to_user_id", "performed_at",
        "moved_json", "left_json", "undone_at", "undo_json",
    ]


async def test_migration_is_idempotent_and_keeps_rows(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await fake_bot.RUN(
        "INSERT INTO user_transfers (from_user_id, to_user_id, moved_json, left_json) "
        "VALUES (1, 2, '{}', '{}')"
    )
    await run_migrations(fake_bot)
    rows = await fake_bot.GET("SELECT from_user_id, to_user_id FROM user_transfers")
    assert rows == [(1, 2)]
