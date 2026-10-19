"""Description cleanup and truncation.

VNDB descriptions reach two surfaces with different rules: embeds take
Discord markdown and a character budget, image cards take plain text and are
cut by pixel width. Both end a truncated excerpt with exactly one ellipsis,
which is what these tests pin down: the string passes through more than one
truncator, and each one could append its own.
"""
from __future__ import annotations

import pytest

from lib.desciption_processing import (
    replace_bbcode,
    strip_orphan_tags,
    to_plain_text,
)
from lib.utils import ELLIPSIS, ellipsize, smart_truncate
from lib.vndb_api import VN_Entry


def _entry(description: str) -> VN_Entry:
    return VN_Entry(
        vndb_id="v1",
        title_en="Title",
        title_ja="タイトル",
        thumbnail_url="",
        description=description,
    )


class TestEllipsize:
    @pytest.mark.parametrize("raw,expected", [
        ("but then she said……", "but then she said…"),
        ("he vanished.", "he vanished…"),
        ("he vanished...", "he vanished…"),
        ("a list of things,", "a list of things…"),
        ("trailing space ", "trailing space…"),
        ("dangling opener (", "dangling opener…"),
        ("彼は消えた。", "彼は消えた…"),
        ("no punctuation", "no punctuation…"),
    ])
    def test_single_ellipsis(self, raw, expected) -> None:
        assert ellipsize(raw) == expected

    def test_closing_punctuation_is_kept(self) -> None:
        # A closing bracket ends a fragment legitimately.
        assert ellipsize('he said "hello"') == 'he said "hello"…'
        assert ellipsize("「こんにちは」") == "「こんにちは」…"

    def test_all_noise_input(self) -> None:
        assert ellipsize("...") == ELLIPSIS
        assert ellipsize("") == ELLIPSIS


class TestSmartTruncate:
    def test_short_text_untouched(self) -> None:
        assert smart_truncate("short enough", 50) == "short enough"

    def test_none_max_length_skips_truncation(self) -> None:
        text = "x" * 5000
        assert smart_truncate(text, None) == text

    def test_result_fits_budget(self) -> None:
        text = "word " * 200
        for cap in (20, 100, 400, 1000):
            assert len(smart_truncate(text, cap)) <= cap

    def test_cuts_on_word_boundary(self) -> None:
        assert smart_truncate("alpha beta gamma delta", 14) == "alpha beta…"

    def test_no_double_ellipsis_when_cut_lands_on_one(self) -> None:
        result = smart_truncate("but then she said…… and left the room", 22)
        assert result.endswith("said…")
        assert "……" not in result and "...…" not in result

    def test_spaceless_text_is_not_emptied(self) -> None:
        # Japanese descriptions have no spaces to back off to.
        text = "彼女は静かに笑った" * 20
        result = smart_truncate(text, 30)
        assert len(result) == 30
        assert result.endswith(ELLIPSIS)

    def test_partial_markdown_link_dropped(self) -> None:
        text = "Adapted from [JAST USA](https://jastusa.com/games/jast037) in 2015"
        result = smart_truncate(text, 30)
        assert "[" not in result and "](" not in result
        assert result == "Adapted from…"

    def test_complete_markdown_link_kept(self) -> None:
        text = "See [JAST](https://jastusa.com) for the release and more besides"
        result = smart_truncate(text, 40)
        assert "[JAST](https://jastusa.com)" in result

    def test_half_open_spoiler_dropped(self) -> None:
        text = "Everything is fine until ||she dies in the final chapter|| later"
        result = smart_truncate(text, 45)
        assert "dies" not in result
        assert result.count("||") % 2 == 0

    def test_empty_ellipsis_appends_nothing(self) -> None:
        result = smart_truncate("alpha beta gamma delta", 14, ellipsis="")
        assert result == "alpha beta"


class TestMarkupProcessing:
    def test_inline_styles_become_markdown(self) -> None:
        out = replace_bbcode("[b]bold[/b] and [i]italic[/i] and [s]gone[/s]")
        assert out == "**bold** and *italic* and ~~gone~~"

    def test_orphan_tags_removed(self) -> None:
        assert strip_orphan_tags("half [i]styled") == "half styled"
        assert replace_bbcode("closer only[/i]") == "closer only"

    def test_quote_and_code_keep_contents(self) -> None:
        assert replace_bbcode("[quote]quoted[/quote]") == "quoted"
        assert replace_bbcode("[code]x = 1[/code]") == "x = 1"

    def test_spoiler_masked_for_embeds(self) -> None:
        assert replace_bbcode("[spoiler]she dies[/spoiler]") == "||she dies||"

    def test_spoiler_dropped_for_images(self) -> None:
        # A PNG has nothing that hides text, so the contents go.
        out = to_plain_text("Fine until [spoiler]she dies[/spoiler] happens")
        assert out == "Fine until happens"

    def test_plain_text_strips_links_and_markers(self) -> None:
        raw = "[From [url=https://jastusa.com/x]JAST USA[/url]] [b]bold[/b]"
        out = to_plain_text(raw)
        assert out == "[From JAST USA] bold"

    def test_plain_text_strips_relative_link_labels(self) -> None:
        assert to_plain_text("meets [Enju](/c161706) there") == "meets Enju there"

    def test_plain_text_collapses_blank_line_runs(self) -> None:
        assert to_plain_text("a\n\n\n\nb") == "a\n\nb"


class TestNormalizedDescription:
    async def test_missing_description(self) -> None:
        assert await _entry("").get_normalized_description() == "No description available."

    async def test_embed_form_is_capped_and_ellipsized(self) -> None:
        entry = _entry("Long prose. " * 200)
        out = await entry.get_normalized_description(max_length=400)
        assert len(out) <= 400
        assert out.endswith(ELLIPSIS)
        assert not out.endswith(".…")

    async def test_embeds_keep_the_ellipsis_character(self) -> None:
        # Discord sets U+2026 on the baseline; only the image cards need the
        # period form (see lib.pillow_helpers.IMAGE_ELLIPSIS).
        entry = _entry("Long prose. " * 200)
        out = await entry.get_normalized_description(max_length=400)
        assert out.endswith("…")
        assert not out.endswith("...")

    async def test_plain_form_has_no_markup(self) -> None:
        entry = _entry("[spoiler]twist[/spoiler] [b]bold[/b] [url=https://x.dev]link[/url]")
        out = await entry.get_normalized_description(max_length=None, plain=True)
        assert out == "bold link"

    async def test_plain_form_uncapped(self) -> None:
        entry = _entry("word " * 500)
        out = await entry.get_normalized_description(max_length=None, plain=True)
        assert ELLIPSIS not in out
        assert len(out) > 1000

    async def test_spoiler_only_description_falls_back(self) -> None:
        entry = _entry("[spoiler]everything[/spoiler]")
        out = await entry.get_normalized_description(plain=True)
        assert out == "No description available."
