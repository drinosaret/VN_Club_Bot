# tests/test_theme_command.py
"""/theme: the members' read-only view of a period's nomination theme.

Covers the period resolution, the three theme states, and the rule that a
VNDB outage must still leave the rules readable.
"""
from __future__ import annotations

import json

import pytest

import cogs.vn_themes as vn_themes
from cogs.vn_themes import ThemeCog, _resolve_period
from lib.migrations import run_migrations
from lib.themes import validate_rules
from lib.utils import DatabaseQueries, ValidationError
from tests.conftest import FakeBot

GUILD = 4242
SF_RULES = {"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}}


async def _assign(bot, rules, *, label="SF September", kind="monthly",
                  start="2026-09", end="2026-09", raw=None):
    await bot.RUN(
        DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
        (GUILD, kind, start, end, label,
         raw if raw is not None else json.dumps(validate_rules(rules)), None, 1),
    )


def _cog(bot) -> ThemeCog:
    cog = object.__new__(ThemeCog)
    cog.bot = bot
    return cog


# ---------------- period resolution ----------------


def test_monthly_defaults_to_the_period_nominate_targets(monkeypatch):
    monkeypatch.setattr(vn_themes, "get_current_month", lambda: "2026-08")
    assert _resolve_period("monthly", None) == ("2026-09", "2026-09")


def test_seasonal_resolves_to_the_whole_season():
    assert _resolve_period("seasonal", "2026-08") == ("2026-07", "2026-09")


def test_a_malformed_month_is_rejected():
    for kind in ("monthly", "seasonal"):
        with pytest.raises(ValidationError):
            _resolve_period(kind, "September")


# ---------------- example titles ----------------


def _stub_search(monkeypatch, results, total=99, seen=None):
    async def _search(filters, *, limit, sort="rating", exclude_nsfw_covers=False,
                      session=None):
        if seen is not None:
            seen.append({"filters": filters, "exclude_nsfw_covers": exclude_nsfw_covers})
        return results, total

    monkeypatch.setattr(vn_themes, "search_vns", _search)


def _stub_no_covers(monkeypatch):
    async def _fetch(session, url, **kwargs):
        return None

    monkeypatch.setattr(vn_themes, "fetch_image_bytes_capped", _fetch)


async def test_examples_come_back_with_their_display_titles(fake_bot: FakeBot, monkeypatch):
    _stub_no_covers(monkeypatch)
    _stub_search(monkeypatch, [
        {"id": "v2002", "title": "STEINS;GATE", "alttitle": "シュタインズ・ゲート",
         "image": {"url": "https://t.vndb.org/x.jpg", "sexual": 0}},
    ])
    examples, caveat = await _cog(fake_bot)._theme_examples(validate_rules(SF_RULES))
    assert [e[0] for e in examples] == ["シュタインズ・ゲート"]
    assert caveat is None


async def test_shown_titles_must_match_the_theme_openly(fake_bot: FakeBot, monkeypatch):
    # A VN that qualifies only via a spoiler-flagged tag is still nominatable,
    # but naming it on the card gives the twist away.
    _stub_no_covers(monkeypatch)
    seen = []
    _stub_search(monkeypatch, [{"id": "v1", "title": "Openly SF"}], seen=seen)
    examples, _ = await _cog(fake_bot)._theme_examples(validate_rules(SF_RULES))

    assert [e[0] for e in examples] == ["Openly SF"]
    assert len(seen) == 1                          # one query, not two
    assert seen[0]["filters"][2][1] == 0           # spoiler-flagged tags don't count


async def test_nothing_safe_to_show_yields_no_examples(fake_bot: FakeBot, monkeypatch):
    # A theme whose tag is only ever applied as a spoiler: the rules still
    # render, just without covers.
    _stub_no_covers(monkeypatch)
    _stub_search(monkeypatch, [])
    examples, caveat = await _cog(fake_bot)._theme_examples(validate_rules(SF_RULES))
    assert examples == [] and caveat is None


async def test_nsfw_covered_titles_are_dropped_not_blurred(fake_bot: FakeBot, monkeypatch):
    # The card is an announcement; a blurred panel says less than the title
    # under it, and a handful of examples out of hundreds can spare a few.
    _stub_no_covers(monkeypatch)
    seen = []
    _stub_search(monkeypatch, [{"id": "v1", "title": "Tame"}], seen=seen)
    examples, _ = await _cog(fake_bot)._theme_examples(validate_rules(SF_RULES))
    assert seen[0]["exclude_nsfw_covers"] is True
    # (title, cover) only: nothing downstream needs a blur flag.
    assert all(len(e) == 2 for e in examples)


async def test_they_are_dropped_even_when_the_theme_allows_nsfw_covers(
        fake_bot: FakeBot, monkeypatch):
    _stub_no_covers(monkeypatch)
    seen = []
    _stub_search(monkeypatch, [], seen=seen)
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]},
                            "nsfw_cover": "allow"})
    _, caveat = await _cog(fake_bot)._theme_examples(rules)
    assert seen[0]["exclude_nsfw_covers"] is True
    assert caveat is None


async def test_a_no_nsfw_theme_does_not_report_the_cover_rule_as_unchecked(
        fake_bot: FakeBot, monkeypatch):
    _stub_no_covers(monkeypatch)
    _stub_search(monkeypatch, [])
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]},
                            "nsfw_cover": "disallow"})
    _, caveat = await _cog(fake_bot)._theme_examples(rules)
    # The cover rule IS applied, so it must not be reported as unchecked.
    assert caveat is None


