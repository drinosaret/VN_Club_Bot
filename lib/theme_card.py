"""
1200x680 nomination-theme card generator.

Powers ``/theme``. Same cream/purple palette as the other cards, so a theme
announcement reads as part of the same set as the monthly banner.

Layout (logical pixels):

    +------------------------------------------------------------+
    | ▍ NOMINATION THEME  •  SEPTEMBER 2026                       |
    | SF September                                                |
    | ┌─ Nomination requirements ────────────────────────────────┐|
    | │ Must have: Science Fiction (2+)                          │|
    | │ Length: Very short to Short                              │|
    | └──────────────────────────────────────────────────────────┘|
    | A FEW THAT FIT                                              |
    | [cover] [cover] [cover] [cover] [cover] [cover]             |
    |  Title   Title   Title   Title   Title   Title              |
    +------------------------------------------------------------+

The cog fetches covers and passes decoded images in; nothing here does I/O.
It also drops NSFW-covered titles before they get here, so every cover on
the strip is safe to show at full size.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Optional

from PIL import Image, ImageDraw

from lib.pillow_helpers import (
    ACCENT, BG, CALLOUT_BG, HAIRLINE,
    INK_PRIMARY, INK_SECONDARY, INK_TERTIARY, PANEL_BG,
    load_japanese_font, paste_aa_rounded, truncate_to_width,
)

logger = logging.getLogger(__name__)


SCALE = 2
WIDTH = 1200 * SCALE

# Rule lines beyond this are summarised as a count. A ruleset can hold one
# line per constraint plus a line per tag bucket, which outgrows the panel.
MAX_RULE_LINES = 6
# Covers across the examples strip. The box is close to the ~5:7 most VN
# covers use, so the fill crop trims very little.
MAX_EXAMPLES = 6
COVER_W = 150
COVER_H = 210
# A one-line caption cuts most Japanese titles mid-word; two holds nearly all
# of them at this width.
CAPTION_LINES = 2
CAPTION_LINE_H = 21


def _wrap_to_width(draw: ImageDraw.ImageDraw, text: str, font, max_width: int,
                   max_lines: int) -> list[str]:
    """Greedy wrap into at most ``max_lines``, ellipsising the last one.

    Japanese titles break between any two characters, so this fills by
    character and only prefers a space when the line already holds one, which
    keeps a Latin title from splitting mid-word.
    """
    lines: list[str] = []
    rest = text.strip()
    while rest and len(lines) < max_lines:
        if draw.textlength(rest, font=font) <= max_width:
            lines.append(rest)
            return lines
        if len(lines) == max_lines - 1:
            lines.append(truncate_to_width(draw, rest, font, max_width))
            return lines
        cut = len(rest)
        while cut > 1 and draw.textlength(rest[:cut], font=font) > max_width:
            cut -= 1
        head = rest[:cut]
        # Break on a space where there is one, including the full-width space
        # Japanese titles use between a name and its subtitle. CJK without one
        # breaks between any two characters, which is how it is set anyway.
        space = max(head.rfind(" "), head.rfind("　"))
        # Take the space unless honouring it would waste most of the line. A
        # title that separates a name from its subtitle splits early and
        # should still split there.
        if space >= cut // 3:
            head = head[:space]
        lines.append(head.strip())
        rest = rest[len(head):].strip()
    return lines


def _draw_cover(img: Image.Image, cover: Optional[Image.Image], box) -> None:
    """Paint one cover into ``box``, cropped to fill and corner-rounded. A
    missing cover leaves the placeholder panel, so the strip keeps its rhythm
    whether or not every fetch succeeded."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    paste_aa_rounded(img, box, radius=8 * SCALE, fill=PANEL_BG)
    if cover is None:
        return

    src = cover.convert("RGB")
    # Cover-fit: scale to the larger ratio, then centre-crop the overflow.
    ratio = max(w / src.width, h / src.height)
    resized = src.resize((max(1, int(src.width * ratio)), max(1, int(src.height * ratio))),
                         Image.Resampling.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    tile = resized.crop((left, top, left + w, top + h))

    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (w * 4 - 1, h * 4 - 1)], radius=8 * SCALE * 4, fill=255)
    img.paste(tile, (x0, y0), mask.resize((w, h), Image.Resampling.LANCZOS))


