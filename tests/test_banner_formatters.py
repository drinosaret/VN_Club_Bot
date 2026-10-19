"""Pure formatters used by the monthly/seasonal banner renderer.

These run thousands of times per render and feed directly into the
image. Mishandling None here either crashes PIL or displays
``None`` literally in a stat cell.
"""
from __future__ import annotations

import pytest

from lib.monthly_banner import (
    format_length_tier,
    _format_read_time_hours,
    days_in_month,
    month_label_for,
)
from lib.vndb_api import _format_platforms


class TestFormatLengthTier:
    @pytest.mark.parametrize("raw,expected", [
        (1, "Very Short"),
        (2, "Short"),
        (3, "Medium"),
        (4, "Long"),
        (5, "Very Long"),
    ])
    def test_numeric_codes(self, raw, expected) -> None:
        assert format_length_tier(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("1", "Very Short"),
        ("3", "Medium"),
        ("5", "Very Long"),
    ])
    def test_numeric_string_codes(self, raw, expected) -> None:
        # VNDB sometimes returns the length code as a string.
        assert format_length_tier(raw) == expected

    @pytest.mark.parametrize("raw", ["Long", "Very Short", "Medium"])
    def test_pretty_strings_pass_through(self, raw) -> None:
        assert format_length_tier(raw) == raw

    def test_none(self) -> None:
        assert format_length_tier(None) is None

    def test_empty_string(self) -> None:
        assert format_length_tier("") is None
        assert format_length_tier("   ") is None


class TestFormatReadTime:
    def test_under_ten_hours_one_decimal(self) -> None:
        # 90 minutes = 1.5 hr
        assert _format_read_time_hours(90) == "~1.5 hr"

    def test_ten_hours_exactly_rounds(self) -> None:
        # Crossover at 10 hours: drops the decimal place.
        out = _format_read_time_hours(600)  # exactly 10.0 hr
        assert out == "~10 hr"

    def test_over_ten_hours_rounds(self) -> None:
        # 700 min = 11.67 hr → "~12 hr"
        assert _format_read_time_hours(700) == "~12 hr"

    @pytest.mark.parametrize("bad", [None, 0, -5])
    def test_falsy_or_negative(self, bad) -> None:
        assert _format_read_time_hours(bad) is None


class TestMonthLabelFor:
    @pytest.mark.parametrize("yyyy_mm,expected", [
        ("2026-01", "January 2026"),
        ("2026-06", "June 2026"),
        ("2026-12", "December 2026"),
    ])
    def test_canonical(self, yyyy_mm, expected) -> None:
        assert month_label_for(yyyy_mm) == expected


class TestDaysInMonth:
    @pytest.mark.parametrize("yyyy_mm,expected", [
        ("2026-01", 31),
        ("2026-02", 28),   # 2026 isn't a leap year
        ("2024-02", 29),   # 2024 IS a leap year
        ("2026-04", 30),
        ("2026-12", 31),
    ])
    def test_calendar(self, yyyy_mm, expected) -> None:
        assert days_in_month(yyyy_mm) == expected


class TestFormatPlatforms:
    def test_none(self) -> None:
        assert _format_platforms(None) is None

    def test_empty(self) -> None:
        assert _format_platforms([]) is None

    def test_windows_only(self) -> None:
        assert _format_platforms(["win"]) == "Windows"

    def test_windows_first_when_present(self) -> None:
        # Windows always anchors the head, regardless of VNDB's order.
        out = _format_platforms(["swi", "win", "ps4"])
        assert out.startswith("Windows")
        assert "Switch" in out
        assert "PS4" in out

    def test_caps_at_three_visible(self) -> None:
        out = _format_platforms(
            ["win", "swi", "ps4", "psv", "ps5", "xbo"]
        )
        # 3 visible + "+N" overflow tag.
        assert out is not None
        assert "+3" in out
        # The first three slots get filled (Windows pinned first).
        assert out.count("·") == 3

    def test_unknown_code_falls_through_uppercased(self) -> None:
        out = _format_platforms(["win", "zzz"])
        # Codes not in _PLATFORM_LABELS get uppercased to make them visible.
        assert out is not None
        assert "Windows" in out
        assert "ZZZ" in out

    def test_dedupes_duplicates(self) -> None:
        # VNDB shouldn't return dupes, but defensively, the same code listed
        # twice doesn't double up the output.
        out = _format_platforms(["win", "win", "swi"])
        assert out is not None
        assert out.count("Windows") == 1
