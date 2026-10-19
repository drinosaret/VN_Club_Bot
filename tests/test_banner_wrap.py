"""Pixel word-wrap for the banner's description callout.

The wrapper is the only truncator on the image path, so its overflow
decision is what puts (or withholds) the single trailing ellipsis. It reads
nothing off ``draw`` but ``textlength``, so a monospace stub exercises it
without fonts or a canvas.
"""
from __future__ import annotations

import pytest

from lib.monthly_banner import MonthlyBannerGenerator, _wrap_lines
from lib.pillow_helpers import IMAGE_ELLIPSIS


CHAR_W = 10


class FakeDraw:
    """Monospace metrics: one character is ``CHAR_W`` pixels wide."""

    def textlength(self, text: str, font=None) -> int:
        return len(text) * CHAR_W


@pytest.fixture
def wrap():
    gen = MonthlyBannerGenerator()
    draw = FakeDraw()

    def _wrap(text: str, cols: int, max_lines: int) -> list[str]:
        return gen._wrap_text_to_pixel_width(
            draw, text, None, cols * CHAR_W, max_lines,
        )

    return _wrap


def _assert_single_trailing_ellipsis(lines: list[str]) -> None:
    """Source text may contain ellipses mid-sentence; only the tail of the
    last line is the truncation marker, and there is exactly one of it."""
    tail = lines[-1]
    assert tail.endswith(IMAGE_ELLIPSIS)
    for doubled in ("......", "……", "...…", "…...", "..…"):
        assert not tail.endswith(doubled), tail


class TestNoFalseOverflow:
    def test_text_that_fits_gets_no_ellipsis(self, wrap) -> None:
        lines = wrap("alpha beta gamma", 20, 4)
        assert lines == ["alpha beta gamma"]

    def test_paragraph_breaks_do_not_trip_overflow(self, wrap) -> None:
        # Blank lines and collapsed whitespace shrink the rendered character
        # count; that must not read as "text left over" and ellipsize.
        lines = wrap("alpha beta\n\ngamma  delta", 20, 6)
        assert lines == ["alpha beta", "", "gamma delta"]

    def test_trailing_blank_lines_are_dropped(self, wrap) -> None:
        lines = wrap("alpha\n\n", 20, 6)
        assert lines == ["alpha"]

    def test_exactly_filling_max_lines_is_not_overflow(self, wrap) -> None:
        lines = wrap("alpha beta gamma delta", 11, 2)
        assert lines == ["alpha beta", "gamma delta"]


class TestOverflow:
    def test_single_trailing_ellipsis(self, wrap) -> None:
        lines = wrap("alpha beta gamma delta epsilon", 11, 2)
        assert len(lines) == 2
        _assert_single_trailing_ellipsis(lines)

    def test_source_ellipsis_is_not_doubled(self, wrap) -> None:
        # The cut lands right after prose ellipses.
        lines = wrap("alpha beta…… gamma delta", 12, 1)
        assert lines == ["alpha bet..."]
        _assert_single_trailing_ellipsis(lines)

    def test_source_ellipsis_mid_line_is_kept(self, wrap) -> None:
        lines = wrap("alpha…… beta gamma delta epsilon", 13, 1)
        assert lines == ["alpha…… be..."]

    def test_trailing_period_replaced_not_appended(self, wrap) -> None:
        lines = wrap("alpha beta. gamma delta epsilon", 11, 1)
        assert lines == ["alpha be..."]

    def test_remaining_paragraph_counts_as_overflow(self, wrap) -> None:
        lines = wrap("alpha beta\nsecond paragraph", 11, 1)
        assert lines == ["alpha be..."]

    def test_ellipsis_never_lands_on_a_blank_line(self, wrap) -> None:
        lines = wrap("alpha\n\nbeta gamma delta", 11, 2)
        assert lines[-1] != ""
        assert all(line.strip() or line == "" for line in lines)

    def test_line_stays_within_width(self, wrap) -> None:
        lines = wrap("aaaaaaaaaa bbbbbbbbbb cccccccccc", 11, 1)
        assert len(lines[0]) <= 11
        assert lines[0].endswith(IMAGE_ELLIPSIS)


class TestBaselineDots:
    """U+2026 is centered on the em box in the Japanese font cascade, so it
    floats at x-height. Anything drawn into an image marks truncation with
    periods instead."""

    def test_wrapper_never_emits_u2026_as_the_marker(self, wrap) -> None:
        lines = wrap("alpha beta gamma delta epsilon zeta", 11, 2)
        assert not lines[-1].endswith("…")
        assert lines[-1].endswith(IMAGE_ELLIPSIS)

    def test_season_minicard_titles_use_periods(self) -> None:
        draw = FakeDraw()
        lines = _wrap_lines(draw, "alpha beta gamma delta", None,
                            11 * CHAR_W, max_lines=1)
        assert lines[-1].endswith(IMAGE_ELLIPSIS)
        assert "…" not in lines[-1]

    def test_season_minicard_title_that_fits_is_untouched(self) -> None:
        draw = FakeDraw()
        assert _wrap_lines(draw, "alpha", None, 11 * CHAR_W) == ["alpha"]


class TestDegenerateInput:
    def test_empty_text(self, wrap) -> None:
        assert wrap("", 20, 4) == []

    def test_zero_lines(self, wrap) -> None:
        assert wrap("alpha", 20, 0) == []

    def test_word_wider_than_the_box(self, wrap) -> None:
        lines = wrap("supercalifragilistic", 8, 1)
        assert len(lines) == 1
        assert lines[0].endswith(IMAGE_ELLIPSIS)
        assert len(lines[0]) <= 8
