"""
Pillow renderer for the badges grid image used by ``/badges``.

Kept separate from ``lib/badges.py`` so call sites that only need the
predicate logic (profile-card strip, /finish unlock detection) don't import
PIL.

Each badge tile is rendered as a stylized disc with a short *tier label*
(e.g. "10", "1M", "Mo", "Vote") inside it — emojis aren't used as the
focal glyph because no font cascade we can rely on (Noto Sans JP, system
defaults) renders them as colored glyphs in Pillow on every platform we
deploy to. Emojis still appear in Discord-text contexts (the /finish
followup, embed labels) where the Discord client handles rendering.

Layout (1200x720 logical, 2x rendered): see ASCII sketch below.

    +------------------------------------------------------------+
    | ▍ ACHIEVEMENTS  • <Owner Display Name>          n/N earned  |
    |                                                            |
    | VOLUME ────────────────────────────────────────────         |
    | [(1)] [(10)] [(25)] [(50)] [(100)]                          |
    | First Reader Enthusiast Scholar Legend                      |
    | CHARACTERS ─────────────────────────────────────────        |
    | [(100K)] [(500K)] [(1M)] [(5M)] [(10M)]                     |
    | ...                                                         |
    +------------------------------------------------------------+
"""

from __future__ import annotations

import io
import logging
import time
from typing import Iterable

from PIL import Image, ImageDraw

from lib.badges import BADGE_DEFS, Badge
from lib.pillow_helpers import (
    ACCENT, ACCENT_INK, BG, HAIRLINE, INK_PRIMARY, INK_SECONDARY,
    INK_TERTIARY, PANEL_BG, PLACEHOLDER_BG,
    load_japanese_font, paste_aa_rounded, truncate_to_width,
)

logger = logging.getLogger(__name__)

SCALE = 2
WIDTH = 1200 * SCALE
# 800 (not 720) so the 5-category grid has room for disc + name in each row
# without crowding into the next category's label band. The other banners
# stay at 480/720 — this one is taller because we're packing five rows.
HEIGHT = 800 * SCALE

_CATEGORY_LABELS = {
    "volume":       "VOLUME",
    "pool":         "POOL PICKS",
    "engagement":   "ENGAGEMENT",
    "leaderboard":  "SEASON LEADERBOARD",
    "consistency":  "CONSISTENCY",
}

_CATEGORY_ORDER = ("volume", "pool", "engagement", "leaderboard", "consistency")


def _badges_grouped() -> list[tuple[str, list[Badge]]]:
    by_cat: dict[str, list[Badge]] = {c: [] for c in _CATEGORY_ORDER}
    for b in BADGE_DEFS:
        by_cat.setdefault(b.category, []).append(b)
    return [(c, by_cat[c]) for c in _CATEGORY_ORDER if by_cat.get(c)]


def _tier_label(b: Badge) -> str:
    """Short text rendered inside the badge disc — chosen to fit in ~3
    characters at the disc font size and read meaningfully when locked.
    Tiered families (monthly_pool_count etc.) render the threshold so the
    visual progresses 1 → 5 → 12 across the row."""
    if b.key == "total_completions":   return str(b.threshold)
    if b.key == "monthly_pool_count":  return f"M·{b.threshold}"
    if b.key == "seasonal_pool_count": return f"S·{b.threshold}"
    if b.key == "votes_cast":          return "Vo"
    if b.key == "nominations_made":    return "No"
    if b.key == "tastemaker_wins":     return "★"
    if b.key == "season_top1_count":   return "1st"
    if b.key == "season_top3_count":   return "T3"
    if b.key == "season_top10_count":  return "T10"
    if b.key == "distinct_months":     return f"{b.threshold}mo"
    return "•"


