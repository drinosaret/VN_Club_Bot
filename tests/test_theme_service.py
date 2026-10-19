"""gather_theme_attributes: transient char-count outage vs genuine absence,
tag-probe resolution, and the length fallback.

A char-count-only theme must fail the gate CLOSED with a "try again" (gather
returns None) when jiten is transiently down, but yield character_count=None
(a real rule rejection) when jiten is reachable and simply has no count.
"""
from __future__ import annotations

import lib.theme_service as ts
from lib.themes import validate_rules
from lib.vndb_api import VN_Entry


class _FakeBot:
    async def GET_ONE(self, query, params=()):
        return None  # no cached vndb_cache.character_count


def _vn(length_minutes=None, length_rating="2"):
    return VN_Entry("v1", "En", "Ja", "", False, length_minutes, length_rating, "")


class _JitenRaises:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_by_vndb_id(self, vndb_id):
        raise RuntimeError("jiten down")


class _JitenNoCount:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_by_vndb_id(self, vndb_id):
        return None  # reachable, but no deck for this VN


_CHAR_RULE = {"schema_version": 1, "character_count": {"min": 1000, "max": None}}


async def test_char_count_transient_outage_returns_none(monkeypatch):
    monkeypatch.setattr(ts, "JitenClient", _JitenRaises)
    out = await ts.gather_theme_attributes(_FakeBot(), "v1", _vn(), _CHAR_RULE)
    assert out is None  # transient -> gate shows "try again", not a rejection


async def test_char_count_genuinely_absent_yields_none_field(monkeypatch):
    monkeypatch.setattr(ts, "JitenClient", _JitenNoCount)
    out = await ts.gather_theme_attributes(_FakeBot(), "v1", _vn(), _CHAR_RULE)
    assert out is not None and out.character_count is None  # -> real reject path


async def test_no_char_rule_never_touches_jiten(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("jiten must not be called when no char-count rule")
    monkeypatch.setattr(ts, "JitenClient", _boom)
    out = await ts.gather_theme_attributes(_FakeBot(), "v1", _vn(), {"schema_version": 1})
    assert out is not None and out.character_count is None


def _stub_tag_matcher(monkeypatch, present=(), fail=()):
    """Stand in for VNDB, recording which probes were asked."""
    asked = []

    async def _matcher(vndb_id, tag_id, max_spoiler, min_level, include_children=True,
                       session=None):
        asked.append((tag_id, max_spoiler, min_level, include_children))
        if tag_id in fail:
            return None
        return tag_id in present

    monkeypatch.setattr(ts, "vn_matches_tag", _matcher)
    return asked


async def test_tag_probes_are_resolved_into_matches(monkeypatch):
    asked = _stub_tag_matcher(monkeypatch, present={"g105"})
    rules = validate_rules({"tags": {
        "all_of": [{"id": "g105", "name": "Science Fiction"}],
        "none_of": [{"id": "g23", "name": "Horror"}]}})
    out = await ts.gather_theme_attributes(_FakeBot(), "v1", _vn(), rules)
    assert out.tag_matches == {("g105", 2, 2.0, True): True, ("g23", 2, 0.0, True): False}
    assert sorted(asked) == [("g105", 2, 2.0, True), ("g23", 2, 0.0, True)]


async def test_unanswered_probe_stays_unknown_rather_than_absent(monkeypatch):
    _stub_tag_matcher(monkeypatch, fail={"g105"})
    rules = validate_rules({"tags": {"all_of": [{"id": "g105", "name": "Science Fiction"}]}})
    out = await ts.gather_theme_attributes(_FakeBot(), "v1", _vn(), rules)
    assert out.tag_matches == {("g105", 2, 2.0, True): None}


async def test_a_tagless_ruleset_asks_vndb_nothing(monkeypatch):
    asked = _stub_tag_matcher(monkeypatch)
    out = await ts.gather_theme_attributes(
        _FakeBot(), "v1", _vn(), validate_rules({"length_rating": {"min": 1, "max": 3}}))
    assert asked == [] and out.tag_matches == {}


async def test_length_falls_back_to_playtime_when_the_enum_is_unset():
    out = await ts.gather_theme_attributes(
        _FakeBot(), "v1", _vn(length_minutes=2438, length_rating=None), {"schema_version": 1})
    assert out.length_rating == 4  # 40h -> Long


async def test_length_enum_wins_over_playtime():
    out = await ts.gather_theme_attributes(
        _FakeBot(), "v1", _vn(length_minutes=2438, length_rating="1"), {"schema_version": 1})
    assert out.length_rating == 1


async def test_length_unknown_when_neither_source_has_it():
    out = await ts.gather_theme_attributes(
        _FakeBot(), "v1", _vn(length_minutes=None, length_rating=None), {"schema_version": 1})
    assert out.length_rating is None
