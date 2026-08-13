# lib/theme_service.py
"""Gather a VN's theme attributes from the cheap cached fields plus, only
when the rules require it, one VNDB fetch and/or a jiten character-count
lookup. Keeps cogs/vn_cycle.py thin and lib/themes.py pure."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import aiohttp

from .jiten_client import JitenClient
from .themes import (
    MAX_TAG_PROBES, RULES_SCHEMA_VERSION, TagProbe, ThemeAttributes,
    rules_need_extras, tag_probes, validate_rules,
)
from .utils import DatabaseQueries
from .vndb_api import COVER_BLUR_THRESHOLD, VN_Entry, fetch_theme_attributes, vn_matches_tag

_log = logging.getLogger(__name__)


async def load_period_theme(bot, guild_id: int, kind: str,
                            start_month: str, end_month: str):
    """The (label, rules) a manager declared for this exact period, or None
    when the period is unthemed or its ruleset constrains nothing.

    Returns ``(label, None)`` when a row exists but its rules can't be loaded,
    which callers must treat as "can't check" rather than "nothing to check":
    the stored JSON is written by two codebases (hikaru's editor and muramasa's
    web console) and a shape either of them gets wrong must not quietly retire
    the gate.
    """
    row = await bot.GET_ONE(
        DatabaseQueries.GET_THEME_ASSIGNMENT_FOR_PERIOD,
        (guild_id, kind, start_month, end_month),
    )
    if not row:
        return None
    label, rules_json = row
    try:
        rules = validate_rules(json.loads(rules_json))
    except (TypeError, ValueError):
        _log.error("theme rules unparseable for guild=%s %s %s..%s",
                   guild_id, kind, start_month, end_month)
        return label, None
    if rules == {"schema_version": RULES_SCHEMA_VERSION}:
        return None
    return label, rules

# VNDB's length enum in minutes: Very short <2h, Short 2-10h, Medium 10-30h,
# Long 30-50h, Very long >50h. Used only when the manual enum is unset, which
# is the common case outside well-known titles.
_LENGTH_BUCKETS = ((120, 1), (600, 2), (1800, 3), (3000, 4))


def _length_from_minutes(minutes: Optional[int]) -> Optional[int]:
    if not minutes or minutes <= 0:
        return None
    for ceiling, rating in _LENGTH_BUCKETS:
        if minutes < ceiling:
            return rating
    return 5


async def _resolve_tag_probes(probes: list[TagProbe], vndb_id: str) -> dict[TagProbe, Optional[bool]]:
    """Answer each probe against VNDB, sharing one connection. An unanswered
    probe stays None so the evaluator fails it closed."""
    if not probes:
        return {}
    if len(probes) > MAX_TAG_PROBES:
        # Authoring warns rather than refuses, so an over-budget ruleset can
        # be saved. Answer what fits; the rest read as unverified, which
        # fails the nomination closed instead of waving it through.
        _log.warning("theme ruleset asks for %d tag probes; capping at %d",
                     len(probes), MAX_TAG_PROBES)
        probes = probes[:MAX_TAG_PROBES]
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        answers = await asyncio.gather(*(
            vn_matches_tag(vndb_id, tag_id, spoiler, level, children, session=session)
            for tag_id, spoiler, level, children in probes
        ), return_exceptions=True)
    resolved: dict[TagProbe, Optional[bool]] = {}
    for probe, answer in zip(probes, answers):
        if isinstance(answer, BaseException):
            _log.warning("tag probe %s failed for %s: %s", probe, vndb_id, answer)
            answer = None
        resolved[probe] = answer
    return resolved


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
    that genuinely has no developers/count yields empty collections / None
    fields, which the evaluator treats as "does not satisfy the rule".
    """
    length_rating = None
    if vn_entry.length_rating not in (None, ""):
        try:
            length_rating = int(vn_entry.length_rating)
        except (TypeError, ValueError):
            length_rating = None
    if length_rating is None:
        # VNDB's manual length enum is unset on most entries; the play-time
        # votes it also publishes answer the same question.
        length_rating = _length_from_minutes(vn_entry.length_minutes)

    character_count = None
    if rules.get("character_count"):
        try:
            character_count = await _get_character_count(bot, vndb_id)
        except _CharCountUnavailable:
            return None  # transient jiten outage -> gate shows "try again"

    developer_ids = frozenset()
    released = None
    nsfw_cover = None
    if rules_need_extras(rules):
        extras = await fetch_theme_attributes(vndb_id)
        if extras is None:
            return None  # fetch failed -> fail closed
        developer_ids = frozenset(extras.get("developer_ids", []))
        released = extras.get("released")
        cover_sexual = extras.get("cover_sexual")
        if cover_sexual is not None:
            nsfw_cover = cover_sexual >= COVER_BLUR_THRESHOLD

    tag_matches = await _resolve_tag_probes(tag_probes(rules), vndb_id)

    return ThemeAttributes(
        length_rating=length_rating,
        character_count=character_count,
        developer_ids=developer_ids,
        tag_matches=tag_matches,
        released=released,
        nsfw_cover=nsfw_cover,
    )
