"""Page buttons on ``lib.pagination.BasePaginationView``: shown only when
there is more than one page, and taken off the message on timeout."""
from __future__ import annotations

import discord

from lib.pagination import BasePaginationView


class _View(BasePaginationView):
    def create_embed(self) -> discord.Embed:
        return discord.Embed(description="\n".join(self.get_page_data()))


class _FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)


def _labels(view: BasePaginationView) -> list[str]:
    return [getattr(item, "label", None) for item in view.children]


def _rows(n: int) -> list[str]:
    return [f"row {i}" for i in range(n)]


async def test_single_page_has_no_nav_buttons() -> None:
    view = _View(_rows(3), "Title", per_page=10)
    assert view.max_pages == 1
    assert view.children == []


async def test_empty_data_is_one_page_without_buttons() -> None:
    view = _View([], "Title", per_page=10)
    assert view.max_pages == 1
    assert view.children == []


async def test_multi_page_shows_all_four_buttons_with_start_disabled() -> None:
    view = _View(_rows(25), "Title", per_page=10)
    assert view.max_pages == 3
    assert _labels(view) == ["«", "‹", "›", "»"]
    assert view.first_page.disabled and view.previous_page.disabled
    assert not view.next_page.disabled and not view.last_page.disabled


async def test_last_page_disables_forward_buttons() -> None:
    view = _View(_rows(25), "Title", per_page=10)
    view.current_page = view.max_pages - 1
    view._update_button_states()
    assert not view.first_page.disabled and not view.previous_page.disabled
    assert view.next_page.disabled and view.last_page.disabled


async def test_set_data_adds_and_removes_buttons() -> None:
    view = _View(_rows(3), "Title", per_page=10)
    assert view.children == []

    view.set_data(_rows(30))
    assert view.max_pages == 3
    assert _labels(view) == ["«", "‹", "›", "»"]
    assert view.current_page == 0

    view.current_page = 2
    view.set_data(_rows(5))
    assert view.max_pages == 1
    assert view.current_page == 0
    assert view.children == []


async def test_set_data_keeps_buttons_once_without_duplicates() -> None:
    view = _View(_rows(30), "Title", per_page=10)
    view.set_data(_rows(40))
    assert _labels(view) == ["«", "‹", "›", "»"]


async def test_timeout_removes_nav_buttons_but_keeps_other_items() -> None:
    view = _View(_rows(30), "Title", per_page=10)
    extra = discord.ui.Button(label="Other", row=1)
    view.add_item(extra)
    message = _FakeMessage()
    view.message = message

    await view.on_timeout()

    assert view.children == [extra]
    assert message.edits == [{"view": view}]


async def test_timeout_with_nothing_left_clears_the_view() -> None:
    view = _View(_rows(30), "Title", per_page=10)
    message = _FakeMessage()
    view.message = message

    await view.on_timeout()

    assert view.children == []
    assert message.edits == [{"view": None}]


async def test_timeout_without_a_message_does_nothing() -> None:
    view = _View(_rows(30), "Title", per_page=10)
    await view.on_timeout()
    assert view.children == []


async def test_timeout_edit_failure_is_swallowed() -> None:
    class _GoneMessage:
        async def edit(self, **_kwargs) -> None:
            raise RuntimeError("message no longer exists")

    view = _View(_rows(30), "Title", per_page=10)
    view.message = _GoneMessage()
    await view.on_timeout()
