"""resolve_vn_from_input + autocomplete value encode/decode.

The function must:
- Round-trip a freshly-created autocomplete value back to its raw VNDB ID
- Recover a VNDB ID from a Discord display-format echo (`[vXXXXX]`)
- Defensively extract an embedded `${vndb|...}` template from garbled input
- REJECT garbage with None instead of rubber-stamping it
"""
from __future__ import annotations

import pytest

from lib.utils import resolve_vn_from_input
from lib.vndb_search import create_autocomplete_value, parse_autocomplete_value


class TestAutocompleteValueRoundtrip:
    """create_autocomplete_value → parse_autocomplete_value should be lossless."""

    def test_basic(self) -> None:
        val = create_autocomplete_value("v39169", "jp", source="vndb")
        parsed = parse_autocomplete_value(val)
        assert parsed == ("v39169", "jp", "vndb")

    def test_no_source(self) -> None:
        val = create_autocomplete_value("v39169", "en")
        parsed = parse_autocomplete_value(val)
        assert parsed == ("v39169", "en", None)

    def test_numeric_id(self) -> None:
        val = create_autocomplete_value(39169, "jp", source="vndb")
        parsed = parse_autocomplete_value(val)
        # Numeric IDs come back as strings; that's expected.
        assert parsed == ("39169", "jp", "vndb")

    @pytest.mark.parametrize("garbage", [
        "",
        "${broken",            # missing closing brace
        "broken}",             # missing opening
        "${nofield}",          # no colon-separator
        "v39169",              # plain ID without template wrapping
    ])
    def test_garbage_returns_none(self, garbage) -> None:
        assert parse_autocomplete_value(garbage) is None


class TestResolveVnFromInput:
    """The end-to-end ID resolver used by /finish, /nominate, /reviews."""

    @pytest.mark.asyncio
    async def test_autocomplete_value(self) -> None:
        # Happy path: Discord submitted the autocomplete value verbatim.
        assert await resolve_vn_from_input("${vndb|v39169:jp}") == "v39169"

    @pytest.mark.asyncio
    async def test_autocomplete_value_no_v_prefix(self) -> None:
        # Some legacy values omit the 'v' inside the template; the
        # resolver re-attaches it.
        assert await resolve_vn_from_input("${vndb|39169:jp}") == "v39169"

    @pytest.mark.asyncio
    async def test_raw_v_id(self) -> None:
        assert await resolve_vn_from_input("v17") == "v17"

    @pytest.mark.asyncio
    async def test_raw_numeric(self) -> None:
        assert await resolve_vn_from_input("17") == "v17"

    @pytest.mark.asyncio
    async def test_strips_whitespace(self) -> None:
        assert await resolve_vn_from_input("   v17  ") == "v17"

    @pytest.mark.asyncio
    async def test_display_echo_with_id_suffix(self) -> None:
        # When Discord echoes the autocomplete *display label* instead of
        # the value (some client versions do this), the [vXXXXX] suffix
        # in the label is the recovery hook.
        label = "Steins;Gate - 2009-10-15 . 9.1/10 [v2002]"
        assert await resolve_vn_from_input(label) == "v2002"

    @pytest.mark.asyncio
    async def test_embedded_template_in_garbage(self) -> None:
        """Discord can send a mangled string that contains a valid
        autocomplete template substring plus extra text. The embedded
        template is extracted rather than the whole string being taken
        as a VNDB ID.
        """
        garbage = "${vndb|v39169:jp} kind: Seasonal voting (3-month range)"
        assert await resolve_vn_from_input(garbage) == "v39169"

    @pytest.mark.asyncio
    async def test_embedded_template_with_leading_v(self) -> None:
        # Same shape, with a spurious leading 'v'.
        garbage = "v${vndb|v39169:jp} kind: Seasonal voting (3-month range)"
        assert await resolve_vn_from_input(garbage) == "v39169"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("garbage", [
        "garbage input",                  # plain prose
        "v",                              # just the prefix
        "v ",                             # prefix with trailing space, no digits
        "abc123",                         # not a valid ID shape
        "vv123",                          # double-v
    ])
    async def test_unrecognized_returns_none(self, garbage) -> None:
        """The fallback rejects anything that doesn't match `v?\\d+`
        rather than prepending 'v' and treating it as a raw VNDB ID, so
        the caller can raise a friendly validation error instead of
        sending garbage to the VNDB API.
        """
        assert await resolve_vn_from_input(garbage) is None

    @pytest.mark.asyncio
    async def test_empty_returns_none(self) -> None:
        assert await resolve_vn_from_input("") is None

    @pytest.mark.asyncio
    async def test_none_returns_none(self) -> None:
        assert await resolve_vn_from_input(None) is None  # type: ignore[arg-type]
