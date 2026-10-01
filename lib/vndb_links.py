"""Linked VNDB accounts and the cached copy of their public lists.

A link maps one Discord user to one VNDB account by username; there is no
ownership proof, so a VNDB account can be linked by at most one Discord user
and managers can remove a link. The list cache holds only what VNDB serves
without a token (entries under public labels) and is replaced wholesale on
each sync, so an entry removed or hidden on VNDB disappears here on the next
pass.

Every request from this module goes through one pacer. VNDB meters the whole
container by IP, and the sync walks many pages, so the pacer keeps this
module to a fraction of the shared budget that autocomplete, banners and
theme checks also draw on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord

_log = logging.getLogger(__name__)

API_BASE = "https://api.vndb.org/kana"
USER_AGENT = "Hikarubot/1.0 (+vnclub.org)"

# VNDB allows 200 requests per 5 minutes per IP. One request every 6s is a
# quarter of that, leaving the rest for the bot's unpaced VNDB traffic.
MIN_REQUEST_INTERVAL = 6.0
# After a 429, requests from this module wait this long before resuming.
THROTTLE_BACKOFF = 60.0
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

PAGE_SIZE = 100
# Upper bound on pages per user per sync; lists beyond this are truncated to
# their most recently modified entries.
MAX_PAGES = 50
SYNC_INTERVAL_HOURS = 24
# A sync that failed on a VNDB outage or bad response becomes due again after
# this long rather than after the full interval.
UNAVAILABLE_RETRY_HOURS = 1
NOTE_LIMIT = 2000

# VNDB's built-in labels. Entries only on Wishlist (5) or Blacklist (6) say
# nothing about having read the VN, so they are not fetched.
LABEL_FINISHED = 2
READ_LABELS = (1, 2, 3, 4, 7)  # Playing, Finished, Stalled, Dropped, Voted

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")
_UID_RE = re.compile(r"^u(\d{1,9})$", re.IGNORECASE)
_PROFILE_URL_RE = re.compile(r"vndb\.org/(u\d{1,9})\b", re.IGNORECASE)


class VndbUnavailable(Exception):
    """VNDB could not be reached or refused the request."""


class VndbThrottled(VndbUnavailable):
    """VNDB answered 429; callers should stop and retry later."""


@dataclass(frozen=True)
class VndbAccount:
    uid: str
    username: str


# ---------------------------------------------------------------- pacing ---

_pace_lock: Optional[asyncio.Lock] = None
_last_request_at = 0.0
# One sync at a time: the loop and a fresh link never interleave pages.
_sync_lock: Optional[asyncio.Lock] = None


def _locks() -> tuple[asyncio.Lock, asyncio.Lock]:
    # Created lazily so they bind to the running loop, not the import-time one.
    global _pace_lock, _sync_lock
    if _pace_lock is None:
        _pace_lock = asyncio.Lock()
    if _sync_lock is None:
        _sync_lock = asyncio.Lock()
    return _pace_lock, _sync_lock


async def _request(method: str, path: str, *, params=None, json=None) -> Optional[dict]:
    """Paced request to the VNDB API. Returns the decoded body, None on 404,
    and raises VndbUnavailable / VndbThrottled otherwise."""
    global _last_request_at
    pace_lock, _ = _locks()
    async with pace_lock:
        wait = _last_request_at + MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        throttled = False
        try:
            async with aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT},
            ) as session:
                async with session.request(
                    method, f"{API_BASE}{path}", params=params, json=json,
                ) as resp:
                    if resp.status == 429:
                        throttled = True
                        raise VndbThrottled("vndb 429")
                    if resp.status == 404:
                        return None
                    if resp.status != 200:
                        detail = " ".join((await resp.text())[:200].split())
                        raise VndbUnavailable(f"vndb {resp.status}: {detail}")
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            # ValueError covers a body that is not valid JSON.
            raise VndbUnavailable(str(e) or type(e).__name__) from e
        finally:
            _last_request_at = time.monotonic()
            if throttled:
                _last_request_at += THROTTLE_BACKOFF - MIN_REQUEST_INTERVAL


# ----------------------------------------------------------- user lookup ---

def parse_account_input(raw: str) -> Optional[str]:
    """Reduce a username, a user id (u123) or a profile URL to the query VNDB
    accepts. Returns None for input that cannot be any of those."""
    raw = (raw or "").strip()
    m = _PROFILE_URL_RE.search(raw)
    if m:
        return m.group(1).lower()
    m = _UID_RE.match(raw)
    if m:
        return f"u{m.group(1)}"
    if _USERNAME_RE.match(raw):
        return raw
    return None


async def lookup_account(query: str) -> Optional[VndbAccount]:
    """Resolve a username or uid to the account VNDB knows. None if VNDB has
    no such user."""
    data = await _request("GET", "/user", params={"q": query})
    if not data:
        return None
    hit = data.get(query)
    if not hit:
        # VNDB keys the response by the query as sent; tolerate a case change.
        hit = next((v for k, v in data.items() if k.lower() == query.lower()), None)
    if not hit or not hit.get("id"):
        return None
    uid = str(hit["id"])
    if not _UID_RE.match(uid):
        # The uid is interpolated into profile URLs and link buttons.
        raise VndbUnavailable("unexpected user id format")
    return VndbAccount(uid=uid.lower(), username=str(hit.get("username") or uid)[:64])


# ---------------------------------------------------------------- queries ---

GET_LINK = """
SELECT vndb_uid, vndb_username FROM vndb_links WHERE user_id = ?;
"""

def pending_sql(alias: str = "l") -> str:
    """SQL condition: the link has nothing fetched to show yet. Either no
    sync has finished, or the last attempt failed on VNDB and nothing is
    cached, in which case a count of zero would be wrong."""
    return (
        f"({alias}.synced_at IS NULL OR ({alias}.sync_error = 'unavailable' "
        f"AND NOT EXISTS (SELECT 1 FROM vndb_ulist pu WHERE pu.user_id = {alias}.user_id)))"
    )


def _synced_at_sql(alias: str = "l") -> str:
    # synced_at as readers should see it: NULL while the list is pending.
    return f"CASE WHEN {pending_sql(alias)} THEN NULL ELSE {alias}.synced_at END"


GET_LINK_DETAIL = f"""
SELECT l.vndb_uid, l.vndb_username, l.linked_at, {_synced_at_sql("l")} AS synced_at, l.sync_error
FROM vndb_links l WHERE l.user_id = ?;
"""

GET_LINK_OWNER = """
SELECT user_id FROM vndb_links WHERE vndb_uid = ?;
"""

UPSERT_LINK = """
INSERT INTO vndb_links (user_id, vndb_uid, vndb_username, linked_in_guild)
VALUES (?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    vndb_uid = excluded.vndb_uid,
    vndb_username = excluded.vndb_username,
    linked_in_guild = excluded.linked_in_guild,
    linked_at = CURRENT_TIMESTAMP,
    synced_at = NULL,
    sync_error = NULL;
"""

DELETE_LINK = "DELETE FROM vndb_links WHERE user_id = ?;"
DELETE_ULIST = "DELETE FROM vndb_ulist WHERE user_id = ?;"

DUE_FOR_SYNC = """
SELECT user_id, vndb_uid FROM vndb_links
WHERE synced_at IS NULL OR synced_at < datetime('now', ?)
ORDER BY synced_at IS NOT NULL, synced_at
LIMIT ?;
"""

MARK_SYNCED = """
UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP, sync_error = NULL,
    vndb_username = COALESCE(?, vndb_username)
WHERE user_id = ? AND vndb_uid = ?;
"""

MARK_SYNC_ERROR = """
UPDATE vndb_links SET synced_at = CURRENT_TIMESTAMP, sync_error = ?
WHERE user_id = ? AND vndb_uid = ?;
"""

# Backdates synced_at so DUE_FOR_SYNC picks the link up again after
# UNAVAILABLE_RETRY_HOURS, while it still queues behind links that are due now.
MARK_SYNC_UNAVAILABLE = f"""
UPDATE vndb_links
SET synced_at = datetime('now', '-{SYNC_INTERVAL_HOURS - UNAVAILABLE_RETRY_HOURS} hours'),
    sync_error = 'unavailable'
WHERE user_id = ? AND vndb_uid = ?;
"""

# Guarded by the link still pointing at the same account, so a sync that
# finishes after an unlink or relink writes nothing.
_INSERT_ULIST = """
INSERT OR REPLACE INTO vndb_ulist
    (user_id, vndb_id, vote, voted, finished, is_finished, notes, lastmod)
SELECT ?, ?, ?, ?, ?, ?, ?, ?
WHERE EXISTS (SELECT 1 FROM vndb_links WHERE user_id = ? AND vndb_uid = ?);
"""

_LINK_STILL_CURRENT = """
SELECT 1 FROM vndb_links WHERE user_id = ? AND vndb_uid = ?;
"""

_DELETE_ULIST_GUARDED = """
DELETE FROM vndb_ulist WHERE user_id = ?
AND EXISTS (SELECT 1 FROM vndb_links WHERE user_id = ? AND vndb_uid = ?);
"""

_UPSERT_TITLE = """
INSERT INTO vndb_vn_titles (vndb_id, title, alttitle) VALUES (?, ?, ?)
ON CONFLICT(vndb_id) DO UPDATE SET title = excluded.title, alttitle = excluded.alttitle;
"""

ULIST_SUMMARY = """
SELECT COUNT(*),
       SUM(is_finished),
       COUNT(vote),
       AVG(vote),
       SUM(CASE WHEN notes IS NOT NULL THEN 1 ELSE 0 END)
FROM vndb_ulist WHERE user_id = ?;
"""


async def get_link(bot, user_id: int) -> Optional[tuple[str, str]]:
    """(vndb_uid, vndb_username) for a linked user, else None."""
    try:
        row = await bot.GET_ONE(GET_LINK, (user_id,))
    except Exception:  # noqa: BLE001
        # Before migrations have created the table, or on a transient DB
        # error, profile rendering carries on without the link.
        _log.debug("vndb link lookup failed for user=%s", user_id, exc_info=True)
        return None
    return (row[0], row[1]) if row else None


def build_vndb_profile_view(account: tuple[str, str]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="VNDB profile", style=discord.ButtonStyle.link,
        url=f"https://vndb.org/{account[0]}",
    ))
    return view


def profile_url(vndb_uid: str) -> str:
    return f"https://vndb.org/{vndb_uid}"


# ------------------------------------------------------------------- sync ---

def _date_from_unix(ts) -> Optional[str]:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def parse_ulist_entry(entry: dict) -> Optional[dict]:
    """Normalize one /ulist result. Returns None for rows without a VN id."""
    vid = entry.get("id")
    if not isinstance(vid, str) or not re.fullmatch(r"v\d+", vid):
        return None
    vote = entry.get("vote")
    if not isinstance(vote, int) or not (10 <= vote <= 100):
        vote = None
    labels = entry.get("labels") or []
    label_ids = {lab.get("id") for lab in labels if isinstance(lab, dict)}
    notes = (entry.get("notes") or "").strip() or None
    if notes and len(notes) > NOTE_LIMIT:
        notes = notes[:NOTE_LIMIT]
    finished = entry.get("finished")
    if not isinstance(finished, str) or not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", finished):
        finished = None
    vn = entry.get("vn") or {}
    return {
        "vndb_id": vid,
        "vote": vote,
        "voted": _date_from_unix(entry.get("voted")),
        "finished": finished,
        "is_finished": 1 if LABEL_FINISHED in label_ids else 0,
        "notes": notes,
        "lastmod": entry.get("lastmod") if isinstance(entry.get("lastmod"), int) else None,
        "title": vn.get("title"),
        "alttitle": vn.get("alttitle"),
    }


async def fetch_ulist(vndb_uid: str) -> Optional[list[dict]]:
    """All read-related public entries for a user, newest change first.
    None when VNDB reports no such user."""
    filters = ["or", *[["label", "=", lab] for lab in READ_LABELS]]
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = await _request("POST", "/ulist", json={
            "user": vndb_uid,
            "fields": "id, vote, voted, finished, notes, lastmod, labels.id, vn.title, vn.alttitle",
            "filters": filters,
            "sort": "lastmod",
            "reverse": True,
            "results": PAGE_SIZE,
            "page": page,
        })
        if data is None:
            return None if page == 1 else rows
        for entry in data.get("results") or []:
            parsed = parse_ulist_entry(entry)
            if parsed:
                rows.append(parsed)
        if not data.get("more"):
            break
    return rows


async def sync_user(bot, user_id: int, vndb_uid: str) -> bool:
    """Refresh one linked user's cached list. Returns True on success.
    VndbThrottled propagates so a batch can stop early."""
    _, sync_lock = _locks()
    async with sync_lock:
        # The link may have changed while this sync waited for the lock;
        # fetching a list nobody will store would only spend request budget.
        if not await bot.GET_ONE(_LINK_STILL_CURRENT, (user_id, vndb_uid)):
            return False
        try:
            account = await lookup_account(vndb_uid)
            if account is None:
                # The account is gone from VNDB, so its entries are too.
                await bot.RUN_TRANSACTION([
                    (_DELETE_ULIST_GUARDED, (user_id, user_id, vndb_uid)),
                    (MARK_SYNC_ERROR, ("account_not_found", user_id, vndb_uid)),
                ])
                _log.info("vndb sync: account gone user=%s", user_id)
                return False
            rows = await fetch_ulist(vndb_uid)
        except VndbThrottled:
            raise
        except VndbUnavailable as e:
            # Recording the attempt keeps the same failure from being retried
            # every tick; the cached list, if any, stays as it was.
            await bot.RUN(MARK_SYNC_UNAVAILABLE, (user_id, vndb_uid))
            _log.warning("vndb sync failed user=%s: %s", user_id, e)
            return False

        rows = rows or []
        statements: list[tuple[str, tuple]] = [
            (_DELETE_ULIST_GUARDED, (user_id, user_id, vndb_uid)),
        ]
        for r in rows:
            statements.append((_INSERT_ULIST, (
                user_id, r["vndb_id"], r["vote"], r["voted"], r["finished"],
                r["is_finished"], r["notes"], r["lastmod"], user_id, vndb_uid,
            )))
            if r["title"] or r["alttitle"]:
                statements.append((_UPSERT_TITLE, (r["vndb_id"], r["title"], r["alttitle"])))
        statements.append((MARK_SYNCED, (account.username, user_id, vndb_uid)))
        await bot.RUN_TRANSACTION(statements)
        _log.info("vndb sync ok user=%s entries=%d", user_id, len(rows))
        return True


async def sync_due(bot, limit: int = 10) -> int:
    """Sync up to ``limit`` users whose cache is stale. Returns how many
    synced; stops at the first 429."""
    due = await bot.GET(DUE_FOR_SYNC, (f"-{SYNC_INTERVAL_HOURS} hours", limit))
    done = 0
    for user_id, vndb_uid in due:
        try:
            if await sync_user(bot, user_id, vndb_uid):
                done += 1
        except VndbThrottled:
            _log.warning("vndb sync: throttled, stopping this pass")
            break
    return done


# ----------------------------------------------------------- scope/report ---

def scope_clause(alias: str = "l") -> str:
    """WHERE fragment restricting linked users to a server: linked there, or
    logged a read there. Bind (guild_id, guild_id, guild_id); a NULL guild
    disables the filter."""
    return (
        f"(? IS NULL OR {alias}.linked_in_guild = ? OR {alias}.user_id IN "
        "(SELECT user_id FROM reading_logs WHERE logged_in_guild = ?))"
    )


# /vndb_leaderboard ranks VNs; /vndb_user_leaderboard ranks members.
VN_CATEGORIES = {
    "most_read": "Most-read VNs",
    "top_rated": "Highest-rated VNs",
    "bottom_rated": "Lowest-rated VNs",
}
USER_CATEGORIES = {
    "most_finished": "Most finished",
    "most_votes": "Most votes cast",
}
LEADERBOARD_CATEGORIES = {**VN_CATEGORIES, **USER_CATEGORIES}
# Categories ranking VNs by their average vote, and the direction of each.
RATED_CATEGORIES = {"top_rated": "DESC", "bottom_rated": "ASC"}
TOP_RATED_MIN_VOTES = 3

# Linked members in scope, and how many of their lists have not synced yet.
# Bind (guild, guild, guild).
LINKED_IN_SCOPE = f"""
SELECT COUNT(*), SUM(CASE WHEN {pending_sql("l")} THEN 1 ELSE 0 END)
FROM vndb_links l WHERE {scope_clause("l")};
"""


def leaderboard_query(category: str, since: Optional[str], min_votes: int = TOP_RATED_MIN_VOTES) -> str:
    """SQL for one category. Bind (since, since, guild, guild, guild).
    ``since`` is an ISO date or None for all time; ``min_votes`` applies to
    top_rated only."""
    scope = scope_clause("l")
    synced_at = _synced_at_sql("l")
    # Member rankings start from the links so members with nothing counted
    # still appear (at zero): the all-time list doubles as the list of who
    # has linked an account. synced_at is NULL for a list still pending.
    if category == "most_finished":
        return f"""
        SELECT l.user_id, l.vndb_username, COUNT(u.vndb_id) AS n, l.vndb_uid, {synced_at}
        FROM vndb_links l LEFT JOIN vndb_ulist u
          ON u.user_id = l.user_id AND u.is_finished = 1 AND (? IS NULL OR u.finished >= ?)
        WHERE {scope}
        GROUP BY l.user_id ORDER BY n DESC, l.vndb_username COLLATE NOCASE
        LIMIT 200;"""
    if category == "most_votes":
        return f"""
        SELECT l.user_id, l.vndb_username, COUNT(u.vndb_id) AS n, l.vndb_uid, {synced_at}
        FROM vndb_links l LEFT JOIN vndb_ulist u
          ON u.user_id = l.user_id AND u.vote IS NOT NULL AND (? IS NULL OR u.voted >= ?)
        WHERE {scope}
        GROUP BY l.user_id ORDER BY n DESC, l.vndb_username COLLATE NOCASE
        LIMIT 200;"""
    if category == "most_read":
        return f"""
        SELECT u.vndb_id, t.title, t.alttitle, COUNT(*) AS n, AVG(u.vote)
        FROM vndb_ulist u JOIN vndb_links l ON l.user_id = u.user_id
        LEFT JOIN vndb_vn_titles t ON t.vndb_id = u.vndb_id
        WHERE u.is_finished = 1 AND (? IS NULL OR u.finished >= ?) AND {scope}
        GROUP BY u.vndb_id ORDER BY n DESC, AVG(u.vote) DESC
        LIMIT 100;"""
    if category in RATED_CATEGORIES:
        direction = RATED_CATEGORIES[category]
        return f"""
        SELECT u.vndb_id, t.title, t.alttitle, COUNT(u.vote) AS n, AVG(u.vote) AS avg_vote
        FROM vndb_ulist u JOIN vndb_links l ON l.user_id = u.user_id
        LEFT JOIN vndb_vn_titles t ON t.vndb_id = u.vndb_id
        WHERE u.vote IS NOT NULL AND (? IS NULL OR u.voted >= ?) AND {scope}
        GROUP BY u.vndb_id HAVING COUNT(u.vote) >= {int(min_votes)}
        ORDER BY avg_vote {direction}, n DESC
        LIMIT 100;"""
    raise ValueError(f"unknown category {category!r}")


# Linked users' entries for one VN: vote and/or note.
VN_IMPRESSIONS = """
SELECT l.user_id, l.vndb_username, u.vote, u.notes, u.is_finished
FROM vndb_ulist u JOIN vndb_links l ON l.user_id = u.user_id
WHERE u.vndb_id = ? AND (u.vote IS NOT NULL OR u.notes IS NOT NULL);
"""

# One user's entries that carry a note or a vote, most recently changed first.
USER_IMPRESSIONS = """
SELECT u.vndb_id, t.title, t.alttitle, u.vote, u.notes, u.lastmod
FROM vndb_ulist u LEFT JOIN vndb_vn_titles t ON t.vndb_id = u.vndb_id
WHERE u.user_id = ? AND (u.notes IS NOT NULL OR u.vote IS NOT NULL)
ORDER BY u.notes IS NULL, u.lastmod DESC;
"""


def format_vndb_vote(vote: Optional[int]) -> Optional[str]:
    """VNDB votes are 10-100 and shown on VNDB as 1.0-10.0."""
    if vote is None:
        return None
    return f"{vote / 10:.1f}/10"
