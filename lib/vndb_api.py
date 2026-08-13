from __future__ import annotations

from .bot import VNClubBot
from .desciption_processing import replace_bbcode, to_plain_text
from .utils import smart_truncate
import aiohttp
import asyncio
import logging
import time
from typing import Optional, Tuple
from dataclasses import dataclass


API_URL = "https://api.vndb.org/kana/vn"

_log = logging.getLogger(__name__)

# VNDB image.sexual is 0-2; >=0.7 catches the "suggestive" band. Lowering
# this requires wiping vndb_cache (see
# lib/migrations._invalidate_vndb_cache_for_blur_threshold).
COVER_BLUR_THRESHOLD = 0.7

# The club reads in Japanese, so a VN originally written in another language
# is not a candidate however well it fits a theme. VNDB's `olang` is the
# original language, unlike `lang`, which also counts translations into it.
CLUB_ORIGINAL_LANGUAGE = "ja"

# Process-local TTL cache for VNDB extras.
# Banners re-render on /season_overview, /monthly, /seasonal, and on vote
# closes — without caching, every render fires a fresh VNDB POST per VN.
# Multi-server amplifies this: one guild's hot path could exhaust the
# shared VNDB budget for all guilds. Tags/rating/votecount don't change
# minute-to-minute, so a 1-hour TTL is plenty.
_VNDB_EXTRAS_TTL_SECONDS = 3600
_VNDB_EXTRAS_CACHE_CAP = 512
_vndb_extras_cache: dict[str, tuple[float, dict]] = {}
_vndb_extras_locks: dict[str, asyncio.Lock] = {}

_theme_attrs_cache: dict[str, tuple[float, dict]] = {}
_theme_attrs_locks: dict[str, asyncio.Lock] = {}

# (vndb_id, tag_id, max_spoiler, min_level, include_children) -> (at, matched)
_tag_match_cache: dict[tuple[str, str, int, float, bool], tuple[float, bool]] = {}

# Tag probes are the only path here that fans out per call, and VNDB meters
# per IP for the whole container. Holding them to a few in flight keeps a
# themed batch from spending the budget that autocomplete and the banners
# share. Created lazily: a module-level Semaphore would bind the import-time
# event loop.
_TAG_MATCH_IN_FLIGHT = 4
_tag_match_gate: Optional[asyncio.Semaphore] = None


def _tag_match_limiter() -> asyncio.Semaphore:
    global _tag_match_gate
    if _tag_match_gate is None:
        _tag_match_gate = asyncio.Semaphore(_TAG_MATCH_IN_FLIGHT)
    return _tag_match_gate

