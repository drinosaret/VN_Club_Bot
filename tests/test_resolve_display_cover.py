"""`resolve_display_cover`: the NSFW-cover -> jiten-cover substitution rule.

VNDB covers flagged NSFW are normally blurred (banners) or hidden (embeds).
When jiten.moe has a cover (always SFW), that is shown instead. This guards the
decision logic, including that the substitution is keyed off the *VNDB* NSFW
flag and that the jiten field is named ``cover_url``.
"""
from __future__ import annotations

from types import SimpleNamespace

from lib.jiten_client import JitenInfo, resolve_display_cover

VNDB_URL = "https://t.vndb.org/cv/00/cover.jpg"
JITEN_URL = "https://cdn.jiten.moe/123/cover.jpg"


def _vn(url="", nsfw=False):
    return SimpleNamespace(thumbnail_url=url, thumbnail_is_nsfw=nsfw)


def _jiten(cover_url):
    return SimpleNamespace(cover_url=cover_url)


class TestResolveDisplayCover:
    def test_nsfw_vndb_with_jiten_cover_substitutes(self) -> None:
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=True), _jiten(JITEN_URL))
        assert (url, is_nsfw) == (JITEN_URL, False)

    def test_nsfw_vndb_jiten_without_cover_keeps_vndb(self) -> None:
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=True), _jiten(None))
        assert (url, is_nsfw) == (VNDB_URL, True)

    def test_nsfw_vndb_no_jiten_keeps_vndb(self) -> None:
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=True), None)
        assert (url, is_nsfw) == (VNDB_URL, True)

    def test_sfw_vndb_with_jiten_cover_keeps_vndb(self) -> None:
        # No substitution when the VNDB cover isn't flagged; VNDB stays canonical.
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=False), _jiten(JITEN_URL))
        assert (url, is_nsfw) == (VNDB_URL, False)

    def test_sfw_vndb_no_jiten(self) -> None:
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=False), None)
        assert (url, is_nsfw) == (VNDB_URL, False)

    def test_missing_thumbnail_url_defaults_to_none(self) -> None:
        url, is_nsfw = resolve_display_cover(SimpleNamespace(), None)
        assert (url, is_nsfw) == (None, False)

    def test_nsfw_vndb_jiten_empty_string_cover_keeps_vndb(self) -> None:
        # Falsy cover_url (e.g. "") must not be substituted.
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=True), _jiten(""))
        assert (url, is_nsfw) == (VNDB_URL, True)

    def test_real_jiten_info_field_name(self) -> None:
        # Guards against a rename of JitenInfo.cover_url breaking substitution.
        info = JitenInfo(deck_id=1, character_count=0, difficulty_raw=0.0, cover_url=JITEN_URL)
        url, is_nsfw = resolve_display_cover(_vn(VNDB_URL, nsfw=True), info)
        assert (url, is_nsfw) == (JITEN_URL, False)
