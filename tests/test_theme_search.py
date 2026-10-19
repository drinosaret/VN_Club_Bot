# tests/test_theme_search.py
from __future__ import annotations
import pytest
from lib.vndb_search import VNDBClient


class _FakeClient(VNDBClient):
    def __init__(self, response):
        super().__init__()
        self._response = response
        self.captured = None
    async def _make_request(self, endpoint, data):
        self.captured = (endpoint, data)
        return self._response


async def test_search_tags_hits_tag_endpoint_and_maps_results():
    tag = {"id": "g542", "name": "Nakige", "category": "cont", "searchable": True}
    c = _FakeClient({"results": [tag]})
    async with c:
        out = await c.search_tags("naki")
    assert c.captured[0] == "tag"
    assert c.captured[1]["filters"] == ["search", "=", "naki"]
    assert out == [tag]


async def test_search_tags_drops_tags_the_vn_filter_cannot_match():
    # VNDB's tag search surfaces grouping nodes like "Theme" and "Story";
    # the VN `tag` filter returns nothing for them, so a rule built on one
    # would reject every VN.
    c = _FakeClient({"results": [
        {"id": "g1", "name": "Theme", "category": "cont", "searchable": False},
        {"id": "g105", "name": "Science Fiction", "category": "cont", "searchable": True},
    ]})
    async with c:
        out = await c.search_tags("theme")
    assert [t["id"] for t in out] == ["g105"]
    assert "searchable" in c.captured[1]["fields"]


async def test_search_producers_hits_producer_endpoint():
    c = _FakeClient({"results": [{"id": "p57", "name": "Key", "original": ""}]})
    async with c:
        out = await c.search_producers("key")
    assert c.captured[0] == "producer"
    assert out[0]["id"] == "p57"


async def test_blank_query_returns_empty_no_request():
    c = _FakeClient({"results": [{"id": "g1", "name": "x"}]})
    async with c:
        assert await c.search_tags("  ") == []
    assert c.captured is None
