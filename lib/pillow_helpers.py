"""
Shared Pillow primitives + palette for hikaru's image renderers.

Originally lived inside ``lib/monthly_banner.py``; lifted here so the new
renderers (``profile_card``, ``club_stats_card``, ``badges``) can reuse the
exact same font cascade, palette, and AA rounded-rectangle helper without
duplication. Renderers that already had inlined copies (e.g. profile_card's
``_paste_aa_rounded``) now wrap these.

Visual identity is one cream/purple palette across every banner — keep new
constants here so swapping a value updates the whole bot.
"""

import functools
import io
import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


# Image-bomb defenses. Pillow's default DecompressionBombWarning only
# warns; we want a hard reject. 25M pixels (~25 megapixels) is plenty
# for any VN cover or Discord avatar — typical sizes are <1M pixels.
# This is module-level so importing pillow_helpers anywhere in the
# bot inherits the cap; renderers don't need to set it themselves.
Image.MAX_IMAGE_PIXELS = 25_000_000

# Body-size cap for remote image fetches. 8MiB covers any realistic
# VN cover (typical ~200KB, generous max ~2MB on the VNDB CDN) and
# Discord avatar (max 8MiB by Discord's own size limit anyway). A
# response above this is either malicious or a 404 HTML page being
# served back inadvertently — both cases we want to drop.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


async def fetch_image_bytes_capped(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int = _MAX_IMAGE_BYTES,
) -> Optional[bytes]:
    """GET ``url`` and return the body bytes — but only if:
      * status is 200
      * Content-Type starts with image/
      * Content-Length (if present) is within the cap
      * The actual streamed body doesn't exceed the cap

    Returns None on any failure (status, type mismatch, oversize,
    network error). Caller is expected to feed the bytes into
    Image.open via a BytesIO wrapper — the global MAX_IMAGE_PIXELS
    cap above then handles a small-bytes-but-huge-decoded image
    (e.g. a heavily-compressed 30k×30k PNG).

    Logs warnings on rejections so an operator can spot a bad upstream
    without the bot just silently dropping fetches.
    """
    # SSRF guard: only follow http(s) URLs. Some upstreams (e.g. jiten's
    # cover_url) flow straight from a third-party API response into here,
    # so a poisoned/misconfigured response containing a ``file://`` or a
    # bare ``http://169.254.169.254/…`` cloud metadata URL would otherwise
    # let the bot probe its own host. Cheap to enforce, no false-positives.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.warning("image fetch rejected: unsafe URL %r", url)
        return None
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning("image fetch %s -> HTTP %d", url, resp.status)
                return None

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                # A 404 page served back as text/html, an XML error
                # body, etc. — would hand garbage to PIL and either
                # raise on decode or render as a broken image.
                logger.warning(
                    "image fetch %s rejected: non-image content-type %r",
                    url, content_type,
                )
                return None

            # Honor Content-Length when present — saves us streaming a
            # huge body we'd reject anyway.
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > max_bytes:
                        logger.warning(
                            "image fetch %s rejected: declared size %s > %d",
                            url, content_length, max_bytes,
                        )
                        return None
                except ValueError:
                    pass  # garbage header — fall through to streaming cap

            # Streaming cap. Accumulate chunks until EOF or until we
            # exceed the cap. NOTE: ``resp.content.read(n)`` with positive
            # ``n`` is NOT a stream-until-n-bytes call — it waits for the
            # first chunk to land, then returns whatever's currently
            # buffered (up to ``n``). For multi-segment bodies (any cover
            # bigger than ~one TCP MSS, so basically all of them) that
            # silently truncates to the first chunk. Use iter_chunked so
            # we actually drain the body.
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "image fetch %s rejected: streamed body exceeded %d bytes",
                        url, max_bytes,
                    )
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except Exception as e:  # noqa: BLE001
        logger.warning("image fetch %s failed: %s", url, e)
        return None


# ---------- Palette (cream/purple, light) ----------
# Module-level so renderers can `from lib.pillow_helpers import PALETTE` or
# pull individual entries. Existing classes keep their own duplicate
# constants for backward-compat; new renderers should import from here.
BG = (251, 248, 241)
INK_PRIMARY = (28, 27, 42)
INK_SECONDARY = (88, 84, 110)
INK_TERTIARY = (146, 140, 160)
HAIRLINE = (216, 210, 196)
PANEL_BG = (243, 238, 224)
CALLOUT_BG = (247, 242, 230)
ACCENT = (88, 70, 150)
ACCENT_INK = (255, 255, 255)
BAR_FILL = (146, 122, 200)       # used by profile-card chart, club-stats trend
NSFW_BG = (210, 196, 200)
PLACEHOLDER_BG = (232, 226, 212)


