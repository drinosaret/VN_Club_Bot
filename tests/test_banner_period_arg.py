# tests/test_banner_period_arg.py
"""/monthly and /seasonal with an explicit month.

The queries are overlap checks, so the whole feature is which month gets
probed and whether the copy still calls it "current".
"""
from __future__ import annotations

import cogs.vn_title_management as vtm
from cogs.vn_title_management import VNTitleManagement


class _Response:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True

    def is_done(self):
        return self.deferred


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class _Guild:
    id = 4242


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()
        self.guild = _Guild()


class _Bot:
    def __init__(self, rows):
        self.rows = rows
        self.probes = []

    async def GET(self, query, params=()):
        self.probes.append(params)
        return self.rows


def _cog(bot) -> VNTitleManagement:
    cog = object.__new__(VNTitleManagement)
    cog.bot = bot
    return cog


async def _run(monkeypatch, *, kind="monthly", month=None, rows=()):
    monkeypatch.setattr(vtm, "get_current_month", lambda: "2026-08")

    async def defer(interaction, _public=None):
        # Reply visibility has its own tests; here only the probe matters.
        await interaction.response.defer()
        return False

    monkeypatch.setattr(vtm, "defer_reply", defer)
    bot = _Bot(list(rows))
    interaction = _Interaction()
    await _cog(bot)._post_pool_kind_banners(
        interaction, kind=kind, embed=False, target_month=month)
    return bot, interaction


async def test_no_month_probes_the_current_one(monkeypatch):
    bot, interaction = await _run(monkeypatch)
    assert bot.probes[0][:2] == ("2026-08", "2026-08")
    assert "current monthly" in interaction.followup.sent[0]


async def test_an_explicit_month_is_probed_instead(monkeypatch):
    bot, interaction = await _run(monkeypatch, month="2026-03")
    assert bot.probes[0][:2] == ("2026-03", "2026-03")


async def test_the_empty_message_names_the_month_that_was_asked_for(monkeypatch):
    _, interaction = await _run(monkeypatch, month="2026-03")
    assert "March 2026" in interaction.followup.sent[0]
    # "Current" would be a lie about a period the caller named.
    assert "current" not in interaction.followup.sent[0].lower()


async def test_seasonal_probes_the_month_and_lets_the_overlap_find_the_season(monkeypatch):
    bot, _ = await _run(monkeypatch, kind="seasonal", month="2026-08")
    assert bot.probes[0][:2] == ("2026-08", "2026-08")


async def test_a_malformed_month_is_refused_before_any_query(monkeypatch):
    bot, interaction = await _run(monkeypatch, month="March")
    assert bot.probes == []
    assert "YYYY-MM" in interaction.followup.sent[0]


async def test_the_guild_is_still_scoped(monkeypatch):
    bot, _ = await _run(monkeypatch, month="2026-03")
    assert bot.probes[0][2] == 4242
