"""Process-local cache + session passthrough on ``fetch_vndb_extras``.

The cache keeps banner re-renders (and the multi-server hot paths
``/season_overview`` / ``/monthly`` / ``/seasonal``) from re-POSTing to VNDB
for the same VN once per page view. If this breaks, either the cache is
silently disabled (per-render TCP+TLS) or a failed response is cached
across calls, both of which only surface under sustained traffic.

These tests stub out aiohttp with a session whose body is fully controlled
and a hit counter, so they can assert "second call did NOT hit the
network."
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import lib.vndb_api as vndb_api
from lib.vndb_api import fetch_vndb_extras


# A canonical "VNDB returned data for this VN" response body. Shape mirrors
# the real API's ``{"results": [{...}]}`` envelope so the parser path is
# exercised end-to-end.
def _payload(vndb_id: str, *, rating: float = 7.5) -> dict:
    return {
        "results": [{
            "rating": rating,
            "votecount": 1234,
            "released": "2024-08-15",
            "tags": [
                {"category": "cont", "spoiler": 0, "name": "Drama", "rating": 3.0},
                {"category": "cont", "spoiler": 0, "name": "Mystery", "rating": 2.5},
            ],
            "platforms": ["win"],
            "developers": [{"name": "Test", "original": ""}],
        }]
    }


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def json(self) -> dict:
        return self._body


class _CountingSession:
    """Records every .post call so tests can assert hit counts.

    Returns the same `Response(200, body)` each time. Tests verifying
    error paths change ``_status`` or ``_body`` between calls.
    """

    def __init__(self, body: dict, *, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.calls: list[tuple[str, dict | None]] = []

    def post(self, url: str, json: dict | None = None) -> _Response:
        self.calls.append((url, json))
        return _Response(self._status, self._body)

    # Context-manager support: fetch_vndb_extras opens one with
    # ``async with aiohttp.ClientSession(...) as session`` when no
    # session is injected. The tests below always inject a session.
    async def __aenter__(self) -> "_CountingSession":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is module-level; wipe it between tests so order
    can't leak state."""
    vndb_api._vndb_extras_cache.clear()
    vndb_api._vndb_extras_locks.clear()
    yield
    vndb_api._vndb_extras_cache.clear()
    vndb_api._vndb_extras_locks.clear()


class TestVndbExtrasCache:
    @pytest.mark.asyncio
    async def test_first_call_hits_network_returns_parsed_dict(self) -> None:
        sess = _CountingSession(_payload("v1"))
        out = await fetch_vndb_extras("v1", session=sess)
        assert out["rating"] == 7.5
        assert out["release_year"] == "2024"
        assert "Drama" in out["top_tags"]
        assert len(sess.calls) == 1

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_serves_from_cache(self) -> None:
        sess = _CountingSession(_payload("v1"))
        await fetch_vndb_extras("v1", session=sess)
        # Mutate the session's body. If the cache works, the second
        # call should NOT see the new value (it's served from the prior
        # cached dict).
        sess._body = _payload("v1", rating=99.9)
        out_again = await fetch_vndb_extras("v1", session=sess)
        assert out_again["rating"] == 7.5, "should serve from cache"
        assert len(sess.calls) == 1, "second call must not hit network"

    @pytest.mark.asyncio
    async def test_distinct_vndb_ids_cached_separately(self) -> None:
        sess = _CountingSession(_payload("v1", rating=7.0))
        await fetch_vndb_extras("v1", session=sess)
        sess._body = _payload("v2", rating=8.0)
        out_v2 = await fetch_vndb_extras("v2", session=sess)
        assert out_v2["rating"] == 8.0
        assert len(sess.calls) == 2, "different VN should hit network"

    @pytest.mark.asyncio
    async def test_id_normalized_with_v_prefix_so_both_forms_share_cache(
        self,
    ) -> None:
        # The function prepends ``v`` if missing, so ``"1"`` and ``"v1"``
        # should resolve to the same cache key.
        sess = _CountingSession(_payload("v1"))
        await fetch_vndb_extras("1", session=sess)
        await fetch_vndb_extras("v1", session=sess)
        assert len(sess.calls) == 1, "v-prefix should normalize cache key"

    @pytest.mark.asyncio
    async def test_non_200_returns_empty_and_does_not_cache(self) -> None:
        sess = _CountingSession({}, status=500)
        out = await fetch_vndb_extras("v_500", session=sess)
        assert out == {}
        # A second call should retry the network: a transient 5xx
        # must not be cached for the full TTL.
        sess._status = 200
        sess._body = _payload("v_500")
        out_retry = await fetch_vndb_extras("v_500", session=sess)
        assert out_retry["rating"] == 7.5
        assert len(sess.calls) == 2, "a 5xx response must not be cached"

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_and_does_not_cache(self) -> None:
        # VNDB-shaped success response but with no matches: treat the
        # same as failure for caching purposes (otherwise a temporary
        # VNDB hiccup that returned empty would freeze a real VN out
        # of banners for the full TTL).
        sess = _CountingSession({"results": []})
        out = await fetch_vndb_extras("v_missing", session=sess)
        assert out == {}
        sess._body = _payload("v_missing")
        out_retry = await fetch_vndb_extras("v_missing", session=sess)
        assert out_retry["rating"] == 7.5
        assert len(sess.calls) == 2

    @pytest.mark.asyncio
    async def test_concurrent_calls_coalesce_into_one_request(self) -> None:
        """Per-key lock: ten concurrent calls for the same VN should
        produce a single underlying POST. Otherwise a render burst on
        the same VN bypasses the cache entirely."""
        sess = _CountingSession(_payload("v_concurrent"))
        results = await asyncio.gather(*[
            fetch_vndb_extras("v_concurrent", session=sess)
            for _ in range(10)
        ])
        assert all(r["rating"] == 7.5 for r in results)
        assert len(sess.calls) == 1, (
            "10 concurrent calls for the same VN should coalesce; "
            f"got {len(sess.calls)} network hits"
        )

    @pytest.mark.asyncio
    async def test_session_param_actually_reused(self) -> None:
        """Passing a session avoids creating a fresh ClientSession.
        Smoke-tested by checking that the injected session received the
        post() call, which pins the implementation's "if session is not
        None" branch.
        """
        sess = _CountingSession(_payload("v_sess"))
        await fetch_vndb_extras("v_sess", session=sess)
        assert sess.calls, "injected session.post was never called"
        url, payload = sess.calls[0]
        assert url == vndb_api.API_URL
        assert payload["filters"] == ["id", "=", "v_sess"]

    @pytest.mark.asyncio
    async def test_cache_eviction_under_cap_overflow(self, monkeypatch) -> None:
        """Verify the soft cap drops the oldest entry instead of growing
        the cache without bound. Lower the cap so the test runs fast.
        """
        monkeypatch.setattr(vndb_api, "_VNDB_EXTRAS_CACHE_CAP", 3)

        sess = _CountingSession(_payload("v1"))
        await fetch_vndb_extras("v1", session=sess)
        sess._body = _payload("v2")
        await fetch_vndb_extras("v2", session=sess)
        sess._body = _payload("v3")
        await fetch_vndb_extras("v3", session=sess)
        sess._body = _payload("v4")  # cap=3, this triggers eviction
        await fetch_vndb_extras("v4", session=sess)

        assert len(vndb_api._vndb_extras_cache) <= 3
        # Oldest (v1) should have been evicted.
        assert "v1" not in vndb_api._vndb_extras_cache
        assert "v4" in vndb_api._vndb_extras_cache
