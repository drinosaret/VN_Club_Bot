"""
1200x480 profile card image generator.

Visual style mirrors the monthly banner (light cream palette, Noto Sans JP,
purple accent, rounded panels). Layout:

    +------------------------------------------------------------+
    | [avatar]    ▍ VN CLUB  •  PROFILE                          |
    |             Display Name                                   |
    | [server     @username · joined YYYY-MM-DD                  |
    |  panel]                                                    |
    |             ┌──────── stats grid (2x2) ─────────────────┐  |
    |             │ TOTAL POINTS    │ VN COMPLETIONS          │  |
    |             │ 1,234           │ 42                      │  |
    |             │ MONTHLY VNS     │ AVG RATING              │  |
    |             │ 8               │ 4.2 / 5  (12 ratings)   │  |
    |             └─────────────────────────────────────────────┘
    |             ┌──────── 6-month activity ───────────────────┐
    |             │ ▁▂▅▇▃▁  (bar chart with month labels)       │
    +------------------------------------------------------------+

Reuses the font cascade + compact-count formatter from ``lib.pillow_helpers``
so the profile / badges / club-stats / banner renderers share one palette
and font cascade. Class-level draw helpers (rounded rect, truncate-to-width)
are kept as local copies to keep the class self-contained.
"""

import asyncio
import io
import logging
import time
from typing import List, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from lib.pillow_helpers import (
    fetch_image_bytes_capped,
    load_japanese_font as _load_japanese_font,
    format_compact_count as _format_compact_count,
)

logger = logging.getLogger(__name__)


def _format_month_short(yyyy_mm: str) -> str:
    """'2026-04' -> 'Apr'."""
    try:
        from datetime import datetime
        return datetime.strptime(yyyy_mm, "%Y-%m").strftime("%b")
    except Exception:
        return yyyy_mm


