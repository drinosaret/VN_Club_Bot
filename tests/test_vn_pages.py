"""VN pages: spoiler-safe field caps, bare numeric VN ids in reading logs,
and newest-first ordering under a single-source filter."""

from __future__ import annotations

import pytest

import cogs.vn_pages as pages_mod
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries
from lib.vndb_api import CREATE_VNDB_CACHE_TABLE, VN_Entry
from tests.conftest import FakeBot

MEMBER = 1001
OTHER = 1002
GUILD = 5001


# ----------------------------------------------------------------- _cap --

def test_cap_leaves_short_text_untouched():
    text = "short ||hidden|| text"
    assert pages_mod._cap(text, 256) == text


def test_cap_drops_spoiler_left_open_by_the_cut():
    text = "visible " + "||" + "x" * 50 + "||"
    out = pages_mod._cap(text, 20)
    assert out.endswith("…")
    assert out.count("||") % 2 == 0
    assert "x" not in out
    assert len(out) <= 20


def test_cap_keeps_closed_spoiler_before_the_cut():
    text = "||a|| " + "y" * 50
    out = pages_mod._cap(text, 20)
    assert out.startswith("||a||")
    assert out.endswith("…")
    assert len(out) <= 20


def test_field_pages_never_leave_spoiler_open():
    name = "n" * 250 + "||secret||"
    value = "v" * 1020 + "||secret||"
    [[(n, v)]] = pages_mod.pack_field_pages([(name, value)], per_page=5, budget=6000)
    assert n.count("||") % 2 == 0 and v.count("||") % 2 == 0
    assert "secret" not in n and "secret" not in v


# ------------------------------------------------------- source filter --

def test_filter_source_orders_by_that_source_only():
    e = pages_mod._new_entry()
    pages_mod._touch(e, "2026-01-05", "log_at")
    pages_mod._touch(e, 1_780_000_000, "vndb_at")  # later than the log
    assert e["written_at"] == e["vndb_at"] > e["log_at"]
    assert pages_mod._filter_source(e, "hikaru")["written_at"] == "2026-01-05"
    assert pages_mod._filter_source(e, "vndb")["written_at"] == e["vndb_at"]
    assert pages_mod._filter_source(e, "both")["written_at"] == e["vndb_at"]


def test_filter_source_empty_side_sorts_last():
    e = pages_mod._new_entry()
    pages_mod._touch(e, 1_780_000_000, "vndb_at")
    assert pages_mod._filter_source(e, "hikaru")["written_at"] == ""


# ----------------------------------------------------------- bare ids --

@pytest.fixture
async def bot(tmp_db_path):
    b = FakeBot(tmp_db_path)
    await run_migrations(b)
    await b.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    await b.RUN(CREATE_VNDB_CACHE_TABLE)
    await run_migrations(b)
    await VN_Entry.save_to_db(b, VN_Entry("v184", "Game A", "ゲームA", "", False, 600, "3", "desc"))
    await VN_Entry.save_to_db(b, VN_Entry("v7", "Game B", "ゲームB", "", False, 600, "3", "desc"))
    return b


async def _log(bot, uid, vid, month="2026-05", comment="c"):
    await bot.RUN(DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
                  (uid, vid, 4, 5, "As Normal VN", month, 6, comment, GUILD))


async def _ulist(bot, uid, vid, vote, lastmod):
    await bot.RUN(
        "INSERT OR IGNORE INTO vndb_links (user_id, vndb_uid, vndb_username, linked_in_guild) VALUES (?, ?, ?, ?)",
        (uid, f"u{uid}", f"name{uid}", GUILD),
    )
    await bot.RUN(
        "INSERT INTO vndb_ulist (user_id, vndb_id, vote, notes, lastmod) VALUES (?, ?, ?, ?, ?)",
        (uid, vid, vote, "note", lastmod),
    )


def _cog(bot):
    cog = pages_mod.VNPages.__new__(pages_mod.VNPages)
    cog.bot = bot
    return cog


async def test_user_entries_merge_bare_log_with_vndb_entry(bot):
    await _log(bot, MEMBER, "184")
    await _log(bot, MEMBER, "v7")
    await _ulist(bot, MEMBER, "v184", 85, 1_780_000_000)
    entries = await _cog(bot).user_entries(MEMBER)
    assert set(entries) == {"v184", "v7"}
    merged = entries["v184"]
    assert merged["title"] is not None
    assert len(merged["logs"]) == 1 and merged["vote"] == 85
    assert merged["log_at"] and merged["vndb_at"]


async def test_vn_entries_find_bare_logs(bot):
    await _log(bot, MEMBER, "184")
    await _log(bot, OTHER, "v184")
    people = await _cog(bot).vn_entries("v184", GUILD)
    assert set(people) == {MEMBER, OTHER}
    people = await _cog(bot).vn_entries("v184", None)
    assert set(people) == {MEMBER, OTHER}
