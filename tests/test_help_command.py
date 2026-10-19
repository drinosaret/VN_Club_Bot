"""/help stays small and private: a short category menu, one category at a
time, and nothing posted where the channel can see it."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import cogs.vn_user_commands as user_mod
from lib.autocomplete import HELP_JSON_PATH
from tests.test_rating_scale_commands import FakeInteraction, FakeUser, bot  # noqa: F401


@pytest.fixture
def help_data():
    with open(HELP_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)["commands"]


class _Resp:
    """Captures what an interaction edits its message into."""

    def __init__(self):
        self.edited = None

    async def edit_message(self, **kwargs):
        self.edited = kwargs


def _pick(value):
    inter = SimpleNamespace(data={"values": [value]}, response=_Resp())
    return inter


async def _run_help(bot, monkeypatch, manager: bool, command=None):
    async def fake_is_manager(_interaction):
        return manager

    monkeypatch.setattr(user_mod, "is_manager", fake_is_manager)
    cog = user_mod.VNUserCommands(bot)
    await cog.cog_load()
    inter = FakeInteraction(bot, FakeUser(1, "member"))
    deferred = {}

    async def defer(**kwargs):
        deferred.update(kwargs)
        inter.response._done = True

    inter.response.defer = defer
    await user_mod.VNUserCommands.help_command.callback(cog, inter, command=command)
    return inter, deferred


async def test_help_is_private(bot, monkeypatch):
    _inter, deferred = await _run_help(bot, monkeypatch, manager=False)
    assert deferred.get("ephemeral") is True


async def test_home_is_short_and_lists_categories_only(bot, monkeypatch, help_data):
    inter, _ = await _run_help(bot, monkeypatch, manager=False)
    embed = inter.sent[0]["embed"]
    # A handful of lines, not every command.
    assert len(embed.description.splitlines()) <= 8
    assert len(embed.description) < 600
    assert "/finish" not in embed.description
    assert "VN Club" in embed.description and "VNDB" in embed.description


async def test_manager_category_only_for_managers(bot, monkeypatch):
    member, _ = await _run_help(bot, monkeypatch, manager=False)
    assert "Manager" not in member.sent[0]["embed"].description
    options = [o.value for o in member.sent[0]["view"].children[0].options]
    assert "manager" not in options

    mgr, _ = await _run_help(bot, monkeypatch, manager=True)
    assert "Manager" in mgr.sent[0]["embed"].description


async def test_category_then_detail_edit_in_place(help_data):
    home = user_mod.HelpHomeView(help_data, include_manager=False)
    pick = _pick("club")
    await home._on_pick(pick)
    cat_embed = pick.response.edited["embed"]
    assert "/finish" in cat_embed.description
    assert "/manage_pool" not in cat_embed.description
    cat_view = pick.response.edited["view"]

    detail_pick = _pick("/finish")
    await cat_view._on_pick(detail_pick)
    detail = detail_pick.response.edited["embed"]
    assert detail.title.endswith("/finish")
    assert any(f.name == "Usage" for f in detail.fields)


async def test_every_category_fits_discord_limits(help_data):
    for include_manager in (False, True):
        for key, label, _s, cmds in user_mod._help_categories(help_data, include_manager):
            assert len(cmds) <= user_mod.HELP_SELECT_CAP
            embed = user_mod._build_help_category_embed(label, cmds)
            assert len(embed.description) <= 4096


async def test_no_command_falls_out_of_help(help_data):
    listed = {
        c["name"]
        for _k, _l, _s, cmds in user_mod._help_categories(help_data, include_manager=True)
        for c in cmds
    }
    hidden = {c["name"] for c in help_data if c.get("category") in user_mod._HELP_HIDDEN_CATEGORIES}
    assert listed | hidden == {c["name"] for c in help_data}


async def test_command_detail_is_private(bot, monkeypatch):
    inter, deferred = await _run_help(bot, monkeypatch, manager=False, command="ratings")
    assert deferred.get("ephemeral") is True
    assert inter.sent[0]["embed"].title.endswith("/ratings")
    assert inter.sent[0].get("view") is None


async def test_settings_panel_shows_default_and_changes_in_place(bot):
    from lib import ratings
    cog = user_mod.VNUserCommands(bot)
    inter = FakeInteraction(bot, FakeUser(7, "member"))
    deferred = {}

    async def defer(**kwargs):
        deferred.update(kwargs)
        inter.response._done = True

    inter.response.defer = defer
    await user_mod.VNUserCommands.settings.callback(cog, inter, rating_scale=None)
    assert deferred.get("ephemeral") is True
    field = inter.sent[0]["embed"].fields[0]
    assert "1-10" in field.value and "(default)" in field.value

    view = inter.sent[0]["view"]
    pick = _pick("100")
    await view._on_scale(pick)
    assert await ratings.get_user_scale(bot, 7) == 100
    edited = pick.response.edited["embed"]
    assert "1-100" in edited.fields[0].value and "(default)" not in edited.fields[0].value


async def test_settings_option_sets_scale_directly(bot):
    from lib import ratings
    cog = user_mod.VNUserCommands(bot)
    inter = FakeInteraction(bot, FakeUser(8, "member"))
    await user_mod.VNUserCommands.settings.callback(
        cog, inter, rating_scale=SimpleNamespace(value=5, name="1-5"))
    assert await ratings.get_user_scale(bot, 8) == 5
    assert "1-5" in inter.sent[0]["embed"].description
