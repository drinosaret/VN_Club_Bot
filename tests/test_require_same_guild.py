"""Cross-guild access enforcement for admin commands.

`require_same_guild` is the shared guard used by `/manage_pool` remove
and edit, `/log_undo`, and `/log_edit` so per-guild VN managers can only
mutate rows in their own guild, whatever primary-key ID they pass.

The two-tier permission model:
  * ``AUTHORIZED_USERS`` (bot operators, env-loaded): bypass; can
    mutate any guild's rows.
  * ``VN_MANAGER_USER_IDS`` / ``VN_MANAGER_ROLE_IDS`` (per-server
    admins): scoped to the guild they're running the command in.

Legacy rows with ``guild_id IS NULL`` are accessible from any guild
because they predate per-guild scoping.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import utils
from lib.utils import ValidationError, require_same_guild


def _fake_interaction(user_id: int, guild_id: int | None) -> SimpleNamespace:
    """Minimal stand-in for discord.Interaction.

    The helper only touches ``user.id`` and ``guild_id``, so a
    SimpleNamespace with those attributes is enough.
    """
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
    )


class TestRequireSameGuild:
    def test_same_guild_passes(self) -> None:
        # User in guild 100, row in guild 100: allowed.
        i = _fake_interaction(user_id=42, guild_id=100)
        require_same_guild(i, row_guild_id=100)  # no exception

    def test_cross_guild_raises(self) -> None:
        # User in guild 100 trying to touch a row in guild 200: blocked.
        i = _fake_interaction(user_id=42, guild_id=100)
        with pytest.raises(ValidationError) as info:
            require_same_guild(i, row_guild_id=200, entity_name="pool entry")
        # The user-facing message names the entity and stays generic.
        assert "pool entry" in info.value.user_message
        # The dev-facing string carries the diagnostic detail.
        assert "100" in str(info.value)
        assert "200" in str(info.value)

    def test_legacy_null_guild_passes(self) -> None:
        # Legacy rows have guild_id IS NULL and apply globally.
        i = _fake_interaction(user_id=42, guild_id=100)
        require_same_guild(i, row_guild_id=None)  # no exception

    def test_dm_context_raises(self) -> None:
        # No guild on the interaction (DM). Should never happen with
        # @app_commands.guild_only() but defensive double-check.
        i = _fake_interaction(user_id=42, guild_id=None)
        with pytest.raises(ValidationError) as info:
            require_same_guild(i, row_guild_id=100)
        assert "server" in info.value.user_message.lower()

    def test_authorized_user_bypasses_cross_guild(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user in AUTHORIZED_USERS (bot operator) can mutate any
        guild's rows. This is the host's escape hatch for cross-server
        clean-up: VN managers can't, but operators can.
        """
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [42])
        i = _fake_interaction(user_id=42, guild_id=100)
        # Cross-guild AND DM both pass for an authorized user.
        require_same_guild(i, row_guild_id=200)
        i_dm = _fake_interaction(user_id=42, guild_id=None)
        require_same_guild(i_dm, row_guild_id=200)

    def test_non_authorized_user_still_blocked(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Authorized list contains someone else; the requester is a
        # plain VN manager, so the cross-guild check still applies.
        monkeypatch.setattr(utils, "AUTHORIZED_USER_IDS", [99])
        i = _fake_interaction(user_id=42, guild_id=100)
        with pytest.raises(ValidationError):
            require_same_guild(i, row_guild_id=200)

    def test_default_entity_name(self) -> None:
        # When entity_name isn't passed, the message uses the generic
        # "entry" wording.
        i = _fake_interaction(user_id=42, guild_id=100)
        with pytest.raises(ValidationError) as info:
            require_same_guild(i, row_guild_id=200)
        assert "entry" in info.value.user_message

    @pytest.mark.parametrize("entity", ["pool entry", "log", "nomination"])
    def test_custom_entity_name_in_message(self, entity: str) -> None:
        i = _fake_interaction(user_id=42, guild_id=100)
        with pytest.raises(ValidationError) as info:
            require_same_guild(i, row_guild_id=200, entity_name=entity)
        assert entity in info.value.user_message
