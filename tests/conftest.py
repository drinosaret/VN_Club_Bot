"""Shared test fixtures.

The bot interacts with SQLite via a handful of methods on the Bot object:
``RUN``, ``RUN_RETURNING_ID``, ``RUN_RETURNING_ROWCOUNT``,
``RUN_TRANSACTION``, ``GET``, and ``GET_ONE``. Every
SQL-touching test uses the ``fake_bot`` fixture, which provides those
methods against a fresh temporary SQLite file. The file path is wired into
``path_to_db`` so production code that opens its own aiosqlite connection
(notably the migration helpers) hits the same DB.

Tests run with ``asyncio_mode = auto`` (see pytest.ini), so test functions
can be plain ``async def``.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Tuple

import aiosqlite
import pytest

# Make ``import lib.x`` and ``import cogs.x`` work when pytest runs from the
# repo root or anywhere else. The bot's own modules assume the repo root is
# on sys.path because main.py is run from there.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class FakeBot:
    """Minimal stand-in for ``lib.bot.VNClubBot`` for SQL-touching tests.

    Exposes only the DB methods (`RUN`, `GET`, ...); the migrations and
    SQL helpers don't care about anything else.
    """

    def __init__(self, path_to_db: str) -> None:
        self.path_to_db = path_to_db

    async def RUN(self, query: str, params: tuple = ()) -> None:
        async with aiosqlite.connect(self.path_to_db) as db:
            await db.execute(query, params)
            await db.commit()

    async def RUN_RETURNING_ID(self, query: str, params: tuple = ()) -> int:
        async with aiosqlite.connect(self.path_to_db) as db:
            cur = await db.execute(query, params)
            new_id = cur.lastrowid
            await db.commit()
            return new_id

    async def RUN_RETURNING_ROWCOUNT(self, query: str, params: tuple = ()) -> int:
        async with aiosqlite.connect(self.path_to_db) as db:
            cur = await db.execute(query, params)
            changed = cur.rowcount
            await db.commit()
            return changed

    async def RUN_TRANSACTION(
        self, statements: list[Tuple[str, tuple]]
    ) -> None:
        async with aiosqlite.connect(self.path_to_db) as db:
            try:
                for q, p in statements:
                    await db.execute(q, p)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def GET(self, query: str, params: tuple = ()) -> list[Any]:
        async with aiosqlite.connect(self.path_to_db) as db:
            async with db.execute(query, params) as cur:
                return await cur.fetchall()

    async def GET_ONE(self, query: str, params: tuple = ()) -> Any:
        async with aiosqlite.connect(self.path_to_db) as db:
            async with db.execute(query, params) as cur:
                return await cur.fetchone()


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """A throwaway SQLite filename for one test. The file isn't created
    upfront; aiosqlite creates it on first connect.
    """
    return str(tmp_path / "test.sqlite3")


@pytest.fixture
def fake_bot(tmp_db_path: str) -> FakeBot:
    """A FakeBot pointing at a fresh DB for one test."""
    return FakeBot(tmp_db_path)


@pytest.fixture(autouse=True)
def _fresh_reply_visibility_cache():
    """lib.visibility caches per guild id in the process; tests reuse ids
    across fresh databases."""
    from lib import visibility
    visibility._cache.clear()
    yield
    visibility._cache.clear()
