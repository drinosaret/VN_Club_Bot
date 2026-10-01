"""
Shared pagination components.
"""

import discord
import logging
from abc import ABC, abstractmethod
from typing import Any, List, Optional
from math import ceil
from lib.utils import DEFAULT_TIMEOUT, add_pagination_footer, create_base_embed

_log = logging.getLogger(__name__)

# Shared page sizes. Paginated replies land in busy channels, so each page is
# kept short and the rest sits behind the page buttons.
PAGE_ROWS = 10           # one-line entries: leaderboards, lists
PAGE_BLOCKS = 3          # multi-line entries: reviews
PAGE_TEXT_BUDGET = 1800  # characters of body text on pages packed by size


class BasePaginationView(discord.ui.View, ABC):
    """Base class for paginated views.

    The page buttons are shown only when there is more than one page, and
    are taken off the message when the view times out, so an expired list
    never shows buttons that no longer answer. Other items a subclass adds
    stay. For the timeout edit to reach the message, the caller stores the
    sent message on ``view.message``.
    """

    def __init__(self, data: List[Any], title: str, per_page: int = 10, timeout: int = DEFAULT_TIMEOUT):
        super().__init__(timeout=timeout)
        self.data = data
        self.title = title
        self.per_page = per_page
        self.current_page = 0
        self.message: Optional[discord.Message] = None
        self.max_pages = self._calculate_max_pages()
        self._update_button_states()

    def _calculate_max_pages(self) -> int:
        """Calculate the maximum number of pages based on data"""
        if not self.data:
            return 1
        return ceil(len(self.data) / self.per_page)

    def set_data(self, data: List[Any]) -> None:
        """Replace the rows and go back to the first page."""
        self.data = data
        self.current_page = 0
        self.max_pages = self._calculate_max_pages()
        self._update_button_states()

    @abstractmethod
    def create_embed(self) -> discord.Embed:
        """Create an embed for the current page - must be implemented by subclasses"""
        pass

    def _nav_buttons(self) -> tuple:
        return (self.first_page, self.previous_page, self.next_page, self.last_page)

    def _update_button_states(self):
        """Show the page buttons only when there are pages to move between,
        and disable the ones that would go nowhere."""
        multi = self.max_pages > 1
        for button in self._nav_buttons():
            present = button in self.children
            if multi and not present:
                self.add_item(button)
            elif not multi and present:
                self.remove_item(button)
        if not multi:
            return
        at_start = self.current_page == 0
        at_end = self.current_page >= self.max_pages - 1
        self.first_page.disabled = at_start
        self.previous_page.disabled = at_start
        self.next_page.disabled = at_end
        self.last_page.disabled = at_end

    def _disable_all_buttons(self):
        """Disable all navigation buttons"""
        for button in self._nav_buttons():
            button.disabled = True

    async def _navigate(self, interaction: discord.Interaction, action: str):
        """Shared edit_message wrapper. A discord.HTTPException here (rate
        limit, deleted source message, expired interaction token) would
        otherwise propagate to discord.py's silent default and the user
        would see a frozen pager, so enough context is logged to debug it."""
        if self.message is None:
            self.message = interaction.message
        try:
            await interaction.response.edit_message(
                embed=self.create_embed(), view=self,
            )
        except Exception:
            _log.exception(
                "pagination edit_message failed: view=%s action=%s page=%d/%d user=%s",
                type(self).__name__, action,
                self.current_page + 1, self.max_pages, interaction.user.id,
            )

    @discord.ui.button(label='«', style=discord.ButtonStyle.secondary, row=0)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to first page"""
        self.current_page = 0
        self._update_button_states()
        await self._navigate(interaction, "first")

    @discord.ui.button(label='‹', style=discord.ButtonStyle.secondary, row=0)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_button_states()
            await self._navigate(interaction, "previous")

    @discord.ui.button(label='›', style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            self._update_button_states()
            await self._navigate(interaction, "next")

    @discord.ui.button(label='»', style=discord.ButtonStyle.secondary, row=0)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to last page"""
        self.current_page = self.max_pages - 1
        self._update_button_states()
        await self._navigate(interaction, "last")

    async def on_timeout(self):
        """Take the page buttons off the message; anything else stays."""
        for button in self._nav_buttons():
            if button in self.children:
                self.remove_item(button)
        if self.message is None:
            return
        try:
            await self.message.edit(view=self if self.children else None)
        except Exception:  # noqa: BLE001
            # The message may be gone or its token expired; nothing to tidy.
            _log.debug("pagination timeout edit skipped for %s", type(self).__name__)

    def get_page_data(self) -> List[Any]:
        """Get data for the current page"""
        start_idx = self.current_page * self.per_page
        end_idx = min(start_idx + self.per_page, len(self.data))
        return self.data[start_idx:end_idx]


class GenericPaginationView(BasePaginationView):
    """Generic pagination view for simple text-based content"""
    
    def __init__(
        self, 
        items: List[str], 
        title: str, 
        per_page: int = 10, 
        color: discord.Color = discord.Color.blue(),
        description: Optional[str] = None
    ):
        super().__init__(items, title, per_page)
        self.color = color
        self.base_description = description
    
    def create_embed(self) -> discord.Embed:
        """Create an embed for the current page"""
        embed = create_base_embed(
            title=self.title,
            description=self.base_description,
            color=self.color
        )
        
        page_items = self.get_page_data()
        if page_items:
            content = "\n".join(page_items)
            # If we have a base description, append the content
            if self.base_description:
                embed.description = f"{self.base_description}\n\n{content}"
            else:
                embed.description = content
        else:
            embed.description = "No items found on this page."
        
        add_pagination_footer(embed, self.current_page, self.max_pages, len(self.data))
        return embed