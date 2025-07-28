import aiohttp
import logging
from typing import Tuple, Union

API_URL = "https://api.vndb.org/kana/vn"

_log = logging.getLogger(__name__)


async def get_vn_info(vndb_id: str) -> Union[Tuple[str, str, str, bool], bool]:
    """Gets VNDB ID, Title, Thumbnail URL, and NSFW status for a given VNDB ID."""
    if not vndb_id.startswith("v"):
        vndb_id = f"v{vndb_id}"

    payload = {
        "filters": ["id", "=", vndb_id],
        "fields": "title, image.url, image.sexual",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, json=payload, timeout=5) as resp:
                if resp.status != 200:
                    _log.error(
                        f"Failed to fetch VNDB info for ID {vndb_id}: {resp.status} {str(resp)}"
                    )
                    return False
                data = await resp.json()
    except Exception as e:
        _log.error(f"Error fetching VNDB info for ID {vndb_id}: {e}")
        return False

    data = await resp.json()
    results = data.get("results") or []
    if not results:
        return False

    vn = results[0]
    # Extract the normalized ID
    vid = vn.get("id", "")
    # Japanese title: use alttitle (original-script title) if present
    # English title: look for an explicit English entry in the 'titles' list
    title = vn.get("title", "")
    # Fallback to the main romanized title if no English-specific title was found
    # Thumbnail info
    thumbnail_url = ""
    thumbnail_is_nsfw = False
    image = vn.get("image") or {}
    thumbnail_url = image.get("url") or ""
    # 'sexual' is 0–2; treat >0 as NSFW
    thumbnail_is_nsfw = image.get("sexual", 0) > 0

    return vid, title, thumbnail_url, thumbnail_is_nsfw
