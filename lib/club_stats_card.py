"""
1200x720 club-stats dashboard image generator.

Powers ``/club_stats``. Rendered with the same cream/purple palette as the
other banners, scoped either to a single guild ("This server") or the whole
deployment ("Global").

Layout (logical pixels):

    +------------------------------------------------------------+
    | ▍ VISUAL NOVEL CLUB  •  <Server Name | GLOBAL>             |
    | ┌─ TOTAL CHARS ──┬─ COMPLETIONS ──┬─ UNIQUE VNS ─┬─ MEMBERS┐|
    | │ 24.5M          │ 365            │ 281          │ 78      │|
    | └────────────────┴────────────────┴──────────────┴─────────┘|
    | RATING DISTRIBUTION                  TOP CONTRIBUTORS       |
    | ▮▮▮▮▮▮▮▮▮▮▮▮ 5★ 49                  1. Username  2,400 pts |
    | ▮▮▮▮▮▮▮▮▮▮▮▮ 4★ 128                 2. ...                  |
    | ...                                                         |
    | MONTHLY ACTIVITY (last 12 months)                           |
    | ▁▂▃▅▇▆▅▃▂▁                                                  |
    +------------------------------------------------------------+
"""

from __future__ import annotations

import io
import logging
import time
from PIL import Image, ImageDraw

from lib.pillow_helpers import (
    ACCENT, ACCENT_INK, BAR_FILL, BG, CALLOUT_BG, HAIRLINE,
    INK_PRIMARY, INK_SECONDARY, INK_TERTIARY, PANEL_BG,
    load_japanese_font, paste_aa_rounded, truncate_to_width,
)

logger = logging.getLogger(__name__)


SCALE = 2
WIDTH = 1200 * SCALE
HEIGHT = 720 * SCALE


