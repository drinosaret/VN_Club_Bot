"""
Monthly banner image generator for the VN of the Month.

Produces a clean, light-themed 1200x480 PNG combining the VN cover with
reading stats from VNDB and jiten.moe. Designed for posting in announcement
channels — the chars/day figure uses the full month length so the banner is
date-stable.

Layout:
    +------------------------------------------------------------+
    | [cover]   ▍ MAY 2026  •  VN OF THE MONTH                   |
    |           Title  · 2007                                    |
    |           Romaji subtitle                                  |
    |                                                            |
    |           ┌──────────── stats grid (2 col x 3 row) ──────┐ |
    |           │ LENGTH         │ VNDB SCORE                  │ |
    |           │ Medium         │ 78  (1.2K votes)            │ |
    |           │ TOTAL CHARS    │ UNIQUE KANJI                │ |
    |           │ 975,115        │ 2,043                       │ |
    |           │ DIFFICULTY     │ DIALOGUE                    │ |
    |           │ 3.15 / 5       │ 46%                         │ |
    |           └────────────────────────────────────────────────┘
    |           ┌──────── 31,456 chars/day · 31-day month ────┐ |
    +------------------------------------------------------------+
"""

import asyncio
import io
import logging
import time
from typing import Any, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Font cascade, palette, and the AA rounded-rect primitive live in
# pillow_helpers now so every renderer (banner, profile card, club stats,
# badges) shares one source of truth. Re-export the legacy underscore names
# so existing `from lib.monthly_banner import _load_japanese_font, …` callers
# keep working.
from lib.pillow_helpers import (
    fetch_image_bytes_capped,
    load_japanese_font as _load_japanese_font,
    format_compact_count as _format_compact_count,
)
from lib.jiten_client import resolve_display_cover

logger = logging.getLogger(__name__)


def _release_year(released: Optional[str]) -> Optional[str]:
    """VNDB ``released`` like '2007-08-10' or '2007' -> '2007'. ``None`` if unparseable."""
    if not released:
        return None
    head = released.strip().split("-", 1)[0]
    return head if head.isdigit() and len(head) == 4 else None


_LENGTH_TIER_LABELS = {
    1: "Very Short",
    2: "Short",
    3: "Medium",
    4: "Long",
    5: "Very Long",
}


def format_length_tier(length: Any) -> Optional[str]:
    """Map VNDB's `length` (1-5 int, possibly stringified) to a readable tier.

    Pass-through for already-formatted strings ("Long", "Very Short", ...).
    Returns None when it can't make sense of the input.
    """
    if length is None:
        return None
    if isinstance(length, str):
        s = length.strip()
        if not s:
            return None
        if s.isdigit():
            return _LENGTH_TIER_LABELS.get(int(s))
        return s  # already a label like "Medium"
    if isinstance(length, int):
        return _LENGTH_TIER_LABELS.get(length)
    return None


def _format_read_time_hours(length_minutes: Optional[int]) -> Optional[str]:
    """Minutes -> '~12 hr' / '~1.5 hr' for the banner stats panel."""
    if not length_minutes or length_minutes <= 0:
        return None
    hours = length_minutes / 60
    if hours >= 10:
        return f"~{round(hours)} hr"
    return f"~{hours:.1f} hr"


