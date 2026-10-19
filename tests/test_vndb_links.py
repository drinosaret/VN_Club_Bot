"""VNDB link parsing, list sync (against a stubbed API) and the report
queries behind /reviews and /vndb leaderboard."""
from __future__ import annotations

import pytest

from lib import vndb_links as vl
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from tests.conftest import FakeBot


class TestParseAccountInput:
    @pytest.mark.parametrize("raw,expected", [
        ("someuser", "someuser"),
        ("  some-user_2 ", "some-user_2"),
        ("u123", "u123"),
        ("U123", "u123"),
        ("https://vndb.org/u4567", "u4567"),
        ("vndb.org/u89/ulist?vnlist=1", "u89"),
    ])
    def test_accepted(self, raw, expected):
        assert vl.parse_account_input(raw) == expected

    @pytest.mark.parametrize("raw", ["", "a", "has space", "x" * 40, "https://example.invalid/u1", "<@123>"])
    def test_rejected(self, raw):
        assert vl.parse_account_input(raw) is None


class TestParseUlistEntry:
    def test_full_entry(self):
        out = vl.parse_ulist_entry({
            "id": "v17", "vote": 85, "voted": 1_700_000_000, "finished": "2024-03-01",
            "notes": "  loved it  ", "lastmod": 5, "labels": [{"id": 2}, {"id": 7}],
            "vn": {"title": "Romaji", "alttitle": "原題"},
        })
        assert out["vndb_id"] == "v17"
        assert out["vote"] == 85
        assert out["voted"] == "2023-11-14"
        assert out["is_finished"] == 1
        assert out["notes"] == "loved it"
        assert out["alttitle"] == "原題"

    def test_bad_values_dropped(self):
        out = vl.parse_ulist_entry({
            "id": "v1", "vote": 500, "voted": "soon", "finished": "yesterday",
            "notes": "", "labels": [{"id": 1}],
        })
        assert out["vote"] is None and out["voted"] is None and out["finished"] is None
        assert out["notes"] is None and out["is_finished"] == 0

    def test_note_capped(self):
        out = vl.parse_ulist_entry({"id": "v1", "notes": "x" * (vl.NOTE_LIMIT + 50)})
        assert len(out["notes"]) == vl.NOTE_LIMIT

    def test_rejects_non_vn_id(self):
        assert vl.parse_ulist_entry({"id": "r12"}) is None
        assert vl.parse_ulist_entry({}) is None


@pytest.fixture
async def link_bot(fake_bot: FakeBot) -> FakeBot:
    await run_migrations(fake_bot)
    await fake_bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    await fake_bot.RUN(
        "CREATE TABLE IF NOT EXISTS users (discord_user_id INTEGER PRIMARY KEY, "
        "user_name TEXT, user_tag TEXT)"
    )
    return fake_bot


async def _link(bot, user_id, uid, name, guild=None):
    await bot.RUN(vl.UPSERT_LINK, (user_id, uid, name, guild))


def _stub_api(monkeypatch, users: dict, lists: dict, calls: list | None = None):
    """users: query -> account dict or None; lists: uid -> list of pages."""
    async def fake_request(method, path, *, params=None, json=None):
        if calls is not None:
            calls.append((method, path))
        if path == "/user":
            q = params["q"]
            return {q: users.get(q)}
        if path == "/ulist":
            pages = lists.get(json["user"])
            if pages is None:
                return None
            idx = json["page"] - 1
            return {"results": pages[idx], "more": idx + 1 < len(pages)}
        raise AssertionError(path)
    monkeypatch.setattr(vl, "_request", fake_request)


