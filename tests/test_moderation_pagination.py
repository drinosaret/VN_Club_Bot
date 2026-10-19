"""Pagination math for the vote-moderation panel.

Discord's `Select` is capped at 25 options. The moderation panel pages
through the full vote history using ``Newer`` / ``Older`` buttons.

These tests exercise the pure pagination logic (slice arithmetic, page
clamping, body-text rendering). The Discord ``View`` plumbing itself
isn't tested (that needs a Discord interaction harness), but the math
that *drives* the view is covered here.
"""
from __future__ import annotations

import pytest

from cogs.vn_cycle import _build_moderation_text


# Fake cycle row with `id` at the CYCLE_ID index. The constant is not
# imported because the order of the row tuple is what matters for
# `cycle_row[CYCLE_ID]` access: index 0 is `id`.
FAKE_CYCLE_ROW = (42,) + (None,) * 30


class TestBuildModerationText:
    def test_zero_votes(self) -> None:
        out = _build_moderation_text("monthly", FAKE_CYCLE_ROW, [])
        assert "Total votes: **0**" in out
        assert "No votes cast yet" in out

    def test_under_25_no_pagination_text(self) -> None:
        # With 25 or fewer votes, no page line should appear.
        votes = [(i, 1, 1, "T", "2026-05-01") for i in range(10)]
        out = _build_moderation_text("monthly", FAKE_CYCLE_ROW, votes)
        assert "Total votes: **10**" in out
        assert "Page " not in out
        assert "newest first" not in out
        assert "Newer" not in out
        assert "Older" not in out

    def test_exactly_25_no_pagination_text(self) -> None:
        # Boundary: 25 fits in one Select, no need to mention pagination.
        votes = [(i, 1, 1, "T", "2026-05-01") for i in range(25)]
        out = _build_moderation_text("monthly", FAKE_CYCLE_ROW, votes)
        assert "Page " not in out

    def test_over_25_shows_page_line(self) -> None:
        # 30 votes, page 0: a style footer line for page 1 of 2. Newer and
        # Older are button labels only, so the body does not mention them.
        votes = [(i, 1, 1, "T", "2026-05-01") for i in range(30)]
        out = _build_moderation_text("monthly", FAKE_CYCLE_ROW, votes, page=0)
        assert "_Page 1/2 · newest first_" in out
        assert "Newer" not in out
        assert "Older" not in out

    def test_last_page_shows_last_page_number(self) -> None:
        votes = [(i, 1, 1, "T", "2026-05-01") for i in range(30)]
        out = _build_moderation_text("monthly", FAKE_CYCLE_ROW, votes, page=1)
        assert "Page 2/2 · newest first" in out

    def test_multi_page(self) -> None:
        # 80 votes: 4 pages of up to 25.
        votes = [(i, 1, 1, "T", "2026-05-01") for i in range(80)]
        for page in range(4):
            out = _build_moderation_text("monthly", FAKE_CYCLE_ROW, votes, page=page)
            assert f"Page {page + 1}/4 · newest first" in out, f"page {page} body wrong"

    def test_page_default_is_zero(self) -> None:
        # The page kwarg has a default of 0 so existing callers don't break.
        votes = [(i, 1, 1, "T", "2026-05-01") for i in range(30)]
        out_default = _build_moderation_text("monthly", FAKE_CYCLE_ROW, votes)
        out_explicit = _build_moderation_text(
            "monthly", FAKE_CYCLE_ROW, votes, page=0,
        )
        assert out_default == out_explicit


class TestPaginationMath:
    """The page-clamp formula that `refresh()` uses to keep the admin's
    position when votes get removed under them. Replicated here as a
    pure function so the math is unit-testable.
    """

    @staticmethod
    def clamp_page(votes_count: int, page: int) -> int:
        """Mirror of refresh()'s clamp logic."""
        max_page = max(0, (votes_count - 1) // 25)
        return min(page, max_page)

    @pytest.mark.parametrize("count,page,expected", [
        (0, 0, 0),         # Empty list: stay on page 0.
        (0, 5, 0),         # Wildly out-of-range page on empty list: clamp to 0.
        (25, 0, 0),        # Single page, on page 0.
        (25, 1, 0),        # Single page, attempted page 1 → clamp.
        (26, 0, 0),        # Two pages, on page 0.
        (26, 1, 1),        # Two pages, on page 1 (the last valid page).
        (26, 5, 1),        # Two pages, attempted page 5 → clamp to 1.
        (50, 1, 1),        # Two full pages, on page 1.
        (51, 1, 1),        # Three pages possible, but page 1 stays page 1.
        (51, 2, 2),        # Three pages, last is page 2.
        (51, 99, 2),       # Far over: clamp.
    ])
    def test_clamp(self, count, page, expected) -> None:
        assert self.clamp_page(count, page) == expected

    def test_clamp_preserves_admin_position(self) -> None:
        # Scenario: 60 votes (3 pages: 0,1,2). Admin on page 2.
        # Remove one vote from page 2. Now 59 votes (still 3 pages).
        # Admin should stay on page 2.
        assert self.clamp_page(60, 2) == 2
        assert self.clamp_page(59, 2) == 2

    def test_clamp_falls_back_when_page_empties(self) -> None:
        # Scenario: 51 votes (3 pages: 0,1,2; last page has 1 vote).
        # Admin on page 2. Removes that last vote → 50 votes (2 pages).
        # Clamp drops them back to page 1.
        assert self.clamp_page(51, 2) == 2
        assert self.clamp_page(50, 2) == 1
