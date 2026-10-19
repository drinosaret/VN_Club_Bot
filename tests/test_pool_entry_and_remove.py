"""/pool_entry recent completions and the /manage_pool remove confirmation."""
from __future__ import annotations

import cogs.vn_title_management as title_mod
import tests.test_rating_scale_commands as base
from tests.test_rating_scale_commands import (
    ALICE,
    BOB,
    CARA,
    GUILD,
    FakeInteraction,
    FakeUser,
    _log,
)

bot = base.bot  # shared DB fixture


async def _add_pool_row(bot, vid="v1"):
    await bot.RUN(
        "INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, is_monthly_points, status) "
        "VALUES (?, ?, '2026-05', '2026-05', 10, 'monthly')", (vid, GUILD))
    (entry_id,) = await bot.GET_ONE("SELECT id FROM vn_titles WHERE vndb_id = ?", (vid,))
    return entry_id


async def _pool_entry_text(bot, entry_id):
    cog = title_mod.VNTitleManagement(bot)
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
    await title_mod.VNTitleManagement.pool_entry.callback(cog, inter, id=entry_id)
    return inter.text()


async def test_recent_completions_include_bare_numeric_ids(bot):
    entry_id = await _add_pool_row(bot)
    await _log(bot, ALICE, "v1", 8, 10, comment="canonical id")
    await _log(bot, BOB, "1", 7, 10, comment="bare id")
    text = await _pool_entry_text(bot, entry_id)
    assert "canonical id" in text
    assert "bare id" in text


async def test_recent_completion_snippet_is_escaped_and_cut(bot):
    entry_id = await _add_pool_row(bot)
    await _log(bot, ALICE, "v1", 8, 10,
               comment="see [here](https://example.invalid)\n**bold**")
    await _log(bot, BOB, "v1", 7, 10, comment="x" * 70 + " ||hidden ending text||")
    await _log(bot, CARA, "v1", 6, 10, comment="y" * 200)
    text = await _pool_entry_text(bot, entry_id)
    assert "\\[here\\]" in text and "\\*\\*bold\\*\\*" in text
    assert "[here](" not in text
    # A cut through a spoiler drops the unclosed marker rather than
    # revealing the rest of the line.
    bob_line = next(line for line in text.splitlines() if "x" * 70 in line)
    assert bob_line.count("||") % 2 == 0 and bob_line.endswith("…")
    cara_line = next(line for line in text.splitlines() if "yyyy" in line)
    assert "y" * 81 not in cara_line and cara_line.endswith("…")


async def test_pool_remove_names_the_removed_vn(bot):
    entry_id = await _add_pool_row(bot)
    cog = title_mod.VNTitleManagement(bot)
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
    await cog._pool_remove(inter, str(entry_id))
    msg = inter.sent[-1]["content"]
    assert f"**#{entry_id}**" in msg and "ゲームA" in msg
    assert await bot.GET_ONE("SELECT id FROM vn_titles WHERE id = ?", (entry_id,)) is None


async def test_pool_remove_falls_back_to_vndb_id_and_escapes_title(bot):
    entry_id = await _add_pool_row(bot, vid="v99")
    cog = title_mod.VNTitleManagement(bot)
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
    await cog._pool_remove(inter, str(entry_id))
    assert "**v99**" in inter.sent[-1]["content"]

    entry_id = await _add_pool_row(bot, vid="v98")
    await bot.RUN("UPDATE vn_titles SET title_cache = ? WHERE id = ?",
                  ("*Odd* [title](https://example.invalid)", entry_id))
    inter = FakeInteraction(bot, FakeUser(ALICE, "alice"))
    await cog._pool_remove(inter, str(entry_id))
    msg = inter.sent[-1]["content"]
    assert "\\*Odd\\* \\[title\\]" in msg and "[title](" not in msg
