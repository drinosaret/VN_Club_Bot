"""House-style helpers in ``lib/style.py``."""
from __future__ import annotations

import pytest

from lib import style


class TestMonths:
    @pytest.mark.parametrize("raw,short,long", [
        ("2026-09", "Sep 2026", "September 2026"),
        ("2026-01", "Jan 2026", "January 2026"),
        ("2025-12", "Dec 2025", "December 2025"),
        # A trailing day is tolerated; only year and month are read.
        ("2026-05-17", "May 2026", "May 2026"),
    ])
    def test_valid_months(self, raw, short, long) -> None:
        assert style.month_short(raw) == short
        assert style.month_long(raw) == long

    @pytest.mark.parametrize("raw", ["", "Summer 2026", "2026", "2026-13", "2026-00", "abcd-ef"])
    def test_unparseable_input_passes_through(self, raw) -> None:
        assert style.month_short(raw) == raw
        assert style.month_long(raw) == raw

    def test_none_becomes_empty(self) -> None:
        assert style.month_short(None) == ""
        assert style.month_long(None) == ""


class TestMonthRange:
    def test_same_year_names_the_year_once(self) -> None:
        assert style.month_range("2026-04", "2026-06") == "Apr to Jun 2026"
        assert style.month_range("2026-04", "2026-06", long=True) == "April to June 2026"

    def test_single_month(self) -> None:
        assert style.month_range("2026-04", "2026-04") == "Apr 2026"
        assert style.month_range("2026-04", None) == "Apr 2026"
        assert style.month_range("2026-04", "", long=True) == "April 2026"

    def test_cross_year_gives_each_end_its_year(self) -> None:
        assert style.month_range("2025-11", "2026-01") == "Nov 2025 to Jan 2026"
        assert style.month_range("2025-11", "2026-01", long=True) == "November 2025 to January 2026"

    def test_unparseable_ends_pass_through(self) -> None:
        assert style.month_range("later", "2026-01") == "later to Jan 2026"


class TestTitle:
    def test_emoji_and_parts(self) -> None:
        assert style.title("🏆", "Leaderboard", "Summer 2026") == "🏆 Leaderboard · Summer 2026"

    def test_empty_parts_are_skipped(self) -> None:
        assert style.title("📋", "Pool", "", "August 2026", "") == "📋 Pool · August 2026"

    def test_no_emoji(self) -> None:
        assert style.title(None, "Pool", "August 2026") == "Pool · August 2026"
        assert style.title("", "Pool") == "Pool"


class TestPlural:
    def test_singular_and_plural(self) -> None:
        assert style.plural(1, "vote") == "1 vote"
        assert style.plural(0, "vote") == "0 votes"
        assert style.plural(3, "vote") == "3 votes"

    def test_irregular_plural(self) -> None:
        assert style.plural(1, "entry", "entries") == "1 entry"
        assert style.plural(2, "entry", "entries") == "2 entries"

    def test_thousands_separator(self) -> None:
        assert style.plural(1234, "log") == "1,234 logs"
        assert style.plural(1_000_000, "point") == "1,000,000 points"


class TestFooter:
    def test_single_page_has_no_page_part(self) -> None:
        assert style.footer(0, 1, "3 entries", "hint") == "3 entries · hint"

    def test_multi_page_leads_with_the_page(self) -> None:
        assert style.footer(1, 5, "30 logs", "hint") == "Page 2/5 · 30 logs · hint"

    def test_empty_bits_are_skipped(self) -> None:
        assert style.footer(0, 2, "", None, "hint") == "Page 1/2 · hint"
        assert style.footer(0, 1, "", None) == ""

    def test_defaults_are_a_single_page(self) -> None:
        assert style.footer() == ""


class TestStatusLines:
    def test_prefixes(self) -> None:
        assert style.ok("Saved.") == "✅ Saved."
        assert style.error("Not found.") == "❌ Not found."
        assert style.info("Nothing yet.") == "ℹ️ Nothing yet."


# ---------------------------------------------------------------- labels ---

def test_link_label_swaps_markup_for_full_width():
    from lib.utils import link_label
    out = link_label("Dies irae ~Interview~ [x](y) a*b|c")
    assert "\\" not in out
    assert out == "Dies irae ～Interview～ ［x］(y) a＊b｜c"


def test_inert_text_keeps_custom_emoji_whole():
    from lib.utils import inert_text
    emoji = "<:SomeEmoji:123456789012345678>"
    assert inert_text(f"nice {emoji} *x*") == f"nice {emoji} \\*x\\*"
    # A cut never lands inside the emoji, and an emoji counts as two
    # characters rather than the length of its code.
    assert inert_text(f"ab {emoji} cd", 4) == "ab…"
    assert inert_text(f"ab {emoji} cd", 8) == f"ab {emoji} cd"
    assert inert_text(f"ab {emoji} cdef", 7) == f"ab {emoji} c…"
