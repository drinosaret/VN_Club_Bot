# tests/test_pool_theme_banner.py
"""The theme banner on /pool: a member browsing a themed period should see
what will be accepted before nominating, not after being turned away.
"""
from __future__ import annotations

import json

from cogs.vn_title_management import VNTitleManagement
from lib.migrations import run_migrations
from lib.themes import validate_rules
from lib.utils import DatabaseQueries
from tests.conftest import FakeBot

GUILD = 4242
SF_RULES = {"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}}


async def _assign(bot, rules, *, label="SF September", kind="monthly",
                  start="2026-09", end="2026-09", raw=None):
    await bot.RUN(
        DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
        (GUILD, kind, start, end, label,
         raw if raw is not None else json.dumps(validate_rules(rules)), None, 1),
    )


def _cog() -> VNTitleManagement:
    return object.__new__(VNTitleManagement)


async def _block(bot, **over):
    args = dict(kind="monthly", start_month="2026-09", end_month="2026-09")
    args.update(over)
    return await _cog()._pool_theme_block(bot, GUILD, **args)


async def test_unthemed_period_shows_nothing(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    assert await _block(fake_bot) == ""


async def test_banner_names_the_theme_and_its_rules(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await _assign(fake_bot, SF_RULES)
    block = await _block(fake_bot)
    assert "SF September" in block
    # The label alone says nothing about what will be accepted.
    assert "Science Fiction" in block
    assert block.startswith("🎯 **Theme: SF September**")


async def test_banner_spells_out_every_constraint(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await _assign(fake_bot, {
        "length_rating": {"min": 1, "max": 2},
        "released": {"after": "2010", "before": None},
        "nsfw_cover": "disallow",
        "tags": {"none_of": [{"id": "g23", "name": "Horror"}]},
    })
    block = await _block(fake_bot)
    for expected in ("Length:", "Released:", "No NSFW covers", "Excludes:"):
        assert expected in block, expected


async def test_banner_shows_a_per_tag_rating_floor(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await _assign(fake_bot, {"tags": {"all_of": [
        {"id": "g105", "name": "Science Fiction", "min_rating": 2.5}]}})
    assert "Science Fiction (2.5+)" in await _block(fake_bot)


async def test_empty_ruleset_shows_nothing(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await _assign(fake_bot, {})
    assert await _block(fake_bot) == ""


async def test_unreadable_rules_say_so_rather_than_reading_as_unthemed(fake_bot: FakeBot):
    # The gate blocks members on this, so the pool must not look unrestricted.
    await run_migrations(fake_bot)
    await _assign(fake_bot, None, raw="{not json")
    block = await _block(fake_bot)
    assert "SF September" in block and "can't be read" in block


async def test_a_monthly_theme_does_not_appear_on_the_seasonal_view(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await _assign(fake_bot, SF_RULES, kind="monthly")
    assert await _block(fake_bot, kind="seasonal", start_month="2026-07",
                        end_month="2026-09") == ""


async def test_another_months_theme_does_not_leak(fake_bot: FakeBot):
    await run_migrations(fake_bot)
    await _assign(fake_bot, SF_RULES, start="2026-09", end="2026-09")
    assert await _block(fake_bot, start_month="2026-10", end_month="2026-10") == ""