class ProfileCardGenerator:
    """Render a 1200x480 user profile card."""

    SCALE = 2

    BANNER_WIDTH = 1200 * SCALE
    # Right column rhythm:
    #   header (eyebrow + name + subtitle)        ~  170*S
    #   stats grid                                  ~  120*S
    #   gap                                            12*S
    #   STANDING panel (rank + opt voting + badges) ~  108*S
    #   gap                                            12*S
    #   activity callout                              100*S
    #     ├─ eyebrow band                             24*S
    #     ├─ count-label band                         24*S  (clear gap from
    #     │                                                  eyebrow, 6*S gap
    #     │                                                  from bar top)
    #     ├─ bar area                                 30*S
    #     └─ month-label band                         22*S
    #   bottom hairline margin                       ~30*S
    BANNER_HEIGHT = 604 * SCALE

    AVATAR_SIZE = 240 * SCALE
    AVATAR_X = 60 * SCALE
    AVATAR_Y = 50 * SCALE

    # "Most active server" badge fills the rest of the cover column under the avatar.
    SERVER_PANEL_X = 40 * SCALE
    SERVER_PANEL_Y = 320 * SCALE
    SERVER_PANEL_W = 280 * SCALE
    SERVER_PANEL_H = 120 * SCALE

    # Reading-streak widget: months in a row with at least one log, ending at
    # the current calendar month (or last month, for grace early in the month).
    # Sits directly below MOST ACTIVE IN so the sidebar's bottom edge tracks
    # the right column (which extends past the avatar+server-panel stack).
    STREAK_PANEL_X = SERVER_PANEL_X
    STREAK_PANEL_Y = SERVER_PANEL_Y + SERVER_PANEL_H + 12 * SCALE
    STREAK_PANEL_W = SERVER_PANEL_W
    STREAK_PANEL_H = 96 * SCALE

    TEXT_X = 360 * SCALE
    TEXT_RIGHT = 1160 * SCALE

    # STANDING-panel inner column widths (offsets from the rank values_x).
    # Column boundaries are fixed so server/global rank numbers vertically
    # align across the two rank rows regardless of period-label text width —
    # the eye should be able to compare seasonal vs. all-time at a glance.
    RANK_PERIOD_W = 130 * SCALE
    RANK_SERVER_W = 170 * SCALE

    BG = (251, 248, 241)
    INK_PRIMARY = (28, 27, 42)
    INK_SECONDARY = (88, 84, 110)
    INK_TERTIARY = (146, 140, 160)
    HAIRLINE = (216, 210, 196)
    PANEL_BG = (243, 238, 224)
    CALLOUT_BG = (247, 242, 230)
    ACCENT = (88, 70, 150)
    BAR_FILL = (146, 122, 200)
    PLACEHOLDER_BG = (232, 226, 212)

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "ProfileCardGenerator":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    # ------------------------------------------------------------------
    # avatar fetching
    # ------------------------------------------------------------------
    async def _fetch_avatar(self, url: str) -> Optional[Image.Image]:
        if not self.session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Hikarubot/1.0 (+vnclub.org)"},
            )
        # Capped fetch: Content-Type=image/*, body size cap, plus the
        # module-level MAX_IMAGE_PIXELS in pillow_helpers handles the
        # decoded-pixel bomb case. Discord avatar URLs are
        # user-influenced (avatar override APIs), so even though Discord
        # itself caps avatar uploads, a maliciously-crafted URL going
        # through the bot's fetch path needs defending.
        data = await fetch_image_bytes_capped(self.session, url)
        if data is None:
            return None

        # See _fetch_cover in monthly_banner.py: context manager closes
        # the source Image after the RGB convert so we don't leak fds.
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                return img.convert("RGB")
        except Exception as e:
            logger.warning("avatar decode failed for %s: %s", url, e)
            return None

    def _fit_square(self, img: Image.Image, size: int) -> Image.Image:
        """Center-crop ``img`` to a square then resize to ``size``."""
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        return img.crop((left, top, left + side, top + side)).resize(
            (size, size), Image.Resampling.LANCZOS
        )

    # ------------------------------------------------------------------
    # primitives — copied from monthly_banner so this module is self-contained.
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

    def _paste_aa_rounded(self, img: Image.Image, box, radius: int,
                          fill=None, outline=None, outline_w: int = 1,
                          oversample: int = 4):
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

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def generate(
        self,
        username: str,
        display_name: str,
        avatar_url: Optional[str],
        total_points: int,
        vn_entries: int,
        monthly_entries: int,
        average_rating: Optional[float],
        rating_count: int,
        most_active_server: Optional[str],
        most_active_count: int,
        recent_activity: List[Tuple[str, int]],
        member_since: Optional[str] = None,
        last_log: Optional[str] = None,
        streak_months: int = 0,
        badge_summary: Optional[Tuple[int, int, List[str]]] = None,
        voting_stats: Optional[dict] = None,
        ranks: Optional[dict] = None,
    ) -> io.BytesIO:
        # Pre-fetch the avatar (the only async work) and run the heavy
        # synchronous PIL render off the event loop so /profile doesn't stall
        # other commands while compositing.
        t0 = time.perf_counter()
        prefetched_avatar: Optional[Image.Image] = None
        if avatar_url:
            prefetched_avatar = await self._fetch_avatar(avatar_url)
        try:
            buf = await asyncio.to_thread(
                self._render_sync,
                username=username, display_name=display_name,
                prefetched_avatar=prefetched_avatar,
                total_points=total_points, vn_entries=vn_entries,
                monthly_entries=monthly_entries,
                average_rating=average_rating, rating_count=rating_count,
                most_active_server=most_active_server,
                most_active_count=most_active_count,
                recent_activity=recent_activity, member_since=member_since,
                last_log=last_log, streak_months=streak_months,
                badge_summary=badge_summary, voting_stats=voting_stats,
                ranks=ranks,
            )
        except Exception:
            logger.exception(
                "profile_card render failed: user=%r avatar=%s",
                username, bool(prefetched_avatar),
            )
            raise
        logger.info(
            "profile_card rendered: user=%r avatar=%s duration_ms=%d",
            username, bool(prefetched_avatar),
            int((time.perf_counter() - t0) * 1000),
        )
        return buf

    def _render_sync(
        self,
        username: str,
        display_name: str,
        prefetched_avatar: Optional[Image.Image],
        total_points: int,
        vn_entries: int,
        monthly_entries: int,
        average_rating: Optional[float],
        rating_count: int,
        most_active_server: Optional[str],
        most_active_count: int,
        recent_activity: List[Tuple[str, int]],
        member_since: Optional[str] = None,
        last_log: Optional[str] = None,
        streak_months: int = 0,
        badge_summary: Optional[Tuple[int, int, List[str]]] = None,
        voting_stats: Optional[dict] = None,
        ranks: Optional[dict] = None,
    ) -> io.BytesIO:
        """
        ``badge_summary`` is ``(unlocked_count, total_count, latest_names)`` —
        rendered as a small strip between the stats grid and the activity
        callout. Passing ``None`` collapses the strip back to the legacy
        layout (no badges row).
        """
        S = self.SCALE
        img = Image.new("RGB", (self.BANNER_WIDTH, self.BANNER_HEIGHT), self.BG)
        draw = ImageDraw.Draw(img)

        # outer hairline
        self._paste_aa_rounded(
            img,
            (16 * S, 16 * S, self.BANNER_WIDTH - 16 * S, self.BANNER_HEIGHT - 16 * S),
            radius=18 * S,
            outline=self.HAIRLINE,
            outline_w=1 * S,
        )

        # ---- avatar (circular) ----
        avatar_box = (
            self.AVATAR_X,
            self.AVATAR_Y,
            self.AVATAR_X + self.AVATAR_SIZE,
            self.AVATAR_Y + self.AVATAR_SIZE,
        )
        if prefetched_avatar is not None:
            raw = prefetched_avatar
            if raw is not None:
                fitted = self._fit_square(raw, self.AVATAR_SIZE)
                # circular crop, anti-aliased via oversample + LANCZOS
                oversample = 4
                ow = self.AVATAR_SIZE * oversample
                big_mask = Image.new("L", (ow, ow), 0)
                ImageDraw.Draw(big_mask).ellipse([(0, 0), (ow - 1, ow - 1)], fill=255)
                mask = big_mask.resize(
                    (self.AVATAR_SIZE, self.AVATAR_SIZE), Image.Resampling.LANCZOS
                )
                img.paste(fitted, (self.AVATAR_X, self.AVATAR_Y), mask)
            else:
                self._draw_avatar_placeholder(draw, avatar_box, "No avatar")
        else:
            self._draw_avatar_placeholder(draw, avatar_box, "No avatar")

        # circular outline around avatar
        self._paste_aa_rounded(
            img, avatar_box, radius=self.AVATAR_SIZE // 2,
            outline=self.HAIRLINE, outline_w=1 * S,
        )

        # ---- "most active server" panel under avatar ----
        if most_active_server:
            self._draw_server_panel(
                draw, img, most_active_server, most_active_count
            )

        # ---- reading-streak widget (sits below MOST ACTIVE IN) ----
        # streak_months is precomputed by the caller from the user's *full*
        # log history, not the 12-cap recent_activity, so a long-time member
        # with a 30-month streak isn't artificially clipped at 12.
        self._draw_streak_panel(draw, streak_months)

        # ---- fonts ----
        font_eyebrow = _load_japanese_font(20 * S, bold=True)
        font_attr = _load_japanese_font(11 * S)
        font_name = _load_japanese_font(40 * S, bold=True)
        font_subtitle = _load_japanese_font(16 * S)
        font_stat_label = _load_japanese_font(13 * S, bold=True)
        font_stat_value = _load_japanese_font(22 * S, bold=True)
        font_chart_label = _load_japanese_font(11 * S, bold=True)
        font_chart_count = _load_japanese_font(11 * S)
        font_callout_eyebrow = _load_japanese_font(13 * S, bold=True)

        # ---- header eyebrow ----
        bar_x = self.TEXT_X
        bar_y = self.AVATAR_Y
        bar_h = 22 * S
        draw.rectangle(
            [bar_x, bar_y, bar_x + 6 * S, bar_y + bar_h], fill=self.ACCENT
        )
        eyebrow = "VN CLUB  •  PROFILE"
        bar_center_y = bar_y + bar_h // 2
        draw.text((bar_x + 16 * S, bar_center_y), eyebrow,
                  fill=self.ACCENT, font=font_eyebrow, anchor="lm")
        # right-side attribution
        eb_ascent, eb_descent = font_eyebrow.getmetrics()
        eyebrow_baseline = bar_center_y + (eb_ascent - eb_descent) // 2
        draw.text((self.TEXT_RIGHT, eyebrow_baseline),
                  "Reading stats", fill=self.INK_TERTIARY,
                  font=font_attr, anchor="rs")

        # ---- name ----
        name_y = bar_y + 38 * S
        max_name_w = self.TEXT_RIGHT - self.TEXT_X
        title_drawn = self._truncate_to_width(draw, display_name, font_name, max_name_w)
        name_ascent, name_descent = font_name.getmetrics()
        name_baseline = name_y + name_ascent
        draw.text((self.TEXT_X, name_baseline), title_drawn,
                  fill=self.INK_PRIMARY, font=font_name, anchor="ls")

        # subtitle: @username · joined YYYY-MM-DD · last log YYYY-MM-DD
        # Place it below the name's full glyph extent (ascent + descent) plus
        # a fixed visual gap, so display names with descenders (g/j/p/q/y)
        # don't graze the subtitle text. Tertiary ink so the name + stats
        # grid remain the visual anchors.
        subtitle_parts = [f"@{username}"]
        if member_since:
            subtitle_parts.append(f"joined {member_since}")
        if last_log:
            subtitle_parts.append(f"last log {last_log}")
        subtitle = "  ·  ".join(subtitle_parts)
        subtitle_y = name_baseline + name_descent + 14 * S
        sub = self._truncate_to_width(draw, subtitle, font_subtitle, max_name_w)
        draw.text((self.TEXT_X, subtitle_y), sub,
                  fill=self.INK_TERTIARY, font=font_subtitle)

        # ---- stats grid (2 col x 2 row) ----
        # Account for the subtitle's own glyph height before the next gap, so
        # the panel starts a consistent visual distance below the subtitle
        # regardless of how tall the subtitle font is.
        sub_ascent, sub_descent = font_subtitle.getmetrics()
        stats_top = subtitle_y + sub_ascent + sub_descent + 16 * S
        row_h = 56 * S
        pad_y = 16 * S
        stats_panel_h = pad_y * 2 + row_h * 2 - 8 * S  # tight bottom because last row has no descenders
        panel = (self.TEXT_X, stats_top, self.TEXT_RIGHT, stats_top + stats_panel_h)
        self._draw_rounded_rect(draw, panel, radius=12 * S,
                                fill=self.PANEL_BG, outline=None)

        col_gap = 24 * S
        pad_x = 24 * S
        col_w = (panel[2] - panel[0] - pad_x * 2 - col_gap) // 2
        col1_x = panel[0] + pad_x
        col2_x = col1_x + col_w + col_gap

        if average_rating is not None and rating_count > 0:
            rating_value = f"{average_rating:.1f} / 5  ({rating_count})"
        else:
            rating_value = "—"

        left_rows = [
            ("TOTAL POINTS", f"{total_points:,}"),
            ("MONTHLY VNS", f"{monthly_entries:,}"),
        ]
        right_rows = [
            ("VN COMPLETIONS", f"{vn_entries:,}"),
            ("AVG RATING", rating_value),
        ]
        self._draw_stat_column(draw, col1_x, panel[1] + pad_y, row_h,
                               left_rows, font_stat_label, font_stat_value)
        self._draw_stat_column(draw, col2_x, panel[1] + pad_y, row_h,
                               right_rows, font_stat_label, font_stat_value)

        # ---- STANDING panel (rank + voting + badges in one container) ----
        # Wrapping these three rows in a shared rounded panel gives a coherent
        # "achievements / standing" block instead of three free-floating
        # one-liners. Each row is opt-in (None / zero-state collapses).
        standing_top = stats_top + stats_panel_h + 12 * S
        standing_h = self._draw_standing_panel(
            draw, standing_top, ranks, voting_stats, badge_summary,
        )

        # ---- 12-month activity callout ----
        callout_top = stats_top + stats_panel_h + 12 * S + (
            standing_h + 12 * S if standing_h > 0 else 0
        )
        # 100*S gives the activity chart enough headroom for: eyebrow band
        # (24*S), count-label band above bars (24*S — clear gap from eyebrow
        # AND from bar tops), bar area (30*S), and month-label band (22*S).
        callout_h = 100 * S
        callout = (self.TEXT_X, callout_top, self.TEXT_RIGHT, callout_top + callout_h)
        self._draw_rounded_rect(draw, callout, radius=10 * S,
                                fill=self.CALLOUT_BG, outline=None)

        bar_w = 8 * S
        draw.rectangle(
            [callout[0], callout[1], callout[0] + bar_w, callout[3]],
            fill=self.ACCENT,
        )

        # callout label (top-left, just inside the accent)
        draw.text(
            (callout[0] + bar_w + 16 * S, callout[1] + 10 * S),
            "RECENT ACTIVITY  ·  LAST 12 MONTHS",
            fill=self.ACCENT, font=font_callout_eyebrow,
        )

        self._draw_activity_chart(
            draw, callout, recent_activity,
            font_chart_label, font_chart_count,
        )

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _draw_avatar_placeholder(self, draw, box, label):
        S = self.SCALE
        # Filled disc as the placeholder background
        cx = (box[0] + box[2]) // 2
        cy = (box[1] + box[3]) // 2
        r = (box[2] - box[0]) // 2
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=self.PLACEHOLDER_BG)
        font = _load_japanese_font(18 * S)
        tw = draw.textlength(label, font=font)
        draw.text((cx - tw / 2, cy - 9 * S), label,
                  fill=self.INK_SECONDARY, font=font)

    def _draw_server_panel(self, draw, img, server_name: str, count: int):
        """Render the sidebar 'MOST ACTIVE IN' panel under the avatar.

        Visual language matches the activity callout (left accent stripe,
        same panel fill) so the sidebar feels like part of the same card
        rather than an isolated widget.
        """
        S = self.SCALE
        box = (
            self.SERVER_PANEL_X,
            self.SERVER_PANEL_Y,
            self.SERVER_PANEL_X + self.SERVER_PANEL_W,
            self.SERVER_PANEL_Y + self.SERVER_PANEL_H,
        )
        self._draw_rounded_rect(draw, box, radius=10 * S,
                                fill=self.PANEL_BG, outline=None)
        # Left-edge accent stripe — mirrors the activity callout for visual
        # consistency between the two panels.
        bar_w = 6 * S
        draw.rectangle(
            [box[0], box[1], box[0] + bar_w, box[3]], fill=self.ACCENT,
        )
        font_label = _load_japanese_font(11 * S, bold=True)
        font_value = _load_japanese_font(16 * S, bold=True)
        font_count = _load_japanese_font(12 * S)
        text_x = box[0] + bar_w + 12 * S
        draw.text(
            (text_x, box[1] + 14 * S),
            "MOST ACTIVE IN",
            fill=self.ACCENT, font=font_label,
        )
        truncated = self._truncate_to_width(
            draw, server_name, font_value,
            self.SERVER_PANEL_W - (bar_w + 12 * S) - 16 * S,
        )
        draw.text(
            (text_x, box[1] + 36 * S),
            truncated, fill=self.INK_PRIMARY, font=font_value,
        )
        # INK_SECONDARY (not TERTIARY) so the count line reads as supporting
        # info rather than fading into the background.
        draw.text(
            (text_x, box[1] + 80 * S),
            f"{count:,} completion(s)",
            fill=self.INK_SECONDARY, font=font_count,
        )

    def _draw_streak_panel(self, draw, streak_months: int):
        """Render the sidebar streak widget under MOST ACTIVE IN. Mirrors the
        server panel's accent stripe + cream fill so the two read as a
        coherent sidebar stack rather than two unrelated tiles."""
        S = self.SCALE
        box = (
            self.STREAK_PANEL_X,
            self.STREAK_PANEL_Y,
            self.STREAK_PANEL_X + self.STREAK_PANEL_W,
            self.STREAK_PANEL_Y + self.STREAK_PANEL_H,
        )
        self._draw_rounded_rect(draw, box, radius=10 * S,
                                fill=self.PANEL_BG, outline=None)
        bar_w = 6 * S
        draw.rectangle(
            [box[0], box[1], box[0] + bar_w, box[3]], fill=self.ACCENT,
        )
        font_label = _load_japanese_font(11 * S, bold=True)
        font_value = _load_japanese_font(28 * S, bold=True)
        font_unit = _load_japanese_font(12 * S)
        text_x = box[0] + bar_w + 12 * S
        draw.text(
            (text_x, box[1] + 14 * S),
            "READING STREAK",
            fill=self.ACCENT, font=font_label,
        )
        if streak_months > 0:
            value = f"{streak_months}"
            unit = "month" if streak_months == 1 else "months in a row"
            value_color = self.INK_PRIMARY
        else:
            value = "—"
            unit = "no active streak"
            value_color = self.INK_TERTIARY
        draw.text(
            (text_x, box[1] + 34 * S),
            value, fill=value_color, font=font_value,
        )
        draw.text(
            (text_x, box[1] + 70 * S),
            unit, fill=self.INK_SECONDARY, font=font_unit,
        )

    def _draw_stat_column(self, draw, x: int, y: int, row_h: int,
                          rows: list, font_label, font_value):
        S = self.SCALE
        for label, value in rows:
            draw.text((x, y), label, fill=self.INK_SECONDARY, font=font_label)
            draw.text((x, y + 18 * S), value, fill=self.INK_PRIMARY, font=font_value)
            y += row_h

    def _draw_rank_block(
        self, draw, top_y: int, ranks: dict,
        *, label_x: Optional[int] = None, values_x: Optional[int] = None,
    ) -> int:
        """Render the two-line RANK row. Returns the height consumed (px).

        Layout is tabular: PERIOD column, SERVER column, GLOBAL column at
        fixed x-offsets so rank numbers stack vertically across both rows.
        Hierarchy comes from weight (bold rank numbers, light denominators)
        instead of color desaturation on the whole second row.

        Optional ``label_x``/``values_x`` let the STANDING panel position
        this block within its inner padded area (defaults to TEXT_X for
        backwards-compat with any caller still using free-float layout).
        """
        S = self.SCALE
        font_label = _load_japanese_font(11 * S, bold=True)
        font_period = _load_japanese_font(13 * S, bold=True)
        font_prefix = _load_japanese_font(13 * S)
        font_num = _load_japanese_font(13 * S, bold=True)
        font_dim = _load_japanese_font(13 * S)

        label_x = label_x if label_x is not None else self.TEXT_X
        values_x = values_x if values_x is not None else label_x + 64 * S

        period_x = values_x
        server_x = values_x + self.RANK_PERIOD_W
        global_x = values_x + self.RANK_PERIOD_W + self.RANK_SERVER_W

        # Eyebrow label — vertically centered with row 1 content (the
        # period name, 13*S). Per-row centering keeps the label visually
        # balanced with its neighbour regardless of how tall the content
        # is, instead of "stuck to the ceiling" of the section.
        ascent_period, descent_period = font_period.getmetrics()
        label_center_y = top_y + (ascent_period + descent_period) // 2
        draw.text((label_x, label_center_y), "RANK",
                  fill=self.ACCENT, font=font_label, anchor="lm")

        # Row 1: current season
        season_label = ranks.get("season_label") or "Current Season"
        cs = ranks.get("current_season") or {}
        self._draw_rank_row(
            draw, top_y, season_label, cs,
            period_x=period_x, server_x=server_x, global_x=global_x,
            font_period=font_period, font_prefix=font_prefix,
            font_num=font_num, font_dim=font_dim,
        )

        # Row 2: all-time
        at = ranks.get("all_time") or {}
        self._draw_rank_row(
            draw, top_y + 18 * S, "All-time", at,
            period_x=period_x, server_x=server_x, global_x=global_x,
            font_period=font_period, font_prefix=font_prefix,
            font_num=font_num, font_dim=font_dim,
        )

        return 18 * S * 2 + 4 * S  # two lines + small gap

    def _draw_rank_row(
        self, draw, y: int, period_label: str, axes: dict,
        *, period_x: int, server_x: int, global_x: int,
        font_period, font_prefix, font_num, font_dim,
    ) -> None:
        """Render one rank row across PERIOD / SERVER / GLOBAL column slots.

        Uses ``anchor="ls"`` (baseline-aligned) so segments share a common
        baseline regardless of whether their text contains ascenders.
        ``anchor="lt"`` proved fragile here: words without ascenders ("server")
        had a shorter bbox, so when both their bbox tops were anchored to ``y``
        the visible text sat *above* their bold ascender-bearing neighbours
        ("#2"), making the row look misaligned.

        DM context (no ``server`` key in ``axes``): the SERVER slot is left
        empty whitespace — no em-dash placeholder — so PERIOD and GLOBAL
        still align cleanly across both rows.
        """
        ascent, _ = font_period.getmetrics()
        baseline_y = y + ascent

        draw.text((period_x, baseline_y), period_label,
                  fill=self.INK_PRIMARY, font=font_period, anchor="ls")

        server = axes.get("server")
        if server is not None:
            r, t = server
            self._draw_rank_value_cell(
                draw, server_x, baseline_y, "server", r, t,
                font_prefix, font_num, font_dim,
            )
        # If "server" key is absent (DM) or value is None, leave the slot
        # empty rather than printing "server —".

        glob = axes.get("global")
        if glob is not None:
            r, t = glob
            self._draw_rank_value_cell(
                draw, global_x, baseline_y, "global", r, t,
                font_prefix, font_num, font_dim,
            )

    def _draw_rank_value_cell(
        self, draw, x: int, baseline_y: int,
        prefix: str, rank: int, total: int,
        font_prefix, font_num, font_dim,
    ) -> None:
        """Render `<prefix> #<rank> / <total>` as three weighted segments,
        all sharing the supplied baseline (``anchor="ls"``).

        prefix → INK_SECONDARY regular (column identifier, recedes)
        #<rank> → INK_PRIMARY bold (the eye-catching number)
        / <total> → INK_TERTIARY regular (denominator, contextual only)
        """
        cursor = x
        prefix_text = f"{prefix} "
        draw.text((cursor, baseline_y), prefix_text,
                  fill=self.INK_SECONDARY, font=font_prefix, anchor="ls")
        cursor += draw.textlength(prefix_text, font=font_prefix)

        num_text = f"#{rank}"
        draw.text((cursor, baseline_y), num_text,
                  fill=self.INK_PRIMARY, font=font_num, anchor="ls")
        cursor += draw.textlength(num_text, font=font_num)

        denom_text = f" / {total}"
        draw.text((cursor, baseline_y), denom_text,
                  fill=self.INK_TERTIARY, font=font_dim, anchor="ls")

    def _draw_voting_block(
        self, draw, top_y: int, voting_stats: dict,
        *, label_x: Optional[int] = None, values_x: Optional[int] = None,
    ) -> int:
        """Render the single-line VOTING row. Returns height consumed.

        Returns 0 (no draw) when every counter is zero — keeps fresh users'
        cards from showing a row of pure zeros. Optional ``label_x``/
        ``values_x`` let the STANDING panel place this block inside its
        padded inner area instead of using the card-wide TEXT_X.
        """
        S = self.SCALE
        voted = voting_stats.get("votes_cast", 0)
        nominated = voting_stats.get("nominations_made", 0)
        taste = voting_stats.get("tastemaker_wins", 0)
        if voted == 0 and nominated == 0 and taste == 0:
            return 0

        font_label = _load_japanese_font(11 * S, bold=True)
        font_value = _load_japanese_font(13 * S)

        label_x = label_x if label_x is not None else self.TEXT_X
        values_x = values_x if values_x is not None else label_x + 64 * S
        draw.text((label_x, top_y), "VOTING",
                  fill=self.ACCENT, font=font_label, anchor="lt")
        line = f"{voted} voted  ·  {nominated} nominated  ·  {taste} tastemaker"
        draw.text((values_x, top_y), line,
                  fill=self.INK_PRIMARY, font=font_value, anchor="lt")
        return 18 * S

    def _draw_standing_panel(
        self,
        draw,
        top_y: int,
        ranks: Optional[dict],
        voting_stats: Optional[dict],
        badge_summary: Optional[Tuple[int, int, List[str]]],
    ) -> int:
        """Render the STANDING container with rank + (optional) voting +
        (optional) badges rows inside one rounded cream panel. Returns
        total height consumed (including the panel's own padding); returns
        0 when nothing to show so the caller can collapse the slot.
        """
        S = self.SCALE
        # Decide which rows render so we can size the panel exactly.
        has_rank = ranks is not None
        will_render_voting = voting_stats is not None and any(
            voting_stats.get(k, 0) for k in
            ("votes_cast", "nominations_made", "tastemaker_wins")
        )
        has_badges = badge_summary is not None
        if not (has_rank or will_render_voting or has_badges):
            return 0

        # Reserve the voting row's vertical space whenever the panel has
        # both a rank row and a badges row, even when the user has cast
        # no votes. Without this, profiles with voting stats render a
        # taller standing panel than profiles without, which floats the
        # activity callout down by ~26*S and breaks the column-bottom
        # alignment against the (absolutely-positioned) left sidebar.
        # The slot stays blank when ``will_render_voting`` is False —
        # the blank gap is intentional whitespace.
        reserve_voting_slot = has_rank and has_badges

        # Per-row heights (must match the helper renderers).
        # badges_h matches rank_h so both sections take the same vertical
        # space — RANK has period-row + all-time-row, BADGES has count-row +
        # latest-row. Earlier versions used 28*S for a single-line badges
        # layout, but mixing 1-row badges with 2-row rank inside one panel
        # made the structure feel uneven.
        rank_h = 18 * S * 2 + 4 * S if has_rank else 0
        voting_h = 18 * S if (will_render_voting or reserve_voting_slot) else 0
        badges_h = 18 * S * 2 + 4 * S if has_badges else 0
        gap = 8 * S
        # Asymmetric gap before BADGES: the row 1 count uses an 18*S bold
        # font, which has heavier visual weight than the 13*S RANK/VOTING
        # rows above. A heavier element reads as needing less actual
        # whitespace before it to feel balanced — the visual rhythm
        # smooths out when this gap is tighter than the standard one.
        gap_before_badges = 4 * S

        rows_h = rank_h + voting_h + badges_h
        # Inter-row gaps: standard `gap` between rank and voting; tighter
        # `gap_before_badges` before the badges row whenever a prior row
        # exists.
        if rank_h > 0 and voting_h > 0:
            rows_h += gap
        if (rank_h > 0 or voting_h > 0) and badges_h > 0:
            rows_h += gap_before_badges

        # Internal padding. pad_top is tighter than pad_bottom on purpose:
        # the badges row 2 has descenders ('y', 'g' in latest names) that
        # eat into perceived clearance, so a slightly larger pad_bottom
        # gives the bottom edge visible breathing room without inflating
        # the top side. The 12*S separation between *sections* lives in
        # the inter-block gaps (stats→standing→callout), not here.
        pad_top = 10 * S
        pad_bottom = 14 * S
        pad_x = 18 * S
        panel_h = pad_top + rows_h + pad_bottom

        # Outer container.
        panel = (self.TEXT_X, top_y, self.TEXT_RIGHT, top_y + panel_h)
        self._draw_rounded_rect(draw, panel, radius=12 * S,
                                fill=self.PANEL_BG, outline=None)

        inner_x = panel[0] + pad_x
        # Match label/values column to the rest of the bot's "LABEL  value"
        # rows: label is small-cap accent, values column starts after a fixed
        # 64*S offset so multiple rows align nicely.
        values_x = inner_x + 64 * S

        cur_y = top_y + pad_top

        # voting_slot_present captures both "we're drawing voting content"
        # and "we're reserving empty space for layout balance". Either way
        # the slot consumes vertical room and contributes a gap to neighbours.
        voting_slot_present = will_render_voting or reserve_voting_slot

        if has_rank:
            self._draw_rank_block(
                draw, cur_y, ranks,
                label_x=inner_x, values_x=values_x,
            )
            cur_y += rank_h
            if voting_slot_present:
                cur_y += gap
            elif has_badges:
                cur_y += gap_before_badges
        if voting_slot_present:
            if will_render_voting:
                self._draw_voting_block(
                    draw, cur_y, voting_stats,
                    label_x=inner_x, values_x=values_x,
                )
            cur_y += voting_h
            if has_badges:
                cur_y += gap_before_badges
        if has_badges:
            self._draw_badges_row(
                draw, cur_y, badge_summary,
                label_x=inner_x, values_x=values_x,
                right_edge=panel[2] - pad_x,
            )

        return panel_h

    def _draw_badges_row(
        self,
        draw,
        top_y: int,
        badge_summary: Tuple[int, int, List[str]],
        *,
        label_x: int,
        values_x: int,
        right_edge: int,
    ) -> None:
        """Render the BADGES row inside the STANDING panel. Two stacked
        rows mirror RANK's structure: label + count on row 1, latest names
        on row 2 underneath.

        Both rows use baseline anchoring (``anchor="lt"`` for the eyebrow
        label and ``anchor="ls"`` for content) — the same scheme as
        ``_draw_rank_row`` — so rank and badges sections sit on a shared
        typographic grid.
        """
        S = self.SCALE
        unlocked_count, total_count, latest_names = badge_summary
        font_label = _load_japanese_font(11 * S, bold=True)
        font_count = _load_japanese_font(18 * S, bold=True)
        font_latest = _load_japanese_font(13 * S)

        # Row 1 — count is baseline-aligned so its 18*S glyph top sits at
        # top_y; label is vertically centered with the count rather than
        # anchored to its top, so it visually balances against the much
        # larger number instead of floating above it. (RANK label uses
        # the same per-row centering against its 13*S period text.)
        ascent_count, descent_count = font_count.getmetrics()
        row1_baseline = top_y + ascent_count
        label_center_y = top_y + (ascent_count + descent_count) // 2

        draw.text((label_x, label_center_y), "BADGES",
                  fill=self.ACCENT, font=font_label, anchor="lm")
        count_text = f"{unlocked_count}/{total_count}"
        draw.text((values_x, row1_baseline), count_text,
                  fill=self.INK_PRIMARY, font=font_count, anchor="ls")

        # Row 2 — latest names. Pushed below the count's full glyph height
        # (ascent + descent) plus a 4*S inter-row gap so the two rows feel
        # like a paired stack rather than crammed together.
        ascent_latest, _ = font_latest.getmetrics()
        row2_top = top_y + ascent_count + descent_count + 4 * S
        row2_baseline = row2_top + ascent_latest

        latest_max_w = right_edge - values_x
        if latest_names:
            latest_text = " · ".join(latest_names[:3])
        else:
            latest_text = "No badges yet — log a VN with /finish"
        latest_text = self._truncate_to_width(
            draw, latest_text, font_latest, latest_max_w
        )
        draw.text((values_x, row2_baseline), latest_text,
                  fill=self.INK_SECONDARY, font=font_latest, anchor="ls")

    def _draw_activity_chart(self, draw, callout, recent_activity,
                             font_label, font_count):
        """Render a tiny bar chart of the last 12 months in the callout panel.

        recent_activity is sorted DESC (newest first); we reverse for display.
        """
        S = self.SCALE
        if not recent_activity:
            font = _load_japanese_font(16 * S)
            msg = "No recent activity"
            tw = draw.textlength(msg, font=font)
            cx = (callout[0] + callout[2]) // 2
            cy = (callout[1] + callout[3]) // 2
            draw.text((cx - tw / 2, cy - 9 * S), msg,
                      fill=self.INK_SECONDARY, font=font)
            return

        # Show up to 12 months chronologically (oldest -> newest left-to-right).
        # 12 reads as "annual context" while still tolerating sparse data
        # for new users with one or two months logged.
        months = list(reversed(recent_activity[:12]))
        max_count = max(c for _, c in months) or 1

        # Carve out room inside the callout, top-down:
        #   eyebrow band       —  24*S  ("RECENT ACTIVITY ..." drawn above)
        #   count-label band   —  24*S  ("N" annotation per bar — the tallest
        #                                 bar's count label uses this entire
        #                                 band; combined with the 6*S gap to
        #                                 the bar top this leaves visible
        #                                 breathing room above and below the
        #                                 number, no eyebrow/bar collisions)
        #   bar area           —  30*S
        #   month-label band   —  22*S  ("Aug / Sep / ..." under bars)
        # Total: 100*S (matches callout_h). chart_y0 is the *top of the bar
        # area*; count labels are drawn with anchor="mb" at y = y0 - 6*S so
        # they always sit cleanly above each bar.
        chart_x0 = callout[0] + 24 * S
        chart_x1 = callout[2] - 24 * S
        chart_y0 = callout[1] + 48 * S  # eyebrow(24) + count-label band(24)
        chart_y1 = callout[3] - 22 * S
        chart_w = chart_x1 - chart_x0
        chart_h = chart_y1 - chart_y0

        slot_w = chart_w / len(months)
        bar_w = min(36 * S, int(slot_w * 0.6))

        for i, (month, count) in enumerate(months):
            slot_cx = chart_x0 + slot_w * i + slot_w / 2
            bar_h = int(chart_h * (count / max_count))
            if count == 0:
                bar_h = 2 * S  # tiny visible nub for zero-months
            x0 = int(slot_cx - bar_w / 2)
            x1 = int(slot_cx + bar_w / 2)
            y1 = int(chart_y1)
            y0 = y1 - bar_h
            draw.rectangle([x0, y0, x1, y1], fill=self.BAR_FILL)

            # Count above bar — anchored "mb" (middle-bottom) so the gap
            # between text bottom and bar top is exact and font-metric
            # independent. 6*S = 12 visual pixels of breathing room between
            # the count and the bar tops, regardless of bar height.
            count_text = _format_compact_count(count)
            draw.text(
                (slot_cx, y0 - 6 * S),
                count_text, fill=self.INK_PRIMARY, font=font_count, anchor="mb",
            )

            # Month label below — same anchored pattern (top-center).
            label = _format_month_short(month)
            draw.text(
                (slot_cx, chart_y1 + 6 * S),
                label, fill=self.INK_TERTIARY, font=font_label, anchor="mt",
            )
