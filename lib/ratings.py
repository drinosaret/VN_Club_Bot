"""Per-user rating scales.

A reading log stores the rating exactly as typed plus the scale it was typed
on (``reading_logs.rating_scale``). Individual ratings are always shown on
their own scale so a 4/5 never reads as a deliberate 80/100. Anything that
combines ratings (averages, distributions, rankings) works on the normalized
1-100 value instead.

Rows written before scales existed, and rows from writers that do not set the
column, carry the column default of 5, which is what those values mean.
"""

from __future__ import annotations

import math
from typing import Optional

from lib.utils import ValidationError

SCALES = (5, 10, 100)
# Applies to anyone without a saved preference. Distinct from the column
# default on reading_logs, which describes legacy rows rather than users.
DEFAULT_SCALE = 10
LEGACY_SCALE = 5
# How each scale is offered to members, in menu order.
SCALE_LABELS = {5: "1-5", 10: "1-10 (default)", 100: "1-100"}


def is_valid_scale(scale: object) -> bool:
    return isinstance(scale, int) and scale in SCALES


def normalize(value: int, scale: int) -> float:
    """Map a rating on ``scale`` onto 1-100."""
    return value * 100.0 / scale


def format_rating(value: Optional[int], scale: Optional[int]) -> str:
    """Render a single rating on its own scale: "4/5", "8/10", "85/100"."""
    if value is None:
        return "No rating"
    scale = scale if is_valid_scale(scale) else LEGACY_SCALE
    return f"{value}/{scale}"


def format_rating_stars(value: Optional[int], scale: Optional[int]) -> str:
    """Like format_rating, with stars appended on the 5-point scale only.
    Longer scales would turn the stars into a bar of noise."""
    text = format_rating(value, scale)
    if value is not None and (scale if is_valid_scale(scale) else LEGACY_SCALE) == 5:
        text += " " + "⭐" * max(0, min(int(value), 5))
    return text


def format_average(normalized: Optional[float]) -> str:
    """Render a normalized (1-100) aggregate on the 10-point display scale."""
    if normalized is None:
        return "none"
    return f"{normalized / 10:.1f}/10"


def bucket5(normalized: float) -> int:
    """Five equal buckets over 1-100: 1-20 -> 1 ... 81-100 -> 5. A legacy
    k-star rating lands in bucket k."""
    return max(1, min(5, math.ceil(normalized / 20)))


# Display labels for bucket5 on the 10-point scale, highest first.
BUCKET_LABELS = {5: "9–10", 4: "7–8", 3: "5–6", 2: "3–4", 1: "1–2"}


def validate_rating(value: Optional[int], scale: int) -> int:
    """Check a typed rating against the author's scale."""
    if value is None or not (1 <= value <= scale):
        raise ValidationError(
            f"rating {value} outside 1-{scale}",
            f"Ratings are on a 1–{scale} scale for this user, so `{value}` doesn't fit. "
            "Use `/settings` to switch between 5, 10 and 100.",
        )
    return value


GET_USER_SETTINGS = """
SELECT rating_scale, scale_notice_seen FROM user_settings WHERE user_id = ?;
"""

UPSERT_RATING_SCALE = """
INSERT INTO user_settings (user_id, rating_scale, scale_notice_seen)
VALUES (?, ?, 1)
ON CONFLICT(user_id) DO UPDATE SET
    rating_scale = excluded.rating_scale,
    scale_notice_seen = 1,
    updated_at = CURRENT_TIMESTAMP;
"""

MARK_SCALE_NOTICE_SEEN = """
INSERT INTO user_settings (user_id, scale_notice_seen)
VALUES (?, 1)
ON CONFLICT(user_id) DO UPDATE SET
    scale_notice_seen = 1,
    updated_at = CURRENT_TIMESTAMP;
"""

# True when the user rated anything before scales existed. Only rows on the
# legacy scale count, so a user who has rated since does not re-qualify.
HAS_LEGACY_RATINGS = """
SELECT 1 FROM reading_logs
WHERE user_id = ? AND user_rating IS NOT NULL AND rating_scale = 5
LIMIT 1;
"""


async def get_user_scale(bot, user_id: int) -> int:
    return await get_saved_scale(bot, user_id) or DEFAULT_SCALE


async def get_saved_scale(bot, user_id: int) -> Optional[int]:
    """The scale the member chose, or None if they never picked one."""
    row = await bot.GET_ONE(GET_USER_SETTINGS, (user_id,))
    if row and is_valid_scale(row[0]):
        return row[0]
    return None


async def set_user_scale(bot, user_id: int, scale: int) -> None:
    if not is_valid_scale(scale):
        raise ValueError(f"unsupported scale {scale!r}")
    await bot.RUN(UPSERT_RATING_SCALE, (user_id, scale))


async def needs_scale_notice(bot, user_id: int) -> bool:
    """A user who rated on the old 5-point scale and has not chosen a scale
    since is told once that the default is now 10."""
    row = await bot.GET_ONE(GET_USER_SETTINGS, (user_id,))
    if row and (row[1] or is_valid_scale(row[0])):
        return False
    return bool(await bot.GET_ONE(HAS_LEGACY_RATINGS, (user_id,)))


async def mark_scale_notice_seen(bot, user_id: int) -> None:
    await bot.RUN(MARK_SCALE_NOTICE_SEEN, (user_id,))
