# lib/theme_service.py
"""Gather a VN's theme attributes from the cheap cached fields plus, only
when the rules require it, one VNDB fetch and/or a jiten character-count
lookup. Keeps cogs/vn_cycle.py thin and lib/themes.py pure."""
from __future__ import annotations

import logging
from typing import Optional

from .jiten_client import JitenClient
from .themes import ThemeAttributes, rules_need_extras
from .vndb_api import VN_Entry, fetch_theme_attributes

_log = logging.getLogger(__name__)


class _CharCountUnavailable(Exception):
    """A transient jiten lookup failure (network/outage), distinct from jiten
    simply having no entry for the VN. Lets the gate show a "try again" message
    instead of a theme rejection, matching the VNDB-extras failure path."""


async def _get_character_count(bot, vndb_id: str) -> Optional[int]:
    """Cached vndb_cache.character_count, else a one-off jiten lookup (and
    backfill the cache). Returns None when jiten genuinely has no count for the
    VN; raises _CharCountUnavailable on a transient lookup failure."""
    row = await bot.GET_ONE(
        "SELECT character_count FROM vndb_cache WHERE vndb_id = ?", (vndb_id,)
    )
    if row and row[0] is not None:
        return int(row[0])
    try:
        async with JitenClient() as jiten:
            info = await jiten.get_by_vndb_id(vndb_id)
    except Exception as e:  # noqa: BLE001
        _log.warning("theme char-count lookup failed (transient) for %s: %s", vndb_id, e)
        raise _CharCountUnavailable(str(e)) from e
    if info and info.character_count:
        await VN_Entry.set_cached_character_count(bot, vndb_id, info.character_count)
        return int(info.character_count)
    return None  # jiten reachable but has no count for this VN


async def gather_theme_attributes(
    bot, vndb_id: str, vn_entry: VN_Entry, rules: dict,
) -> Optional[ThemeAttributes]:
    """Build ThemeAttributes for evaluating ``rules`` against this VN.

    Returns None when a required attribute source is transiently unreachable (a
    VNDB extras fetch failure, or a jiten char-count outage), so the caller fails
    the gate closed with a "try again" message (source down != VN passes). A VN
    that genuinely has no tags/developers/count yields empty collections / None
    fields, which the evaluator treats as "does not satisfy the rule".
    """
    length_rating = None
    if vn_entry.length_rating not in (None, ""):
        try:
            length_rating = int(vn_entry.length_rating)
        except (TypeError, ValueError):
            length_rating = None
    nsfw_cover = bool(vn_entry.thumbnail_is_nsfw)

    character_count = None
    if rules.get("character_count"):
        try:
            character_count = await _get_character_count(bot, vndb_id)
        except _CharCountUnavailable:
            return None  # transient jiten outage -> gate shows "try again"

    tags: dict[str, tuple[float, int]] = {}
    developer_ids = frozenset()
    released = None
    if rules_need_extras(rules):
        extras = await fetch_theme_attributes(vndb_id)
        if extras is None:
            return None  # fetch failed -> fail closed
        tags = {t["id"]: (t["rating"], t["spoiler"]) for t in extras.get("tags", []) if t.get("id")}
        developer_ids = frozenset(extras.get("developer_ids", []))
        released = extras.get("released")

    return ThemeAttributes(
        length_rating=length_rating,
        character_count=character_count,
        developer_ids=developer_ids,
        tags=tags,
        released=released,
        nsfw_cover=nsfw_cover,
    )
