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


class BasePaginationView(discord.ui.View, ABC):
    """Base class for paginated Discord UI views with navigation buttons"""
    
    def __init__(self, data: List[Any], title: str, per_page: int = 10, timeout: int = DEFAULT_TIMEOUT):
        super().__init__(timeout=timeout)
        self.data = data
        self.title = title
        self.per_page = per_page
        self.current_page = 0
        self.max_pages = self._calculate_max_pages()
        
        # Update button states
        self._update_button_states()
    
    def _calculate_max_pages(self) -> int:
        """Calculate the maximum number of pages based on data"""
        if not self.data:
            return 1
        return ceil(len(self.data) / self.per_page)
    
    @abstractmethod
    def create_embed(self) -> discord.Embed:
        """Create an embed for the current page - must be implemented by subclasses"""
        pass
    
    def _update_button_states(self):
        """Update button enabled/disabled states based on current page"""
        # Disable all buttons if only one page or no data
        if self.max_pages <= 1:
            self._disable_all_buttons()
            return
        
        # Update individual buttons
        self.first_page.disabled = (self.current_page == 0)
        self.previous_page.disabled = (self.current_page == 0)
        self.next_page.disabled = (self.current_page >= self.max_pages - 1)
        self.last_page.disabled = (self.current_page >= self.max_pages - 1)
    
    def _disable_all_buttons(self):
        """Disable all navigation buttons"""
        self.first_page.disabled = True
        self.previous_page.disabled = True
        self.next_page.disabled = True
        self.last_page.disabled = True

    @discord.ui.button(label='⏪', style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to first page"""
        self.current_page = 0
        self._update_button_states()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label='◀️', style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self._update_button_states()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label='▶️', style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            self._update_button_states()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label='⏩', style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to last page"""
        self.current_page = self.max_pages - 1
        self._update_button_states()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def on_timeout(self):
        """Disable all buttons when the view times out"""
        self._disable_all_buttons()
        # Note: The message won't be updated automatically, but buttons will be disabled

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