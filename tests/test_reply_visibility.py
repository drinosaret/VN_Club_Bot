"""Command replies are private unless the channel is open, the command is
public by default, or a manager asks for a public reply."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import discord
import pytest
from discord import app_commands

import cogs.admin_visibility as panel_mod
from lib import visibility
from lib.migrations import run_migrations
from tests.conftest import FakeBot

GUILD, OPEN, OTHER, FORUM = 4242, 11, 12, 13
MEMBER, MANAGER = 101, 900


class _Response:
    def __init__(self):
        self.deferred = None
        self.edited = None
        self._done = False

    async def defer(self, ephemeral=False, **_):
        self.deferred = ephemeral
        self._done = True

    def is_done(self):
        return self._done

    async def send_message(self, *a, **k):
        self._done = True

    async def edit_message(self, **kwargs):
        self.edited = kwargs
        self._done = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


def _inter(bot, user_id=MEMBER, channel_id=OTHER, parent_id=None, guild_id=GUILD, command="test"):
    return SimpleNamespace(
        client=bot, guild_id=guild_id, channel_id=channel_id,
        channel=SimpleNamespace(id=channel_id, parent_id=parent_id),
        user=SimpleNamespace(id=user_id, roles=[], display_name="u", name="u"),
        response=_Response(), followup=_Followup(), data={},
        command=SimpleNamespace(name=command, qualified_name=command),
    )


@pytest.fixture
async def bot(tmp_db_path):
    b = FakeBot(tmp_db_path)
    await run_migrations(b)
    await b.RUN(
        "INSERT INTO guild_managers (guild_id, principal_type, principal_id) VALUES (?, 'user', ?)",
        (GUILD, MANAGER),
    )
    await visibility.set_public_channels(b, GUILD, [OPEN, FORUM], MANAGER)
    return b


@pytest.mark.parametrize("user,channel,parent,public,private", [
    (MEMBER, OTHER, None, None, True),     # default: private
    (MEMBER, OPEN, None, None, False),     # open channel
    (MEMBER, 99, FORUM, None, False),      # thread under an open forum
    (MEMBER, OPEN, None, False, True),     # anyone can keep it private
    (MEMBER, OTHER, None, True, True),     # members cannot force public
    (MANAGER, OTHER, None, None, True),    # managers are private by default too
    (MANAGER, OTHER, None, True, False),   # ... unless they ask
    (MANAGER, OPEN, None, False, True),
])
async def test_reply_rules(bot, user, channel, parent, public, private):
    inter = _inter(bot, user, channel, parent)
    assert await visibility.defer_reply(inter, public) is private
    assert inter.response.deferred is private


async def test_direct_messages_are_not_forced_private(bot):
    inter = _inter(bot, guild_id=None)
    assert await visibility.reply_is_private(inter) is False


async def test_public_everywhere_mode(bot):
    await visibility.set_public_everywhere(bot, GUILD, True, MANAGER)
    assert await visibility.reply_is_private(_inter(bot)) is False
    await visibility.set_public_everywhere(bot, GUILD, False, MANAGER)
    assert await visibility.reply_is_private(_inter(bot)) is True


async def test_settings_are_per_server(bot):
    assert await visibility.reply_is_private(_inter(bot, channel_id=OPEN, guild_id=777)) is True


async def test_channel_list_replaced_not_merged(bot):
    await visibility.set_public_channels(bot, GUILD, [OTHER], MANAGER)
    assert await visibility.reply_is_private(_inter(bot, channel_id=OPEN)) is True
    assert await visibility.reply_is_private(_inter(bot, channel_id=OTHER)) is False


async def test_panel_requires_manager(bot):
    cog = panel_mod.AdminVisibility(bot)
    inter = _inter(bot, MEMBER)
    await panel_mod.AdminVisibility.manage_visibility.callback(cog, inter)
    assert inter.response.deferred is True
    assert not any(m.get("view") for m in inter.followup.sent)


async def test_panel_saves_mode_and_channels(bot):
    cog = panel_mod.AdminVisibility(bot)
    inter = _inter(bot, MANAGER)
    await panel_mod.AdminVisibility.manage_visibility.callback(cog, inter)
    msg = inter.followup.sent[0]
    assert msg["ephemeral"] is True
    view = msg["view"]
    assert "<#11>" in msg["embed"].fields[1].value

    click = _inter(bot, MANAGER)
    click.data = {"values": [panel_mod.MODE_PUBLIC]}
    assert await view.interaction_check(click)
    await view._on_mode(click)
    assert (await visibility.guild_settings(bot, GUILD))[0] is True
    assert click.response.edited["embed"].fields[0].value == "Public everywhere"

    stranger = _inter(bot, MEMBER)
    assert not await view.interaction_check(stranger)


@app_commands.command(name="finish", description="d")
async def _finish(interaction: discord.Interaction, public: Optional[bool] = None):
    pass


@app_commands.command(name="logs", description="d")
async def _logs(interaction: discord.Interaction, public: Optional[bool] = None):
    pass


@app_commands.command(name="nominate", description="d")
async def _nominate(interaction: discord.Interaction, title: str):
    pass


def _tree():
    group = app_commands.Group(name="admin", description="d")
    return SimpleNamespace(walk_commands=lambda: [_logs, _nominate, group, _finish])


@pytest.mark.parametrize("user,command,channel,public,private", [
    (MEMBER, "finish", OTHER, None, False),    # public by default
    (MEMBER, "finish", OTHER, False, True),    # anyone can still keep it private
    (MANAGER, "finish", OTHER, None, False),
    (MEMBER, "logs", OTHER, None, True),       # unlisted command stays private
    (MEMBER, "logs", OPEN, None, False),       # open channels still apply
])
async def test_public_commands(bot, user, command, channel, public, private):
    await visibility.set_public_commands(bot, GUILD, ["finish"], MANAGER)
    inter = _inter(bot, user, channel, command=command)
    assert await visibility.defer_reply(inter, public) is private


async def test_public_commands_are_per_server(bot):
    await visibility.set_public_commands(bot, GUILD, ["finish"], MANAGER)
    assert await visibility.reply_is_private(_inter(bot, guild_id=777, command="finish")) is True


async def test_public_command_list_replaced_not_merged(bot):
    await visibility.set_public_commands(bot, GUILD, ["finish"], MANAGER)
    await visibility.set_public_commands(bot, GUILD, ["logs"], MANAGER)
    assert await visibility.reply_is_private(_inter(bot, command="finish")) is True
    assert await visibility.reply_is_private(_inter(bot, command="logs")) is False
    await visibility.set_public_commands(bot, GUILD, [], MANAGER)
    assert await visibility.reply_is_private(_inter(bot, command="logs")) is True


def test_only_commands_with_public_option_are_offered():
    bot = SimpleNamespace(tree=_tree())
    assert visibility.public_capable_commands(bot) == ["finish", "logs"]
    assert visibility.public_capable_commands(SimpleNamespace()) == []


async def test_panel_saves_public_commands(bot):
    bot.tree = _tree()
    cog = panel_mod.AdminVisibility(bot)
    inter = _inter(bot, MANAGER)
    await panel_mod.AdminVisibility.manage_visibility.callback(cog, inter)
    msg = inter.followup.sent[0]
    view = msg["view"]
    picker = next(i for i in view.children if getattr(i, "callback", None) == view._on_commands)
    assert [o.value for o in picker.options] == ["finish", "logs"]
    assert msg["embed"].fields[2].value.startswith("None")

    click = _inter(bot, MANAGER)
    click.data = {"values": ["finish"]}
    await view._on_commands(click)
    assert (await visibility.guild_settings(bot, GUILD)).commands == {"finish"}
    assert click.response.edited["embed"].fields[2].value == "`/finish`"
    new_picker = next(
        i for i in click.response.edited["view"].children
        if isinstance(i, discord.ui.Select) and i.row == 2
    )
    assert [o.default for o in new_picker.options] == [True, False]
