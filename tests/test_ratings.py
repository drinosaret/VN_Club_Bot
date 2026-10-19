"""Per-user rating scales: pure helpers, the reading_logs migration, and the
user_settings preference/notice logic."""
from __future__ import annotations

import pytest

from lib import ratings
from lib.migrations import run_migrations
from lib.utils import DatabaseQueries, ValidationError
from tests.conftest import FakeBot

LEGACY_READING_LOGS = """
CREATE TABLE reading_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    vndb_id TEXT,
    user_rating INTEGER,
    reward_reason TEXT NOT NULL,
    reward_month TEXT NOT NULL,
    points INTEGER NOT NULL,
    comment TEXT,
    logged_in_guild INTEGER,
    completed_at TIMESTAMP
);
"""


class TestPureHelpers:
    @pytest.mark.parametrize("value,scale,expected", [
        (4, 5, 80.0), (5, 5, 100.0), (1, 5, 20.0),
        (7, 10, 70.0), (85, 100, 85.0),
    ])
    def test_normalize(self, value, scale, expected):
        assert ratings.normalize(value, scale) == expected

    def test_format_on_own_scale(self):
        assert ratings.format_rating(4, 5) == "4/5"
        assert ratings.format_rating(8, 10) == "8/10"
        assert ratings.format_rating(85, 100) == "85/100"

    def test_format_unknown_scale_falls_back_to_legacy(self):
        assert ratings.format_rating(3, None) == "3/5"
        assert ratings.format_rating(3, 7) == "3/5"

    def test_stars_only_on_five_point(self):
        assert ratings.format_rating_stars(4, 5).count("⭐") == 4
        assert "⭐" not in ratings.format_rating_stars(8, 10)

    def test_average_display(self):
        assert ratings.format_average(78.0) == "7.8/10"
        assert ratings.format_average(None) == "none"

    @pytest.mark.parametrize("stars", [1, 2, 3, 4, 5])
    def test_legacy_stars_land_in_matching_bucket(self, stars):
        assert ratings.bucket5(ratings.normalize(stars, 5)) == stars

    @pytest.mark.parametrize("value,scale,bucket", [
        (1, 10, 1), (2, 10, 1), (3, 10, 2), (6, 10, 3), (7, 10, 4), (8, 10, 4),
        (9, 10, 5), (10, 10, 5), (1, 100, 1), (100, 100, 5), (81, 100, 5), (80, 100, 4),
    ])
    def test_buckets(self, value, scale, bucket):
        assert ratings.bucket5(ratings.normalize(value, scale)) == bucket

    def test_validate_within_scale(self):
        assert ratings.validate_rating(10, 10) == 10
        assert ratings.validate_rating(1, 5) == 1

    @pytest.mark.parametrize("value,scale", [(6, 5), (11, 10), (0, 10), (None, 10), (101, 100)])
    def test_validate_rejects_out_of_scale(self, value, scale):
        with pytest.raises(ValidationError) as exc:
            ratings.validate_rating(value, scale)
        assert f"1–{scale}" in exc.value.user_message


class TestMigration:
    async def test_legacy_rows_become_out_of_five(self, fake_bot: FakeBot):
        await fake_bot.RUN(LEGACY_READING_LOGS)
        await fake_bot.RUN(
            "INSERT INTO reading_logs (user_id, vndb_id, user_rating, reward_reason, "
            "reward_month, points) VALUES (1, 'v1', 4, 'As Normal VN', '2026-01', 6)"
        )
        await run_migrations(fake_bot)
        (scale,) = await fake_bot.GET_ONE("SELECT rating_scale FROM reading_logs")
        assert scale == 5

    async def test_migration_is_idempotent(self, fake_bot: FakeBot):
        await fake_bot.RUN(LEGACY_READING_LOGS)
        await run_migrations(fake_bot)
        await run_migrations(fake_bot)
        cols = [r[1] for r in await fake_bot.GET("PRAGMA table_info(reading_logs)")]
        assert cols.count("rating_scale") == 1

    async def test_writer_unaware_of_scale_gets_legacy_default(self, fake_bot: FakeBot):
        await fake_bot.RUN(LEGACY_READING_LOGS)
        await run_migrations(fake_bot)
        # Shape of an INSERT from a writer that predates the column.
        await fake_bot.RUN(
            "INSERT INTO reading_logs (user_id, vndb_id, user_rating, reward_reason, "
            "reward_month, points) VALUES (2, 'v2', 3, 'x', '2026-02', 6)"
        )
        (scale,) = await fake_bot.GET_ONE(
            "SELECT rating_scale FROM reading_logs WHERE user_id = 2"
        )
        assert scale == 5

    async def test_fresh_install_create_has_column(self, fake_bot: FakeBot):
        await run_migrations(fake_bot)
        await fake_bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
        cols = [r[1] for r in await fake_bot.GET("PRAGMA table_info(reading_logs)")]
        assert "rating_scale" in cols


