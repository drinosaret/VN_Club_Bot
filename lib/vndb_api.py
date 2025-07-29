from __future__ import annotations

from .bot import VNClubBot
from .desciption_processing import replace_bbcode
import aiohttp
import logging
from typing import Optional, Tuple
from dataclasses import dataclass


API_URL = "https://api.vndb.org/kana/vn"

_log = logging.getLogger(__name__)

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

    async def get_points_not_monthly(self) -> int:
        if self.length_minutes:
            points = self.length_minutes // 600
            if points < 1:
                points = 1
            return points
        elif self.length_rating:
            return self.length_rating
        return 1

    async def get_vndb_link(self) -> str:
        return f"https://vndb.org/{self.vndb_id}"

    async def get_normalized_description(self, max_length=1000) -> str:
        """Get a normalized description for the VN."""
        desc = self.description.strip()
        if not desc:
            return "No description available."
        desc = replace_bbcode(desc)

        if len(desc) > max_length:
            desc = desc[:max_length].rsplit(" ", 1)[0] + "..."
        return desc

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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, json=payload, timeout=5) as resp:
                    if resp.status != 200:
                        _log.error(f"VNDB API error {resp.status} for ID {vndb_id}")
                        return None
                    data = await resp.json()
        except Exception as e:
            _log.error(f"Error fetching VNDB for ID {vndb_id}: {e}")
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
        thumbnail_is_nsfw = image.get("sexual", 0) > 0

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