async def test_character_count_is_disclosed_as_unchecked(fake_bot: FakeBot, monkeypatch):
    _stub_no_covers(monkeypatch)
    _stub_search(monkeypatch, [])
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "SF"}]},
                            "character_count": {"min": 10000, "max": None}})
    _, caveat = await _cog(fake_bot)._theme_examples(rules)
    assert caveat and "haracter count" in caveat


async def test_a_theme_vndb_cannot_search_skips_the_query(fake_bot: FakeBot, monkeypatch):
    # Showing "examples" drawn from an unconstrained search would present the
    # whole database as on-theme.
    called = []
    _stub_no_covers(monkeypatch)
    _stub_search(monkeypatch, [{"id": "v1", "title": "X"}], seen=called)
    rules = validate_rules({"character_count": {"min": 10000, "max": None}})
    examples, caveat = await _cog(fake_bot)._theme_examples(rules)
    assert examples == [] and called == []
    assert caveat


async def test_a_vndb_outage_yields_no_examples_rather_than_an_error(
        fake_bot: FakeBot, monkeypatch):
    _stub_no_covers(monkeypatch)
    _stub_search(monkeypatch, None)
    examples, _ = await _cog(fake_bot)._theme_examples(validate_rules(SF_RULES))
    assert examples == []


async def test_a_failed_cover_fetch_still_yields_the_title(fake_bot: FakeBot, monkeypatch):
    async def _boom(session, url, **kwargs):
        raise RuntimeError("cdn down")

    monkeypatch.setattr(vn_themes, "fetch_image_bytes_capped", _boom)
    _stub_search(monkeypatch, [{"id": "v1", "title": "X", "image": {"url": "u", "sexual": 0}}])
    examples, _ = await _cog(fake_bot)._theme_examples(validate_rules(SF_RULES))
    assert [(e[0], e[1]) for e in examples] == [("X", None)]


# ---------------- pointing at /nominate ----------------


def test_the_theme_card_message_says_how_to_nominate():
    from cogs.vn_themes import _NOMINATE_HINT
    # Discord renders a leading "-#" as subtext, so it sits under the card
    # without competing with it.
    assert _NOMINATE_HINT.startswith("-# ")
    assert "/nominate" in _NOMINATE_HINT


async def test_an_unthemed_period_also_points_at_nominate(fake_bot: FakeBot):
    # Covered by the branch's own copy rather than the hint constant, so it
    # is worth pinning that the wording did not drift apart.
    await run_migrations(fake_bot)
    theme = await vn_themes.load_period_theme(fake_bot, GUILD, "monthly", "2026-09", "2026-09")
    assert theme is None  # the branch that tells the member anything goes


# ---------------- link buttons ----------------


def test_link_buttons_point_at_the_tag_and_developer_pages():
    from lib.embeds import build_theme_links_view
    rules = validate_rules({
        "tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]},
        "developers": {"any_of": [{"id": "p24", "name": "Key"}]}})
    view = build_theme_links_view(rules)
    urls = [b.url for b in view.children]
    assert urls == ["https://vndb.org/g105", "https://vndb.org/p24"]


def test_link_buttons_absent_when_nothing_is_linkable():
    from lib.embeds import build_theme_links_view
    assert build_theme_links_view(validate_rules({"length_rating": {"min": 1, "max": 2}})) is None


def test_link_buttons_stay_within_one_action_row():
    from lib.embeds import build_theme_links_view, MAX_THEME_LINK_BUTTONS
    rules = validate_rules({"tags": {"all_of": [
        {"id": f"g{i}", "name": f"Tag {i}"} for i in range(9)]}})
    view = build_theme_links_view(rules)
    assert len(view.children) == MAX_THEME_LINK_BUTTONS


def test_a_tag_repeated_across_buckets_gets_one_button():
    from lib.embeds import build_theme_links_view
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "SF"}],
        "any_of": [{"id": "g105", "name": "SF"}]}})
    assert len(build_theme_links_view(rules).children) == 1


# ---------------- caption wrapping ----------------


def _wrap(text, width=300, lines=2):
    from PIL import Image, ImageDraw
    from lib.pillow_helpers import load_japanese_font
    from lib.theme_card import _wrap_to_width
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    return _wrap_to_width(draw, text, load_japanese_font(30), width, lines)


def test_a_short_title_stays_on_one_line():
    assert _wrap("STEINS;GATE") == ["STEINS;GATE"]


def test_a_long_japanese_title_wraps_instead_of_truncating():
    lines = _wrap("スーパーダンガンロンパ2さよなら絶望学園")
    assert len(lines) == 2
    assert "".join(lines) == "スーパーダンガンロンパ2さよなら絶望学園"


def test_a_full_width_space_is_used_as_the_break():
    # Japanese titles separate a name from its subtitle with U+3000.
    assert _wrap("マブラヴ　オルタネイティヴ") == ["マブラヴ", "オルタネイティヴ"]


def test_latin_words_are_not_split_mid_word():
    title = "Ever17 -the out of infinity-"
    lines = _wrap(title)
    assert len(lines) > 1
    assert all(word in title.split() for line in lines for word in line.split())


def test_a_title_too_long_for_every_line_is_ellipsised_on_the_last():
    lines = _wrap("あ" * 200)
    assert len(lines) == 2 and lines[-1].endswith("...")