class MonthlyBannerGenerator:
    """Render a 1200x480 monthly book-club banner.

    All layout constants below are stated in *logical* pixels (the design
    grid). The image is actually rendered at SCALE× those dimensions for
    crisp display on HiDPI screens — every coord and font size is multiplied
    by ``SCALE`` at draw time.
    """

    SCALE = 2  # render at 2x; bump higher only if Discord's 8MB cap allows

    BANNER_WIDTH = 1200 * SCALE
    BANNER_HEIGHT = 480 * SCALE

    COVER_WIDTH = 280 * SCALE
    COVER_HEIGHT = 400 * SCALE
    COVER_X = 40 * SCALE
    COVER_Y = 40 * SCALE
    TEXT_X = 360 * SCALE       # right column starts here
    TEXT_RIGHT = 1160 * SCALE  # end of usable text area

    # Light, paper-cream palette
    BG = (251, 248, 241)
    INK_PRIMARY = (28, 27, 42)
    INK_SECONDARY = (88, 84, 110)
    INK_TERTIARY = (146, 140, 160)
    HAIRLINE = (216, 210, 196)
    PANEL_BG = (243, 238, 224)
    CALLOUT_BG = (247, 242, 230)  # subtle cream tint, lighter than PANEL_BG
    ACCENT = (88, 70, 150)        # deep purple
    ACCENT_INK = (255, 255, 255)  # text on accent
    NSFW_BG = (210, 196, 200)
    PLACEHOLDER_BG = (232, 226, 212)

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "MonthlyBannerGenerator":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ------------------------------------------------------------------
    # cover loading
    # ------------------------------------------------------------------
    async def _fetch_cover(self, cover_url: str) -> Optional[Image.Image]:
        if not self.session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Hikarubot/1.0 (+vnclub.org)"},
            )
        # Capped fetch: enforces Content-Type=image/*, Content-Length
        # under 8MiB, and streaming-body cap so a malicious/misconfigured
        # upstream can't blow up RSS via a multi-GB response. The
        # module-level MAX_IMAGE_PIXELS cap (in pillow_helpers) then
        # protects against a small-bytes / huge-decoded image bomb.
        data = await fetch_image_bytes_capped(self.session, cover_url)
        if data is None:
            return None

        # PIL keeps the source file handle open on the Image returned by
        # Image.open until you explicitly close it. Using the context-
        # manager form ensures the original is closed once we have the
        # RGB copy — without this, a long-running bot rendering one
        # banner per cycle leaks one fd per render.
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                return img.convert("RGB")
        except Exception as e:
            logger.warning("cover decode failed for %s: %s", cover_url, e)
            return None

    def _fit_cover(self, img: Image.Image) -> Image.Image:
        target_ratio = self.COVER_WIDTH / self.COVER_HEIGHT
        src_ratio = img.width / img.height
        if src_ratio > target_ratio:
            new_h = self.COVER_HEIGHT
            new_w = int(img.width * (new_h / img.height))
        else:
            new_w = self.COVER_WIDTH
            new_h = int(img.height * (new_w / img.width))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - self.COVER_WIDTH) // 2
        top = (new_h - self.COVER_HEIGHT) // 2
        return img.crop((left, top, left + self.COVER_WIDTH, top + self.COVER_HEIGHT))

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------
    def _draw_rounded_rect(self, draw: ImageDraw.ImageDraw, coords, radius: int,
                           fill, outline=None, width: int = 1):
        x1, y1, x2, y2 = coords
        if fill:
            draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
            draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
            draw.pieslice([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=fill)
            draw.pieslice([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=fill)
            draw.pieslice([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=fill)
            draw.pieslice([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=fill)
        if outline:
            draw.line([x1 + radius, y1, x2 - radius, y1], fill=outline, width=width)
            draw.line([x1 + radius, y2, x2 - radius, y2], fill=outline, width=width)
            draw.line([x1, y1 + radius, x1, y2 - radius], fill=outline, width=width)
            draw.line([x2, y1 + radius, x2, y2 - radius], fill=outline, width=width)
            draw.arc([x1, y1, x1 + radius * 2, y1 + radius * 2], 180, 270, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y1, x2, y1 + radius * 2], 270, 360, fill=outline, width=width)
            draw.arc([x1, y2 - radius * 2, x1 + radius * 2, y2], 90, 180, fill=outline, width=width)
            draw.arc([x2 - radius * 2, y2 - radius * 2, x2, y2], 0, 90, fill=outline, width=width)

    def _truncate_to_width(self, draw: ImageDraw.ImageDraw, text: str,
                           font: ImageFont.ImageFont, max_width: int) -> str:
        if not text:
            return ""
        if draw.textlength(text, font=font) <= max_width:
            return text
        ellipsis = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            candidate = text[:mid].rstrip() + ellipsis
            if draw.textlength(candidate, font=font) <= max_width:
                lo = mid + 1
            else:
                hi = mid
        return text[: max(1, lo - 1)].rstrip() + ellipsis

    def _wrap_text_to_pixel_width(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_width: int,
        max_lines: int,
    ) -> list[str]:
        """Word-wrap ``text`` into up to ``max_lines`` lines that each
        fit ``max_width`` pixels. Last line gets ellipsized via
        ``_truncate_to_width`` if there's overflow.

        Whitespace-greedy: walks word by word, starting a new line when
        appending the next word would exceed ``max_width``. Single words
        wider than ``max_width`` are themselves truncated rather than
        creating a degenerate empty line. Newlines (\\n) in the input
        are honored as hard breaks.
        """
        if not text or max_lines <= 0:
            return []
        lines: list[str] = []
        # Honor hard line breaks first (descriptions sometimes contain them).
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                # Preserve blank lines only if they fit within max_lines.
                if lines and len(lines) < max_lines:
                    lines.append("")
                continue
            words = paragraph.split()
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if draw.textlength(candidate, font=font) <= max_width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                    if len(lines) >= max_lines:
                        break
                # Word alone overflows the available width — drop it
                # in alone (truncated by the line-cap below if needed).
                current = word
            if current and len(lines) < max_lines:
                lines.append(current)
            if len(lines) >= max_lines:
                break
        if not lines:
            return []
        # Trim to cap and ellipsize the last line if we ran out of room.
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        # Detect overflow: did we have remaining text we couldn't fit?
        # Heuristic: if the original text's char count is meaningfully
        # larger than what we wrote out, the last line should ellipsize.
        rendered_len = sum(len(line) for line in lines) + max(0, len(lines) - 1)
        if rendered_len < len(text.strip()):
            tail = lines[-1]
            ellipsized = self._truncate_to_width(
                draw, tail + "…", font, max_width,
            )
            lines[-1] = ellipsized
        return lines

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def generate(
        self,
        title: str,
        subtitle: Optional[str],
        cover_url: Optional[str],
        cover_is_nsfw: bool,
        month_label: str,
        month_days: int,
        total_chars: Optional[int],
        chars_per_day: Optional[int],
        difficulty: Optional[float],
        length_tier: Optional[str],
        vndb_rating: Optional[float] = None,
        vndb_votecount: Optional[int] = None,
        unique_kanji: Optional[int] = None,
        dialogue_pct: Optional[float] = None,
        release_year: Optional[str] = None,
        top_tags: Optional[list[str]] = None,
        length_minutes: Optional[int] = None,
        eyebrow_label: Optional[str] = None,
        # Used only by the no-jiten layout path. Jiten-present renders
        # ignore them so the dense layout stays unchanged.
        platforms: Optional[str] = None,
        description: Optional[str] = None,
        developer: Optional[str] = None,
        tag_count: Optional[int] = None,
        render_cover: bool = True,
    ) -> io.BytesIO:
        # The cover fetch is the only async work in the render path. Pre-fetch
        # it here, then push the heavy synchronous PIL render onto a worker
        # thread so a single banner doesn't stall the event loop for hundreds
        # of milliseconds.
        t0 = time.perf_counter()
        prefetched_cover: Optional[Image.Image] = None
        if cover_url and render_cover:
            prefetched_cover = await self._fetch_cover(cover_url)
        try:
            buf = await asyncio.to_thread(
                self._render_sync,
                title=title, subtitle=subtitle,
                cover_is_nsfw=cover_is_nsfw, prefetched_cover=prefetched_cover,
                month_label=month_label, month_days=month_days,
                total_chars=total_chars, chars_per_day=chars_per_day,
                difficulty=difficulty, length_tier=length_tier,
                vndb_rating=vndb_rating, vndb_votecount=vndb_votecount,
                unique_kanji=unique_kanji, dialogue_pct=dialogue_pct,
                release_year=release_year, top_tags=top_tags,
                length_minutes=length_minutes, eyebrow_label=eyebrow_label,
                platforms=platforms,
                description=description,
                developer=developer,
                tag_count=tag_count,
                render_cover=render_cover,
            )
        except Exception:
            logger.exception(
                "monthly_banner render failed: title=%r month=%s cover=%s",
                title, month_label, bool(prefetched_cover),
            )
            raise
        logger.info(
            "monthly_banner rendered: title=%r month=%s cover=%s duration_ms=%d",
            title, month_label, bool(prefetched_cover),
            int((time.perf_counter() - t0) * 1000),
        )
        return buf

    def _render_sync(
        self,
        title: str,
        subtitle: Optional[str],
        cover_is_nsfw: bool,
        prefetched_cover: Optional[Image.Image],
        month_label: str,
        month_days: int,
        total_chars: Optional[int],
        chars_per_day: Optional[int],
        difficulty: Optional[float],
        length_tier: Optional[str],
        vndb_rating: Optional[float] = None,
        vndb_votecount: Optional[int] = None,
        unique_kanji: Optional[int] = None,
        dialogue_pct: Optional[float] = None,
        release_year: Optional[str] = None,
        top_tags: Optional[list[str]] = None,
        length_minutes: Optional[int] = None,
        eyebrow_label: Optional[str] = None,
        platforms: Optional[str] = None,
        description: Optional[str] = None,
        developer: Optional[str] = None,
        tag_count: Optional[int] = None,
        render_cover: bool = True,
    ) -> io.BytesIO:
        S = self.SCALE
        # When the cover is hidden the text block reclaims its column: it
        # starts at the left margin and runs the full width. With a cover it
        # starts to the cover's right.
        text_x = self.TEXT_X if render_cover else self.COVER_X
        img = Image.new("RGB", (self.BANNER_WIDTH, self.BANNER_HEIGHT), self.BG)
        draw = ImageDraw.Draw(img)

        # outer hairline (anti-aliased via oversample+LANCZOS)
        self._paste_aa_rounded(
            img, (16 * S, 16 * S, self.BANNER_WIDTH - 16 * S, self.BANNER_HEIGHT - 16 * S),
            radius=18 * S, outline=self.HAIRLINE, outline_w=1 * S,
        )

        # ---- cover ----
        # Skipped entirely in coverless mode (render_cover=False) so the cover
        # column is reclaimed by the text block rather than left as an empty box.
        if render_cover:
            cover_box = (self.COVER_X, self.COVER_Y,
                         self.COVER_X + self.COVER_WIDTH, self.COVER_Y + self.COVER_HEIGHT)
            if prefetched_cover is not None:
                raw = prefetched_cover
                if raw is not None:
                    fitted = self._fit_cover(raw)
                    if cover_is_nsfw:
                        # Blur flagged covers; see COVER_BLUR_THRESHOLD in lib/vndb_api.py.
                        fitted = fitted.filter(ImageFilter.GaussianBlur(radius=20))
                    # Mask the cover with the same rounded shape as its border so
                    # the image's square corners don't poke past the rounded
                    # outline. Render the mask at OVERSAMPLE× and downsample with
                    # LANCZOS so the cover's clipped corners get anti-aliased
                    # transitions, matching the AA outline drawn over them.
                    cover_radius = 4 * S
                    oversample = 4
                    ow, oh = fitted.size[0] * oversample, fitted.size[1] * oversample
                    big_mask = Image.new("L", (ow, oh), 0)
                    ImageDraw.Draw(big_mask).rounded_rectangle(
                        [(0, 0), (ow - 1, oh - 1)],
                        radius=cover_radius * oversample,
                        fill=255,
                    )
                    mask = big_mask.resize(fitted.size, Image.Resampling.LANCZOS)
                    img.paste(fitted, (self.COVER_X, self.COVER_Y), mask)
                else:
                    self._draw_cover_placeholder(draw, cover_box, "Cover unavailable")
            elif cover_is_nsfw:
                self._draw_cover_placeholder(draw, cover_box, "🔞 NSFW", bg=self.NSFW_BG)
            else:
                self._draw_cover_placeholder(draw, cover_box, "No cover")

            # subtle frame around cover area (anti-aliased)
            self._paste_aa_rounded(img, cover_box, radius=4 * S,
                                   outline=self.HAIRLINE, outline_w=1 * S)

        # ---- fonts (logical sizes; rendered at SCALE×) ----
        font_eyebrow = _load_japanese_font(20 * S, bold=True)
        font_title = _load_japanese_font(40 * S, bold=True)
        font_year = _load_japanese_font(22 * S)
        font_subtitle = _load_japanese_font(18 * S)
        font_stat_label = _load_japanese_font(13 * S, bold=True)
        font_stat_value = _load_japanese_font(22 * S, bold=True)
        font_callout_big = _load_japanese_font(38 * S, bold=True)
        font_callout_sub = _load_japanese_font(18 * S)

        # ---- header (eyebrow + attribution) ----
        bar_x = text_x
        # Top of the bar aligns with the cover's top edge — keeps the right
        # column's top margin equal to the cover's top margin, and the
        # bottom margins land closer to balanced for both subtitle cases.
        bar_y = self.COVER_Y
        bar_h = 22 * S
        draw.rectangle([bar_x, bar_y, bar_x + 6 * S, bar_y + bar_h], fill=self.ACCENT)
        eyebrow_suffix = eyebrow_label or "VN OF THE MONTH"
        eyebrow = f"{month_label.upper()}  •  {eyebrow_suffix.upper()}"
        # Vertically center the eyebrow text in the bar using anchor='lm'
        # so positioning doesn't depend on font-specific ascender padding.
        bar_center_y = bar_y + bar_h // 2
        draw.text((bar_x + 16 * S, bar_center_y), eyebrow,
                  fill=self.ACCENT, font=font_eyebrow, anchor="lm")

        # Attribution paired with eyebrow on the right edge.
        # Share the eyebrow's *baseline* (not bbox-middle) so both texts
        # read as if on the same line. Computed from the larger font's
        # metrics so it adapts to font changes.
        font_attr = _load_japanese_font(11 * S)
        eb_ascent, eb_descent = font_eyebrow.getmetrics()
        eyebrow_baseline = bar_center_y + (eb_ascent - eb_descent) // 2
        attr_text = "Data: VNDB · jiten.moe"
        draw.text((self.TEXT_RIGHT, eyebrow_baseline),
                  attr_text, fill=self.INK_TERTIARY, font=font_attr, anchor="rs")

        # ---- title (with optional release year inline) ----
        title_y = bar_y + 38 * S
        if release_year:
            year_text = f"  ·  {release_year}"
            year_w = draw.textlength(year_text, font=font_year)
        else:
            year_text = ""
            year_w = 0

        max_title_w = self.TEXT_RIGHT - text_x - int(year_w)
        title_drawn = self._truncate_to_width(draw, title, font_title, max_title_w)
        title_ascent, title_descent = font_title.getmetrics()
        title_baseline = title_y + title_ascent
        # Visual middle of the title's font extent — used to vertically
        # center the smaller inline year so it doesn't sit visually below
        # the title's center despite sharing its baseline.
        title_visual_middle = title_baseline - (title_ascent - title_descent) // 2
        draw.text((text_x, title_baseline), title_drawn,
                  fill=self.INK_PRIMARY, font=font_title, anchor="ls")
        if year_text:
            title_w = draw.textlength(title_drawn, font=font_title)
            draw.text((text_x + title_w, title_visual_middle),
                      year_text, fill=self.INK_TERTIARY, font=font_year, anchor="lm")

        subtitle_y = title_y + 50 * S
        if subtitle and subtitle.strip() and subtitle.strip() != title.strip():
            sub = self._truncate_to_width(draw, subtitle, font_subtitle,
                                          self.TEXT_RIGHT - text_x)
            draw.text((text_x, subtitle_y), sub,
                      fill=self.INK_SECONDARY, font=font_subtitle)
            after_subtitle_y = subtitle_y + 30 * S
        else:
            after_subtitle_y = subtitle_y + 14 * S

        # ---- tag pills (between title block and stats panel) ----
        if top_tags:
            self._draw_tag_pills(img, draw, after_subtitle_y, top_tags, text_x)
            stats_top = after_subtitle_y + (24 + 12) * S  # pill_h + gap below
        else:
            stats_top = after_subtitle_y + 4 * S

        # ---- stats grid (2 col x 3 row) ----
        # 178 = 18 top pad + 142 content (3 rows) + 18 bottom pad — symmetric.
        stats_panel_h = 178 * S
        panel = (text_x, stats_top, self.TEXT_RIGHT, stats_top + stats_panel_h)
        self._draw_rounded_rect(draw, panel, radius=12 * S,
                                fill=self.PANEL_BG, outline=None)

        # column layout — row_h must clear (label_height + label_value_gap + value_height)
        # plus a few px of breathing room, otherwise the value text crowds the next label.
        col_gap = 24 * S
        pad_x = 24 * S
        # Top pad is smaller than the implied bottom pad (panel_h - top_pad -
        # content) to compensate for the visual asymmetry between the small
        # LABEL row at the top (which has ~3px of ascender padding above the
        # cap line) and the larger VALUE row at the bottom (whose digits have
        # no descenders and bottom out at the baseline).
        pad_y = 14 * S
        row_h = 50 * S
        col_w = (panel[2] - panel[0] - pad_x * 2 - col_gap) // 2
        col1_x = panel[0] + pad_x
        col2_x = col1_x + col_w + col_gap

        if total_chars is None:
            # No jiten data → 2×3 grid mirroring the jiten layout's row
            # count, populated entirely from VNDB so the panel doesn't
            # read as half-empty:
            #   LENGTH · VNDB SCORE
            #   READ TIME · PLATFORMS
            #   DEVELOPER · TAGS
            # YEAR is excluded (already shown next to the title).
            # ORIGINAL LANGUAGE is excluded (the audience is Japanese
            # learners — value would be "Japanese" on nearly every card).
            read_time = _format_read_time_hours(length_minutes)
            left_rows = [
                ("LENGTH", length_tier or "—"),
                ("READ TIME", read_time or "—"),
                ("DEVELOPER", self._truncate_to_width(
                    draw, developer or "—", font_stat_value, col_w,
                )),
            ]
            right_rows = [
                ("VNDB SCORE", self._format_vndb_score(vndb_rating, vndb_votecount)),
                ("PLATFORMS", self._truncate_to_width(
                    draw, platforms or "—", font_stat_value, col_w,
                )),
                ("TAGS", str(tag_count) if tag_count else "—"),
            ]
            self._draw_stat_column(draw, col1_x, panel[1] + pad_y, row_h,
                                   left_rows, font_stat_label, font_stat_value)
            self._draw_stat_column(draw, col2_x, panel[1] + pad_y, row_h,
                                   right_rows, font_stat_label, font_stat_value)
        else:
            left_rows = [
                ("LENGTH", length_tier or "Unknown"),
                ("TOTAL CHARACTERS", f"{total_chars:,}"),
                ("DIFFICULTY",
                 f"{difficulty:.2f} / 5" if difficulty and difficulty > 0 else "—"),
            ]
            right_rows = [
                ("VNDB SCORE", self._format_vndb_score(vndb_rating, vndb_votecount)),
                ("UNIQUE KANJI", f"{unique_kanji:,}" if unique_kanji else "—"),
                ("DIALOGUE",
                 f"{dialogue_pct:.0f}%" if dialogue_pct and dialogue_pct > 0 else "—"),
            ]
            self._draw_stat_column(draw, col1_x, panel[1] + pad_y, row_h,
                                   left_rows, font_stat_label, font_stat_value)
            self._draw_stat_column(draw, col2_x, panel[1] + pad_y, row_h,
                                   right_rows, font_stat_label, font_stat_value)

        # ---- callout (quote-style: cream fill, accent bar on left) ----
        callout_top = stats_top + stats_panel_h + 14 * S
        callout_h = 70 * S
        callout = (text_x, callout_top, self.TEXT_RIGHT, callout_top + callout_h)
        self._draw_rounded_rect(draw, callout, radius=10 * S,
                                fill=self.CALLOUT_BG, outline=None)

        # Left accent bar
        bar_w = 8 * S
        draw.rectangle(
            [callout[0], callout[1], callout[0] + bar_w, callout[3]],
            fill=self.ACCENT,
        )

        # `month_days` may exceed 31 for multi-month windows (seasonal banners
        # span the full 3-month season). Pick "month" vs "season" wording so
        # the callout reads correctly in both cases.
        window_word = "season" if month_days > 31 else "month"

        # Three callout shapes:
        #   1. Jiten present + chars/day → focal big number "X chars/day"
        #      followed by the month-days tail. (existing dense layout)
        #   2. No jiten + description present → repurpose the quote block
        #      to render the VN's description excerpt as an actual quote.
        #      The eyebrow already conveys the month, so dropping the
        #      "X-day month" tail here is a deliberate trade for readable
        #      flavour text. Keeps the panel from feeling under-filled.
        #   3. No jiten + no description either → fall back to making the
        #      month-days the focal stat (today's behaviour, kept).
        callout_text_x = callout[0] + bar_w + 24 * S
        callout_text_right = callout[2] - 18 * S
        callout_inner_w = callout_text_right - callout_text_x

        cleaned_description = (description or "").strip()
        if total_chars is None and cleaned_description:
            # Mode 2: description as quote.
            quote_font = _load_japanese_font(15 * S)
            line_h = 22 * S
            max_lines = max(1, callout_h // line_h)
            lines = self._wrap_text_to_pixel_width(
                draw, cleaned_description, quote_font,
                callout_inner_w, max_lines,
            )
            block_h = line_h * len(lines)
            text_y = callout[1] + (callout_h - block_h) // 2
            for line in lines:
                draw.text((callout_text_x, text_y), line,
                          fill=self.INK_PRIMARY, font=quote_font)
                text_y += line_h
        else:
            # Modes 1 + 3: focal-number callout (existing rendering).
            if chars_per_day is not None and chars_per_day > 0:
                big_text = f"{chars_per_day:,}"
                unit_text = "chars/day"
                tail_text = f"·  {month_days}-day {window_word}"
            else:
                big_text = f"{month_days}-day {window_word}"
                unit_text = ""
                tail_text = ""

            gap_after_big = 12 * S
            gap_before_tail = 14 * S
            big_w = draw.textlength(big_text, font=font_callout_big)
            unit_w = draw.textlength(unit_text, font=font_callout_sub) if unit_text else 0

            # Big number vertically centered in the callout. Smaller inline
            # pieces share its baseline (sit lower than visual center) —
            # reads as "trailing detail" against the focal big number,
            # more anchored than visually-centering the smaller text.
            callout_center_y = callout[1] + callout_h // 2
            big_ascent, big_descent = font_callout_big.getmetrics()
            baseline_y = callout_center_y + (big_ascent - big_descent) // 2
            draw.text((callout_text_x, callout_center_y), big_text,
                      fill=self.ACCENT, font=font_callout_big, anchor="lm")
            unit_x = callout_text_x + big_w + (gap_after_big if unit_text else 0)
            draw.text((unit_x, baseline_y), unit_text,
                      fill=self.INK_PRIMARY, font=font_callout_sub, anchor="ls")
            tail_x = unit_x + unit_w + (gap_before_tail if unit_text else gap_after_big)
            draw.text((tail_x, baseline_y), tail_text,
                      fill=self.INK_TERTIARY, font=font_callout_sub, anchor="ls")

        buf = io.BytesIO()
        # PNG optimize=True burns ~30% of encode time picking the optimal
        # compression filter for a few percent file-size win. The output
        # is already well under Discord's 8MiB cap, so the smaller render
        # latency is the better trade for a Discord-only delivery path.
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _format_vndb_score(self, rating: Optional[float], votes: Optional[int]) -> str:
        if rating is None or rating <= 0:
            return "—"
        votes_str = f"  ({_format_compact_count(votes)} votes)" if votes else ""
        return f"{rating:.0f}{votes_str}"

    def _draw_stat_column(self, draw: ImageDraw.ImageDraw, x: int, y: int, row_h: int,
                          rows: list[tuple[str, str]],
                          font_label: ImageFont.ImageFont,
                          font_value: ImageFont.ImageFont):
        S = self.SCALE
        for label, value in rows:
            draw.text((x, y), label, fill=self.INK_SECONDARY, font=font_label)
            draw.text((x, y + 18 * S), value, fill=self.INK_PRIMARY, font=font_value)
            y += row_h

    def _draw_tag_pills(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                        y: int, tags: list[str], text_x: int):
        """Render tags as small left-aligned pills, attached visually to the title block."""
        if not tags:
            return
        S = self.SCALE
        font = _load_japanese_font(13 * S)
        pill_h = 24 * S
        pad_x = 12 * S
        gap = 8 * S

        widths = [int(draw.textlength(t, font=font)) + pad_x * 2 for t in tags]

        avail = self.TEXT_RIGHT - text_x
        kept = list(zip(tags, widths))
        while kept and (sum(w for _, w in kept) + gap * (len(kept) - 1)) > avail:
            kept.pop()
        if not kept:
            return

        x = text_x
        pill_center_y = y + pill_h // 2

        # Pre-render an anti-aliased pill shape by drawing at OVERSAMPLE×
        # the target size into a temp image and downsampling with LANCZOS.
        # The text is drawn directly on the canvas afterwards (PIL handles
        # font hinting/AA itself, so text doesn't need oversampling).
        OVERSAMPLE = 4
        radius = 12 * S

        for label, w in kept:
            self._paste_aa_rounded(
                img, (x, y, x + w, y + pill_h), radius,
                fill=self.PANEL_BG,
                outline=self.INK_TERTIARY,
                outline_w=1 * S,
                oversample=OVERSAMPLE,
            )
            draw.text((x + pad_x, pill_center_y), label,
                      fill=self.INK_PRIMARY, font=font, anchor="lm")
            x += w + gap

    def _paste_aa_rounded(self, img: Image.Image, box, radius: int,
                          fill=None, outline=None, outline_w: int = 1,
                          oversample: int = 4):
        """Render a rounded rectangle at OVERSAMPLE× resolution and downsample
        with LANCZOS to anti-alias the corners. PIL's draw primitives don't
        AA at integer pixels, so this is the standard workaround for smooth
        small-radius shapes. Works for fill+outline, outline-only, or
        fill-only by passing None for the unused parameter."""
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        ow, oh = w * oversample, h * oversample
        layer = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle(
            [(0, 0), (ow - 1, oh - 1)],
            radius=radius * oversample,
            fill=fill,
            outline=outline,
            width=outline_w * oversample,
        )
        layer = layer.resize((w, h), Image.Resampling.LANCZOS)
        img.paste(layer, (x0, y0), layer)

    def _draw_cover_placeholder(self, draw: ImageDraw.ImageDraw,
                                box: Tuple[int, int, int, int], label: str,
                                bg: Optional[Tuple[int, int, int]] = None):
        S = self.SCALE
        bg = bg or self.PLACEHOLDER_BG
        self._draw_rounded_rect(draw, box, radius=4 * S, fill=bg,
                                outline=self.HAIRLINE, width=1 * S)
        font = _load_japanese_font(22 * S)
        text_w = draw.textlength(label, font=font)
        x = box[0] + ((box[2] - box[0]) - text_w) / 2
        y = box[1] + ((box[3] - box[1]) - 24 * S) / 2
        draw.text((x, y), label, fill=self.INK_SECONDARY, font=font)

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


# ---------- shared helpers used by callers that build banners from a VN ----------

def month_label_for(yyyy_mm: str) -> str:
    """Format a YYYY-MM string for the banner eyebrow (e.g. '2026-06' -> 'June 2026')."""
    from datetime import datetime
    return datetime.strptime(yyyy_mm, "%Y-%m").strftime("%B %Y")


def days_in_month(yyyy_mm: str) -> int:
    """Calendar days in a given YYYY-MM. Used to drive banner chars/day math."""
    from calendar import monthrange
    y, m = yyyy_mm.split("-")
    return monthrange(int(y), int(m))[1]


def days_between_months(start_yyyy_mm: str, end_yyyy_mm: str) -> int:
    """Total calendar days from the 1st of `start` through the last day of `end`.

    Used for seasonal banners where the active window spans 3 months — the
    chars/day calculation should use the full multi-month span rather than
    just the start month's day count.
    """
    from calendar import monthrange
    sy, sm = (int(x) for x in start_yyyy_mm.split("-"))
    ey, em = (int(x) for x in end_yyyy_mm.split("-"))
    total = 0
    y, m = sy, sm
    while (y, m) <= (ey, em):
        total += monthrange(y, m)[1]
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return total


async def render_banner_for_vn_entry(
    banner_gen: "MonthlyBannerGenerator",
    vn_info,
    jiten_data,
    target_month: str,
    vndb_extras: Optional[dict] = None,
    target_end_month: Optional[str] = None,
    eyebrow_label: Optional[str] = None,
    period_label_override: Optional[str] = None,
    cover_mode: str = "shown",
) -> io.BytesIO:
    """
    Build a banner from a hikaru `VN_Entry` + optional `JitenInfo` + optional
    VNDB extras (rating/votes/release/tags). Used by /monthly, /seasonal, and
    the cycle cog's winner promotion.

    `target_end_month` (default = `target_month`) extends the active-window
    day count to a multi-month range — used for seasonal banners so chars/day
    is computed across the full season rather than just the start month.

    `eyebrow_label` overrides the trailing text after the period in the
    banner's eyebrow. Defaults to "VN OF THE MONTH"; pass "VN OF THE SEASON"
    for seasonal renders. `period_label_override` replaces the period text
    itself (e.g., "Spring 2026" instead of the start-month label).

    ``cover_mode`` controls the cover region: ``"shown"`` (default) uses the
    real cover (the jiten SFW swap for an NSFW VNDB cover); ``"blurred"`` forces
    the blur on the original VNDB cover, for comparison; ``"hidden"`` drops the
    cover and reflows the text block to the full width.
    """
    end_month = target_end_month or target_month
    target_days = (
        days_between_months(target_month, end_month)
        if end_month != target_month
        else days_in_month(target_month)
    )
    chars = jiten_data.character_count if jiten_data else None
    # Ceil, not floor: the figure is the minimum daily pace that actually
    # finishes the VN within the window. Floor would land a few hundred chars
    # short over a full month.
    cpd = (-(-chars // target_days)) if (chars and target_days) else None
    extras = vndb_extras or {}
    # Pre-clean the description text for the no-jiten callout. The
    # generate() call ignores it on the jiten-present path, so this is
    # a no-op cost on the dense layout.
    description_clean: Optional[str] = None
    if jiten_data is None and vn_info.description:
        description_clean = await vn_info.get_normalized_description(max_length=300)
        if description_clean == "No description available.":
            description_clean = None
    # Cover region per cover_mode. "shown": real cover (jiten SFW swap for an
    # NSFW VNDB cover). "blurred": force the blur on the original VNDB cover
    # (not the jiten swap) so the comparison shows the blur on the real art.
    # "hidden": no cover, text reflows full-width.
    if cover_mode == "hidden":
        display_cover_url, display_is_nsfw, show_cover = None, False, False
    elif cover_mode == "blurred":
        # Force the blur only when there's actually a cover to blur; a coverless
        # VN keeps is_nsfw False so it falls to the neutral "No cover" box rather
        # than a misleading NSFW placeholder.
        url = getattr(vn_info, "thumbnail_url", None) or None
        display_cover_url, display_is_nsfw, show_cover = url, bool(url), True
    else:
        display_cover_url, display_is_nsfw = resolve_display_cover(vn_info, jiten_data)
        show_cover = True
    return await banner_gen.generate(
        title=vn_info.title_ja or vn_info.title_en or vn_info.vndb_id,
        subtitle=vn_info.title_en if vn_info.title_ja else None,
        cover_url=display_cover_url,
        cover_is_nsfw=display_is_nsfw,
        month_label=period_label_override or month_label_for(target_month),
        month_days=target_days,
        total_chars=chars,
        chars_per_day=cpd,
        difficulty=jiten_data.difficulty_raw if jiten_data else None,
        length_tier=format_length_tier(vn_info.length_rating),
        length_minutes=vn_info.length_minutes,
        vndb_rating=extras.get("rating"),
        vndb_votecount=extras.get("votecount"),
        unique_kanji=jiten_data.unique_kanji_count if jiten_data else None,
        dialogue_pct=jiten_data.dialogue_percentage if jiten_data else None,
        release_year=extras.get("release_year"),
        top_tags=extras.get("top_tags") or None,
        eyebrow_label=eyebrow_label,
        platforms=extras.get("platforms"),
        developer=extras.get("developer"),
        tag_count=extras.get("tag_count"),
        description=description_clean,
        render_cover=show_cover,
    )


# ---------- season overview composite ----------

def _render_seasonal_placeholder_header(
    banner_gen: "MonthlyBannerGenerator",
    *,
    season_label: str,
    season_period_label: Optional[str],
) -> Image.Image:
    """Render a 1200×480 (×SCALE) placeholder strip used as the top section
    of the season-overview composite when no seasonal VN has been set.

    Visually matches the real banner's outer frame (cream BG + hairline
    inset rounded rectangle) so the composite stays cohesive, with a
    centered title block stating the season + timeframe + "No seasonal
    pick yet"."""
    S = banner_gen.SCALE
    W = banner_gen.BANNER_WIDTH
    H = banner_gen.BANNER_HEIGHT

    img = Image.new("RGB", (W, H), banner_gen.BG)
    draw = ImageDraw.Draw(img)

    # Outer hairline frame — same 16px inset / 18px corner radius the
    # real banner uses, keeping the placeholder visually consistent.
    banner_gen._paste_aa_rounded(
        img, (16 * S, 16 * S, W - 16 * S, H - 16 * S),
        radius=18 * S, outline=banner_gen.HAIRLINE, outline_w=1 * S,
    )

    # Fonts mirror the real banner's eyebrow / title / subtitle sizing,
    # bumped up where appropriate since the placeholder has the entire
    # banner area to itself rather than sharing space with the cover.
    font_eyebrow = _load_japanese_font(20 * S, bold=True)
    font_title = _load_japanese_font(52 * S, bold=True)
    font_period = _load_japanese_font(24 * S)
    font_status = _load_japanese_font(16 * S)

    # Measure the stacked block so we can vertically center it inside the
    # banner area. Heights come from each font's ascent+descent.
    eb_a, eb_d = font_eyebrow.getmetrics()
    ti_a, ti_d = font_title.getmetrics()
    pe_a, pe_d = font_period.getmetrics()
    st_a, st_d = font_status.getmetrics()

    eyebrow_h = eb_a + eb_d
    title_h = ti_a + ti_d
    period_h = pe_a + pe_d
    status_h = st_a + st_d

    gap_eyebrow_title = 18 * S
    gap_title_period = 14 * S
    gap_period_status = 22 * S

    has_period = bool(season_period_label)
    block_h = (
        eyebrow_h + gap_eyebrow_title + title_h
        + (gap_title_period + period_h if has_period else 0)
        + gap_period_status + status_h
    )

    block_top = (H - block_h) // 2
    center_x = W // 2

    # Eyebrow line: small accent bar + "VN OF THE SEASON" label, centered
    # as a unit so it visually anchors the block the same way the real
    # banner's eyebrow anchors the right column.
    eyebrow_text = "VN OF THE SEASON"
    bar_w = 6 * S
    bar_h = 28 * S
    bar_gap = 12 * S
    eyebrow_text_w = draw.textlength(eyebrow_text, font=font_eyebrow)
    eyebrow_total_w = bar_w + bar_gap + eyebrow_text_w
    eyebrow_x = center_x - eyebrow_total_w / 2
    eyebrow_y = block_top
    # Center the bar vertically against the eyebrow text's visual middle.
    eyebrow_mid = eyebrow_y + eyebrow_h // 2
    draw.rectangle(
        [eyebrow_x, eyebrow_mid - bar_h // 2,
         eyebrow_x + bar_w, eyebrow_mid - bar_h // 2 + bar_h],
        fill=banner_gen.ACCENT,
    )
    draw.text(
        (eyebrow_x + bar_w + bar_gap, eyebrow_mid),
        eyebrow_text, fill=banner_gen.ACCENT, font=font_eyebrow, anchor="lm",
    )

    # Title (season label, e.g. "Spring 2026").
    title_y = eyebrow_y + eyebrow_h + gap_eyebrow_title
    draw.text(
        (center_x, title_y), season_label,
        fill=banner_gen.INK_PRIMARY, font=font_title, anchor="mt",
    )

    # Subtitle (period label, e.g. "April – June 2026").
    if has_period:
        period_y = title_y + title_h + gap_title_period
        draw.text(
            (center_x, period_y), season_period_label,
            fill=banner_gen.INK_SECONDARY, font=font_period, anchor="mt",
        )
        after_period_y = period_y + period_h
    else:
        after_period_y = title_y + title_h

    # Status line — uses INK_TERTIARY when present on the generator,
    # falls back to INK_SECONDARY for older revisions to be defensive.
    status_y = after_period_y + gap_period_status
    status_ink = getattr(banner_gen, "INK_TERTIARY", banner_gen.INK_SECONDARY)
    draw.text(
        (center_x, status_y), "No seasonal pick yet",
        fill=status_ink, font=font_status, anchor="mt",
    )

    return img


async def _prefetch_overview_cover(
    banner_gen: "MonthlyBannerGenerator", pick: dict,
) -> Optional[Image.Image]:
    """Fetch the cover Image for one overview-strip pick, or None when the
    pick has no url or the fetch fails. Returning the raw Image lets the
    (synchronous) compositing loop later run inside asyncio.to_thread
    without needing async I/O. Flagged covers are blurred in
    `_draw_overview_card`, not skipped here.
    """
    url = pick.get("cover_url")
    if not url:
        return None
    try:
        return await banner_gen._fetch_cover(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("season-overview cover fetch failed: %s", e)
        return None


async def render_season_overview(
    banner_gen: "MonthlyBannerGenerator",
    seasonal_buf: Optional[io.BytesIO],
    monthly_cells: list[tuple[str, list[dict]]],
    *,
    season_label: Optional[str] = None,
    season_period_label: Optional[str] = None,
) -> io.BytesIO:
    """Compose a season-overview image: seasonal banner (or a placeholder
    header) on top, a row of per-month cells underneath listing each month's
    monthly pick(s).

    Args:
        banner_gen: an open MonthlyBannerGenerator (reuses its session for
            cover fetches).
        seasonal_buf: BytesIO output of `render_banner_for_vn_entry` for the
            seasonal pick, or ``None`` when no seasonal pick has been set.
            When ``None`` a placeholder header strip (same dimensions as the
            real banner) is rendered showing the season label + timeframe so
            the bottom-strip layout still fits.
        monthly_cells: 3-tuple-list of (month_label, picks). Each pick is a
            dict ``{"cover_url": str|None, "title": str, "is_nsfw": bool}``.
            Empty pick list renders a "No monthly pick" placeholder.
        season_label: e.g. ``"Spring 2026"``. Used only when ``seasonal_buf``
            is ``None`` to fill the placeholder header. Falls back to
            ``"Season"`` if not provided.
        season_period_label: e.g. ``"April – June 2026"``. Used only when
            ``seasonal_buf`` is ``None``.

    Returns:
        BytesIO PNG of the composite.
    """
    S = banner_gen.SCALE
    W = banner_gen.BANNER_WIDTH

    # Pre-fetch every monthly-cell cover in parallel up front. The
    # subsequent compositing pass is then purely synchronous and can
    # be pushed off the event loop via asyncio.to_thread — matching
    # how MonthlyBannerGenerator.generate already handles its own
    # render. Without this split the loop blocks ~1-2s per call while
    # the PIL composite + PNG encode runs, which is rude on a
    # multi-server deployment.
    flat_picks = [pick for _, picks in monthly_cells for pick in picks]
    prefetched_covers = await asyncio.gather(
        *[_prefetch_overview_cover(banner_gen, p) for p in flat_picks]
    )
    cover_by_pick_id = {
        id(pick): cover for pick, cover in zip(flat_picks, prefetched_covers)
    }

    # Top section: paste the seasonal banner as-is, or render a placeholder
    # header strip the same size as a real banner when no seasonal exists.
    if seasonal_buf is not None:
        seasonal_buf.seek(0)
        # Close the source Image after the RGB convert so we don't leak
        # an fd per composite. See _fetch_cover for the same pattern.
        with Image.open(seasonal_buf) as _src:
            top_img = _src.convert("RGB")
    else:
        top_img = _render_seasonal_placeholder_header(
            banner_gen,
            season_label=season_label or "Season",
            season_period_label=season_period_label,
        )
    top_h = top_img.height

    # Layout for the bottom strip. Sized so the row scales with the busiest
    # cell — empty cells just show a placeholder card at the same height so
    # the grid stays even.
    cell_count = len(monthly_cells) or 1
    cell_w = W // cell_count
    pad = 16 * S
    header_h = 44 * S
    card_h = 90 * S            # mini-card (cover + title) row height
    cover_w = 50 * S
    cover_h = 70 * S
    cover_gap = 12 * S         # space between cover and text
    card_gap = 6 * S           # vertical gap between stacked cards
    section_top_pad = 12 * S
    section_bottom_pad = 16 * S

    max_stack = max((max(1, len(picks)) for _, picks in monthly_cells), default=1)
    strip_h = section_top_pad + header_h + max_stack * card_h \
        + max(0, max_stack - 1) * card_gap + section_bottom_pad

    out_h = top_h + strip_h

    def _composite_sync() -> io.BytesIO:
        # Everything inside here is pure PIL — no awaits — so the
        # async caller can hand it to asyncio.to_thread and free the
        # event loop while it runs.
        canvas = Image.new("RGB", (W, out_h), banner_gen.BG)
        canvas.paste(top_img, (0, 0))

        draw = ImageDraw.Draw(canvas)

        # Hairline divider between seasonal banner and monthly strip.
        draw.line(
            [(0, top_h), (W, top_h)], fill=banner_gen.HAIRLINE, width=1 * S,
        )

        font_header = _load_japanese_font(20 * S, bold=True)
        font_title = _load_japanese_font(15 * S, bold=True)
        font_meta = _load_japanese_font(13 * S)

        for col_idx, (month_label, picks) in enumerate(monthly_cells):
            cell_x0 = col_idx * cell_w
            cell_x1 = cell_x0 + cell_w

            # Vertical separator between cells (skip the leftmost edge).
            if col_idx > 0:
                draw.line(
                    [(cell_x0, top_h + section_top_pad // 2),
                     (cell_x0, out_h - section_bottom_pad // 2)],
                    fill=banner_gen.HAIRLINE, width=1 * S,
                )

            # Header: month label, centered in the cell.
            header_y = top_h + section_top_pad
            header_w = draw.textlength(month_label, font=font_header)
            draw.text(
                (cell_x0 + (cell_w - header_w) / 2, header_y),
                month_label, fill=banner_gen.INK_PRIMARY, font=font_header,
            )

            cards_y = header_y + header_h
            if not picks:
                # Single placeholder card spanning the same vertical
                # band so the row stays visually balanced when other
                # cells have stacks. No pre-fetched cover by definition.
                placeholder = {"cover_url": None, "title": "No monthly pick", "is_nsfw": False}
                _draw_overview_card(
                    banner_gen, canvas, draw, placeholder,
                    prefetched_cover=None,
                    x=cell_x0 + pad, y=cards_y,
                    inner_w=cell_w - 2 * pad,
                    cover_w=cover_w, cover_h=cover_h,
                    cover_gap=cover_gap,
                    card_h=card_h,
                    font_title=font_title, font_meta=font_meta,
                    dim=True,
                )
                continue

            for pick in picks:
                _draw_overview_card(
                    banner_gen, canvas, draw, pick,
                    prefetched_cover=cover_by_pick_id.get(id(pick)),
                    x=cell_x0 + pad, y=cards_y,
                    inner_w=cell_w - 2 * pad,
                    cover_w=cover_w, cover_h=cover_h,
                    cover_gap=cover_gap,
                    card_h=card_h,
                    font_title=font_title, font_meta=font_meta,
                    dim=False,
                )
                cards_y += card_h + card_gap

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        buf.seek(0)
        return buf

    return await asyncio.to_thread(_composite_sync)


def _draw_overview_card(
    banner_gen: "MonthlyBannerGenerator",
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    pick: dict,
    *,
    prefetched_cover: Optional[Image.Image],
    x: int, y: int, inner_w: int,
    cover_w: int, cover_h: int, cover_gap: int, card_h: int,
    font_title: ImageFont.ImageFont, font_meta: ImageFont.ImageFont,
    dim: bool,
):
    """Draw one mini-card (cover + title) inside a season-overview cell.

    The cover Image is fetched by `render_season_overview` upfront and
    passed in via `prefetched_cover` so this function (and the caller's
    compositing loop) can run inside asyncio.to_thread.
    """
    S = banner_gen.SCALE
    cover_box = (x, y + (card_h - cover_h) // 2,
                 x + cover_w, y + (card_h - cover_h) // 2 + cover_h)

    # Cover: resize+center-crop the pre-fetched Image into the small
    # thumb box. NSFW or missing covers fall back to a labelled
    # placeholder so the layout never collapses.
    cover_img = None
    if prefetched_cover is not None:
        try:
            ratio_target = cover_w / cover_h
            ratio_src = prefetched_cover.width / prefetched_cover.height
            if ratio_src > ratio_target:
                new_h = cover_h
                new_w = int(prefetched_cover.width * (new_h / prefetched_cover.height))
            else:
                new_w = cover_w
                new_h = int(prefetched_cover.height * (new_w / prefetched_cover.width))
            resized = prefetched_cover.resize((new_w, new_h), Image.Resampling.LANCZOS)
            left = (new_w - cover_w) // 2
            top = (new_h - cover_h) // 2
            cover_img = resized.crop((left, top, left + cover_w, top + cover_h))
        except Exception as e:  # noqa: BLE001
            logger.warning("season-overview cover composite failed: %s", e)

    if cover_img is not None:
        if pick.get("is_nsfw"):
            # Smaller radius than the main banner; mini-card is ~100x140.
            cover_img = cover_img.filter(ImageFilter.GaussianBlur(radius=8))
        canvas.paste(cover_img, (cover_box[0], cover_box[1]))
    else:
        label = "NSFW" if pick.get("is_nsfw") else "—"
        bg = banner_gen.NSFW_BG if pick.get("is_nsfw") else banner_gen.PLACEHOLDER_BG
        banner_gen._draw_cover_placeholder(draw, cover_box, label, bg=bg)

    # Title text — wrap to up to 2 lines, ellipsize the second line if needed.
    text_x = x + cover_w + cover_gap
    text_w = inner_w - cover_w - cover_gap
    title = pick.get("title") or "—"
    ink = banner_gen.INK_TERTIARY if dim else banner_gen.INK_PRIMARY

    lines = _wrap_lines(draw, title, font_title, text_w, max_lines=2)
    line_h = 19 * S
    block_h = line_h * len(lines)
    text_y = y + (card_h - block_h) // 2
    for line in lines:
        draw.text((text_x, text_y), line, fill=ink, font=font_title)
        text_y += line_h


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    """Greedy word-wrap into at most `max_lines` lines, ellipsizing the last
    line on overflow. Falls back to char-level wrap when a single token is
    wider than the box (long URLs / CJK runs without spaces)."""
    if not text:
        return [""]
    if draw.textlength(text, font=font) <= max_width:
        return [text]

    words = text.split(" ")
    lines: list[str] = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip() if cur else w
        if draw.textlength(candidate, font=font) <= max_width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            # If a single word is wider than the line, char-wrap it.
            if draw.textlength(w, font=font) > max_width:
                buf = ""
                for ch in w:
                    if draw.textlength(buf + ch, font=font) <= max_width:
                        buf += ch
                    else:
                        if buf:
                            lines.append(buf)
                        buf = ch
                cur = buf
            else:
                cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)

    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # If the original text didn't fit and we hit the line cap, ellipsize the
    # last line.
    consumed = " ".join(lines)
    if consumed != text:
        last = lines[-1]
        ellipsis = "…"
        while last and draw.textlength(last + ellipsis, font=font) > max_width:
            last = last[:-1]
        lines[-1] = (last + ellipsis) if last else ellipsis
    return lines
