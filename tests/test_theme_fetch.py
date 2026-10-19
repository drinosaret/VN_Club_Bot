# tests/test_theme_fetch.py
from __future__ import annotations
import pytest
import lib.vndb_api as vndb_api
from lib.vndb_api import fetch_theme_attributes, vn_matches_tag


def _payload(vid: str, **over) -> dict:
    vn = {
        "id": vid,
        "released": "2012-05-01",
        "image": {"sexual": 0.2},
        "developers": [{"id": "p57", "name": "Key"}],
    }
    vn.update(over)
    return {"results": [vn]}


class _Response:
    def __init__(self, status, body): self.status, self._body = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None
    async def json(self): return self._body


class _CountingSession:
    def __init__(self, body, status=200):
        self._body, self._status, self.calls = body, status, []
    def post(self, url, json=None):
        self.calls.append((url, json)); return _Response(self._status, self._body)
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return None


@pytest.fixture(autouse=True)
def _clear():
    vndb_api._theme_attrs_cache.clear(); vndb_api._theme_attrs_locks.clear()
    vndb_api._tag_match_cache.clear()
    yield
    vndb_api._theme_attrs_cache.clear(); vndb_api._theme_attrs_locks.clear()
    vndb_api._tag_match_cache.clear()


async def test_parses_devs_released_and_cover():
    sess = _CountingSession(_payload("v1"))
    out = await fetch_theme_attributes("v1", session=sess)
    assert out["released"] == "2012-05-01"
    assert out["developer_ids"] == ["p57"]
    assert out["cover_sexual"] == 0.2
    assert len(sess.calls) == 1


async def test_unrated_cover_reports_unknown_not_safe():
    sess = _CountingSession(_payload("v1", image={"url": "x"}))
    assert (await fetch_theme_attributes("v1", session=sess))["cover_sexual"] is None
    vndb_api._theme_attrs_cache.clear()
    sess2 = _CountingSession(_payload("v2", image=None))
    assert (await fetch_theme_attributes("v2", session=sess2))["cover_sexual"] is None


async def test_second_call_cached():
    sess = _CountingSession(_payload("v1"))
    await fetch_theme_attributes("v1", session=sess)
    await fetch_theme_attributes("v1", session=sess)
    assert len(sess.calls) == 1


async def test_non_200_returns_none_no_cache():
    sess = _CountingSession({}, status=500)
    assert await fetch_theme_attributes("v9", session=sess) is None
    sess._status, sess._body = 200, _payload("v9")
    assert await fetch_theme_attributes("v9", session=sess) is not None
    assert len(sess.calls) == 2


async def test_empty_results_returns_none():
    sess = _CountingSession({"results": []})
    assert await fetch_theme_attributes("v_missing", session=sess) is None


async def test_v_prefix_normalized():
    sess = _CountingSession(_payload("v1"))
    await fetch_theme_attributes("1", session=sess)
    await fetch_theme_attributes("v1", session=sess)
    assert len(sess.calls) == 1


async def test_tag_match_asks_vndb_with_the_hierarchy_following_filter():
    sess = _CountingSession({"results": [{"id": "v17"}]})
    assert await vn_matches_tag("v17", "g105", 2, 1.0, session=sess) is True
    filters = sess.calls[0][1]["filters"]
    # `tag` walks the tag DAG; `dtag` would only see directly-applied tags.
    assert filters == ["and", ["id", "=", "v17"], ["tag", "=", ["g105", 2, 1.0]]]


async def test_tag_match_reports_absence_and_caches_it():
    sess = _CountingSession({"results": []})
    assert await vn_matches_tag("v2002", "g105", 2, 1.0, session=sess) is False
    assert await vn_matches_tag("v2002", "g105", 2, 1.0, session=sess) is False
    assert len(sess.calls) == 1


