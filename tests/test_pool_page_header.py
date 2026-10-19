# tests/test_pool_page_header.py
"""What leads a /pool page, and what its title and footer carry.

The invariant worth guarding: the section loop binds `header`, so anything
the embed needs afterwards has to survive it. A leak prints the
last section's title above the first section on every page.
"""
from __future__ import annotations

import json

from cogs.vn_title_management import VNTitleManagement
from lib.migrations import run_migrations
from lib.themes import validate_rules
from lib.utils import DatabaseQueries
from lib.vndb_api import CREATE_VNDB_CACHE_TABLE
from tests.conftest import FakeBot

GUILD = 4242
MONTH = "2026-08"


async def _schema(bot):
    """Migrations plus the tables the cogs create for themselves, matching the
    harness in test_period_sweep.py. The pool select joins all of them."""
    await run_migrations(bot)
    for query in (DatabaseQueries.CREATE_VN_TITLES_TABLE,
                  DatabaseQueries.CREATE_VN_CYCLES_TABLE,
                  DatabaseQueries.CREATE_VN_VOTES_TABLE,
                  DatabaseQueries.CREATE_READING_LOGS_TABLE,
                  CREATE_VNDB_CACHE_TABLE):
        await bot.RUN(query)
    # The all_servers view annotates each row with its source server.
    bot.get_guild = lambda guild_id: None


def _cog(bot) -> VNTitleManagement:
    cog = object.__new__(VNTitleManagement)
    cog.bot = bot
    return cog


async def _add(bot, vndb_id, status, title, *, month=MONTH):
    await bot.RUN(
        "INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, "
        "is_monthly_points, status, nominator_user_id, title_cache) "
        "VALUES (?, ?, ?, ?, 1, ?, 7, ?)",
        (vndb_id, GUILD, month, month, status, title),
    )


