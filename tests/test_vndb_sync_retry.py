"""A VNDB sync that fails because VNDB is unreachable is retried after the
shorter retry window, and a link that has never fetched anything keeps
reading as syncing meanwhile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import cogs.vndb_links as links_mod
from lib import vndb_links as vl
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from tests.test_rating_scale_commands import CogBot, FakeInteraction, FakeUser

GUILD = 4242
ALICE, BOB = 101, 102


@pytest.fixture
async def bot(tmp_db_path):
    b = CogBot(tmp_db_path)
    await run_migrations(b)
    await b.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    await b.RUN(
        "CREATE TABLE IF NOT EXISTS users (discord_user_id INTEGER PRIMARY KEY, "
        "user_name TEXT, user_tag TEXT)"
    )
    for uid, name in ((ALICE, "alice"), (BOB, "bob")):
        await b.RUN("INSERT INTO users (discord_user_id, user_name, user_tag) VALUES (?, ?, ?)",
                    (uid, name, name))
    return b


def _down(monkeypatch):
    async def boom(*a, **k):
        raise vl.VndbUnavailable("down")
    monkeypatch.setattr(vl, "_request", boom)


def _up(monkeypatch, uid="u10", entries=()):
    async def fake_request(method, path, *, params=None, json=None):
        if path == "/user":
            return {params["q"]: {"id": uid, "username": "n"}}
        return {"results": list(entries), "more": False}
    monkeypatch.setattr(vl, "_request", fake_request)


async def _link(bot, user_id=ALICE, uid="u10"):
    await bot.RUN(vl.UPSERT_LINK, (user_id, uid, f"n{user_id}", GUILD))


async def _state(bot, user_id=ALICE):
    return await bot.GET_ONE(
        "SELECT synced_at, sync_error FROM vndb_links WHERE user_id = ?", (user_id,)
    )


async def _due(bot):
    return await bot.GET(vl.DUE_FOR_SYNC, (f"-{vl.SYNC_INTERVAL_HOURS} hours", 10))


class TestRetryTiming:
    async def test_unavailable_is_due_again_after_the_retry_window(self, bot, monkeypatch):
        await _link(bot)
        _down(monkeypatch)
        assert await vl.sync_user(bot, ALICE, "u10") is False
        synced_at, err = await _state(bot)
        assert err == "unavailable"
        # Backdated by the interval less the retry window, give or take a minute.
        stamp = datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S")
        expected = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=vl.SYNC_INTERVAL_HOURS - vl.UNAVAILABLE_RETRY_HOURS
        )
        assert abs((stamp - expected).total_seconds()) < 60
        assert await _due(bot) == []
        await bot.RUN(
            "UPDATE vndb_links SET synced_at = datetime(synced_at, ?) WHERE user_id = ?",
            (f"-{vl.UNAVAILABLE_RETRY_HOURS * 60 + 1} minutes", ALICE),
        )
        assert await _due(bot) == [(ALICE, "u10")]

    async def test_account_not_found_still_waits_the_full_interval(self, bot, monkeypatch):
        await _link(bot)

        async def no_user(method, path, *, params=None, json=None):
            return {params["q"]: None}

        monkeypatch.setattr(vl, "_request", no_user)
        await vl.sync_user(bot, ALICE, "u10")
        _, err = await _state(bot)
        assert err == "account_not_found"
        await bot.RUN(
            "UPDATE vndb_links SET synced_at = datetime(synced_at, '-2 hours') WHERE user_id = ?",
            (ALICE,),
        )
        assert await _due(bot) == []

    async def test_unavailable_after_relink_writes_nothing(self, bot, monkeypatch):
        await _link(bot, uid="u10")

        async def relink_then_fail(method, path, *, params=None, json=None):
            # The member links another account while the old sync is running.
            await bot.RUN(vl.UPSERT_LINK, (ALICE, "u20", "other", GUILD))
            raise vl.VndbUnavailable("down")

        monkeypatch.setattr(vl, "_request", relink_then_fail)
        await vl.sync_user(bot, ALICE, "u10")
        assert await _state(bot) == (None, None)

    async def test_success_after_unavailable_clears_the_error(self, bot, monkeypatch):
        await _link(bot)
        _down(monkeypatch)
        await vl.sync_user(bot, ALICE, "u10")
        _up(monkeypatch)
        assert await vl.sync_user(bot, ALICE, "u10") is True
        synced_at, err = await _state(bot)
        assert err is None and synced_at is not None
        assert (await bot.GET_ONE(vl.LINKED_IN_SCOPE, (None, None, None)))[1] == 0
        # A successful sync with an empty list counts as synced, at zero.
        detail = await bot.GET_ONE(vl.GET_LINK_DETAIL, (ALICE,))
        assert detail[3] is not None


class TestPendingDisplay:
    async def test_never_synced_failure_reads_as_syncing(self, bot, monkeypatch):
        await _link(bot)
        _down(monkeypatch)
        await vl.sync_user(bot, ALICE, "u10")
        assert await bot.GET_ONE(vl.LINKED_IN_SCOPE, (None, None, None)) == (1, 1)
        assert (await bot.GET_ONE(vl.GET_LINK_DETAIL, (ALICE,)))[3] is None
        for cat in vl.USER_CATEGORIES:
            rows = await bot.GET(vl.leaderboard_query(cat, None), (None, None, None, None, None))
            assert rows[0][4] is None
        cog = links_mod.VndbLinks(bot)
        inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
        await links_mod.VndbLinks.user_leaderboard.callback(
            cog, inter, category=None, timeframe=None, server=None,
        )
        text = inter.text()
        assert "list syncing" in text and "0 finished" not in text

    async def test_failure_after_a_good_sync_keeps_showing_the_cache(self, bot, monkeypatch):
        await _link(bot)
        _up(monkeypatch, entries=[{"id": "v1", "labels": [{"id": 2}]}])
        assert await vl.sync_user(bot, ALICE, "u10") is True
        _down(monkeypatch)
        await vl.sync_user(bot, ALICE, "u10")
        assert (await _state(bot))[1] == "unavailable"
        assert await bot.GET_ONE(vl.LINKED_IN_SCOPE, (None, None, None)) == (1, 0)
        rows = await bot.GET(vl.leaderboard_query("most_finished", None), (None, None, None, None, None))
        assert rows[0][2] == 1 and rows[0][4] is not None

    async def test_pending_count_is_per_link(self, bot, monkeypatch):
        await _link(bot, ALICE, "u10")
        await _link(bot, BOB, "u20")
        await bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (BOB,))
        _down(monkeypatch)
        await vl.sync_user(bot, ALICE, "u10")
        assert await bot.GET_ONE(vl.LINKED_IN_SCOPE, (None, None, None)) == (2, 1)


class TestRelinkSameAccount:
    async def _relink(self, bot, monkeypatch):
        async def found(_query):
            return vl.VndbAccount(uid="u10", username="n")

        monkeypatch.setattr(vl, "lookup_account", found)
        cog = links_mod.VndbLinks(bot)
        spawned = []
        monkeypatch.setattr(cog, "_spawn_sync", lambda uid, vuid: spawned.append((uid, vuid)))
        inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
        await cog._link(inter, "u10")
        return inter.text(), spawned

    async def test_recent_failed_attempt_refreshes_now(self, bot, monkeypatch):
        await _link(bot)
        # A failed attempt a moment ago, with an earlier list still cached.
        await bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id) VALUES (?, 'v1')", (ALICE,))
        await bot.RUN(
            "UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP, sync_error = 'unavailable' "
            "WHERE user_id = ?", (ALICE,),
        )
        text, spawned = await self._relink(bot, monkeypatch)
        assert "Refreshing your list now." in text
        assert spawned == [(ALICE, "u10")]

    async def test_after_backdated_failure_refreshes_now(self, bot, monkeypatch):
        await _link(bot)
        _down(monkeypatch)
        await vl.sync_user(bot, ALICE, "u10")
        text, spawned = await self._relink(bot, monkeypatch)
        assert "Refreshing your list now." in text
        assert spawned == [(ALICE, "u10")]

    async def test_recent_success_still_waits(self, bot, monkeypatch):
        await _link(bot)
        await bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = ?", (ALICE,))
        text, spawned = await self._relink(bot, monkeypatch)
        assert "refreshed a few minutes ago" in text
        assert spawned == []
