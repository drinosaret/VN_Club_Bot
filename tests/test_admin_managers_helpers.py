"""Pure helpers from ``cogs/admin_managers.py``.

The cog itself is heavy to test in isolation (discord.py + slash command
plumbing), but its input-validation and rendering helpers are pure
Python and worth direct coverage: they gate every ``/manage_managers``
invocation and a buggy ``_format_principal_line`` would mis-render the
list of managers in the audit-style ``list`` action.
"""
from __future__ import annotations

import pytest

from cogs.admin_managers import _format_principal_line, _parse_guild_id
from lib.utils import ValidationError


class TestParseGuildId:
    @pytest.mark.parametrize("raw,expected", [
        ("123456789", 123456789),
        ("  42  ", 42),                  # whitespace trimmed
        ("0", 0),
        ("99999999999999999999", 99999999999999999999),  # big int, wider than any Discord ID
    ])
    def test_valid_numeric_strings(self, raw, expected) -> None:
        assert _parse_guild_id(raw) == expected

    @pytest.mark.parametrize("bad", [
        "", "abc", "12abc", "abc12", "1.5", "1,2", None, "1 2",
    ])
    def test_invalid_raises_validation_error(self, bad) -> None:
        with pytest.raises(ValidationError) as exc:
            _parse_guild_id(bad)
        # ValidationError surfaces a user-friendly message.
        assert "guild_id" in exc.value.user_message.lower()


class _FakeGuild:
    """Minimal stand-in for discord.Guild; only get_role is exercised."""

    def __init__(self, roles: dict[int, "_FakeRole"]) -> None:
        self._roles = roles

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class _FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


class TestFormatPrincipalLine:
    def test_user_mentions_by_id(self) -> None:
        out = _format_principal_line(
            bot=None, guild=None,
            principal_type="user", principal_id=42,
            added_by_user_id=99, added_at="2026-06-01",
        )
        assert "<@42>" in out, "user line should use a Discord mention token"
        assert "<@99>" in out, "added_by should mention the granter"
        # The stored date renders as a Discord date timestamp (UTC midnight).
        assert "<t:1780272000:d>" in out
        assert "2026-06-01" not in out

    def test_full_sqlite_timestamp_renders_as_discord_date(self) -> None:
        out = _format_principal_line(
            bot=None, guild=None,
            principal_type="user", principal_id=42,
            added_by_user_id=99, added_at="2026-06-01 12:00:00",
        )
        assert "<t:1780315200:d>" in out

    def test_unparseable_added_at_is_left_out(self) -> None:
        out = _format_principal_line(
            bot=None, guild=None,
            principal_type="user", principal_id=42,
            added_by_user_id=99, added_at="not a date",
        )
        assert out == "<@42> · added by <@99>"

    def test_role_resolves_via_guild(self) -> None:
        guild = _FakeGuild(roles={7: _FakeRole(name="VN Manager")})
        out = _format_principal_line(
            bot=None, guild=guild,
            principal_type="role", principal_id=7,
            added_by_user_id=1, added_at="2026-06-01",
        )
        assert "VN Manager" in out
        assert "(role)" in out

    def test_role_falls_back_when_not_resolvable(self) -> None:
        # Guild is None (listing a guild the bot is not in) or the role no
        # longer exists: the row still renders, labelled "Unknown role".
        out_no_guild = _format_principal_line(
            bot=None, guild=None,
            principal_type="role", principal_id=7,
            added_by_user_id=1, added_at="2026-06-01",
        )
        assert out_no_guild.startswith("Unknown role")

        empty_guild = _FakeGuild(roles={})
        out_no_role = _format_principal_line(
            bot=None, guild=empty_guild,
            principal_type="role", principal_id=7,
            added_by_user_id=1, added_at="2026-06-01",
        )
        assert out_no_role.startswith("Unknown role")

    def test_unknown_principal_type_does_not_crash(self) -> None:
        # Forward-compat: a future migration introducing a new principal
        # type would corrupt the listing if this branch raised. Verify
        # the fallback renders a deterministic placeholder instead.
        out = _format_principal_line(
            bot=None, guild=None,
            principal_type="webhook", principal_id=123,
            added_by_user_id=1, added_at="2026-06-01",
        )
        assert "webhook:123" in out

    def test_added_by_none_omits_the_granter(self) -> None:
        # added_by_user_id can be NULL.
        # The "added by" part is left out rather than rendering "<@None>".
        out = _format_principal_line(
            bot=None, guild=None,
            principal_type="user", principal_id=42,
            added_by_user_id=None, added_at="2020-01-01",
        )
        assert "added by" not in out
        assert "<@None>" not in out
        assert out == "<@42> · <t:1577836800:d>"
