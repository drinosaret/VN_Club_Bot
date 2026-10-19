"""Pure-function tests for the cycle subsystem.

These tests target the functions in `cogs/vn_cycle.py` that don't touch
Discord or the DB: date arithmetic, the tiebreak rule, the close-voting
summary string builder, and the duration parser fed by the OpenVotingModal.

If any of these break, winners are silently mis-promoted, the auto-close
timer mis-fires, or status messages read confusingly, all of which are hard
to catch from the outside once live.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from cogs.vn_cycle import (
    _build_close_voting_summary,
    _days_in_month,
    _format_closes_at_relative,
    _next_month,
    _parse_duration_to_seconds,
    _seconds_until,
    _truncate_label,
    _votes_phrase,
    _winners_after_tiebreak,
)


# Tally rows mirror the production shape coming out of TALLY_VOTES:
#   (nomination_id, vndb_id, title, ..., votes_count); column 6 holds votes.
# Tests only care about the title (col 2) and the vote count (col 6), so a
# helper makes the intent legible without dragging in the full row width.
def _tally_row(*, nomination_id: int, vndb_id: str, title: str, votes: int):
    return (nomination_id, vndb_id, title, None, None, None, votes)


# ============================================================================
# _winners_after_tiebreak
# ============================================================================
# The vote-close path's most consequential pure function. A bug here either
# promotes the wrong tied row or silently drops a clean winner. Tied seats
# are resolved deterministically to the lower vn_titles.id row, which the
# SQL caller delivers first via the ORDER BY vt.id ASC secondary sort. Tests
# below mirror that "lower id wins ties" rule.


class TestWinnersAfterTiebreakSingleWinner:
    def test_clean_winner_promoted(self) -> None:
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=1)
        assert [w[1] for w in winners] == ["v1"]

    def test_top_tie_promotes_earliest(self) -> None:
        # [5, 5] returns the first row. SQL caller orders by id ASC so
        # the first row is the earliest-nominated of the tied set.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=5),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=1)
        assert [w[1] for w in winners] == ["v1"]

    def test_zero_votes_never_wins(self) -> None:
        # [0, 0] → []: no support, no win, regardless of tie shape.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=0),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=0),
        ]
        assert _winners_after_tiebreak(tally, winner_count=1) == []

    def test_sole_nominee_with_votes_wins(self) -> None:
        tally = [_tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10)]
        winners = _winners_after_tiebreak(tally, winner_count=1)
        assert [w[1] for w in winners] == ["v1"]

    def test_sole_nominee_zero_votes_loses(self) -> None:
        tally = [_tally_row(nomination_id=1, vndb_id="v1", title="A", votes=0)]
        assert _winners_after_tiebreak(tally, winner_count=1) == []


class TestWinnersAfterTiebreakMultiWinner:
    def test_two_clean_winners(self) -> None:
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
            _tally_row(nomination_id=3, vndb_id="v3", title="C", votes=3),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=2)
        assert [w[1] for w in winners] == ["v1", "v2"]

    def test_seat_two_tied_promotes_lower_id(self) -> None:
        # [10, 5, 5] with winner_count=2 -> seat 1 clean for the 10,
        # seat 2 contested but won by the lower-id 5 via SQL id ASC.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
            _tally_row(nomination_id=3, vndb_id="v3", title="C", votes=5),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=2)
        assert [w[1] for w in winners] == ["v1", "v2"]

    def test_seat_one_tied_promotes_both_tied(self) -> None:
        # [5, 5, 3] with winner_count=2 -> both tied 5s win, ordered by
        # id ASC. The 3-vote row never gets a seat.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=5),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
            _tally_row(nomination_id=3, vndb_id="v3", title="C", votes=3),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=2)
        assert [w[1] for w in winners] == ["v1", "v2"]

    def test_winner_count_exceeds_tally_returns_all_winners(self) -> None:
        # 3 distinct vote counts and winner_count=5: all 3 are promoted,
        # with no out-of-bounds error.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
            _tally_row(nomination_id=3, vndb_id="v3", title="C", votes=1),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=5)
        assert [w[1] for w in winners] == ["v1", "v2", "v3"]

    def test_trailing_zero_doesnt_win_even_at_capacity(self) -> None:
        # winner_count=2 with [5, 0]: only the first wins. A 0-vote
        # "winner" never claims a seat.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=5),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=0),
        ]
        winners = _winners_after_tiebreak(tally, winner_count=2)
        assert [w[1] for w in winners] == ["v1"]


class TestWinnersAfterTiebreakEdgeCases:
    def test_empty_tally(self) -> None:
        assert _winners_after_tiebreak([], winner_count=1) == []

    def test_winner_count_zero(self) -> None:
        tally = [_tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10)]
        assert _winners_after_tiebreak(tally, winner_count=0) == []


# ============================================================================
# _build_close_voting_summary
# ============================================================================


class TestCloseVotingSummary:
    def test_no_nominees_says_nothing_to_promote(self) -> None:
        out = _build_close_voting_summary(
            "monthly", "June 2026", winners=[], promoted_pool_ids={},
            tally=[],
        )
        assert "no nominees" in out.lower()
        assert "nothing to promote" in out.lower()

    def test_zero_votes_with_nominees_says_no_votes_cast(self) -> None:
        # Voting was open and there were nominees, but nobody voted.
        # Distinguished from "no nominees" so admins can tell the two
        # cases apart from the summary message alone.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=0),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=0),
        ]
        out = _build_close_voting_summary(
            "monthly", "June 2026", winners=[], promoted_pool_ids={},
            tally=tally,
        )
        assert "no votes cast" in out.lower()

    def test_single_winner_promoted_includes_pool_id(self) -> None:
        winners = [_tally_row(nomination_id=1, vndb_id="v1", title="A", votes=5)]
        out = _build_close_voting_summary(
            "monthly", "June 2026", winners=winners,
            promoted_pool_ids={"v1": 42}, tally=winners,
        )
        assert "**A**" in out
        assert "#42" in out
        assert "wins" in out
        # Solo winner with no following row: not a tie.
        assert "tied" not in out.lower()

    def test_single_winner_tie_resolved_acknowledges(self) -> None:
        # Two nominations tied at 5 votes. The first row (lower id by
        # SQL secondary sort) wins. Summary explicitly calls out the
        # tie so the admin doesn't think it was an uncontested win.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=5),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
        ]
        winners = [tally[0]]
        out = _build_close_voting_summary(
            "monthly", "June 2026", winners=winners,
            promoted_pool_ids={"v1": 42}, tally=tally,
        )
        assert "tied" in out.lower()
        assert "earliest nomination" in out.lower()
        assert "**A**" in out
        assert "#42" in out

    def test_single_winner_pool_removed_mid_vote_warns(self) -> None:
        # Edge case: pool entry was deleted between vote-open and vote-
        # close, so the winner can't be auto-promoted. Admin gets a
        # warning, not a silent succeed.
        winners = [_tally_row(nomination_id=1, vndb_id="v1", title="A", votes=3)]
        out = _build_close_voting_summary(
            "monthly", "June 2026", winners=winners,
            promoted_pool_ids={},  # v1 NOT in promoted_pool_ids => missing
            tally=winners,
        )
        assert "**A**" in out
        assert "removed" in out.lower()
        assert "/manage_pool" in out

    def test_multi_winner_lists_all(self) -> None:
        winners = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
        ]
        out = _build_close_voting_summary(
            "seasonal", "Summer 2026", winners=winners,
            promoted_pool_ids={"v1": 7, "v2": 8}, tally=winners,
        )
        assert "**A**" in out
        assert "**B**" in out
        assert "#7" in out
        assert "#8" in out
        assert "Summer 2026" in out
        # No tied seat in this scenario.
        assert "earliest of a tie" not in out

    def test_multi_winner_tied_seat_annotated(self) -> None:
        # [10, 5, 5] with winner_count=2: seat 2 contested. Lower-id
        # 5-vote row takes seat 2. Summary annotates only that seat.
        tally = [
            _tally_row(nomination_id=1, vndb_id="v1", title="A", votes=10),
            _tally_row(nomination_id=2, vndb_id="v2", title="B", votes=5),
            _tally_row(nomination_id=3, vndb_id="v3", title="C", votes=5),
        ]
        winners = [tally[0], tally[1]]
        out = _build_close_voting_summary(
            "monthly", "June 2026", winners=winners,
            promoted_pool_ids={"v1": 7, "v2": 8}, tally=tally,
        )
        assert "**A**" in out
        assert "**B**" in out
        assert "earliest of a tie" in out


# ============================================================================
# _parse_duration_to_seconds
# ============================================================================


class TestParseDurationToSeconds:
    @pytest.mark.parametrize("text,expected", [
        ("30s", 30),
        ("10m", 600),
        ("2h", 7200),
        ("1d", 86400),
        ("1w", 7 * 86400),
        ("24h", 24 * 3600),
    ])
    def test_unit_suffixes(self, text, expected) -> None:
        assert _parse_duration_to_seconds(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("3", 3 * 3600),
        ("12", 12 * 3600),
        ("48", 48 * 3600),
    ])
    def test_bare_int_treated_as_hours_for_backcompat(self, text, expected) -> None:
        # Docstring contract: bare int = hours, so raw integers typed
        # into the OpenVotingModal duration field read as hours.
        assert _parse_duration_to_seconds(text) == expected

    @pytest.mark.parametrize("falsy", [None, "", "  ", "0", "none", "NONE", "None"])
    def test_falsy_means_no_timer(self, falsy) -> None:
        assert _parse_duration_to_seconds(falsy) is None

    @pytest.mark.parametrize("bad", ["abc", "h", "1x", "-5h", "5.5h", "h1"])
    def test_unparseable_returns_none(self, bad) -> None:
        assert _parse_duration_to_seconds(bad) is None

    def test_internal_whitespace_tolerated(self) -> None:
        # `int()` accepts trailing whitespace, so "1 h" parses as 1
        # hour. Lock this behaviour in so a future "tighten the parser"
        # change has to think about it instead of silently breaking
        # users typing "1 h" in the modal.
        assert _parse_duration_to_seconds("1 h") == 3600

    def test_uppercase_normalized(self) -> None:
        assert _parse_duration_to_seconds("2H") == 7200
        assert _parse_duration_to_seconds("3W") == 3 * 7 * 86400


# ============================================================================
# Date helpers: _next_month, _days_in_month
# ============================================================================


class TestNextMonth:
    @pytest.mark.parametrize("current,expected", [
        ("2026-01", "2026-02"),
        ("2026-11", "2026-12"),
        ("2026-12", "2027-01"),  # year rollover
        ("2099-12", "2100-01"),  # century rollover
    ])
    def test_advance_one_month(self, current, expected) -> None:
        assert _next_month(current) == expected


class TestDaysInMonth:
    @pytest.mark.parametrize("yyyy_mm,expected", [
        ("2026-01", 31),
        ("2026-02", 28),   # 2026 is not a leap year
        ("2024-02", 29),   # 2024 IS a leap year
        ("2000-02", 29),   # century leap rule: divisible by 400
        ("1900-02", 28),   # century non-leap: divisible by 100 but not 400
        ("2026-04", 30),
        ("2026-07", 31),
        ("2026-12", 31),
    ])
    def test_calendar_days(self, yyyy_mm, expected) -> None:
        assert _days_in_month(yyyy_mm) == expected


# ============================================================================
# _seconds_until, _format_closes_at_relative: auto-close timer feeders
# ============================================================================


class TestSecondsUntil:
    def test_future_returns_positive(self) -> None:
        # 1 hour from now (UTC). seconds_until uses naive parsing then
        # forces UTC; pass a SQLite-format string.
        future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        as_sqlite = future.strftime("%Y-%m-%d %H:%M:%S")
        secs = _seconds_until(as_sqlite)
        assert secs is not None
        # ~3600s, allow generous slop for test runtime jitter.
        assert 3500 < secs <= 3600

    def test_past_returns_negative(self) -> None:
        past = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        as_sqlite = past.strftime("%Y-%m-%d %H:%M:%S")
        secs = _seconds_until(as_sqlite)
        assert secs is not None and secs < 0

    def test_none_input(self) -> None:
        assert _seconds_until(None) is None

    def test_garbage_input_returns_none(self) -> None:
        # Returns None on parse failure rather than crashing the
        # scheduled-close polling loop.
        assert _seconds_until("not-a-date") is None
        assert _seconds_until("") is None


class TestFormatClosesAtRelative:
    def test_none_yields_empty(self) -> None:
        # NULL closes_at field: no auto-close timer was set. Returning
        # "" lets the embed renderer skip the "closes <t:R>" line.
        assert _format_closes_at_relative(None) == ""
        assert _format_closes_at_relative("") == ""

    def test_garbage_yields_empty(self) -> None:
        assert _format_closes_at_relative("not-a-timestamp") == ""

    def test_valid_renders_discord_relative_token(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        out = _format_closes_at_relative(future)
        # Discord relative-timestamp shape: <t:UNIX:R>.
        assert out.startswith("<t:")
        assert out.endswith(":R>")
        # The unix-seconds substring should be a positive int.
        inner = out[len("<t:"):-len(":R>")]
        assert int(inner) > 0


# ============================================================================
# Small formatters
# ============================================================================


class TestTruncateLabel:
    def test_short_passes_through(self) -> None:
        assert _truncate_label("Short", 100) == "Short"

    def test_overlong_truncated_with_ellipsis(self) -> None:
        out = _truncate_label("A" * 200, 50)
        assert len(out) <= 50
        assert out.endswith("…") or out.endswith("...")


class TestVotesPhrase:
    @pytest.mark.parametrize("n,expected", [
        (0, "0 votes"),
        (1, "1 vote"),
        (2, "2 votes"),
        (10, "10 votes"),
    ])
    def test_pluralization(self, n, expected) -> None:
        assert _votes_phrase(n) == expected