_BUNDLED_FONT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "fonts",
)


@functools.lru_cache(maxsize=64)
def load_japanese_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Locate a Noto Sans JP / CJK-capable TrueType font and load it at ``size``.

    Cached per ``(size, bold)`` so the discovery cascade runs once per
    (size, weight). Order:

    1. Bundled Noto Sans JP in ``assets/fonts/`` (cross-environment parity).
    2. Linux Noto CJK system paths.
    3. matplotlib font scan (Windows / macOS dev fallback).
    4. Windows Fonts directory.
    5. PIL bitmap fallback.
    """
    fonts_to_try: list[str] = []

    bundled = "NotoSansJP-Bold.ttf" if bold else "NotoSansJP-Regular.ttf"
    fonts_to_try.append(os.path.join(_BUNDLED_FONT_DIR, bundled))

    if bold:
        fonts_to_try.extend([
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        ])
    fonts_to_try.extend([
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ])

    try:
        import matplotlib.font_manager as fm
        bold_keywords = ("Bold", "Heavy", "Black", "ExtraBold")
        mpl_fonts: list[str] = []
        if bold:
            for font in fm.fontManager.ttflist:
                if font.name == "Noto Sans JP" and any(k in font.fname for k in bold_keywords):
                    mpl_fonts.append(font.fname)
                    break
        if not mpl_fonts:
            for font in fm.fontManager.ttflist:
                if font.name == "Noto Sans JP" and "Regular" in font.fname:
                    mpl_fonts.append(font.fname)
                    break
        if not mpl_fonts:
            skip = bold_keywords + ("Medium", "DemiLight", "Light", "Thin") if not bold else ()
            for font in fm.fontManager.ttflist:
                if skip and any(x in font.fname for x in skip):
                    continue
                if font.name == "Noto Sans JP":
                    mpl_fonts.append(font.fname)
                    break
        fonts_to_try.extend(mpl_fonts)
    except Exception as e:
        logger.debug("matplotlib font scan skipped: %s", e)

    if os.name == "nt":
        windows_fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        if bold:
            fonts_to_try.extend([
                os.path.join(windows_fonts_dir, "NotoSansJP-Bold.otf"),
                os.path.join(windows_fonts_dir, "NotoSansJP-Bold.ttf"),
                os.path.join(windows_fonts_dir, "msgothic.ttc"),
            ])
        fonts_to_try.extend([
            os.path.join(windows_fonts_dir, "NotoSansCJK-Regular.ttc"),
            os.path.join(windows_fonts_dir, "NotoSansJP-Regular.otf"),
            os.path.join(windows_fonts_dir, "NotoSansJP-Regular.ttf"),
            os.path.join(windows_fonts_dir, "msgothic.ttc"),
            os.path.join(windows_fonts_dir, "meiryo.ttc"),
            os.path.join(windows_fonts_dir, "yugothic.ttf"),
        ])

    fonts_to_try.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    ])

    for path in fonts_to_try:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue

    logger.warning("No TrueType font found for size %d; using PIL default", size)
    return ImageFont.load_default()


def format_compact_count(n: Optional[int]) -> str:
    """1234 -> '1.2K', 12345 -> '12.3K', 1500000 -> '1.5M'. ``None`` -> '—'."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        v = n / 1_000
        return f"{v:.1f}K" if v < 10 else f"{v:.0f}K"
    return f"{n}"


def paste_aa_rounded(img: Image.Image, box, radius: int,
                     fill=None, outline=None, outline_w: int = 1,
                     oversample: int = 4):
    """Draw a rounded rectangle at OVERSAMPLE× resolution and downsample with
    LANCZOS for anti-aliased corners. Pillow's primitives don't AA at
    integer pixels, so this is the standard workaround.
    """
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


def truncate_to_width(draw: ImageDraw.ImageDraw, text: str,
                      font: ImageFont.ImageFont, max_width: int) -> str:
    """Binary-search the longest prefix of ``text`` that fits in ``max_width``,
    appending an ellipsis when truncation occurs."""
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
