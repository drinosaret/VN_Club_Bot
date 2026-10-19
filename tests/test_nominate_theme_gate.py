# tests/test_nominate_theme_gate.py
"""The /nominate theme gate wiring: who gets stopped, who gets warned, and
what happens when the theme can't be evaluated.

The rule engine has its own tests (test_theme_rules.py); these cover the
decisions the cog makes around it, which is where a themed period can quietly
stop being enforced.
"""
from __future__ import annotations

import json

import pytest

import cogs.vn_cycle as vn_cycle
from cogs.vn_cycle import VNCycleCog
from lib.migrations import run_migrations
from lib.themes import ThemeAttributes, tag_probes, validate_rules
from lib.utils import DatabaseQueries, ValidationError
from lib.vndb_api import VN_Entry
from tests.conftest import FakeBot

GUILD = 4242
SF_RULES = {"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}}


class _FakeUser:
    def __init__(self, user_id=7, roles=()):
        self.id = user_id
        self.roles = list(roles)


class _FakeGuild:
    def __init__(self, guild_id=GUILD):
        self.id = guild_id


class _FakeInteraction:
    def __init__(self, guild_id=GUILD):
        self.guild = _FakeGuild(guild_id)
        self.guild_id = guild_id
        self.user = _FakeUser()


def _cog(bot) -> VNCycleCog:
    """The cog without its __init__: the gate helpers only reach self.bot,
    and a real init wants a live discord.py Bot."""
    cog = object.__new__(VNCycleCog)
    cog.bot = bot
    return cog


async def _assign_theme(bot, rules, *, label="SF September",
                        kind="monthly", start="2026-09", end="2026-09",
                        guild_id=GUILD, raw=None):
    await bot.RUN(
        DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
        (guild_id, kind, start, end, label,
         raw if raw is not None else json.dumps(validate_rules(rules)), None, 1),
    )


def _vn():
    return VN_Entry("v2002", "STEINS;GATE", "シュタインズ・ゲート", "", False, None, "3", "")


def _patch(monkeypatch, *, manager, attrs="pass", rules=SF_RULES):
    """Wire the gate's two collaborators. ``attrs`` is 'pass', 'fail', or
    None to stand for an unreachable attribute source."""
    async def _is_manager(interaction):
        return manager

    async def _gather(bot, vndb_id, vn_info, theme_rules):
        if attrs is None:
            return None
        present = {"g105"} if attrs == "pass" else set()
        return ThemeAttributes(
            length_rating=3,
            tag_matches={p: p[0] in present for p in tag_probes(theme_rules)},
        )

    monkeypatch.setattr(vn_cycle, "is_manager", _is_manager)
    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _gather)


async def _check(cog, interaction):
    return await cog._check_nomination_theme(
        interaction, "monthly", "2026-09", "2026-09",
        "v2002", _vn(), "STEINS;GATE", "September 2026",
    )


async def test_off_theme_nomination_is_rejected_for_an_ordinary_member(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    _patch(monkeypatch, manager=False, attrs="fail")
    with pytest.raises(ValidationError) as err:
        await _check(_cog(fake_bot), _FakeInteraction())
    assert "doesn't fit the theme" in err.value.user_message
    assert "Science Fiction" in err.value.user_message


async def test_off_theme_nomination_warns_a_manager_instead_of_blocking(
        fake_bot: FakeBot, monkeypatch):
    # A manager nominating an off-theme VN is still checked and sees a
    # warning, rather than skipping the gate and seeing nothing at all.
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    _patch(monkeypatch, manager=True, attrs="fail")
    warning = await _check(_cog(fake_bot), _FakeInteraction())
    assert warning and "doesn't fit the theme" in warning
    assert "Science Fiction" in warning


async def test_on_theme_nomination_passes_silently_for_both(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    for manager in (True, False):
        _patch(monkeypatch, manager=manager, attrs="pass")
        assert await _check(_cog(fake_bot), _FakeInteraction()) is None


async def test_manager_is_still_evaluated_not_short_circuited(
        fake_bot: FakeBot, monkeypatch):
    # An implementation that checks is_manager first and returns would never
    # call the evaluator at all.
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    called = []

    async def _is_manager(interaction):
        return True

    async def _gather(bot, vndb_id, vn_info, theme_rules):
        called.append(vndb_id)
        return ThemeAttributes(tag_matches={})

    monkeypatch.setattr(vn_cycle, "is_manager", _is_manager)
    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _gather)
    await _check(_cog(fake_bot), _FakeInteraction())
    assert called == ["v2002"]


async def test_unthemed_period_never_touches_vndb(fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)

    async def _boom(*a, **k):
        raise AssertionError("no theme means no attribute fetch")

    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _boom)
    assert await _check(_cog(fake_bot), _FakeInteraction()) is None


