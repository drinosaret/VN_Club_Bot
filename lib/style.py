"""House style for everything the bot posts.

Every embed, list and status line takes its colours, separator, date format
and status markers from here, so the whole bot reads as one voice:

  * titles: an emoji, sentence case, then " · " and the subject;
  * one separator, " · ", in titles, rows and footers;
  * footers: page, count and at most one hint, no brand text;
  * status lines start with OK, ERROR or INFO and nothing else;
  * months read "Sep 2026" in running text and "September 2026" in titles;
  * no internal values (ids, phase codes, stored reason strings) in output.
"""

from __future__ import annotations

from typing import Optional

import discord

# The profile card's accent. Informational embeds use it; leaderboards and
# the /finish confirmation are the only exceptions.
ACCENT = discord.Color.from_rgb(88, 70, 150)
LEADERBOARD = discord.Color.gold()
SUCCESS = discord.Color.green()

SEP = " · "

OK = "✅"
ERROR = "❌"
INFO = "ℹ️"

_MONTHS_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


def _split_month(month: Optional[str]) -> Optional[tuple[int, int]]:
    try:
        year, mon = (month or "").split("-")[:2]
        year_i, mon_i = int(year), int(mon)
    except ValueError:
        return None
    if not 1 <= mon_i <= 12:
        return None
    return year_i, mon_i


def month_short(month: Optional[str]) -> str:
    """'2026-09' -> 'Sep 2026'. Unparseable input is returned unchanged."""
    parts = _split_month(month)
    if parts is None:
        return month or ""
    return f"{_MONTHS_SHORT[parts[1] - 1]} {parts[0]}"


def month_long(month: Optional[str]) -> str:
    """'2026-09' -> 'September 2026'. Unparseable input is returned unchanged."""
    parts = _split_month(month)
    if parts is None:
        return month or ""
    return f"{_MONTHS_LONG[parts[1] - 1]} {parts[0]}"


def month_range(start: Optional[str], end: Optional[str], long: bool = False) -> str:
    """'2026-04', '2026-06' -> 'Apr to Jun 2026'; a single month when both
    ends match; each end with its year when the years differ."""
    fmt = month_long if long else month_short
    if not end or end == start:
        return fmt(start)
    a, b = _split_month(start), _split_month(end)
    if a and b and a[0] == b[0]:
        names = _MONTHS_LONG if long else _MONTHS_SHORT
        return f"{names[a[1] - 1]} to {names[b[1] - 1]} {b[0]}"
    return f"{fmt(start)} to {fmt(end)}"


def title(emoji: Optional[str], *parts: str) -> str:
    """'🏆', 'Leaderboard', 'Summer 2026' -> '🏆 Leaderboard · Summer 2026'."""
    text = SEP.join(p for p in parts if p)
    return f"{emoji} {text}" if emoji else text


def plural(n: int, noun: str, plural_form: Optional[str] = None) -> str:
    """1, 'vote' -> '1 vote'; 3, 'vote' -> '3 votes', with a thousands separator."""
    word = noun if n == 1 else (plural_form or noun + "s")
    return f"{n:,} {word}"


def footer(page: int = 0, pages: int = 1, *bits: Optional[str]) -> str:
    """'Page 2/5 · 30 logs · hint'. The page part is left out on a single
    page; empty bits are skipped."""
    parts = [f"Page {page + 1}/{pages}"] if pages > 1 else []
    parts += [b for b in bits if b]
    return SEP.join(parts)


def ok(text: str) -> str:
    return f"{OK} {text}"


def error(text: str) -> str:
    return f"{ERROR} {text}"


def info(text: str) -> str:
    return f"{INFO} {text}"