async def test_tag_match_probe_params_are_part_of_the_cache_key():
    sess = _CountingSession({"results": [{"id": "v1"}]})
    await vn_matches_tag("v1", "g105", 2, 1.0, session=sess)
    await vn_matches_tag("v1", "g105", 0, 1.0, session=sess)
    assert len(sess.calls) == 2


async def test_tag_match_failure_is_unknown_not_absent():
    # VNDB 400s the whole request on an unknown tag id or out-of-range knob;
    # that must read as "could not verify", never as "tag absent".
    sess = _CountingSession({}, status=400)
    assert await vn_matches_tag("v1", "g99999999", 2, 1.0, session=sess) is None
    sess._status, sess._body = 200, {"results": [{"id": "v1"}]}
    assert await vn_matches_tag("v1", "g99999999", 2, 1.0, session=sess) is True


def _vn(vid, sexual=0.0):
    return {"id": vid, "title": f"Title {vid}", "rating": 80,
            "image": {"url": f"https://t.vndb.org/{vid}.jpg", "sexual": sexual}}


async def test_search_returns_results_and_the_full_total():
    sess = _CountingSession({"results": [_vn("v1"), _vn("v2")], "count": 1729})
    results, total = await vndb_api.search_vns(["tag", "=", "g105"], limit=8, session=sess)
    assert [v["id"] for v in results] == ["v1", "v2"]
    assert total == 1729
    assert sess.calls[0][1]["count"] is True


async def test_search_is_narrowed_to_japanese_originals_by_default():
    # The club reads in Japanese; a count over every language roughly doubles.
    sess = _CountingSession({"results": [], "count": 0})
    await vndb_api.search_vns(["tag", "=", "g105"], session=sess)
    assert sess.calls[0][1]["filters"] == [
        "and", ["tag", "=", "g105"], ["olang", "=", "ja"]]


async def test_search_language_narrowing_can_be_lifted():
    sess = _CountingSession({"results": [], "count": 0})
    await vndb_api.search_vns(["tag", "=", "g105"], original_language=None, session=sess)
    assert sess.calls[0][1]["filters"] == ["tag", "=", "g105"]


async def test_search_drops_nsfw_covers_when_the_theme_disallows_them():
    sess = _CountingSession({"results": [_vn("v1", 0.9), _vn("v2", 0.1), _vn("v3", 0.7)],
                             "count": 3})
    results, _ = await vndb_api.search_vns(
        ["tag", "=", "g105"], limit=8, exclude_nsfw_covers=True, session=sess)
    assert [v["id"] for v in results] == ["v2"]
    # Over-fetch so removals don't leave a short page.
    assert sess.calls[0][1]["results"] > 8


async def test_search_keeps_nsfw_covers_otherwise():
    sess = _CountingSession({"results": [_vn("v1", 0.9)], "count": 1})
    results, _ = await vndb_api.search_vns(["tag", "=", "g105"], limit=8, session=sess)
    assert [v["id"] for v in results] == ["v1"]


async def test_search_respects_the_limit():
    sess = _CountingSession({"results": [_vn(f"v{i}") for i in range(20)], "count": 20})
    results, _ = await vndb_api.search_vns(["tag", "=", "g105"], limit=3, session=sess)
    assert len(results) == 3


async def test_search_failure_is_reported_as_no_examples():
    sess = _CountingSession({}, status=500)
    assert await vndb_api.search_vns(["tag", "=", "g105"], session=sess) == (None, None)


async def test_search_without_a_filter_makes_no_request():
    # An unconstrained search would present the whole database as on-theme.
    sess = _CountingSession({"results": [_vn("v1")], "count": 1})
    assert await vndb_api.search_vns(None, session=sess) == (None, None)
    assert sess.calls == []


async def test_tag_match_clamps_out_of_range_knobs():
    sess = _CountingSession({"results": []})
    await vn_matches_tag("v1", "g105", 7, 9.5, session=sess)
    assert sess.calls[0][1]["filters"][2] == ["tag", "=", ["g105", 2, 3.0]]
