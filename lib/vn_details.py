"""What VNDB publishes about one VN, for /vndb and its full-info view.

The club reads in Japanese, so translation data (release languages,
translated titles, aliases) is not fetched or shown.

One request covers both the short card and the full view, and the result is
cached in process for an hour, so opening the full view after the card costs
no second request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from lib.vndb_api import API_URL, COVER_BLUR_THRESHOLD, _PLATFORM_LABELS

_log = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_CACHE_CAP = 256
_cache: dict[str, tuple[float, "VNDetails"]] = {}
_locks: dict[str, asyncio.Lock] = {}

FIELDS = (
    "title, alttitle, devstatus, released, platforms, "
    "length, length_minutes, length_votes, rating, votecount, average, "
    "description, image.url, image.sexual, developers.name, developers.original, "
    "tags.name, tags.category, tags.spoiler, tags.rating, relations.relation, "
    "relations.relation_official, relations.title, relations.alttitle, "
    "extlinks.label, extlinks.url"
)

DEVSTATUS_LABELS = {0: "Finished", 1: "In development", 2: "Cancelled"}
RELATION_LABELS = {
    "seq": "Sequel", "preq": "Prequel", "set": "Same setting",
    "alt": "Alternative version", "char": "Shares characters",
    "side": "Side story", "par": "Parent story", "ser": "Same series",
    "fan": "Fandisc", "orig": "Original game",
}
@dataclass
class VNDetails:
    vndb_id: str
    title: str
    alttitle: Optional[str] = None
    devstatus: Optional[int] = None
    released: Optional[str] = None
    platforms: list[str] = field(default_factory=list)
    length: Optional[int] = None
    length_minutes: Optional[int] = None
    length_votes: int = 0
    rating: Optional[float] = None
    votecount: int = 0
    average: Optional[float] = None
    description: str = ""
    image_url: Optional[str] = None
    image_is_nsfw: bool = False
    developers: list[str] = field(default_factory=list)
    tags: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    extlinks: list[dict] = field(default_factory=list)

    @property
    def display_title(self) -> str:
        """Original-script title first, matching the rest of the bot."""
        return self.alttitle or self.title or self.vndb_id

    @property
    def romanized_title(self) -> Optional[str]:
        return self.title if self.alttitle and self.title != self.alttitle else None

    # Attribute names resolve_display_cover reads.
    @property
    def thumbnail_url(self) -> Optional[str]:
        return self.image_url

    @property
    def thumbnail_is_nsfw(self) -> bool:
        return self.image_is_nsfw


def platform_label(code: str) -> str:
    return _PLATFORM_LABELS.get(code) or code.upper()


def release_year(released: Optional[str]) -> Optional[str]:
    head = (released or "").split("-", 1)[0]
    return head if len(head) == 4 and head.isdigit() else None


def parse_details(vn: dict) -> Optional[VNDetails]:
    vid = vn.get("id")
    if not isinstance(vid, str) or not vid.startswith("v"):
        return None
    image = vn.get("image") or {}
    devs = []
    for d in vn.get("developers") or []:
        name = (d.get("original") or "").strip() or (d.get("name") or "").strip()
        if name and name not in devs:
            devs.append(name)
    return VNDetails(
        vndb_id=vid,
        title=vn.get("title") or vid,
        alttitle=vn.get("alttitle") or None,
        devstatus=vn.get("devstatus"),
        released=vn.get("released") if isinstance(vn.get("released"), str) else None,
        platforms=list(vn.get("platforms") or []),
        length=vn.get("length"),
        length_minutes=vn.get("length_minutes"),
        length_votes=vn.get("length_votes") or 0,
        rating=vn.get("rating"),
        votecount=vn.get("votecount") or 0,
        average=vn.get("average"),
        description=vn.get("description") or "",
        image_url=image.get("url") or None,
        image_is_nsfw=(image.get("sexual") or 0) >= COVER_BLUR_THRESHOLD,
        developers=devs,
        tags=list(vn.get("tags") or []),
        relations=list(vn.get("relations") or []),
        extlinks=list(vn.get("extlinks") or []),
    )


def safe_tags(details: VNDetails, limit: int) -> list[str]:
    """Tag names without spoilers or sexual-content tags, strongest first.
    Both kinds are hidden by default on VNDB itself."""
    tags = [
        t for t in details.tags
        if t.get("name") and (t.get("spoiler") or 0) == 0 and t.get("category") != "ero"
    ]
    tags.sort(key=lambda t: t.get("rating") or 0, reverse=True)
    return [t["name"] for t in tags[:limit]]


async def fetch_vn_details(vndb_id: str) -> Optional[VNDetails]:
    """The VN's details, from cache or VNDB. None if VNDB has no such VN or
    cannot be reached."""
    if not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"
    cached = _cache.get(vndb_id)
    if cached and time.monotonic() - cached[0] < _TTL_SECONDS:
        return cached[1]
    lock = _locks.setdefault(vndb_id, asyncio.Lock())
    async with lock:
        cached = _cache.get(vndb_id)
        if cached and time.monotonic() - cached[0] < _TTL_SECONDS:
            return cached[1]
        payload = {"filters": ["id", "=", vndb_id], "fields": FIELDS}
        data = None
        for attempt in (1, 2):
            try:
                timeout = aiohttp.ClientTimeout(total=10 * attempt, connect=5 * attempt)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(API_URL, json=payload) as resp:
                        if resp.status != 200:
                            _log.warning("vn details %s for %s", resp.status, vndb_id)
                            return None
                        data = await resp.json()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    _log.warning("vn details fetch failed for %s: %s", vndb_id, e)
                    return None
        results = (data or {}).get("results") or []
        details = parse_details(results[0]) if results else None
        if details is None:
            return None
        _cache[vndb_id] = (time.monotonic(), details)
        if len(_cache) > _CACHE_CAP:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
            _locks.pop(oldest, None)
        return details