CREATE_VNDB_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS vndb_cache (
    vndb_id TEXT PRIMARY KEY,
    title_en TEXT,
    title_ja TEXT,
    thumbnail_url TEXT,
    thumbnail_is_nsfw BOOLEAN DEFAULT FALSE,
    length_minutes INTEGER,
    length_rating TEXT,
    description TEXT,
    character_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

ADD_VNDB_ENTRY_QUERY = """
INSERT INTO vndb_cache (vndb_id, title_en, title_ja, thumbnail_url, thumbnail_is_nsfw, length_minutes, length_rating, description)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

GET_VNDB_ENTRY_QUERY = """
SELECT vndb_id, title_en, title_ja, thumbnail_url, thumbnail_is_nsfw, length_minutes, length_rating, description
FROM vndb_cache WHERE vndb_id = ?;
"""

# Used by /finish + /club_stats lazy backfill: store the jiten character count
# alongside the VNDB metadata so club-wide chars/sums are a pure SQL aggregate
# instead of N jiten round-trips per query.
SET_VNDB_CHARACTER_COUNT_QUERY = """
UPDATE vndb_cache SET character_count = ? WHERE vndb_id = ?;
"""


@dataclass
class VN_Entry:
    vndb_id: str
    title_en: str
    title_ja: str
    thumbnail_url: str
    thumbnail_is_nsfw: bool = False
    length_minutes: Optional[int] = None
    length_rating: Optional[str] = None
    description: str = ""

    def __repr__(self):
        return (
            f"VN_Entry(vndb_id={self.vndb_id}, title_en={self.title_en}, "
            f"title_ja={self.title_ja}, thumbnail_url={self.thumbnail_url}, "
            f"thumbnail_is_nsfw={self.thumbnail_is_nsfw})"
        )

    @classmethod
    async def save_to_db(cls, bot: VNClubBot, entry: "VN_Entry"):
        """Save VN details to the database."""
        _log.info(f"Saving VNDB entry to DB: {entry}")
        await bot.RUN(
            ADD_VNDB_ENTRY_QUERY,
            (
                entry.vndb_id,
                entry.title_en,
                entry.title_ja,
                entry.thumbnail_url,
                entry.thumbnail_is_nsfw,
                entry.length_minutes,
                entry.length_rating,
                entry.description,
            ),
        )

    @classmethod
    async def _get_from_db(cls, bot: VNClubBot, vndb_id: str) -> Optional[Tuple]:
        """Fetch VN details from the database."""
        result = await bot.GET_ONE(GET_VNDB_ENTRY_QUERY, (vndb_id,))
        if result:
            _log.info(f"Fetched VNDB entry from DB for {vndb_id}")
            return result
        _log.info(f"VNDB entry not found in DB for {vndb_id}.")
        return None

    @classmethod
    async def set_cached_character_count(
        cls, bot: VNClubBot, vndb_id: str, character_count: int
    ) -> None:
        """Persist a jiten character_count onto the vndb_cache row.

        Called from /finish after the JitenClient lookup so club-wide totals
        in /club_stats stay cheap (pure SQL SUM) without re-fetching jiten on
        each dashboard render. No-op if the row doesn't exist yet — the cache
        is populated on first /finish anyway via save_to_db.
        """
        if not vndb_id or character_count is None or character_count < 0:
            return
        try:
            await bot.RUN(
                SET_VNDB_CHARACTER_COUNT_QUERY,
                (int(character_count), vndb_id),
            )
        except Exception as e:  # noqa: BLE001
            _log.debug("set_cached_character_count failed for %s: %s", vndb_id, e)

    async def get_points_not_monthly(self) -> int:
        if self.length_minutes:
            reading_hours = round(self.length_minutes / 600) * 10
            points = (reading_hours // 10) + 1
            return points
        elif self.length_rating:
            return int(self.length_rating)
        return 1

    async def get_vndb_link(self) -> str:
        return f"https://vndb.org/{self.vndb_id}"

    async def get_normalized_description(
        self, max_length: Optional[int] = 1000, *, plain: bool = False,
    ) -> str:
        """Get a normalized description for the VN.

        ``plain`` renders markup-free text for the image cards; embeds take
        the Discord-markdown form. ``max_length=None`` leaves truncation to
        the caller, which the banner needs because it cuts by pixel width.
        """
        if not self.description:
            return "No description available."
        desc = self.description.strip()
        if not desc:
            return "No description available."
        desc = to_plain_text(desc) if plain else replace_bbcode(desc)
        if not desc:
            return "No description available."
        return smart_truncate(desc, max_length)

    @staticmethod
    async def _fetch_from_vndb(
        vndb_id: str,
    ) -> Optional[Tuple[str, str, str, str, bool, Optional[int], Optional[str], str]]:
        if not vndb_id.startswith("v"):
            vndb_id = f"v{vndb_id}"

        payload = {
            "filters": ["id", "=", vndb_id],
            "fields": "title, image.url, image.sexual, titles.title, titles.official, titles.lang, length, length_minutes, description",
        }

        # Two attempts. VNDB occasionally drops connections (single-host CDN,
        # ~10s tail-latency on cold paths) and a fresh /finish for a new VN
        # has no cache to fall back on — losing the user's submission to a
        # one-off blip is brutal. The retry uses a slightly longer total
        # timeout so we don't double-burn the same fast deadline.
        data = None
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                timeout = aiohttp.ClientTimeout(
                    total=10 if attempt == 1 else 20,
                    connect=5 if attempt == 1 else 10,
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(API_URL, json=payload) as resp:
                        if resp.status != 200:
                            _log.error("VNDB API error %s for ID %s (attempt %d)", resp.status, vndb_id, attempt)
                            # HTTP-level errors aren't likely to fix themselves
                            # on retry (auth, 4xx, server 5xx that's stuck).
                            # Bail rather than burn another round-trip.
                            return None
                        data = await resp.json()
                        break
            except Exception as e:
                last_exc = e
                if attempt == 1:
                    _log.warning("VNDB fetch attempt 1 failed for %s: %s. Retrying.", vndb_id, e)
                    continue
                _log.exception("Error fetching VNDB for ID %s after retry", vndb_id)
                return None
        if data is None:
            # Defensive — shouldn't reach here, but if both attempts somehow
            # exited the loop without populating data, surface the cause.
            if last_exc is not None:
                _log.error("VNDB fetch exhausted retries for %s: %s", vndb_id, last_exc)
            return None

        results = data.get("results", [])
        if not results:
            return None

        vn = results[0]
        vid = vn.get("id", "")

        # Extract titles
        titles = vn.get("titles", [])
        en_title = next(
            (t["title"] for t in titles if t.get("lang") == "en" and t.get("official")),
            vn.get("title", ""),
        )
        ja_title = next(
            (t["title"] for t in titles if t.get("lang") == "ja" and t.get("official")),
            "",
        )

        image = vn.get("image") or {}
        thumbnail_url = image.get("url", "")
        # `image.sexual` can be None for unrated covers; coerce to 0.
        sexual_score = image.get("sexual") or 0
        thumbnail_is_nsfw = sexual_score >= COVER_BLUR_THRESHOLD

        length_minutes = vn.get("length_minutes")
        length_rating = vn.get("length")
        description = vn.get("description", "")

        return (
            vid,
            en_title,
            ja_title,
            thumbnail_url,
            thumbnail_is_nsfw,
            length_minutes,
            length_rating,
            description,
        )


async def from_vndb_id(bot: VNClubBot, vndb_id: str) -> Optional[VN_Entry]:
    """Fetch or create VN_Entry from DB or VNDB API."""
    if not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"
    vn_info = await VN_Entry._get_from_db(bot, vndb_id)
    if not vn_info:
        vn_info = await VN_Entry._fetch_from_vndb(vndb_id)
        if not vn_info:
            _log.error(f"Failed to fetch VNDB info for ID {vndb_id}.")
            return None
        entry = VN_Entry(*vn_info)
        await VN_Entry.save_to_db(bot, entry)
        return entry
    return VN_Entry(*vn_info)


# Display labels for VNDB's `platforms` short codes. Common platforms
# get their human label; obscure retro codes (FM Towns, PC-88, etc.)
# fall back to the upper-cased raw code so the banner never blanks
# unexpectedly. Add entries here when a new code shows up in the wild.
_PLATFORM_LABELS: dict[str, str] = {
    "win": "Windows",
    "lin": "Linux",
    "mac": "macOS",
    "ios": "iOS",
    "and": "Android",
    "web": "Web",
    "dvd": "DVD",
    "bdp": "Blu-ray",
    "swi": "Switch",
    "ps1": "PS1",
    "ps2": "PS2",
    "ps3": "PS3",
    "ps4": "PS4",
    "ps5": "PS5",
    "psp": "PSP",
    "psv": "Vita",
    "drc": "Dreamcast",
    "nds": "DS",
    "3ds": "3DS",
    "n3d": "3DS",
    "wii": "Wii",
    "wiu": "Wii U",
    "xb1": "Xbox One",
    "xbo": "Xbox One",
    "xbs": "Xbox Series",
    "xbx": "Xbox",
    "x36": "Xbox 360",
    "pc8": "PC-88",
    "pc9": "PC-98",
    "p88": "PC-88",
    "p98": "PC-98",
    "fmt": "FM Towns",
    "x68": "X68000",
    "msx": "MSX",
    "mob": "Mobile",
    "smd": "Mega Drive",
    "scd": "Sega CD",
    "sat": "Saturn",
    "sfc": "Super Famicom",
    "nes": "NES",
    "gba": "GBA",
    "gbc": "GBC",
    "vnd": "VNDS",
    "oth": "Other",
}


def _format_platforms(codes: Optional[list[str]], max_shown: int = 3) -> Optional[str]:
    """Format VNDB platform codes into a compact display string.

    Examples:
      ['win', 'swi']               → 'Windows · Switch'
      ['win', 'swi', 'ps4', 'psv'] → 'Windows · Switch · PS4 · +1'
    Returns None for empty/missing input so the caller can render '—'.
    Windows is sorted first when present (it's the de-facto baseline
    for JP VNs); the rest stay in VNDB's natural order.
    """
    if not codes:
        return None
    seen: list[str] = []
    if "win" in codes:
        seen.append("win")
    for c in codes:
        if c != "win" and c not in seen:
            seen.append(c)
    labels = [_PLATFORM_LABELS.get(c) or c.upper() for c in seen]
    if len(labels) <= max_shown:
        return " · ".join(labels)
    head = " · ".join(labels[:max_shown])
    return f"{head} · +{len(labels) - max_shown}"


async def fetch_vndb_extras(
    vndb_id: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> dict:
    """
    Fetch the VNDB fields not held in `vndb_cache`: rating, votecount, release
    year, tags, platforms, developer. Used by the monthly banner so it can
    show VNDB-derived data even when jiten.moe has no entry for the VN.

    Cached process-locally for ``_VNDB_EXTRAS_TTL_SECONDS`` to keep banner
    re-renders and multi-server hot paths from re-hitting VNDB for the
    same VN. Pass ``session`` to reuse an existing aiohttp session
    (avoids a fresh TCP+TLS handshake per call).

    Returns a dict (possibly empty on failure) with optional keys:
        rating: float | None
        votecount: int | None
        release_year: str | None  (e.g. "2010")
        top_tags: list[str]       — content (`cont`) tags preferred; falls
                                    back to technical (`tech`) tags when
                                    no content tags exist
        platforms: str | None     — formatted display string, e.g.
                                    "Windows · Switch · PS4"
        developer: str | None     — first developer's `original` if non-empty,
                                    else `name` (e.g. "ねこねこソフト", "Key")
        tag_count: int | None     — total tag count from VNDB (unfiltered),
                                    independent of the top_tags pill list
    """
    if not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"

    now = time.monotonic()
    cached = _vndb_extras_cache.get(vndb_id)
    if cached is not None and (now - cached[0]) < _VNDB_EXTRAS_TTL_SECONDS:
        _log.debug(
            "vndb_extras: cache hit key=%s age_s=%.1f", vndb_id, now - cached[0],
        )
        return cached[1]
    _log.debug("vndb_extras: cache miss key=%s, fetching", vndb_id)

    # Per-VN lock so concurrent renders (e.g., two guilds' /monthly at the
    # same time on the same featured VN) coalesce into a single VNDB call.
    lock = _vndb_extras_locks.get(vndb_id)
    if lock is None:
        lock = asyncio.Lock()
        _vndb_extras_locks[vndb_id] = lock

    async with lock:
        # Recheck after acquiring — another coroutine may have populated it.
        cached = _vndb_extras_cache.get(vndb_id)
        if cached is not None and (time.monotonic() - cached[0]) < _VNDB_EXTRAS_TTL_SECONDS:
            _log.debug(
                "vndb_extras: cache hit key=%s (post-lock, coalesced)", vndb_id,
            )
            return cached[1]

        t_fetch = time.monotonic()

        payload = {
            "filters": ["id", "=", vndb_id],
            "fields": (
                "rating, votecount, released, "
                "tags.name, tags.category, tags.spoiler, tags.rating, "
                "platforms, developers.name, developers.original"
            ),
        }

        async def _do_request(s: aiohttp.ClientSession) -> Optional[dict]:
            async with s.post(API_URL, json=payload) as resp:
                if resp.status != 200:
                    _log.warning(f"VNDB extras {resp.status} for {vndb_id}")
                    return None
                return await resp.json()

        try:
            if session is not None:
                data = await _do_request(session)
            else:
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as own_session:
                    data = await _do_request(own_session)
        except Exception as e:
            _log.warning(f"VNDB extras fetch failed for {vndb_id}: {e}")
            return {}

        if data is None:
            return {}

    results = data.get("results") or []
    if not results:
        return {}
    vn = results[0]

    # Tag pills: prefer content-category tags; fall back to technical
    # ones when the VN has no content coverage (common for niche /
    # doujin entries — the screenshot case 妖華子譚 had zero content
    # tags). Both are sorted by rating and capped at 5.
    raw_tags = vn.get("tags") or []
    def _by_category(cat: str) -> list[str]:
        filtered = [
            t for t in raw_tags
            if t.get("category") == cat
            and (t.get("spoiler") or 0) == 0
            and t.get("name")
        ]
        filtered.sort(key=lambda t: t.get("rating") or 0, reverse=True)
        return [t["name"] for t in filtered[:5]]

    top_tags = _by_category("cont") or _by_category("tech")

    released = vn.get("released")
    release_year = None
    if isinstance(released, str) and len(released) >= 4 and released[:4].isdigit():
        release_year = released[:4]

    devs = vn.get("developers") or []
    developer = None
    if devs:
        first = devs[0] or {}
        developer = (first.get("original") or "").strip() or (first.get("name") or "").strip() or None

    tag_count = len(raw_tags) if raw_tags else None

    result = {
        "rating": vn.get("rating"),
        "votecount": vn.get("votecount"),
        "release_year": release_year,
        "top_tags": top_tags,
        "platforms": _format_platforms(vn.get("platforms")),
        "developer": developer,
        "tag_count": tag_count,
    }

    # Cache the result and evict the oldest entry if we've grown past the cap.
    # Soft cap — we just drop the single oldest key, which is good enough for
    # a process that handles dozens of distinct VNs per day, not thousands.
    _vndb_extras_cache[vndb_id] = (time.monotonic(), result)
    if len(_vndb_extras_cache) > _VNDB_EXTRAS_CACHE_CAP:
        oldest_id = min(_vndb_extras_cache, key=lambda k: _vndb_extras_cache[k][0])
        _vndb_extras_cache.pop(oldest_id, None)
        _vndb_extras_locks.pop(oldest_id, None)
    _log.debug(
        "vndb_extras: fetched key=%s in %.0fms", vndb_id,
        (time.monotonic() - t_fetch) * 1000,
    )
    return result


async def vn_matches_tag(
    vndb_id: str,
    tag_id: str,
    max_spoiler: int,
    min_level: float,
    include_children: bool = True,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[bool]:
    """Does this VN carry ``tag_id``, as VNDB itself would answer it?

    Asks VNDB rather than reading the VN's own tag list, because that list
    holds only directly-applied tags: a VN carrying a child tag does not list
    the parent it sits under, though a VNDB search for the broader
    tag returns it. The tag endpoint publishes no parent/child links (tags
    form a DAG), so the hierarchy can only be resolved server-side.

    ``include_children`` picks the filter: `tag` credits a VN for any
    descendant of the tag, `dtag` requires the tag itself. The difference is
    large enough to change a theme's reach several times over.

    Returns None when the answer is unavailable (network failure, non-200,
    or a filter VNDB rejects such as an unknown tag id) so the caller can
    fail closed instead of reading "no answer" as "no match".
    """
    if not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"
    # VNDB rejects out-of-range values outright, taking the whole request
    # with them; clamp to the documented domains.
    max_spoiler = max(0, min(2, int(max_spoiler)))
    min_level = max(0.0, min(3.0, float(min_level)))

    key = (vndb_id, tag_id, max_spoiler, min_level, include_children)
    now = time.monotonic()
    cached = _tag_match_cache.get(key)
    if cached is not None and (now - cached[0]) < _VNDB_EXTRAS_TTL_SECONDS:
        return cached[1]

    payload = {
        "filters": ["and", ["id", "=", vndb_id],
                    [("tag" if include_children else "dtag"), "=",
                     [tag_id, max_spoiler, min_level]]],
        "fields": "id",
        "results": 1,
    }

    async def _do(s: aiohttp.ClientSession) -> Optional[bool]:
        async with _tag_match_limiter():
            async with s.post(API_URL, json=payload) as resp:
                if resp.status != 200:
                    _log.warning("tag match %s for %s/%s", resp.status, vndb_id, tag_id)
                    return None
                data = await resp.json()
                return bool(data.get("results"))

    # Two attempts, like the metadata fetch: an unanswered probe fails the
    # nomination closed, so a dropped connection would read to the nominator
    # as their VN not carrying a tag it has.
    matched = None
    for attempt in (1, 2):
        try:
            if session is not None:
                matched = await _do(session)
            else:
                timeout = aiohttp.ClientTimeout(
                    total=10 if attempt == 1 else 20,
                    connect=5 if attempt == 1 else 10,
                )
                async with aiohttp.ClientSession(timeout=timeout) as own:
                    matched = await _do(own)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 1:
                _log.warning("tag match attempt 1 failed for %s/%s: %s. Retrying.",
                             vndb_id, tag_id, e)
                continue
            _log.warning("tag match failed for %s/%s: %s", vndb_id, tag_id, e)
            return None

    if matched is None:
        return None
    _tag_match_cache[key] = (time.monotonic(), matched)
    if len(_tag_match_cache) > _VNDB_EXTRAS_CACHE_CAP:
        oldest = min(_tag_match_cache, key=lambda k: _tag_match_cache[k][0])
        _tag_match_cache.pop(oldest, None)
    return matched


async def search_vns(
    filters: list,
    *,
    limit: int = 8,
    sort: str = "rating",
    exclude_nsfw_covers: bool = False,
    original_language: Optional[str] = CLUB_ORIGINAL_LANGUAGE,
    session: Optional[aiohttp.ClientSession] = None,
) -> tuple[Optional[list[dict]], Optional[int]]:
    """Run a VNDB filter and return ``(results, total_matches)``.

    Used to show real titles that satisfy a nomination theme. ``total_matches``
    is the full count behind the page. Returns ``(None, None)`` on any failure,
    so a caller can drop the examples and still show whatever else it was going
    to.

    ``original_language`` narrows to VNs written in that language, defaulting
    to the club's. Pass None only for a search that genuinely spans languages,
    since a count taken over every language includes titles this club does not
    read.

    ``exclude_nsfw_covers`` drops results whose cover is rated at or above
    COVER_BLUR_THRESHOLD. VNDB has no filter for that, so it is applied here
    over an over-fetched page.
    """
    if not filters:
        return None, None
    if original_language:
        filters = ["and", filters, ["olang", "=", original_language]]

    payload = {
        "filters": filters,
        "fields": "id, title, alttitle, rating, votecount, released, image.url, image.sexual",
        # Over-fetch so local NSFW removal can still fill the page. Same one
        # request either way, and a theme can match a lot of adult titles.
        "results": min(limit * 5, 100) if exclude_nsfw_covers else limit,
        "sort": sort,
        "reverse": True,
        "count": True,
    }

    async def _do(s: aiohttp.ClientSession) -> Optional[dict]:
        async with s.post(API_URL, json=payload) as resp:
            if resp.status != 200:
                # The body carries VNDB's reason for rejecting a filter, which
                # is the only useful signal here. Bounded and single-lined: the
                # log is read through the console, which renders it as text.
                detail = " ".join((await resp.text())[:200].split())
                _log.warning("vn search %s: %s", resp.status, detail)
                return None
            return await resp.json()

    data = None
    for attempt in (1, 2):
        try:
            if session is not None:
                data = await _do(session)
            else:
                timeout = aiohttp.ClientTimeout(
                    total=10 if attempt == 1 else 20,
                    connect=5 if attempt == 1 else 10,
                )
                async with aiohttp.ClientSession(timeout=timeout) as own:
                    data = await _do(own)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 1:
                _log.warning("vn search attempt 1 failed: %s. Retrying.", e)
                continue
            _log.warning("vn search failed: %s", e)
            return None, None

    if not data:
        return None, None
    results = data.get("results") or []
    if exclude_nsfw_covers:
        results = [
            vn for vn in results
            if ((vn.get("image") or {}).get("sexual") or 0) < COVER_BLUR_THRESHOLD
        ]
    return results[:limit], data.get("count")


async def fetch_theme_attributes(
    vndb_id: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[dict]:
    """Fetch the theme-gate attributes not held in vndb_cache: developer
    producer ids, the full release date, and the cover's sexual rating.

    Returns None on any failure (network, non-200, no results) so the caller
    fails the theme gate closed rather than silently passing a VN it could not
    verify. On success returns:
        {"developer_ids": ["p<id>", ...],
         "released": "YYYY-MM-DD" | "YYYY-MM" | "YYYY" | None,
         "cover_sexual": float | None}

    ``cover_sexual`` is None when the VN has no cover or the cover carries no
    sexuality votes, which the NSFW rule must treat as unknown rather than
    safe. Tags are not here: they need one filtered query per tag, see
    ``vn_matches_tag``.

    Cached process-locally like fetch_vndb_extras (1h TTL); pass ``session``
    to reuse a connection.
    """
    if not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"

    now = time.monotonic()
    cached = _theme_attrs_cache.get(vndb_id)
    if cached is not None and (now - cached[0]) < _VNDB_EXTRAS_TTL_SECONDS:
        return cached[1]

    lock = _theme_attrs_locks.get(vndb_id)
    if lock is None:
        lock = asyncio.Lock()
        _theme_attrs_locks[vndb_id] = lock

    async with lock:
        cached = _theme_attrs_cache.get(vndb_id)
        if cached is not None and (time.monotonic() - cached[0]) < _VNDB_EXTRAS_TTL_SECONDS:
            return cached[1]

        payload = {
            "filters": ["id", "=", vndb_id],
            "fields": "released, image.sexual, developers.id, developers.name",
        }

        async def _do(s: aiohttp.ClientSession) -> Optional[dict]:
            async with s.post(API_URL, json=payload) as resp:
                if resp.status != 200:
                    _log.warning("theme attrs %s for %s", resp.status, vndb_id)
                    return None
                return await resp.json()

        try:
            if session is not None:
                data = await _do(session)
            else:
                timeout = aiohttp.ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as own:
                    data = await _do(own)
        except Exception as e:  # noqa: BLE001
            _log.warning("theme attrs fetch failed for %s: %s", vndb_id, e)
            return None

    if not data:
        return None
    results = data.get("results") or []
    if not results:
        return None
    vn = results[0]

    developer_ids = [d.get("id") for d in (vn.get("developers") or []) if d.get("id")]
    image = vn.get("image") or {}
    cover_sexual = image.get("sexual")
    result = {
        "developer_ids": developer_ids,
        "released": vn.get("released"),
        "cover_sexual": float(cover_sexual) if cover_sexual is not None else None,
    }

    _theme_attrs_cache[vndb_id] = (time.monotonic(), result)
    if len(_theme_attrs_cache) > _VNDB_EXTRAS_CACHE_CAP:
        oldest = min(_theme_attrs_cache, key=lambda k: _theme_attrs_cache[k][0])
        _theme_attrs_cache.pop(oldest, None)
        _theme_attrs_locks.pop(oldest, None)
    # A lookup that never succeeds still left a lock behind, and only the
    # success path above evicts, so ids that always fail would accumulate.
    if len(_theme_attrs_locks) > _VNDB_EXTRAS_CACHE_CAP * 2:
        for stale in [k for k in _theme_attrs_locks
                      if k not in _theme_attrs_cache and not _theme_attrs_locks[k].locked()]:
            _theme_attrs_locks.pop(stale, None)
    return result
