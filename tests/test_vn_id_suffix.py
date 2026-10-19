"""Display-format echo recovery for autocompleted VN IDs.

Discord clients can submit the autocomplete *display label* (shaped like
``"Title · YYYY-MM-DD · rating/10 [vXXXXX]"``) instead of the underlying
value (``${vndb|v2002:en}``). The bot survives this by extracting the
``[vXXXXX]`` suffix from the label.

These tests call ``resolve_vn_from_input`` directly, so drift in the real
extractor is caught.
"""
from __future__ import annotations

import pytest

from lib.utils import resolve_vn_from_input


class TestVnIdRecoveryFromDisplayLabel:
    @pytest.mark.asyncio
    async def test_full_label(self) -> None:
        label = "Steins;Gate - 2009-10-15 . 9.1/10 [v2002]"
        assert await resolve_vn_from_input(label) == "v2002"

    @pytest.mark.asyncio
    async def test_truncated_label(self) -> None:
        # Long titles get truncated, but the [vXXXXX] suffix is preserved
        # by the label builder. The extractor must still find it.
        label = "This is a very long title... - 2024-12-31 . 9.6/10 [v12345]"
        assert await resolve_vn_from_input(label) == "v12345"

    @pytest.mark.asyncio
    async def test_label_without_badges(self) -> None:
        # The simplest case: no release date, no rating, just title and ID.
        label = "Some VN Title [v99999]"
        assert await resolve_vn_from_input(label) == "v99999"

    @pytest.mark.asyncio
    async def test_legacy_label_without_id_suffix(self) -> None:
        # Pre-id-suffix labels can't be recovered. The strict fallback
        # rejects this rather than rubber-stamping.
        legacy = "Steins;Gate - 2009-10-15 - 9.1/10"
        assert await resolve_vn_from_input(legacy) is None

    @pytest.mark.asyncio
    async def test_label_with_japanese_title(self) -> None:
        # The extractor is regex-based on `[vNNN]`, so Unicode in the
        # title doesn't affect recovery.
        label = "月姫 -A piece of blue glass moon- [v56439]"
        assert await resolve_vn_from_input(label) == "v56439"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("vn_id", ["v1", "v17", "v12345", "v9999999"])
    async def test_id_range(self, vn_id: str) -> None:
        # Single-digit, mid-range, and large IDs all extract correctly.
        label = f"Some Title [{vn_id}]"
        assert await resolve_vn_from_input(label) == vn_id