async def test_empty_ruleset_constrains_nothing(fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, {})

    async def _boom(*a, **k):
        raise AssertionError("an empty ruleset has nothing to check")

    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _boom)
    assert await _check(_cog(fake_bot), _FakeInteraction()) is None


async def test_unparseable_rules_block_a_member_rather_than_opening_the_gate(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, None, raw="{not json at all")
    _patch(monkeypatch, manager=False)
    with pytest.raises(ValidationError) as err:
        await _check(_cog(fake_bot), _FakeInteraction())
    assert "misconfigured" in err.value.user_message


async def test_unparseable_rules_warn_a_manager(fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, None, raw=json.dumps({"tags": {"all_of": "not a list"}}))
    _patch(monkeypatch, manager=True)
    warning = await _check(_cog(fake_bot), _FakeInteraction())
    assert warning and "misconfigured" in warning


async def test_unreachable_attribute_source_blocks_a_member(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    _patch(monkeypatch, manager=False, attrs=None)
    with pytest.raises(ValidationError) as err:
        await _check(_cog(fake_bot), _FakeInteraction())
    assert "Try again" in err.value.user_message


async def test_unreachable_attribute_source_only_warns_a_manager(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    _patch(monkeypatch, manager=True, attrs=None)
    assert "Try again" in await _check(_cog(fake_bot), _FakeInteraction())


async def test_ballot_audit_separates_off_theme_from_unverifiable(
        fake_bot: FakeBot, monkeypatch):
    # An entry the audit merely couldn't check must not be named alongside a
    # prompt to delete it: that would cost someone their pick over a VNDB blip.
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    rules = validate_rules(SF_RULES)
    probes = tag_probes(rules)
    verdicts = {"v1": True, "v2": False, "v3": None}

    async def _from_vndb_id(bot, vndb_id):
        return _vn()

    async def _gather(bot, vndb_id, vn_info, theme_rules):
        return ThemeAttributes(tag_matches={p: verdicts[vndb_id] for p in probes})

    monkeypatch.setattr(vn_cycle, "from_vndb_id", _from_vndb_id)
    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _gather)

    async def _rows(query, params=()):
        return [(i, 1, vid, 7, GUILD, f"Title {vid}", "")
                for i, vid in enumerate(("v1", "v2", "v3"), start=1)]

    cog = _cog(fake_bot)
    monkeypatch.setattr(cog.bot, "GET", _rows, raising=False)
    note = await cog._off_theme_ballot_note(GUILD, 1, "monthly", "2026-09", "2026-09")

    assert "off-theme" in note and "Title v2" in note
    assert "Title v1" not in note                      # on theme
    off, unchecked = note.split("Couldn't be checked")
    assert "Title v3" in unchecked and "Title v3" not in off
    assert "manage_pool" in off and "manage_pool" not in unchecked


async def test_ballot_audit_is_silent_when_every_entry_fits(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES)
    rules = validate_rules(SF_RULES)

    async def _from_vndb_id(bot, vndb_id):
        return _vn()

    async def _gather(bot, vndb_id, vn_info, theme_rules):
        return ThemeAttributes(tag_matches={p: True for p in tag_probes(rules)})

    monkeypatch.setattr(vn_cycle, "from_vndb_id", _from_vndb_id)
    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _gather)

    async def _rows(query, params=()):
        return [(1, 1, "v1", 7, GUILD, "Title", "")]

    cog = _cog(fake_bot)
    monkeypatch.setattr(cog.bot, "GET", _rows, raising=False)
    assert await cog._off_theme_ballot_note(GUILD, 1, "monthly", "2026-09", "2026-09") == ""


def test_ballot_note_trades_the_tail_of_a_long_list_for_a_count():
    labels = [f"**A very long visual novel title number {i}**" for i in range(25)]
    joined = VNCycleCog._join_capped(labels)
    assert "more" in joined and len(joined) < 400


async def test_a_theme_in_one_guild_does_not_gate_another(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES, guild_id=GUILD)

    async def _boom(*a, **k):
        raise AssertionError("another guild's theme must not be read")

    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _boom)
    assert await _check(_cog(fake_bot), _FakeInteraction(guild_id=GUILD + 1)) is None


async def test_the_seasonal_lane_does_not_read_the_monthly_theme(
        fake_bot: FakeBot, monkeypatch):
    await run_migrations(fake_bot)
    await _assign_theme(fake_bot, SF_RULES, kind="monthly")

    async def _boom(*a, **k):
        raise AssertionError("a monthly theme must not gate a seasonal nomination")

    monkeypatch.setattr(vn_cycle, "gather_theme_attributes", _boom)
    cog = _cog(fake_bot)
    assert await cog._check_nomination_theme(
        _FakeInteraction(), "seasonal", "2026-09", "2026-11",
        "v2002", _vn(), "STEINS;GATE", "Fall 2026") is None