async def _assign_theme(bot, label="SF August"):
    await bot.RUN(
        DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
        (GUILD, "monthly", MONTH, MONTH, label,
         json.dumps(validate_rules(
             {"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}})), None, 1),
    )


async def _pages(bot, **over):
    args = dict(guild_id=GUILD, month=8, year=2026, filter_value="all",
                all_servers=False, view_mode="monthly")
    args.update(over)
    return await _cog(bot)._build_pool_pages(bot, **args)


async def _seed(bot):
    await _schema(bot)
    await _add(bot, "v912", "monthly", "A Pick")
    await _add(bot, "v9148", "nominated", "A Nomination")
    await _add(bot, "v1278", "nominated", "Another Nomination")


# ---------------- what leads the description ----------------


async def test_both_sections_appear_exactly_once(fake_bot: FakeBot):
    # The shadowing guard: a leaked loop variable repeats a section title.
    await _seed(fake_bot)
    body = (await _pages(fake_bot))[0].description
    assert body.count("Monthly nominations") == 1
    assert body.count("Picks") == 1
    # Each section header carries its row count.
    assert "### Picks · 1" in body
    assert "### Monthly nominations · 2" in body


async def test_an_unthemed_page_opens_on_its_first_section(fake_bot: FakeBot):
    await _seed(fake_bot)
    first_line = (await _pages(fake_bot))[0].description.splitlines()[0]
    assert "Picks" in first_line


async def test_a_themed_page_opens_on_the_theme(fake_bot: FakeBot):
    await _seed(fake_bot)
    await _assign_theme(fake_bot)
    description = (await _pages(fake_bot))[0].description
    assert description.startswith("🎯 **Theme: SF August**")
    assert "Science Fiction" in description
    # Still exactly one of each section beneath it.
    assert description.count("Monthly nominations") == 1


async def test_the_theme_leads_every_page_of_a_long_pool(fake_bot: FakeBot):
    await _schema(fake_bot)
    await _assign_theme(fake_bot)
    for i in range(120):
        await _add(fake_bot, f"v{i}", "nominated", f"A reasonably long VN title number {i}")
    pages = await _pages(fake_bot)
    assert len(pages) > 1
    for page in pages:
        assert page.description.startswith("🎯 **Theme: SF August**")
        assert len(page.description) <= 4096


async def test_the_global_view_shows_no_theme(fake_bot: FakeBot):
    # Themes are per-guild, so there is no single one to show across servers.
    await _seed(fake_bot)
    await _assign_theme(fake_bot)
    description = (await _pages(fake_bot, all_servers=True))[0].description
    assert "Theme" not in description


# ---------------- what the title and footer carry ----------------


async def test_the_default_view_titles_only_the_period(fake_bot: FakeBot):
    # This server and no filter are the defaults, so neither is named.
    await _seed(fake_bot)
    page = (await _pages(fake_bot))[0]
    assert page.title == "📋 Pool · August 2026"
    assert page.footer.text == "3 entries · /nominate to add a VN"


async def test_the_title_carries_filter_and_scope(fake_bot: FakeBot):
    await _seed(fake_bot)
    page = (await _pages(fake_bot, filter_value="nominations", all_servers=True))[0]
    assert page.title == "📋 Pool · August 2026 · Nominations · All servers"
    assert page.footer.text == "2 entries · /nominate to add a VN"


async def test_the_footer_says_how_to_add_to_the_pool(fake_bot: FakeBot):
    # Someone reading the pool is the likeliest person to want to add to it,
    # and nothing else on the embed names the command.
    await _seed(fake_bot)
    assert "/nominate" in (await _pages(fake_bot))[0].footer.text


async def test_the_empty_pool_still_says_how_to_add_to_it(fake_bot: FakeBot):
    await _schema(fake_bot)
    assert "/nominate" in (await _pages(fake_bot))[0].footer.text


async def test_the_title_reflects_the_view_it_is_showing(fake_bot: FakeBot):
    # The seasonal view names the season instead of the month.
    await _seed(fake_bot)
    await fake_bot.RUN(
        "INSERT INTO vn_titles (vndb_id, guild_id, start_month, end_month, "
        "is_monthly_points, status, nominator_user_id, title_cache) "
        "VALUES ('v1', ?, '2026-07', '2026-09', 1, 'seasonal', 7, 'A Seasonal Pick')",
        (GUILD,),
    )
    page = (await _pages(fake_bot, view_mode="seasonal", all_servers=True))[0]
    assert page.title == "📋 Pool · Summer 2026 · All servers"
    assert "A Seasonal Pick" in page.description
    assert page.footer.text == "1 entry · /nominate to add a VN"


async def test_the_page_counter_appears_only_when_there_is_more_than_one(fake_bot: FakeBot):
    await _seed(fake_bot)
    assert "Page" not in (await _pages(fake_bot))[0].footer.text

    await _schema(fake_bot)
    for i in range(120):
        await _add(fake_bot, f"v{i}", "nominated", f"A reasonably long VN title number {i}")
    pages = await _pages(fake_bot)
    assert len(pages) > 1
    assert f"Page 1/{len(pages)}" in pages[0].footer.text
    assert f"Page {len(pages)}/{len(pages)}" in pages[-1].footer.text


async def test_the_empty_state_keeps_its_title_and_footer_and_says_so(fake_bot: FakeBot):
    await _schema(fake_bot)
    pages = await _pages(fake_bot)
    assert len(pages) == 1
    assert pages[0].description == "ℹ️ Nothing in the pool for August 2026."
    assert pages[0].title == "📋 Pool · August 2026"
    assert pages[0].footer.text == "0 entries · /nominate to add a VN"


async def test_an_empty_themed_period_still_shows_the_theme(fake_bot: FakeBot):
    # Nothing nominated yet is exactly when knowing the theme matters most.
    await _schema(fake_bot)
    await _assign_theme(fake_bot)
    description = (await _pages(fake_bot))[0].description
    assert description.startswith("🎯 **Theme: SF August**")
    assert description.endswith("ℹ️ Nothing in the pool for August 2026.")