def render_badges_grid(
    unlocked: Iterable[str],
    owner_display_name: str,
) -> io.BytesIO:
    """Render the badges grid for ``owner_display_name``."""
    t0 = time.perf_counter()
    unlocked_set = set(unlocked)
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    S = SCALE

    # Outer hairline frame matching the other banners.
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
    eyebrow_x = header_x + 18 * S
    draw.text(
        (eyebrow_x, header_y - 2 * S),
        "ACHIEVEMENTS  •  " + (owner_display_name or "—").upper(),
        fill=ACCENT, font=font_eyebrow,
    )

    total = len(BADGE_DEFS)
    earned = sum(1 for b in BADGE_DEFS if b.id in unlocked_set)
    counter_main = f"{earned}"
    counter_sub = f" / {total} earned"
    font_count_main = load_japanese_font(36 * S, bold=True)
    font_count_sub = load_japanese_font(18 * S)

    main_w = draw.textlength(counter_main, font=font_count_main)
    sub_w = draw.textlength(counter_sub, font=font_count_sub)
    counter_x = WIDTH - 48 * S - (main_w + sub_w)
    counter_y_main = header_y - 6 * S
    draw.text((counter_x, counter_y_main), counter_main,
              fill=ACCENT, font=font_count_main)
    main_ascent, _ = font_count_main.getmetrics()
    main_baseline = counter_y_main + main_ascent
    sub_ascent, _ = font_count_sub.getmetrics()
    sub_y = main_baseline - sub_ascent
    draw.text((counter_x + main_w, sub_y), counter_sub,
              fill=INK_SECONDARY, font=font_count_sub)

    # ---------- categories ----------
    grouped = _badges_grouped()

    section_top = header_y + 60 * S
    section_bottom_pad = 32 * S
    available_h = HEIGHT - section_top - section_bottom_pad
    cat_h = available_h // max(1, len(grouped))
    cat_label_h = 24 * S
    tile_pad_top = 8 * S
    tile_h = cat_h - cat_label_h - tile_pad_top - 8 * S
    tile_w = (WIDTH - 96 * S - 16 * S * 4) // 5
    tile_gap = 16 * S

    font_cat = load_japanese_font(15 * S, bold=True)
    font_disc = load_japanese_font(22 * S, bold=True)
    font_name = load_japanese_font(13 * S, bold=True)
    font_desc = load_japanese_font(11 * S)

    y = section_top
    for cat_id, badges in grouped:
        # Category label + hairline.
        draw.text(
            (48 * S, y),
            _CATEGORY_LABELS.get(cat_id, cat_id.upper()),
            fill=INK_SECONDARY, font=font_cat,
        )
        draw.line(
            [(48 * S, y + cat_label_h - 4 * S),
             (WIDTH - 48 * S, y + cat_label_h - 4 * S)],
            fill=HAIRLINE, width=1 * S,
        )

        row_w = len(badges) * tile_w + max(0, len(badges) - 1) * tile_gap
        row_x0 = 48 * S + ((WIDTH - 96 * S - row_w) // 2)
        tile_y0 = y + cat_label_h + tile_pad_top

        for idx, b in enumerate(badges):
            tx0 = row_x0 + idx * (tile_w + tile_gap)
            tile_box = (tx0, tile_y0, tx0 + tile_w, tile_y0 + tile_h)
            is_unlocked = b.id in unlocked_set

            paste_aa_rounded(
                img, tile_box, radius=10 * S,
                fill=PANEL_BG if is_unlocked else PLACEHOLDER_BG,
                outline=HAIRLINE, outline_w=1 * S,
            )

            # Disc — accent-filled circle (or hollow when locked).
            # disc_y0 was 12*S; lifted to 6*S to free vertical room beneath
            # the disc for the name. With the name still bottom-anchored
            # (descender-safe), the freed space flows directly into the
            # disc→name gap so labels no longer crowd the circles.
            disc_d = 56 * S
            disc_x0 = tx0 + (tile_w - disc_d) // 2
            disc_y0 = tile_y0 + 6 * S
            disc_box = (disc_x0, disc_y0, disc_x0 + disc_d, disc_y0 + disc_d)
            paste_aa_rounded(
                img, disc_box, radius=disc_d // 2,
                fill=ACCENT if is_unlocked else PLACEHOLDER_BG,
                outline=ACCENT if is_unlocked else INK_TERTIARY,
                outline_w=2 * S,
            )

            # Disc label — auto-shrink the font when the rendered text
            # would exceed the disc's inner width. Keeps short labels (e.g.
            # "1", "★") at full size while letting longer ones like "M·12"
            # fit without overflow.
            label = _tier_label(b)
            disc_inner = disc_d - 12 * S
            label_font = font_disc
            if draw.textlength(label, font=label_font) > disc_inner:
                label_font = load_japanese_font(17 * S, bold=True)
            label_w = draw.textlength(label, font=label_font)
            label_x = disc_x0 + (disc_d - label_w) // 2
            label_ascent, label_descent = label_font.getmetrics()
            label_y = disc_y0 + (disc_d - (label_ascent + label_descent)) // 2 - 2 * S
            draw.text(
                (label_x, label_y), label,
                fill=ACCENT_INK if is_unlocked else INK_TERTIARY,
                font=label_font,
            )

            # Name centered under disc, bottom-anchored within the tile so
            # the descender stays inside the rounded panel even at the tightest
            # 5-category packing. (Disc-relative positioning would let the
            # descent clip the bottom border on tall fonts like Noto Sans JP.)
            # We omit the description in the grid view — it would push the
            # layout into the next category's row at five rows tall.
            name_text = truncate_to_width(draw, b.name, font_name, tile_w - 12 * S)
            name_w = draw.textlength(name_text, font=font_name)
            name_x = tx0 + (tile_w - name_w) // 2
            name_ascent, name_descent = font_name.getmetrics()
            name_h = name_ascent + name_descent
            name_y = tile_y0 + tile_h - name_h - 8 * S
            draw.text(
                (name_x, name_y), name_text,
                fill=INK_PRIMARY if is_unlocked else INK_TERTIARY,
                font=font_name,
            )

        y += cat_h

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    logger.info(
        "badges_grid rendered: owner=%r unlocked=%d duration_ms=%d",
        owner_display_name, len(unlocked_set),
        int((time.perf_counter() - t0) * 1000),
    )
    return buf
