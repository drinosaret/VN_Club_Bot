"""Defensive caps on the remote-image fetch path.

`fetch_image_bytes_capped` in `lib/pillow_helpers.py` is the shared
gate for VN cover and Discord avatar fetches. It exists because PIL
alone (with `Image.MAX_IMAGE_PIXELS` set) doesn't bound a multi-GB
body, an HTML 404 page being decoded as image data, or a small
compressed image that decodes to a very large size in memory.

These tests stub out `aiohttp.ClientSession` with a minimal fake so
the rejection paths run without touching the network.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lib.pillow_helpers import fetch_image_bytes_capped


class _FakeContent:
    """Stand-in for aiohttp's StreamReader. Only the methods the
    function uses (read and iter_chunked) are implemented.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._cursor = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._body[self._cursor:]
            self._cursor = len(self._body)
            return chunk
        chunk = self._body[self._cursor:self._cursor + size]
        self._cursor += len(chunk)
        return chunk

    async def iter_chunked(self, size: int):
        while chunk := await self.read(size):
            yield chunk


class _FakeResponse:
    def __init__(
        self, *,
        status: int = 200,
        content_type: str = "image/png",
        content_length: str | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.content = _FakeContent(body)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    """Minimal aiohttp.ClientSession replacement. Records the last URL
    so tests can assert it was called (no real network)."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_url: str | None = None

    def get(self, url: str) -> _FakeResponse:
        self.last_url = url
        return self._response


class TestFetchImageBytesCapped:
    @pytest.mark.asyncio
    async def test_happy_path_returns_body(self) -> None:
        body = b"\x89PNG\r\n\x1a\n" + b"x" * 1000  # tiny "PNG"
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="image/png", body=body,
            content_length=str(len(body)),
        ))
        result = await fetch_image_bytes_capped(sess, "https://x.test/cover.png")
        assert result == body

    @pytest.mark.asyncio
    async def test_non_200_rejected(self) -> None:
        sess = _FakeSession(_FakeResponse(status=404, body=b""))
        result = await fetch_image_bytes_capped(sess, "https://x.test/missing.png")
        assert result is None

    @pytest.mark.asyncio
    async def test_html_content_type_rejected(self) -> None:
        # The 404 page case: server returns 200 with an HTML error
        # body. Without the content-type check, HTML would reach PIL.
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="text/html",
            body=b"<html>Not Found</html>",
        ))
        result = await fetch_image_bytes_capped(sess, "https://x.test/page")
        assert result is None

    @pytest.mark.asyncio
    async def test_xml_content_type_rejected(self) -> None:
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="application/xml",
            body=b"<error>...</error>",
        ))
        result = await fetch_image_bytes_capped(sess, "https://x.test/api")
        assert result is None

    @pytest.mark.asyncio
    async def test_content_length_over_cap_rejected(self) -> None:
        # Declared > cap → rejected before streaming anything.
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="image/png",
            content_length=str(50 * 1024 * 1024),
            body=b"x",  # body itself short, but Content-Length lies
        ))
        result = await fetch_image_bytes_capped(sess, "https://x.test/huge.png")
        assert result is None

    @pytest.mark.asyncio
    async def test_garbage_content_length_falls_through_to_streaming_cap(
        self,
    ) -> None:
        # Non-integer Content-Length header: ignored, so the streaming
        # cap enforces the limit instead.
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="image/png",
            content_length="not-a-number",
            body=b"x" * 1000,
        ))
        result = await fetch_image_bytes_capped(sess, "https://x.test/garbage-cl.png")
        assert result == b"x" * 1000

    @pytest.mark.asyncio
    async def test_streamed_body_over_cap_rejected(self) -> None:
        # No Content-Length, body is huge: the streaming read(N+1)
        # idiom catches it.
        oversize = b"x" * (9 * 1024 * 1024)  # over the default byte cap
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="image/png", body=oversize,
        ))
        result = await fetch_image_bytes_capped(sess, "https://x.test/big.png")
        assert result is None

    @pytest.mark.asyncio
    async def test_custom_max_bytes_respected(self) -> None:
        # The helper accepts a max_bytes override for callers with
        # tighter budgets.
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="image/png", body=b"x" * 1000,
        ))
        result = await fetch_image_bytes_capped(
            sess, "https://x.test/.png", max_bytes=100,
        )
        assert result is None  # 1000 > 100

    @pytest.mark.asyncio
    async def test_image_pixel_cap_is_set_at_import(self) -> None:
        """Verify the module-level MAX_IMAGE_PIXELS cap is in effect.
        Without this cap, a small-bytes / huge-decoded PNG can still
        blow up RSS even if the byte-size guards pass."""
        from PIL import Image
        assert Image.MAX_IMAGE_PIXELS == 25_000_000


class TestUrlSchemeGuard:
    """The URL passed to ``fetch_image_bytes_capped`` can flow from
    third-party APIs (jiten ``cover_url``), so only http(s) URLs with a
    host are fetched. Any rejection here happens BEFORE the session.get
    call, so a session that raises if touched proves the early-return
    path is taken.
    """

    class _ExplodingSession:
        """A session whose .get raises if invoked; proves the guard
        short-circuits before any network access."""

        def get(self, url):  # pragma: no cover - shouldn't be hit
            raise AssertionError(
                f"session.get called for url={url!r}; URL guard failed"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_url", [
        "file:///etc/passwd",
        "file://C:/Windows/system.ini",
        "javascript:alert(1)",
        "ftp://example.com/x.png",
        "data:image/png;base64,iVBOR",
        "",                                  # empty
        "not-a-url",                         # no scheme
        "//cdn.example.com/x.png",           # scheme-relative: no scheme to urlparse
        "http://",                           # http but empty netloc
        "https:///path",                     # missing host
    ])
    async def test_rejects_unsafe_or_invalid_urls(self, bad_url) -> None:
        sess = self._ExplodingSession()
        result = await fetch_image_bytes_capped(sess, bad_url)
        assert result is None, f"expected None for {bad_url!r}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("good_url", [
        "https://cdn.example.com/cover.png",
        "http://example.test/x.jpg",
        # IP-literal hosts are allowed at this layer; the response
        # Content-Type / size caps + Discord-side filtering cover those
        # cases. This guard checks only scheme/netloc, not host policy.
        "https://203.0.113.1/x.png",
    ])
    async def test_accepts_http_https_urls(self, good_url) -> None:
        body = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        sess = _FakeSession(_FakeResponse(
            status=200, content_type="image/png", body=body,
        ))
        result = await fetch_image_bytes_capped(sess, good_url)
        assert result == body
        assert sess.last_url == good_url


class TestEmptyTallyAutoClose:
    """When every nominee is removed mid-vote, the auto-close path
    closes the cycle with no winners instead of raising
    ValidationError("no nominees") and leaving it in phase='voting'
    to re-fire on every pass.

    This test exercises the SQL behavior directly. The cog-side
    branching is too I/O-bound to unit-test, but the CLOSE_CYCLE step
    is plain SQL, so the phase flip can be checked here.
    """

    @pytest.mark.asyncio
    async def test_close_cycle_query_moves_to_closed_phase(
        self,
    ) -> None:
        # Pure-SQL behavior: CLOSE_CYCLE flips phase + stamps closed_at.
        # The cog calls this exact query on the empty-tally auto path.
        from lib.migrations import run_migrations
        from lib.utils import DatabaseQueries
        from tests.conftest import FakeBot
        import tempfile, os

        # Use a tempfile (the conftest fixture isn't accessible here
        # without yield-fixturing), keeping this test fully self-contained.
        with tempfile.TemporaryDirectory() as tmp:
            bot = FakeBot(os.path.join(tmp, "test.sqlite3"))
            await run_migrations(bot)
            cycle_id = await bot.RUN_RETURNING_ID(
                "INSERT INTO vn_cycles "
                "(guild_id, phase, kind, target_month) "
                "VALUES (?, 'voting', 'monthly', ?)",
                (100, "2026-06"),
            )
            await bot.RUN(DatabaseQueries.CLOSE_CYCLE, (cycle_id,))
            row = await bot.GET_ONE(
                "SELECT phase, closed_at FROM vn_cycles WHERE id = ?",
                (cycle_id,),
            )
            assert row[0] == "closed"
            assert row[1] is not None, "closed_at should be stamped"
