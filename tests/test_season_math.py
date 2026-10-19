"""Anime-season math: season ↔ months ↔ season-index conversions.

Seasonal cycles span 3 months following the anime convention (Winter
Jan–Mar, Spring Apr–Jun, Summer Jul–Sep, Fall Oct–Dec). Bugs here
mis-target every seasonal banner and `/season_overview` composite.
"""
from __future__ import annotations

import pytest

from lib.utils import (
    ANIME_SEASON_ORDER,
    month_to_season_name,
    next_season,
    season_index,
    season_to_months,
)


class TestSeasonToMonths:
    @pytest.mark.parametrize("season,year,expected", [
        ("winter", 2026, ["2026-01", "2026-02", "2026-03"]),
        ("spring", 2026, ["2026-04", "2026-05", "2026-06"]),
        ("summer", 2026, ["2026-07", "2026-08", "2026-09"]),
        ("fall",   2026, ["2026-10", "2026-11", "2026-12"]),
    ])
    def test_canonical(self, season, year, expected) -> None:
        assert season_to_months(season, year) == expected

    def test_case_insensitive(self) -> None:
        # The bot accepts upper / mixed case from user-typed inputs.
        assert season_to_months("WINTER", 2026) == season_to_months("winter", 2026)
        assert season_to_months("Spring", 2026) == season_to_months("spring", 2026)

    def test_unknown_season_raises(self) -> None:
        with pytest.raises(KeyError):
            season_to_months("autumn", 2026)  # "fall", not "autumn"


class TestMonthToSeasonName:
    @pytest.mark.parametrize("month,expected", [
        (1, "winter"), (2, "winter"), (3, "winter"),
        (4, "spring"), (5, "spring"), (6, "spring"),
        (7, "summer"), (8, "summer"), (9, "summer"),
        (10, "fall"), (11, "fall"), (12, "fall"),
    ])
    def test_all_months(self, month, expected) -> None:
        assert month_to_season_name(month) == expected

    @pytest.mark.parametrize("bad", [0, 13, -1, 100])
    def test_invalid_raises(self, bad) -> None:
        with pytest.raises(ValueError):
            month_to_season_name(bad)


class TestNextSeason:
    def test_winter_to_spring(self) -> None:
        assert next_season(2026, "winter") == (2026, "spring")

    def test_spring_to_summer(self) -> None:
        assert next_season(2026, "spring") == (2026, "summer")

    def test_summer_to_fall(self) -> None:
        assert next_season(2026, "summer") == (2026, "fall")

    def test_fall_wraps_to_next_winter(self) -> None:
        # The year-rollover case: fall 2026 → winter 2027, not winter 2026.
        assert next_season(2026, "fall") == (2027, "winter")

    def test_case_insensitive_input(self) -> None:
        assert next_season(2026, "Fall") == (2027, "winter")

    def test_round_trip_year(self) -> None:
        # Stepping forward 4 seasons lands you in the same season one year on.
        y, s = 2026, "winter"
        for _ in range(4):
            y, s = next_season(y, s)
        assert (y, s) == (2027, "winter")


class TestSeasonIndex:
    def test_monotonic(self) -> None:
        # Each season is one higher than the previous.
        order = list(ANIME_SEASON_ORDER)
        for year in (2025, 2026):
            for i in range(len(order) - 1):
                a = season_index(year, order[i])
                b = season_index(year, order[i + 1])
                assert b == a + 1, f"non-monotonic at {year} {order[i]}->{order[i+1]}"

    def test_year_boundary(self) -> None:
        # fall Y → winter Y+1 is one season apart.
        a = season_index(2026, "fall")
        b = season_index(2027, "winter")
        assert b == a + 1
