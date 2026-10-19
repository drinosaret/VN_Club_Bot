"""Tests for ``validate_user_permission``.

The function takes `(interaction, custom_message)` and returns True or
raises ValidationError. Access comes from AUTHORIZED_USERS or a
per-guild ``guild_managers`` row in the DB; no env-loaded manager list
is consulted.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

from lib import utils
from lib.migrations import run_migrations
from lib.utils import (
    DatabaseQueries,
    ValidationError,
    validate_user_permission,
)
from tests.conftest import FakeBot


def _fake_interaction(
    bot: FakeBot,
    user_id: int,
    guild_id: Optional[int],
    role_ids: Optional[list[int]] = None,
) -> SimpleNamespace:
    """Minimal stand-in for discord.Interaction.

    The helper reads ``user.id``, ``guild_id``, ``user.roles[*].id``,
    and ``client``. A SimpleNamespace with those is enough.
    """
    roles = [SimpleNamespace(id=rid) for rid in (role_ids or [])]
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, roles=roles),
        guild_id=guild_id,
        client=bot,
    )


@pytest.fixture
async def schema_bot(fake_bot: FakeBot) -> FakeBot:
    await run_migrations(fake_bot)
    return fake_bot


class TestAuthorizedUsersBypass:
    @pytest.mark.asyncio
    async def test_authorized_user_passes_in_any_guild(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # AUTHORIZED_USERS = bot operators. They pass regardless of
        # whether guild_managers has any entries.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [42])
        i = _fake_interaction(schema_bot, user_id=42, guild_id=100)
        assert await validate_user_permission(i) is True

    @pytest.mark.asyncio
    async def test_authorized_user_passes_in_dm(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bot operator running an admin command in DM still passes;
        # operators are unconditional. Whether the command is *useful* in DM
        # is the command's problem, not this function's.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [42])
        i = _fake_interaction(schema_bot, user_id=42, guild_id=None)
        assert await validate_user_permission(i) is True

    @pytest.mark.asyncio
    async def test_non_authorized_falls_through(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # User not in AUTHORIZED_USERS, no guild_managers row: raises.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [99])
        i = _fake_interaction(schema_bot, user_id=42, guild_id=100)
        with pytest.raises(ValidationError):
            await validate_user_permission(i)


class TestPerGuildUserGrant:
    @pytest.mark.asyncio
    async def test_user_grant_passes(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 42, 999),
        )
        i = _fake_interaction(schema_bot, user_id=42, guild_id=100)
        assert await validate_user_permission(i) is True

    @pytest.mark.asyncio
    async def test_user_grant_scoped_to_guild(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # User is a manager in guild 100, but they're invoking in guild 200.
        # Per-guild scoping means this MUST fail.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "user", 42, 999),
        )
        i = _fake_interaction(schema_bot, user_id=42, guild_id=200)
        with pytest.raises(ValidationError):
            await validate_user_permission(i)


class TestPerGuildRoleGrant:
    @pytest.mark.asyncio
    async def test_role_grant_passes(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "role", 7, 999),
        )
        # User has role 7 in their member roles.
        i = _fake_interaction(
            schema_bot, user_id=42, guild_id=100, role_ids=[7, 8, 9],
        )
        assert await validate_user_permission(i) is True

    @pytest.mark.asyncio
    async def test_role_grant_misses_when_user_lacks_role(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "role", 7, 999),
        )
        # User doesn't have role 7.
        i = _fake_interaction(
            schema_bot, user_id=42, guild_id=100, role_ids=[1, 2, 3],
        )
        with pytest.raises(ValidationError):
            await validate_user_permission(i)

    @pytest.mark.asyncio
    async def test_role_grant_scoped_to_guild(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Role 7 is a manager in guild 100, but user is invoking in guild 200.
        # Role IDs are guild-unique anyway, but the WHERE guild_id = ?
        # makes the scoping explicit.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        await schema_bot.RUN(
            DatabaseQueries.INSERT_GUILD_MANAGER, (100, "role", 7, 999),
        )
        i = _fake_interaction(
            schema_bot, user_id=42, guild_id=200, role_ids=[7],
        )
        with pytest.raises(ValidationError):
            await validate_user_permission(i)


class TestDmContext:
    @pytest.mark.asyncio
    async def test_dm_without_authorization_raises(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Non-AUTHORIZED user in DM has no guild to scope against, so
        # raises with a specific message about needing a server.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        i = _fake_interaction(schema_bot, user_id=42, guild_id=None)
        with pytest.raises(ValidationError) as info:
            await validate_user_permission(i)
        assert "server" in info.value.user_message.lower()


class TestCustomMessage:
    @pytest.mark.asyncio
    async def test_custom_message_surfaces_on_denial(
        self, schema_bot: FakeBot, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        i = _fake_interaction(schema_bot, user_id=42, guild_id=100)
        with pytest.raises(ValidationError) as info:
            await validate_user_permission(
                i, custom_message="Only the log owner or an admin can edit this log.",
            )
        assert "Only the log owner" in info.value.user_message


class TestNoEnvFallback:
    """Only AUTHORIZED_USERS and per-guild guild_managers rows grant
    access. A global VN_MANAGER_USER_IDS env list is not a fallback;
    setting one grants nothing.
    """

    @pytest.mark.asyncio
    async def test_env_vn_manager_user_ids_ignored(
        self,
        schema_bot: FakeBot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # validate_user_permission ignores any global env list, even one
        # that is set. The only paths to True are AUTHORIZED_USER_IDS
        # and guild_managers.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [])
        # utils defines no ALLOWED_USER_IDS attribute; if one existed,
        # validate_user_permission must not honor it.
        monkeypatch.setattr(utils, "ALLOWED_USER_IDS", [42], raising=False)
        i = _fake_interaction(schema_bot, user_id=42, guild_id=100)
        with pytest.raises(ValidationError):
            await validate_user_permission(i)