@pytest.fixture
async def scale_bot(fake_bot: FakeBot) -> FakeBot:
    await run_migrations(fake_bot)
    await fake_bot.RUN(DatabaseQueries.CREATE_READING_LOGS_TABLE)
    return fake_bot


async def _log(bot, user_id, rating, scale, vndb_id="v1", month="2026-01"):
    await bot.RUN(
        DatabaseQueries.ADD_READING_LOG_OR_IGNORE,
        (user_id, vndb_id, rating, scale, "As Normal VN", month, 6, "c", 100),
    )


class TestPreference:
    async def test_default_is_ten(self, scale_bot):
        assert await ratings.get_user_scale(scale_bot, 1) == ratings.DEFAULT_SCALE == 10

    async def test_set_and_get(self, scale_bot):
        await ratings.set_user_scale(scale_bot, 1, 100)
        assert await ratings.get_user_scale(scale_bot, 1) == 100
        await ratings.set_user_scale(scale_bot, 1, 5)
        assert await ratings.get_user_scale(scale_bot, 1) == 5

    async def test_rejects_unknown_scale(self, scale_bot):
        with pytest.raises(ValueError):
            await ratings.set_user_scale(scale_bot, 1, 7)

    async def test_notice_for_legacy_rater_only_once(self, scale_bot):
        await _log(scale_bot, 1, 4, 5)
        assert await ratings.needs_scale_notice(scale_bot, 1) is True
        await ratings.mark_scale_notice_seen(scale_bot, 1)
        assert await ratings.needs_scale_notice(scale_bot, 1) is False
        # Marking seen does not pick a scale.
        assert await ratings.get_user_scale(scale_bot, 1) == 10

    async def test_no_notice_for_new_user(self, scale_bot):
        await _log(scale_bot, 2, 8, 10)
        assert await ratings.needs_scale_notice(scale_bot, 2) is False

    async def test_no_notice_after_choosing_scale(self, scale_bot):
        await _log(scale_bot, 3, 4, 5)
        await ratings.set_user_scale(scale_bot, 3, 10)
        assert await ratings.needs_scale_notice(scale_bot, 3) is False


class TestAggregates:
    async def test_average_mixes_scales_on_normalized_value(self, scale_bot):
        await _log(scale_bot, 1, 4, 5, "v1")      # 80
        await _log(scale_bot, 1, 6, 10, "v2")     # 60
        await _log(scale_bot, 1, 70, 100, "v3")   # 70
        avg, count = await scale_bot.GET_ONE(DatabaseQueries.GET_USER_AVERAGE_RATING, (1,))
        assert count == 3
        assert avg == pytest.approx(70.0)

    async def test_distribution_rows_carry_scale(self, scale_bot):
        await _log(scale_bot, 1, 4, 5, "v1")
        await _log(scale_bot, 2, 4, 10, "v1")
        rows = await scale_bot.GET(DatabaseQueries.CLUB_STATS_RATING_DIST, (None, None))
        assert sorted(rows) == [(4, 5, 1), (4, 10, 1)]

    async def test_edit_restamps_scale(self, scale_bot):
        await _log(scale_bot, 1, 4, 5)
        (log_id,) = await scale_bot.GET_ONE("SELECT log_id FROM reading_logs")
        await scale_bot.RUN(DatabaseQueries.UPDATE_LOG_COMMENT_RATING, ("c2", 9, 10, log_id))
        row = await scale_bot.GET_ONE(DatabaseQueries.GET_LOG_BY_ID, (log_id,))
        assert row[2] == 9 and row[8] == 10