def render_theme_card(
    *,
    theme_label: str,
    period_label: str,
    rule_lines: list[str],
    examples: list[tuple[str, Optional[Image.Image]]],  # (title, cover)
    caveat: Optional[str] = None,
) -> io.BytesIO:
    """Render the theme card.

    The cog resolves the theme, runs the VNDB query and fetches covers; this
    takes finished values and lays them out. Returns a BytesIO PNG.
    """
    t0 = time.perf_counter()
    S = SCALE
    inner_x = 48 * S
    inner_w = WIDTH - 96 * S
    row_h = 34 * S

    shown_rules = rule_lines[:MAX_RULE_LINES]
    overflow = len(rule_lines) - len(shown_rules)
    if overflow > 0:
        shown_rules.append(f"and {overflow} more condition(s)")
    shown_rules = shown_rules or ["No restrictions."]

    # The rule list is variable-length and the examples strip sits under it,
    # so the canvas is sized to the content rather than the content squeezed
    # into a fixed canvas.
    header_y = 44 * S
    title_y = header_y + 46 * S
    panel_top = title_y + 78 * S
    panel_h = 44 * S + row_h * len(shown_rules) + 18 * S
    strip_top = panel_top + panel_h + 34 * S
    covers_top = strip_top + 30 * S
    cw, ch = COVER_W * S, COVER_H * S
    caption_y = covers_top + ch + 10 * S
    # No examples means no strip at all: a lone heading over blank space reads
    # as a failed render.
    content_bottom = (caption_y + CAPTION_LINE_H * S * CAPTION_LINES + 6 * S
                      if examples else panel_top + panel_h)
    caveat_y = content_bottom + 8 * S
    if caveat:
        content_bottom = caveat_y + 22 * S
    height = content_bottom + 40 * S

    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    paste_aa_rounded(
        img, (16 * S, 16 * S, WIDTH - 16 * S, height - 16 * S),
        radius=18 * S, outline=HAIRLINE, outline_w=1 * S,
    )

    # ---------- header ----------
    draw.rectangle((inner_x, header_y, inner_x + 6 * S, header_y + 28 * S), fill=ACCENT)
    draw.text(
        (inner_x + 18 * S, header_y - 2 * S),
        f"NOMINATION THEME  •  {period_label.upper()}",
        fill=ACCENT, font=load_japanese_font(20 * S, bold=True),
    )

    font_title = load_japanese_font(52 * S, bold=True)
    draw.text(
        (inner_x, title_y),
        truncate_to_width(draw, theme_label, font_title, inner_w),
        fill=INK_PRIMARY, font=font_title,
    )

    # ---------- rules panel ----------
    font_rule_head = load_japanese_font(14 * S, bold=True)
    font_rule = load_japanese_font(22 * S)
    paste_aa_rounded(
        img, (inner_x, panel_top, inner_x + inner_w, panel_top + panel_h),
        radius=12 * S, fill=CALLOUT_BG,
        left_accent_w=6 * S, left_accent_fill=ACCENT,
    )
    draw.text((inner_x + 26 * S, panel_top + 16 * S),
              "NOMINATION REQUIREMENTS", fill=INK_SECONDARY, font=font_rule_head)
    for i, line in enumerate(shown_rules):
        draw.text(
            (inner_x + 26 * S, panel_top + 46 * S + i * row_h),
            truncate_to_width(draw, line, font_rule, inner_w - 52 * S),
            fill=INK_PRIMARY, font=font_rule,
        )

    # ---------- examples strip ----------
    if examples:
        draw.text((inner_x, strip_top), "A FEW THAT FIT",
                  fill=INK_SECONDARY, font=load_japanese_font(14 * S, bold=True))

        gap = (inner_w - cw * MAX_EXAMPLES) // max(1, MAX_EXAMPLES - 1)
        font_caption = load_japanese_font(15 * S)
        for i, (title, cover) in enumerate(examples[:MAX_EXAMPLES]):
            cx = inner_x + i * (cw + gap)
            _draw_cover(img, cover, (cx, covers_top, cx + cw, covers_top + ch))
            for n, line in enumerate(
                _wrap_to_width(draw, title, font_caption, cw, CAPTION_LINES)
            ):
                draw.text((cx, caption_y + n * CAPTION_LINE_H * S),
                          line, fill=INK_TERTIARY, font=font_caption)

    if caveat:
        font_caveat = load_japanese_font(14 * S)
        draw.text(
            (inner_x, caveat_y),
            truncate_to_width(draw, caveat, font_caveat, inner_w),
            fill=INK_TERTIARY, font=font_caveat,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    logger.info(
        "theme_card rendered: theme=%r rules=%d examples=%d duration_ms=%d",
        theme_label, len(rule_lines), len(examples),
        int((time.perf_counter() - t0) * 1000),
    )
    return buf
