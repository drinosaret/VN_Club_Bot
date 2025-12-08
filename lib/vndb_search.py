"""Utility client for VNDB's JSON API."""

import aiohttp
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class VNDBClient:
    """
    Async client for interacting with the VNDB API
    Documentation: https://vndb.org/d11
    """

    def __init__(self, client_name: str = "VNClubBot", client_version: str = "1.0"):
        self.base_url = "https://api.vndb.org/kana"
        self.client_name = client_name
        self.client_version = client_version
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Async context manager entry"""
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": f"{self.client_name}/{self.client_version}",
                "Content-Type": "application/json"
            },
            timeout=aiohttp.ClientTimeout(total=30)
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()

    async def _make_request(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Make a request to the VNDB API"""
        if not self.session:
            raise RuntimeError("VNDBClient must be used as an async context manager")

        url = f"{self.base_url}/{endpoint}"

        try:
            async with self.session.post(url, json=data) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"VNDB API error: {response.status} - {await response.text()}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status
                    )
        except aiohttp.ClientError as e:
            logger.error(f"Network error when contacting VNDB: {e}")
            raise

    async def search_vns(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search VNDB for visual novels with richer metadata for autocomplete."""
        query = (query or "").strip()
        if not query:
            return []

        limit = max(1, min(limit, 25))

        payload = {
            "filters": ["search", "=", query],
            "fields": (
                "id,title,released,rating,"
                "image.url,image.sexual,"
                "titles.title,titles.lang,titles.latin"
            ),
            "sort": "searchrank",
            "results": limit
        }

        try:
            response = await self._make_request("vn", payload)
            return response.get("results", [])
        except Exception as exc:
            logger.error("Error searching VNDB for '%s': %s", query, exc)
            return []


def normalize_vn_titles(vn: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extract primary, English, Japanese, and Romaji titles from VNDB payload."""
    titles = {
        "primary": vn.get("title"),
        "en": None,
        "ja": None,
        "romaji": None,
    }

    for entry in (vn.get("titles") or []):
        lang = entry.get("lang")
        main_title = entry.get("title")
        latin = entry.get("latin")

        if lang == "en" and main_title:
            titles["en"] = main_title
        elif lang == "ja" and main_title:
            titles["ja"] = main_title
            if latin:
                titles["romaji"] = latin
        elif lang in ("x-jat", "romaji") and (latin or main_title):
            if not titles["romaji"]:
                titles["romaji"] = latin or main_title

    if not titles["primary"]:
        titles["primary"] = titles["en"] or titles["ja"] or titles["romaji"]

    return titles


async def search_visual_novel(query: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Search VNDB for visual novels and normalize the response."""
    try:
        client = VNDBClient()
        async with client as api:
            raw_results = await api.search_vns(query, limit=limit)

        results = []
        for vn in raw_results:
            title_data = normalize_vn_titles(vn)
            image = (vn.get("image") or {})

            # Prioritize Japanese title for display
            display_title = title_data.get("ja") or title_data.get("primary") or vn.get("title")

            results.append({
                "id": vn.get("id"),
                "titles": title_data,
                "display": display_title,
                "image": image.get("url"),
                "rating": vn.get("rating"),
                "released": vn.get("released"),
            })

        return results

    except Exception as exc:
        logger.error("Error searching visual novels: %s", exc)
        return []


def create_autocomplete_value(item_id: Union[str, int], field: str, source: Optional[str] = None) -> str:
    """Create autocomplete value string with optional media source metadata."""
    id_part = str(item_id)
    if source:
        id_part = f"{source}|{id_part}"
    return f"${{{id_part}:{field}}}"


def parse_autocomplete_value(value: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """Parse autocomplete value into (item_id, field, source)."""
    if not value or not value.startswith("${") or not value.endswith("}"):
        return None

    try:
        content = value[2:-1]
        id_and_field = content.rsplit(':', 1)
        if len(id_and_field) != 2:
            return None

        id_part, field = id_and_field
        source: Optional[str] = None
        item_id = id_part

        if '|' in id_part:
            source, item_id = id_part.split('|', 1)

        return item_id, field, source

    except Exception as exc:
        logger.error("Error parsing autocomplete value: %s", exc)
        return None