class TestSync:
    async def test_sync_replaces_rows_and_titles(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "oldname")
        await link_bot.RUN(
            "INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (1, 'v999', 50)"
        )
        _stub_api(
            monkeypatch,
            users={"u10": {"id": "u10", "username": "newname"}},
            lists={"u10": [
                [{"id": "v1", "vote": 90, "labels": [{"id": 2}], "vn": {"title": "A"}}],
                [{"id": "v2", "notes": "hm", "labels": [{"id": 4}], "vn": {"title": "B"}}],
            ]},
        )
        assert await vl.sync_user(link_bot, 1, "u10") is True
        rows = await link_bot.GET("SELECT vndb_id, vote, notes, is_finished FROM vndb_ulist ORDER BY vndb_id")
        assert rows == [("v1", 90, None, 1), ("v2", None, "hm", 0)]
        (name, synced) = await link_bot.GET_ONE(
            "SELECT vndb_username, synced_at FROM vndb_links WHERE user_id = 1"
        )
        assert name == "newname" and synced is not None
        assert await link_bot.GET_ONE("SELECT title FROM vndb_vn_titles WHERE vndb_id = 'v2'") == ("B",)

    async def test_sync_after_unlink_writes_nothing(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "n")

        async def fake_request(method, path, *, params=None, json=None):
            if path == "/user":
                return {params["q"]: {"id": "u10", "username": "n"}}
            # The member unlinks while the list is being fetched.
            await link_bot.RUN(vl.DELETE_LINK, (1,))
            return {"results": [{"id": "v1", "vote": 90}], "more": False}

        monkeypatch.setattr(vl, "_request", fake_request)
        await vl.sync_user(link_bot, 1, "u10")
        assert await link_bot.GET("SELECT * FROM vndb_ulist") == []

    async def test_account_gone_marks_error(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "n")
        _stub_api(monkeypatch, users={}, lists={})
        assert await vl.sync_user(link_bot, 1, "u10") is False
        (err,) = await link_bot.GET_ONE("SELECT sync_error FROM vndb_links WHERE user_id = 1")
        assert err == "account_not_found"

    async def test_unavailable_leaves_cache_untouched(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "n")
        await link_bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (1, 'v5', 70)")

        async def boom(*a, **k):
            raise vl.VndbUnavailable("down")

        monkeypatch.setattr(vl, "_request", boom)
        assert await vl.sync_user(link_bot, 1, "u10") is False
        assert await link_bot.GET("SELECT vndb_id FROM vndb_ulist") == [("v5",)]

    async def test_unavailable_records_attempt(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "n")

        async def boom(*a, **k):
            raise vl.VndbUnavailable("down")

        monkeypatch.setattr(vl, "_request", boom)
        await vl.sync_user(link_bot, 1, "u10")
        err, synced = await link_bot.GET_ONE(
            "SELECT sync_error, synced_at FROM vndb_links WHERE user_id = 1"
        )
        assert err == "unavailable" and synced is not None
        # A failed user no longer heads the due queue.
        assert await link_bot.GET(vl.DUE_FOR_SYNC, ("-24 hours", 10)) == []

    async def test_account_gone_clears_cached_entries(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "n")
        await link_bot.RUN("INSERT INTO vndb_ulist (user_id, vndb_id, vote) VALUES (1, 'v5', 70)")
        _stub_api(monkeypatch, users={}, lists={})
        await vl.sync_user(link_bot, 1, "u10")
        assert await link_bot.GET("SELECT * FROM vndb_ulist") == []

    async def test_stale_queued_sync_makes_no_requests(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u20", "relinked")
        calls = []
        _stub_api(monkeypatch, users={}, lists={}, calls=calls)
        assert await vl.sync_user(link_bot, 1, "u10") is False
        assert calls == []

    async def test_malformed_uid_rejected(self, monkeypatch):
        async def fake_request(method, path, *, params=None, json=None):
            return {params["q"]: {"id": "u1/../x", "username": "n"}}

        monkeypatch.setattr(vl, "_request", fake_request)
        with pytest.raises(vl.VndbUnavailable):
            await vl.lookup_account("someone")

    async def test_sync_due_stops_on_throttle(self, link_bot, monkeypatch):
        await _link(link_bot, 1, "u10", "a")
        await _link(link_bot, 2, "u20", "b")
        calls = []

        async def throttled(method, path, **k):
            calls.append(path)
            raise vl.VndbThrottled("429")

        monkeypatch.setattr(vl, "_request", throttled)
        assert await vl.sync_due(link_bot) == 0
        assert len(calls) == 1

    async def test_due_query_skips_fresh(self, link_bot):
        await _link(link_bot, 1, "u10", "a")
        await _link(link_bot, 2, "u20", "b")
        await link_bot.RUN("UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP WHERE user_id = 2")
        due = await link_bot.GET(vl.DUE_FOR_SYNC, ("-24 hours", 10))
        assert due == [(1, "u10")]

    async def test_one_account_one_discord_user(self, link_bot):
        await _link(link_bot, 1, "u10", "a")
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await _link(link_bot, 2, "u10", "a")


async def _entry(bot, user_id, vid, vote=None, finished=None, is_finished=0, notes=None, voted=None):
    await bot.RUN(
        "INSERT INTO vndb_ulist (user_id, vndb_id, vote, voted, finished, is_finished, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, vid, vote, voted, finished, is_finished, notes),
    )


class TestLeaderboards:
    @pytest.fixture
    async def board(self, link_bot):
        await _link(link_bot, 1, "u1", "alice", guild=100)
        await _link(link_bot, 2, "u2", "bob", guild=200)
        await _link(link_bot, 3, "u3", "cara", guild=None)
        for vid in ("v1", "v2", "v3"):
            await _entry(link_bot, 1, vid, vote=80, is_finished=1, finished="2025-05-01", voted="2025-05-01")
        await _entry(link_bot, 2, "v1", vote=100, is_finished=1, finished="2026-01-10", voted="2026-01-10")
        await _entry(link_bot, 3, "v1", vote=90, is_finished=1, finished="2026-02-01", voted="2026-02-01")
        await _entry(link_bot, 3, "v4", notes="wishlisted with a note")
        # Cara logged a read in guild 100, so she counts there too.
        await link_bot.RUN(
            DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
            (3, "v1", 8, 10, "As Normal VN", "2026-02", 6, "c", 100),
        )
        return link_bot

    async def _run(self, bot, cat, since=None, guild=None):
        return await bot.GET(vl.leaderboard_query(cat, since), (since, since, guild, guild, guild))

    async def test_most_finished(self, board):
        rows = await self._run(board, "most_finished")
        assert [(r[1], r[2]) for r in rows] == [("alice", 3), ("bob", 1), ("cara", 1)]

    async def test_most_finished_this_year(self, board):
        rows = await self._run(board, "most_finished", since="2026-01-01")
        # Every linked member comes back; only bob and cara finished this year.
        assert {r[1] for r in rows if r[2] > 0} == {"bob", "cara"}
        assert {r[1]: r[2] for r in rows}["alice"] == 0

    async def test_server_scope_uses_link_guild_or_logs(self, board):
        rows = await self._run(board, "most_finished", guild=100)
        assert {r[1] for r in rows} == {"alice", "cara"}

    async def test_most_votes(self, board):
        rows = await self._run(board, "most_votes")
        assert rows[0][1] == "alice" and rows[0][2] == 3

    async def test_most_read(self, board):
        rows = await self._run(board, "most_read")
        assert rows[0][0] == "v1" and rows[0][3] == 3

    async def test_top_rated_needs_min_votes(self, board):
        rows = await self._run(board, "top_rated")
        assert [r[0] for r in rows] == ["v1"]
        assert rows[0][4] == pytest.approx(90.0)

    async def test_unlinked_users_drop_out(self, board):
        await board.RUN(vl.DELETE_LINK, (1,))
        rows = await self._run(board, "most_finished")
        assert "alice" not in {r[1] for r in rows}

    def test_unknown_category(self):
        with pytest.raises(ValueError):
            vl.leaderboard_query("nope", None)

    async def test_impression_queries(self, board):
        rows = await board.GET(vl.VN_IMPRESSIONS, ("v1",))
        assert {r[1] for r in rows} == {"alice", "bob", "cara"}
        rows = await board.GET(vl.USER_IMPRESSIONS, (3,))
        # The noted entry sorts ahead of vote-only ones.
        assert rows[0][0] == "v4"


def test_vote_format():
    assert vl.format_vndb_vote(85) == "8.5/10"
    assert vl.format_vndb_vote(None) is None


class TestPackPages:
    def test_respects_budget_and_count(self):
        from cogs.vndb_links import pack_pages
        blocks = ["a" * 40] * 10
        pages = pack_pages(blocks, budget=100, max_per_page=5, separator="\n\n")
        assert all(len(p) <= 100 for p in pages)
        assert sum(p.count("a" * 40) for p in pages) == 10

    def test_count_cap(self):
        from cogs.vndb_links import pack_pages
        pages = pack_pages(["x"] * 12, budget=1000, max_per_page=5, separator="\n")
        assert [p.count("x") for p in pages] == [5, 5, 2]

    def test_oversized_block_gets_own_page(self):
        from cogs.vndb_links import pack_pages
        pages = pack_pages(["s", "L" * 500, "t"], budget=100, max_per_page=5, separator="\n")
        assert len(pages) == 3
        assert len(pages[1]) == 100 and pages[1].endswith("…")

    def test_review_sized_blocks_fit_embed(self):
        from cogs.vndb_links import pack_pages
        from lib.utils import EMBED_DESCRIPTION_BUFFER, MAX_EMBED_DESCRIPTION
        # An escaped 500-character comment doubles in length.
        block = "**name** · Hikaru 8/10\n💬 " + "\\*" * 500 + "\n📝 " + "n" * 300
        pages = pack_pages([block] * 7, MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER - 80, 5, "\n\n")
        assert all(len(p) <= MAX_EMBED_DESCRIPTION - EMBED_DESCRIPTION_BUFFER for p in pages)
        assert sum(p.count("**name**") for p in pages) == 7
