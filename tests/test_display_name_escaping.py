"""Display names and handles reach embed markdown as literal text, and the
participants panel always shows a readable handle."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import cogs.vn_cycle as vn_cycle
import cogs.vn_user_commands as vn_user_commands
from lib.embeds import EmbedBuilder

# Kept under the 40-character display cap so every marker survives the cut.
HOSTILE = "[claim](https://x.io) **b** __u__ ~~s~~"


def _assert_inert(text: str) -> None:
    assert "[claim](" not in text
    assert "\\[claim\\]" in text
    assert "\\*\\*b\\*\\*" in text
    assert "\\_\\_u\\_\\_" in text
    assert "\\~\\~s\\~\\~" in text


def test_leaderboard_row_escapes_username() -> None:
    data = [{"username": HOSTILE, "points": 10, "completions": 1}]
    embed = EmbedBuilder.create_leaderboard_embed("Board", data, 0, 1, 10)
    blob = (embed.description or "") + "".join(f.value for f in embed.fields)
    _assert_inert(blob)


def test_leaderboard_row_keeps_missing_name_placeholder() -> None:
    data = [{"username": None, "points": 1, "completions": 0}]
    embed = EmbedBuilder.create_leaderboard_embed("Board", data, 0, 1, 10)
    blob = (embed.description or "") + "".join(f.value for f in embed.fields)
    assert "**?**" in blob


@pytest.mark.asyncio
async def test_server_standings_escapes_names(monkeypatch) -> None:
    async def fake_username(_bot, _uid):
        return HOSTILE

    monkeypatch.setattr(vn_user_commands, "get_username_db", fake_username)
    bot = SimpleNamespace(get_guild=lambda _gid: None)
    # Four servers so both the podium block and an overflow field render.
    rows = [(uid, "v1", None, None, 10 - uid, None, 100 + uid) for uid in range(1, 5)]
    embed = await vn_user_commands._build_server_standings_embed(bot, "All time", rows)
    _assert_inert(embed.description)
    assert embed.fields
    _assert_inert(embed.fields[0].value)


class _PanelBot:
    def __init__(self, cached: dict, stored_rows: list):
        self._cached = cached
        self._stored_rows = stored_rows

    def get_user(self, uid):
        return self._cached.get(uid)

    async def GET(self, _query, params):
        return [r for r in self._stored_rows if r[0] in params]


async def _render_panel(monkeypatch, bot, voters) -> str:
    async def no_persist(_bot, _ids):
        return None

    monkeypatch.setattr(vn_cycle, "_persist_resolved_users", no_persist)
    nominee = (7, 1, "v1", 99, 5, "Title", "2026-01-01")
    view = vn_cycle.ParticipantsView(bot, 1, [nominee], {7: len(voters)})
    view.voters_cache[7] = voters
    embed = await view.render_embed()
    view.stop()
    return embed.description


@pytest.mark.asyncio
async def test_participants_cached_user_shows_handle_and_mention(monkeypatch) -> None:
    bot = _PanelBot({11: SimpleNamespace(name="reader_one")}, [])
    desc = await _render_panel(monkeypatch, bot, [(11, None)])
    assert "@reader\\_one (<@11>)" in desc


@pytest.mark.asyncio
async def test_participants_stored_tag_is_escaped(monkeypatch) -> None:
    bot = _PanelBot({}, [(12, HOSTILE, None)])
    desc = await _render_panel(monkeypatch, bot, [(12, None)])
    _assert_inert(desc)
    assert "<@12>" not in desc


@pytest.mark.asyncio
async def test_participants_unknown_member_fallback(monkeypatch) -> None:
    bot = _PanelBot({}, [])
    desc = await _render_panel(monkeypatch, bot, [(13, None)])
    assert vn_cycle._UNKNOWN_MEMBER in desc
    assert "<@13>" not in desc
