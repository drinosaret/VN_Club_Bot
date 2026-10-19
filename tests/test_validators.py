"""Validators and small format helpers in ``lib/utils.py``.

These are the building blocks that touch every command: month
parsing, text truncation and points/rating display.
If one breaks, every embed in the bot is silently corrupted.
"""
from __future__ import annotations

import pytest

from lib.utils import (
    calculate_non_monthly_points,
    format_points_display,
    inert_text,
    is_month_in_range,
    safe_int_conversion,
    truncate_text,
    validate_month_format,
)


class TestValidateMonthFormat:
    @pytest.mark.parametrize("month", [
        "2026-01", "2026-12", "1999-06", "2099-07",
    ])
    def test_valid_months(self, month: str) -> None:
        assert validate_month_format(month) is True

    @pytest.mark.parametrize("month", [
        "",                # empty
        "2026",            # missing month
        "2026-",           # trailing dash, no month
        "2026-13",         # month 13 doesn't exist
        "2026-00",         # month 0 doesn't exist
        "2026-1",          # missing leading zero
        "26-01",           # short year
        "2026/01",         # wrong separator
        "January 2026",    # not YYYY-MM at all
        "2026-01-01",      # has day component
        "abcd-ef",         # non-digits
    ])
    def test_invalid_months(self, month: str) -> None:
        assert validate_month_format(month) is False

    def test_none_is_falsy(self) -> None:
        # The helper guards against falsy input; None would crash strptime
        # otherwise.
        assert validate_month_format(None) is False  # type: ignore[arg-type]


class TestIsMonthInRange:
    def test_inclusive_start(self) -> None:
        assert is_month_in_range("2026-04", "2026-04", "2026-06") is True

    def test_inclusive_end(self) -> None:
        assert is_month_in_range("2026-06", "2026-04", "2026-06") is True

    def test_middle(self) -> None:
        assert is_month_in_range("2026-05", "2026-04", "2026-06") is True

    def test_before_range(self) -> None:
        assert is_month_in_range("2026-03", "2026-04", "2026-06") is False

    def test_after_range(self) -> None:
        assert is_month_in_range("2026-07", "2026-04", "2026-06") is False

    def test_single_month_range(self) -> None:
        # start == end (e.g. monthly cycles): only that exact month matches.
        assert is_month_in_range("2026-05", "2026-05", "2026-05") is True
        assert is_month_in_range("2026-06", "2026-05", "2026-05") is False


class TestTruncateText:
    def test_short_unchanged(self) -> None:
        assert truncate_text("hello", 10) == "hello"

    def test_exact_length_unchanged(self) -> None:
        assert truncate_text("0123456789", 10) == "0123456789"

    def test_long_gets_truncated(self) -> None:
        out = truncate_text("0123456789X", 10)
        assert len(out) == 10
        assert out.endswith("...")

    def test_custom_suffix(self) -> None:
        out = truncate_text("0123456789X", 10, suffix="…")
        assert len(out) == 10
        assert out.endswith("…")

    def test_empty_input(self) -> None:
        # Falsy input bypasses truncation.
        assert truncate_text("", 10) == ""


class TestFormatPoints:
    def test_thousand_separators(self) -> None:
        # 1234 → "1,234": the comma is the spec.
        out = format_points_display(1234, 5678)
        assert "1,234" in out
        assert "5,678" in out

    def test_zero_to_positive(self) -> None:
        out = format_points_display(0, 10)
        assert "0" in out and "10" in out


class TestInertText:
    def test_link_syntax_is_escaped(self) -> None:
        out = inert_text("[click](https://example.invalid)")
        assert out.startswith("\\[click\\]")

    def test_markdown_markers_escaped_once(self) -> None:
        assert inert_text("**bold** _it_ `code`") == "\\*\\*bold\\*\\* \\_it\\_ \\`code\\`"
        # An existing backslash is escaped itself, so it can't re-arm a marker.
        assert inert_text("\\*") == "\\\\\\*"

    def test_spoiler_tags_survive(self) -> None:
        assert inert_text("the ||twist|| was good") == "the ||twist|| was good"

    def test_newlines_flattened(self) -> None:
        assert inert_text("a\nb\tc") == "a b c"

    def test_limit_applies_before_escaping(self) -> None:
        out = inert_text("*" * 10, limit=3)
        assert out == "\\*\\*\\*…"

    def test_empty(self) -> None:
        assert inert_text(None) == ""
        assert inert_text("") == ""


class TestNonMonthlyPoints:
    def test_floor_to_one(self) -> None:
        # Multiplier currently rounds towards 0; even tiny monthly point
        # values should award at least 1.
        assert calculate_non_monthly_points(0) == 1
        assert calculate_non_monthly_points(1) >= 1

    def test_returns_int(self) -> None:
        assert isinstance(calculate_non_monthly_points(10), int)


class TestSafeIntConversion:
    @pytest.mark.parametrize("raw,expected", [
        ("42", 42),
        (42, 42),
        ("0", 0),
        ("-5", -5),
    ])
    def test_valid(self, raw, expected) -> None:
        assert safe_int_conversion(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", None, "1.5", "12e3"])
    def test_invalid_uses_default(self, raw) -> None:
        assert safe_int_conversion(raw, default=99) == 99

    def test_default_default_is_zero(self) -> None:
        assert safe_int_conversion("garbage") == 0