def render_club_stats(
    *,
    scope_label: str,
    total_points: int,
    total_completions: int,
    unique_vns: int,
    active_members: int,
    top_contributors: list[tuple[str, int, int]],   # (display_name, points, completions)
    rating_distribution: list[tuple[int, int]],     # [(rating, count), ...] sorted asc
    monthly_trend: list[tuple[str, int]],           # [(yyyy-mm, count), ...] oldest→newest
) -> io.BytesIO:
    """Render the club-stats dashboard.

    All heavy lifting (DB queries, jiten backfill, scope handling) happens in
    the cog; this function takes pre-aggregated values and lays them out.
    Returns a BytesIO PNG.
    """
    t0 = time.perf_counter()
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    S = SCALE

    # Outer frame.
    paste_aa_rounded(
        img, (16 * S, 16 * S, WIDTH - 16 * S, HEIGHT - 16 * S),
        radius=18 * S, outline=HAIRLINE, outline_w=1 * S,
    )

    # ---------- header ----------
    header_x = 48 * S
    header_y = 44 * S
    accent_bar = (header_x, header_y, header_x + 6 * S, header_y + 28 * S)
    draw.rectangle(accent_bar, fill=ACCENT)

    font_eyebrow = load_japanese_font(20 * S, bold=True)
    eyebrow_text = "VISUAL NOVEL CLUB  •  " + scope_label.upper()
    draw.text(
        (header_x + 18 * S, header_y - 2 * S),
        eyebrow_text, fill=ACCENT, font=font_eyebrow,
    )

    # ---------- big-number tiles row ----------
    tiles_top = header_y + 56 * S
    tiles_h = 110 * S
    inner_x = 48 * S
    inner_w = WIDTH - 96 * S
    tile_gap = 16 * S
    tile_w = (inner_w - tile_gap * 3) // 4

    tiles = [
        ("TOTAL POINTS",    f"{total_points:,}"),
        ("COMPLETIONS",     f"{total_completions:,}"),
        ("UNIQUE VNS",      f"{unique_vns:,}"),
        ("ACTIVE MEMBERS",  f"{active_members:,}"),
    ]

    font_tile_label = load_japanese_font(13 * S, bold=True)
    font_tile_value = load_japanese_font(40 * S, bold=True)

    for i, (label, value) in enumerate(tiles):
        tx0 = inner_x + i * (tile_w + tile_gap)
        tile_box = (tx0, tiles_top, tx0 + tile_w, tiles_top + tiles_h)
        paste_aa_rounded(
            img, tile_box, radius=12 * S, fill=PANEL_BG,
            outline=None,
        )
        draw.text(
            (tx0 + 18 * S, tiles_top + 14 * S),
            label, fill=INK_SECONDARY, font=font_tile_label,
        )
        # Value sits roughly centered-ish in the tile, with consistent baseline.
        draw.text(
            (tx0 + 18 * S, tiles_top + 36 * S),
            value, fill=INK_PRIMARY, font=font_tile_value,
        )

    # ---------- rating distribution + top contributors row ----------
    panels_top = tiles_top + tiles_h + 16 * S
    panels_h = 280 * S
    half_w = (inner_w - tile_gap) // 2

    # Rating distribution panel (left)
    dist_box = (inner_x, panels_top, inner_x + half_w, panels_top + panels_h)
    paste_aa_rounded(
        img, dist_box, radius=12 * S, fill=CALLOUT_BG, outline=None,
    )
    font_panel_eyebrow = load_japanese_font(13 * S, bold=True)
    draw.text(
        (dist_box[0] + 16 * S, dist_box[1] + 14 * S),
        "RATING DISTRIBUTION", fill=ACCENT, font=font_panel_eyebrow,
    )

    # 1..5 rows. Bars stretch right based on max count.
    rating_map = dict(rating_distribution)
    max_count = max(rating_map.values()) if rating_map else 0
    if max_count == 0:
        max_count = 1  # avoid division by zero; bars render as zero-width

    font_rating_label = load_japanese_font(15 * S, bold=True)
    font_rating_count = load_japanese_font(13 * S)

    bar_area_x = dist_box[0] + 60 * S
    bar_area_right = dist_box[2] - 60 * S
    bar_area_w = bar_area_right - bar_area_x
    row_top = dist_box[1] + 48 * S
    row_h = (panels_h - 48 * S - 16 * S) // 5  # 5 rating rows

    for i, rating in enumerate([5, 4, 3, 2, 1]):  # high→low for visual descent
        count = rating_map.get(rating, 0)
        ry = row_top + i * row_h
        # Star label
        draw.text(
            (dist_box[0] + 16 * S, ry + (row_h - 22 * S) // 2),
            f"{rating}★", fill=INK_PRIMARY, font=font_rating_label,
        )
        # Bar
        bar_w = int(bar_area_w * (count / max_count)) if count else 0
        bar_h = 20 * S
        bar_y = ry + (row_h - bar_h) // 2
        if bar_w > 0:
            paste_aa_rounded(
                img,
                (bar_area_x, bar_y, bar_area_x + bar_w, bar_y + bar_h),
                radius=4 * S, fill=BAR_FILL, outline=None,
            )
        # Count text right of bar (or at bar end)
        count_x = bar_area_x + bar_w + 8 * S
        draw.text(
            (count_x, bar_y + 1 * S),
            f"{count:,}", fill=INK_SECONDARY, font=font_rating_count,
        )

    # Top contributors panel (right)
    contrib_box = (
        inner_x + half_w + tile_gap, panels_top,
        inner_x + 2 * half_w + tile_gap, panels_top + panels_h,
    )
    paste_aa_rounded(
        img, contrib_box, radius=12 * S, fill=CALLOUT_BG, outline=None,
    )
    draw.text(
        (contrib_box[0] + 16 * S, contrib_box[1] + 14 * S),
        "TOP CONTRIBUTORS", fill=ACCENT, font=font_panel_eyebrow,
    )

    font_contrib_rank = load_japanese_font(15 * S, bold=True)
    font_contrib_name = load_japanese_font(14 * S, bold=True)
    font_contrib_pts = load_japanese_font(13 * S)

    contrib_top = contrib_box[1] + 48 * S
    contrib_max_h = panels_h - 48 * S - 16 * S
    rows_to_show = top_contributors[:5]
    contrib_row_h = contrib_max_h // 5

    for i, (name, pts, completions) in enumerate(rows_to_show):
        ry = contrib_top + i * contrib_row_h
        rank_text = f"{i + 1}."
        draw.text(
            (contrib_box[0] + 16 * S, ry + (contrib_row_h - 22 * S) // 2),
            rank_text, fill=INK_TERTIARY, font=font_contrib_rank,
        )

        name_x = contrib_box[0] + 56 * S
        # Right-side: points · completions
        right_text = f"{pts:,} pts  ·  {completions:,} VNs"
        right_w = draw.textlength(right_text, font=font_contrib_pts)
        right_x = contrib_box[2] - 16 * S - right_w
        # Truncate name to fit between rank and right block.
        name_max_w = right_x - name_x - 16 * S
        name_drawn = truncate_to_width(draw, name, font_contrib_name, name_max_w)
        draw.text(
            (name_x, ry + (contrib_row_h - 22 * S) // 2),
            name_drawn, fill=INK_PRIMARY, font=font_contrib_name,
        )
        draw.text(
            (right_x, ry + (contrib_row_h - 20 * S) // 2),
            right_text, fill=INK_SECONDARY, font=font_contrib_pts,
        )

    # ---------- monthly trend strip ----------
    trend_top = panels_top + panels_h + 16 * S
    trend_h = HEIGHT - trend_top - 32 * S
    trend_box = (inner_x, trend_top, inner_x + inner_w, trend_top + trend_h)
    paste_aa_rounded(
        img, trend_box, radius=12 * S, fill=PANEL_BG, outline=None,
    )
    bar_strip_x = trend_box[0] + 8 * S
    draw.rectangle(
        [bar_strip_x, trend_box[1], bar_strip_x + 6 * S, trend_box[3]],
        fill=ACCENT,
    )
    draw.text(
        (trend_box[0] + 22 * S, trend_box[1] + 12 * S),
        "MONTHLY ACTIVITY  •  LAST 12 MONTHS",
        fill=ACCENT, font=font_panel_eyebrow,
    )

    # Bars across the bottom of the trend panel.
    trend_data = list(monthly_trend)  # already oldest→newest
    if trend_data:
        max_trend = max(c for _, c in trend_data) or 1

        # Vertical bands. Reserving a dedicated label band above the bars
        # means the count text sits in the same place regardless of bar
        # height — no more "label kissing the eyebrow when the bar is tall".
        font_trend_label = load_japanese_font(11 * S, bold=True)
        font_trend_count = load_japanese_font(11 * S)
        count_label_band = 24 * S   # gap above the tallest bar's top edge
        month_label_band = 24 * S   # gap below the shortest bar for "Jan"
        bars_top = trend_box[1] + 44 * S + count_label_band
        bars_bottom = trend_box[3] - month_label_band
        bar_area_h = bars_bottom - bars_top

        # Cap column width so a sparse trend (e.g. only 3 months of data)
        # doesn't blow up into huge fat bars that fill the whole panel
        # awkwardly. When there are few entries, we centre the strip
        # horizontally with extra room on the sides.
        inner_left = trend_box[0] + 24 * S
        inner_right = trend_box[2] - 24 * S
        inner_w = inner_right - inner_left
        n = len(trend_data)
        bar_gap = 10 * S
        max_col_w = 96 * S
        natural_col_w = (inner_w - bar_gap * max(0, n - 1)) // max(1, n)
        col_w = max(8 * S, min(natural_col_w, max_col_w))
        strip_w = n * col_w + max(0, n - 1) * bar_gap
        bars_left = inner_left + (inner_w - strip_w) // 2

        # Count-label vertical: 6*S of breathing room above the bar's top.
        # Constant rather than relative to height so labels stay aligned to
        # each bar individually.
        count_gap_above_bar = 6 * S
        count_ascent, _ = font_trend_count.getmetrics()

        for i, (yyyy_mm, count) in enumerate(trend_data):
            cx0 = bars_left + i * (col_w + bar_gap)
            bar_h = int(bar_area_h * (count / max_trend))
            bar_top = bars_bottom - bar_h
            if bar_h > 0:
                paste_aa_rounded(
                    img,
                    (cx0, bar_top, cx0 + col_w, bars_bottom),
                    radius=4 * S, fill=BAR_FILL, outline=None,
                )

            # Count above bar — only when non-zero. Empty bars already read
            # as "0" visually; printing "0" twelve times across a sparse
            # trend just adds noise. Anchored to a stable gap above the
            # bar's top regardless of bar height.
            if count > 0:
                count_text = f"{count}"
                cw = draw.textlength(count_text, font=font_trend_count)
                count_y = bar_top - count_gap_above_bar - count_ascent
                draw.text(
                    (cx0 + (col_w - cw) // 2, count_y),
                    count_text, fill=INK_SECONDARY, font=font_trend_count,
                )

            # Month label below
            try:
                from datetime import datetime
                label = datetime.strptime(yyyy_mm, "%Y-%m").strftime("%b")
            except Exception:
                label = yyyy_mm
            lw = draw.textlength(label, font=font_trend_label)
            draw.text(
                (cx0 + (col_w - lw) // 2, bars_bottom + 6 * S),
                label, fill=INK_PRIMARY, font=font_trend_label,
            )
    else:
        # No data — small placeholder line.
        draw.text(
            (trend_box[0] + 22 * S, trend_box[1] + 44 * S),
            "(no completions in scope)",
            fill=INK_TERTIARY, font=load_japanese_font(13 * S),
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    logger.info(
        "club_stats_card rendered: scope=%r contributors=%d duration_ms=%d",
        scope_label, len(top_contributors),
        int((time.perf_counter() - t0) * 1000),
    )
    return buf
