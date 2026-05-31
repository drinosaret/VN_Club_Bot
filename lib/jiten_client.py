"""
Async client for the jiten.moe API (Japanese reading stats for visual novels).

The TS proxy in vn-club-resources notes ~37% raw failure rate for Node fetch
to api.jiten.moe, so this client retries with backoff on network errors and
5xx responses. In-process TTL caches mirror the proxy's strategy: 24h for
deck-id mappings (essentially never change), 6h for full deck info.

API contract reference:
    GET /api/media-deck/by-link-id/2/{vndb_id}   -> JSON array of deck IDs
    GET /api/media-deck/{deck_id}/detail         -> { data: {...} } or {...}

linkType=2 is VNDB. mediaType=7 is VN.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class JitenInfo:
    """Subset of jiten media-deck data the bot needs for the monthly banner."""
    deck_id: int
    character_count: int
    difficulty_raw: float
    cover_url: Optional[str] = None
    unique_kanji_count: int = 0
    dialogue_percentage: float = 0.0


class JitenClient:
    """Async context-manager client for jiten.moe."""

    BASE_URL = "https://api.jiten.moe/api"

    DECK_ID_TTL_SECONDS = 24 * 60 * 60   # 24h
    NULL_DECK_ID_TTL_SECONDS = 60 * 60   # 1h — recheck "not on jiten" sooner
    DECK_INFO_TTL_SECONDS = 6 * 60 * 60  # 6h
    CACHE_MAX_ENTRIES = 5000

    RETRY_BACKOFF_SECONDS = (1, 3, 5)
    REQUEST_TIMEOUT_SECONDS = 30

    # Class-level caches so multiple JitenClient context entries share state
    # within the bot process (a fresh client every command would otherwise
    # defeat caching). Dict reads/writes are themselves race-free in
    # single-threaded asyncio, but the network call between "miss" and
    # "populate" is an await point — two concurrent commands for the
    # same vndb_id will both miss the cache and both hit jiten. The
    # per-key locks below dedupe those: the second caller waits on the
    # first's lock, finds the populated cache on recheck, and returns
    # without making a redundant request.
    _deck_id_cache: Dict[str, "tuple[Optional[int], float]"] = {}
    _deck_info_cache: Dict[int, "tuple[Optional[JitenInfo], float]"] = {}
    _deck_id_locks: Dict[str, asyncio.Lock] = {}
    _deck_info_locks: Dict[int, asyncio.Lock] = {}

    @classmethod
    def _lock_for(cls, locks_dict, key):
        lock = locks_dict.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks_dict[key] = lock
        return lock

    def __init__(self, client_name: str = "Hikarubot", client_version: str = "1.0"):
        self.client_name = client_name
        self.client_version = client_version
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "JitenClient":
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": f"{self.client_name}/{self.client_version} (+vnclub.org)",
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            self.session = None

    async def _get_with_retry(self, url: str) -> Optional[aiohttp.ClientResponse]:
        """
        GET ``url`` with retries on network errors and 5xx responses.
        Returns the response on 2xx, None on 4xx (after first try), raises on
        sustained failure after all retries.
        """
        if not self.session:
            raise RuntimeError("JitenClient must be used as an async context manager")

        last_exc: Optional[BaseException] = None
        for attempt, backoff in enumerate((0,) + self.RETRY_BACKOFF_SECONDS):
            if backoff:
                # Add ±25% jitter so simultaneous failures (e.g. jiten 5xx
                # spike) don't all retry in lockstep and create a thundering
                # herd when the upstream comes back.
                await asyncio.sleep(backoff + random.uniform(-backoff * 0.25, backoff * 0.25))
            try:
                resp = await self.session.get(url)
                if resp.status >= 500:
                    logger.warning(
                        "jiten %s -> %d (attempt %d/%d)",
                        url, resp.status, attempt + 1, len(self.RETRY_BACKOFF_SECONDS) + 1,
                    )
                    resp.release()
                    continue
                return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                logger.warning(
                    "jiten %s network error (attempt %d/%d): %s",
                    url, attempt + 1, len(self.RETRY_BACKOFF_SECONDS) + 1, exc,
                )
                continue

        if last_exc:
            logger.error("jiten %s exhausted retries: %s", url, last_exc)
        else:
            logger.error("jiten %s exhausted retries with persistent 5xx", url)
        return None

    @classmethod
    def _evict(cls, cache: dict, ttl_for_value):
        """Evict expired entries; if still over cap, drop oldest by timestamp.

        Bounded so the cache can never grow unboundedly even when every
        entry is fresh — without this fallback, sustained traffic on
        unique IDs would let the cache grow forever.
        """
        if len(cache) <= cls.CACHE_MAX_ENTRIES:
            return
        now = time.time()
        expired = [k for k, (val, ts) in cache.items() if now - ts > ttl_for_value(val)]
        for k in expired:
            del cache[k]
        if len(cache) > cls.CACHE_MAX_ENTRIES:
            sorted_items = sorted(cache.items(), key=lambda kv: kv[1][1])
            excess = len(cache) - cls.CACHE_MAX_ENTRIES
            for k, _ in sorted_items[:excess]:
                del cache[k]

    @classmethod
    def _evict_expired(cls):
        """Eviction pass for both caches when they grow too large."""
        cls._evict(
            cls._deck_id_cache,
            lambda deck_id: cls.DECK_ID_TTL_SECONDS if deck_id is not None else cls.NULL_DECK_ID_TTL_SECONDS,
        )
        cls._evict(
            cls._deck_info_cache,
            lambda _info: cls.DECK_INFO_TTL_SECONDS,
        )

    async def resolve_deck_id(self, vndb_id: str) -> Optional[int]:
        """
        Map a VNDB id (e.g. "v17") to its jiten media-deck id.
        Returns ``None`` if the VN isn't on jiten.
        """
        # Fast path: cache hit, no lock needed.
        cached = self._deck_id_cache.get(vndb_id)
        if cached is not None:
            deck_id, ts = cached
            ttl = self.DECK_ID_TTL_SECONDS if deck_id is not None else self.NULL_DECK_ID_TTL_SECONDS
            if time.time() - ts < ttl:
                logger.debug(
                    "jiten resolve_deck_id: cache hit vndb=%s deck=%s age_s=%.1f",
                    vndb_id, deck_id, time.time() - ts,
                )
                return deck_id

        # Slow path: acquire the per-key lock, then recheck cache. If
        # another coroutine populated it while we waited, we return
        # the freshly-cached value instead of firing a redundant fetch.
        async with self._lock_for(self._deck_id_locks, vndb_id):
            cached = self._deck_id_cache.get(vndb_id)
            if cached is not None:
                deck_id, ts = cached
                ttl = self.DECK_ID_TTL_SECONDS if deck_id is not None else self.NULL_DECK_ID_TTL_SECONDS
                if time.time() - ts < ttl:
                    logger.debug(
                        "jiten resolve_deck_id: cache hit vndb=%s (post-lock, coalesced)",
                        vndb_id,
                    )
                    return deck_id

            logger.debug("jiten resolve_deck_id: cache miss vndb=%s, fetching", vndb_id)
            t0 = time.monotonic()
            url = f"{self.BASE_URL}/media-deck/by-link-id/2/{vndb_id}"
            resp = await self._get_with_retry(url)
            if resp is None:
                return None

            try:
                if resp.status == 404 or 400 <= resp.status < 500:
                    deck_id: Optional[int] = None
                elif resp.status == 200:
                    ids = await resp.json()
                    deck_id = ids[0] if isinstance(ids, list) and ids else None
                else:
                    logger.warning("jiten resolve %s unexpected status %d", vndb_id, resp.status)
                    return None
            finally:
                resp.release()

            self._deck_id_cache[vndb_id] = (deck_id, time.time())
            self._evict_expired()
            logger.debug(
                "jiten resolve_deck_id: fetched vndb=%s deck=%s in %.0fms",
                vndb_id, deck_id, (time.monotonic() - t0) * 1000,
            )
            return deck_id

    async def get_deck_detail(self, deck_id: int) -> Optional[Dict[str, Any]]:
        """Raw deck-detail payload, with the ``{ data: ... }`` wrapper unwrapped."""
        url = f"{self.BASE_URL}/media-deck/{deck_id}/detail"
        resp = await self._get_with_retry(url)
        if resp is None:
            return None
        try:
            if resp.status != 200:
                return None
            payload = await resp.json()
        finally:
            resp.release()

        if isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        return payload if isinstance(payload, dict) else None

    async def get_by_vndb_id(self, vndb_id: str) -> Optional[JitenInfo]:
        """
        Convenience: VNDB id -> :class:`JitenInfo` (or ``None``).

        Returns ``None`` for VNs not on jiten or when both retry attempts and
        the underlying requests fail. Returns partial data when possible —
        if ``character_count`` or ``difficulty_raw`` is missing from the
        payload it is coerced to 0 / 0.0.
        """
        deck_id = await self.resolve_deck_id(vndb_id)
        if deck_id is None:
            return None

        # Fast path: cache hit, no lock needed.
        cached = self._deck_info_cache.get(deck_id)
        if cached is not None:
            info, ts = cached
            if time.time() - ts < self.DECK_INFO_TTL_SECONDS:
                logger.debug(
                    "jiten deck_info: cache hit deck=%s age_s=%.1f",
                    deck_id, time.time() - ts,
                )
                return info

        # Slow path: per-deck lock dedupes concurrent fetches for the
        # same deck. Recheck cache after acquiring in case another
        # coroutine populated it while we were waiting.
        async with self._lock_for(self._deck_info_locks, deck_id):
            cached = self._deck_info_cache.get(deck_id)
            if cached is not None:
                info, ts = cached
                if time.time() - ts < self.DECK_INFO_TTL_SECONDS:
                    logger.debug(
                        "jiten deck_info: cache hit deck=%s (post-lock, coalesced)",
                        deck_id,
                    )
                    return info

            logger.debug("jiten deck_info: cache miss deck=%s, fetching", deck_id)
            t0 = time.monotonic()
            detail = await self.get_deck_detail(deck_id)
            if detail is None:
                return None

            # The /detail payload wraps fields under {parentDeck, mainDeck, subDecks}.
            # mainDeck holds the per-VN counts; fall back to top-level for safety
            # in case the schema changes.
            main_deck = detail.get("mainDeck") if isinstance(detail.get("mainDeck"), dict) else detail

            try:
                character_count = int(main_deck.get("characterCount", 0) or 0)
            except (TypeError, ValueError):
                character_count = 0
            try:
                difficulty_raw = float(main_deck.get("difficultyRaw", 0.0) or 0.0)
            except (TypeError, ValueError):
                difficulty_raw = 0.0
            try:
                unique_kanji_count = int(main_deck.get("uniqueKanjiCount", 0) or 0)
            except (TypeError, ValueError):
                unique_kanji_count = 0
            try:
                dialogue_percentage = float(main_deck.get("dialoguePercentage", 0.0) or 0.0)
            except (TypeError, ValueError):
                dialogue_percentage = 0.0
            cover_url = main_deck.get("coverName") or None

            info = JitenInfo(
                deck_id=deck_id,
                character_count=character_count,
                difficulty_raw=difficulty_raw,
                cover_url=cover_url,
                unique_kanji_count=unique_kanji_count,
                dialogue_percentage=dialogue_percentage,
            )
            self._deck_info_cache[deck_id] = (info, time.time())
            self._evict_expired()
            logger.debug(
                "jiten deck_info: fetched deck=%s chars=%d in %.0fms",
                deck_id, character_count, (time.monotonic() - t0) * 1000,
            )
            return info


def resolve_display_cover(vn_info, jiten_data) -> "tuple[Optional[str], bool]":
    """Pick the cover URL + NSFW flag to actually display.

    A VNDB cover flagged NSFW (image.sexual >= COVER_BLUR_THRESHOLD) is
    replaced by the jiten.moe cover when one exists (jiten covers are always
    SFW), so the real cover shows instead of a blur/placeholder/hidden
    thumbnail. Otherwise the VNDB cover and its real flag are returned
    unchanged (including the NSFW + no-jiten case, which stays flagged).

    Duck-typed so it works with any object exposing the relevant attributes
    (``VN_Entry``, ``JitenInfo``) without importing them here.

    Returns ``(cover_url: Optional[str], is_nsfw: bool)``.
    """
    vndb_url = getattr(vn_info, "thumbnail_url", None)
    is_nsfw = bool(getattr(vn_info, "thumbnail_is_nsfw", False))
    if is_nsfw and jiten_data is not None:
        jiten_cover = getattr(jiten_data, "cover_url", None)
        if jiten_cover:
            return jiten_cover, False
    return vndb_url, is_nsfw
